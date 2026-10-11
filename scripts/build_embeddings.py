#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块二：Google Vertex AI gemini-embedding-2 (1024 维) 向量预计算与缓存脚本
依据 HANDOFF.md §2 & §5：
  - 接口：POST https://go.penguin.ooo/api/v1beta/models/gemini-embedding-2:batchEmbedContents
  - 请求体格式：{"requests": [{"content": {"parts": [{"text": "..."}]}, "outputDimensionality": 1024}]}
  - 严禁在 requests 元素内部传 "model" 字段（否则触发 Vertex AI oneof _model HTTP 400 冲突），且每次 requests 仅传单条；
  - 对 60 条 SKU 的 retrieval_text 与 3 条平台规则的文本计算 1024 维向量，校验维度==1024、全有限数值、非零范数与 float16 (halfvec) 可量化性，落盘至 data/embeddings_cache.json。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
CATALOG_PATH = DATA_DIR / "ecommerce_catalog_60skus.json"
RULES_PATH = DATA_DIR / "platform_rules.json"
CACHE_PATH = DATA_DIR / "embeddings_cache.json"
ENV_PATH = ROOT_DIR / ".env"

SHANGHAI_TZ = timezone(timedelta(hours=8))


def load_env(env_path: Path = ENV_PATH) -> Dict[str, str]:
    env_map: Dict[str, str] = {}
    if not env_path.exists():
        return env_map
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env_map[k.strip()] = v.strip()
    return env_map


def compute_text_key(model: str, dim: int, text: str) -> str:
    raw = f"{model}:{dim}:{text.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def quantize_fp16_roundtrip(vec: List[float]) -> List[float]:
    """
    模拟 pgvector halfvec(1024) 的 IEEE 754 float16 量化与反量化，
    确保向量在 float16 精度下无 NaN/Inf 溢出且范数有效。
    """
    packed = struct.pack(f"<{len(vec)}e", *vec)
    unpacked = list(struct.unpack(f"<{len(vec)}e", packed))
    return [round(float(x), 7) for x in unpacked]


def validate_embedding_vector(vec: Any, expected_dim: int = 1024) -> Tuple[bool, str]:
    if not isinstance(vec, list) or len(vec) != expected_dim:
        return False, f"向量维度错误: 期望 {expected_dim}，实际 {len(vec) if isinstance(vec, list) else type(vec)}"
    for idx, v in enumerate(vec):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(float(v)):
            return False, f"向量第 {idx} 维不是有限浮点数: {v!r}"
    norm_sq = sum(float(x) * float(x) for x in vec)
    if norm_sq <= 1e-12:
        return False, f"向量范数接近 0: norm_sq={norm_sq}"
    try:
        fp16_vec = quantize_fp16_roundtrip([float(x) for x in vec])
        if not all(math.isfinite(x) for x in fp16_vec):
            return False, "向量量化为 halfvec(fp16) 后出现 Inf/NaN"
    except Exception as exc:
        return False, f"向量量化为 halfvec(fp16) 失败: {exc}"
    return True, "OK"


def fetch_single_embedding(
    text: str,
    embedding_url: str,
    api_key: str,
    dim: int = 1024,
    max_retries: int = 3,
) -> Tuple[List[float], float]:
    """
    调用 POST /api/v1beta/models/gemini-embedding-2:batchEmbedContents 获取单条 1024 维向量。
    严格遵循 HANDOFF.md §2：requests 内单条元素不传 model 字段。
    """
    payload = {
        "requests": [
            {
                "content": {
                    "parts": [
                        {"text": text},
                    ]
                },
                "outputDimensionality": dim,
            }
        ]
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        t0 = time.perf_counter()
        try:
            resp = requests.post(embedding_url, json=payload, headers=headers, timeout=20)
            rtt_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            embeddings_list = data.get("embeddings")
            if not isinstance(embeddings_list, list) or not embeddings_list:
                raise RuntimeError(f"响应缺少 embeddings 数组: {str(data)[:200]}")
            values = embeddings_list[0].get("values")
            ok, msg = validate_embedding_vector(values, expected_dim=dim)
            if not ok:
                raise RuntimeError(msg)
            return [float(x) for x in values], rtt_ms
        except Exception as exc:
            last_err = exc
            if attempt < max_retries:
                time.sleep(0.8 * attempt)
    raise RuntimeError(f"gemini-embedding-2 请求失败 (已重试 {max_retries} 次): {last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="预计算 60 SKU 与平台规则的 gemini-embedding-2 (1024维) 向量")
    parser.add_argument("--refresh", action="store_true", help="忽略已有缓存，强制重新调用远端 API 计算全部向量")
    parser.add_argument("--workers", type=int, default=6, help="并发请求数 (默认 6)")
    args = parser.parse_args()

    env = load_env()
    embedding_url = env.get(
        "EMBEDDING_URL",
        "https://go.penguin.ooo/api/v1beta/models/gemini-embedding-2:batchEmbedContents",
    ).strip()
    api_key = env.get("EMBEDDING_API_KEY", "").strip() or env.get("LLM_PRIMARY_API_KEY", "").strip()
    model_name = env.get("EMBEDDING_MODEL", "gemini-embedding-2").strip()
    dim = int(env.get("EMBEDDING_DIM", "1024"))

    if not api_key:
        print("[ERROR] .env 中缺少 EMBEDDING_API_KEY 配置", file=sys.stderr)
        return 1

    catalog: List[Dict[str, Any]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    rules: List[Dict[str, Any]] = json.loads(RULES_PATH.read_text(encoding="utf-8"))

    existing_by_hash: Dict[str, List[float]] = {}
    if not args.refresh and CACHE_PATH.exists():
        try:
            old_cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(old_cache, dict):
                for item in old_cache.get("sku_embeddings", {}).values():
                    if isinstance(item, dict) and item.get("text_key") and isinstance(item.get("embedding"), list):
                        ok, _ = validate_embedding_vector(item["embedding"], expected_dim=dim)
                        if ok:
                            existing_by_hash[item["text_key"]] = item["embedding"]
                for item in old_cache.get("rule_embeddings", {}).values():
                    if isinstance(item, dict) and item.get("text_key") and isinstance(item.get("embedding"), list):
                        ok, _ = validate_embedding_vector(item["embedding"], expected_dim=dim)
                        if ok:
                            existing_by_hash[item["text_key"]] = item["embedding"]
        except Exception:
            existing_by_hash = {}

    tasks: List[Tuple[str, str, str, str]] = []
    for sku in catalog:
        sid = sku["sku_id"]
        rt = sku["retrieval_text"]
        tkey = compute_text_key(model_name, dim, rt)
        tasks.append(("sku", sid, rt, tkey))

    for rule in rules:
        rid = rule["rule_id"]
        rtext = f"【规则】{rule['title']} ({rule['rule_version']})：{rule['content']}"
        tkey = compute_text_key(model_name, dim, rtext)
        tasks.append(("rule", rid, rtext, tkey))

    print(
        f"[*] 开始构建 {len(catalog)} 条 SKU + {len(rules)} 条平台规则的 {model_name} ({dim}维) 向量..."
    )

    sku_embeddings: Dict[str, Dict[str, Any]] = {}
    rule_embeddings: Dict[str, Dict[str, Any]] = {}
    cache_hits = 0
    api_calls = 0
    failures: List[str] = []

    def process_item(item: Tuple[str, str, str, str]) -> Tuple[str, str, str, str, List[float], float, bool]:
        kind, item_id, text, tkey = item
        if tkey in existing_by_hash:
            return kind, item_id, text, tkey, existing_by_hash[tkey], 0.0, True
        vec, rtt = fetch_single_embedding(text, embedding_url, api_key, dim=dim)
        return kind, item_id, text, tkey, vec, rtt, False

    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(process_item, t): t for t in tasks}
        for idx, fut in enumerate(concurrent.futures.as_completed(future_map), 1):
            kind, item_id, _, _ = future_map[fut]
            try:
                k, iid, text, tkey, vec, rtt, is_hit = fut.result()
                l2_norm = round(math.sqrt(sum(x * x for x in vec)), 6)
                record = {
                    "id": iid,
                    "model": model_name,
                    "dim": dim,
                    "text_key": tkey,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "l2_norm": l2_norm,
                    "rtt_ms": rtt,
                    "embedding": vec,
                }
                if k == "sku":
                    sku_embeddings[iid] = record
                else:
                    rule_embeddings[iid] = record
                if is_hit:
                    cache_hits += 1
                else:
                    api_calls += 1
                status_tag = "CACHE_HIT" if is_hit else f"API {rtt:.0f}ms"
                print(f"  [{idx:02d}/{len(tasks)}] [OK] {iid} | dim={len(vec)} | norm={l2_norm:.4f} | {status_tag}")
            except Exception as exc:
                failures.append(f"{item_id}: {exc}")
                print(f"  [{idx:02d}/{len(tasks)}] [FAIL] {item_id}: {exc}", file=sys.stderr)

    if failures:
        print(f"[ERROR] 向量构建存在失败项 ({len(failures)}):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    ordered_sku_embs = {k: sku_embeddings[k] for k in sorted(sku_embeddings.keys())}
    ordered_rule_embs = {k: rule_embeddings[k] for k in sorted(rule_embeddings.keys())}

    cache_payload = {
        "metadata": {
            "provider": "gemini_vertex_v1beta",
            "model": model_name,
            "endpoint": embedding_url,
            "dimension": dim,
            "quantization_target": "halfvec(1024)",
            "sku_count": len(ordered_sku_embs),
            "rule_count": len(ordered_rule_embs),
            "cache_hits": cache_hits,
            "api_calls": api_calls,
            "generated_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        },
        "sku_embeddings": ordered_sku_embs,
        "rule_embeddings": ordered_rule_embs,
    }
    CACHE_PATH.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    elapsed = time.perf_counter() - t0
    print(
        f"[OK] 向量缓存已写入 {CACHE_PATH} (SKUs={len(ordered_sku_embs)}, Rules={len(ordered_rule_embs)}, "
        f"API={api_calls}, CacheHits={cache_hits}, 耗时={elapsed:.2f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
