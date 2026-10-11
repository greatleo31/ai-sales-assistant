#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块二：60 SKU 商品图片抓取、魔数/SHA-256 校验、本地与腾讯云 COS 双落盘及 image_manifest.json 生成脚本
依据 HANDOFF.md §2 & §5 与复审要求 (R8 / S9 / F4)：
  1. 从 Bing 免 Key 异步接口 (https://cn.bing.com/images/async?q=...&mmasync=1) 抓取每条 SKU 的商品图；
  2. 严格校验图片二进制魔数 (JPEG/PNG) 与 Pillow 图像完整性，统一规范化为 RGB JPEG 落盘至 data/images/{sku_id}.jpg；
  3. 计算 64 位 SHA-256，使用腾讯云 COS XML API (HMAC-SHA1 签名) 上传至 COS Bucket，生成可公开访问的 cos_image_url；
  4. 写入 data/image_manifest.json 并联动 scripts/generate_catalog.py 更新 ecommerce_catalog_60skus.json。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import hmac
import html
import io
import json
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from PIL import Image

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
IMAGES_DIR = DATA_DIR / "images"
MANIFEST_PATH = DATA_DIR / "image_manifest.json"
CATALOG_PATH = DATA_DIR / "ecommerce_catalog_60skus.json"
ENV_PATH = ROOT_DIR / ".env"

SHANGHAI_TZ = timezone(timedelta(hours=8))

BING_ASYNC_URL = "https://cn.bing.com/images/async"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MIMG_SRC_PATTERN = re.compile(r'<img[^>]+class="[^"]*mimg[^"]*"[^>]+src="(https?://[^"]+)"', re.IGNORECASE)
DATA_SRC_PATTERN = re.compile(r'data-src="(https?://[^"]+)"', re.IGNORECASE)
MURL_PATTERN = re.compile(r"murl&quot;:&quot;(https?://[^&]+?)&quot;", re.IGNORECASE)


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


def detect_magic_format(data: bytes) -> Optional[str]:
    """检测图片字节流魔数（JPEG: FF D8 FF，PNG: 89 50 4E 47 0D 0A 1A 0A）。"""
    if len(data) < 16:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    return None


def normalize_to_jpeg(raw_bytes: bytes) -> Tuple[bytes, int, int]:
    """
    校验图像可解码性并统一转换为标准 RGB JPEG（确保本地 /static/images/{sku_id}.jpg
    魔数 100% 为 FF D8 FF JPEG 且 Content-Type 为 image/jpeg）。
    """
    with Image.open(io.BytesIO(raw_bytes)) as img:
        img.load()
        width, height = img.size
        if width < 200 or height < 150:
            raise ValueError(f"图片尺寸过小: {width}x{height} (要求 >= 200x150)")
        if img.mode != "RGB":
            rgb_img = img.convert("RGB")
        else:
            rgb_img = img
        out_buf = io.BytesIO()
        rgb_img.save(out_buf, format="JPEG", quality=90, optimize=True)
        jpeg_bytes = out_buf.getvalue()
    if detect_magic_format(jpeg_bytes) != "JPEG":
        raise ValueError("转换后 JPEG 魔数校验失败")
    return jpeg_bytes, width, height


def search_bing_image_urls(query: str, session: requests.Session) -> List[str]:
    """请求 Bing 免 Key 异步图片接口并解析候选图片 URL（优先高可用 mimg 缩略源，兼顾 murl 原图）。"""
    params = {
        "q": query,
        "first": "0",
        "count": "25",
        "mmasync": "1",
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://cn.bing.com/images/search",
    }
    resp = session.get(BING_ASYNC_URL, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    body = resp.text

    candidates: List[str] = []
    # 1. HANDOFF.md §2 指定的 img.mimg src.split("?")[0] 加上保留必要参数的完整 URL
    for raw_u in MIMG_SRC_PATTERN.findall(body) + DATA_SRC_PATTERN.findall(body):
        u_unescaped = html.unescape(raw_u)
        u_clean = u_unescaped.split("?")[0]
        if u_clean.startswith("http") and not u_clean.endswith("/th") and u_clean not in candidates:
            candidates.append(u_clean)
        if u_unescaped.startswith("http") and u_unescaped not in candidates:
            candidates.append(u_unescaped)

    # 2. 补充 murl 原图候选
    for raw_m in MURL_PATTERN.findall(body):
        u_m = html.unescape(raw_m)
        if u_m.startswith("http") and u_m not in candidates:
            candidates.append(u_m)

    return candidates


def download_valid_sku_image(
    sku: Dict[str, Any],
    session: requests.Session,
    claimed_sha256s: set[str],
    claimed_urls: set[str],
    lock: Any,
) -> Tuple[bytes, str, int, int, str]:
    """针对单条 SKU 检索并下载首张通过魔数、尺寸与全局 SHA-256 唯一性校验的真实商品图片。"""
    queries = [
        sku["search_query"],
        f"{sku['brand']} {sku['title']}",
        sku["title"],
    ]
    seen_urls: set[str] = set()
    last_err: Optional[Exception] = None

    for q in queries:
        try:
            urls = search_bing_image_urls(q, session)
        except Exception as exc:
            last_err = exc
            continue

        for url in urls[:20]:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            with lock:
                if url in claimed_urls:
                    continue
            try:
                r = session.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Referer": "https://cn.bing.com/"},
                    timeout=8,
                )
                if r.status_code != 200 or len(r.content) < 2048:
                    continue
                raw_magic = detect_magic_format(r.content)
                if raw_magic not in ("JPEG", "PNG"):
                    continue
                jpeg_bytes, w, h = normalize_to_jpeg(r.content)
                sha256_hex = hashlib.sha256(jpeg_bytes).hexdigest()
                with lock:
                    if sha256_hex in claimed_sha256s or url in claimed_urls:
                        continue
                    claimed_sha256s.add(sha256_hex)
                    claimed_urls.add(url)
                return jpeg_bytes, url, w, h, sha256_hex
            except Exception as exc:
                last_err = exc
                continue

    raise RuntimeError(f"[{sku['sku_id']}] 无法从 Bing 获取唯一且有效的 JPEG/PNG 商品图: {last_err}")


def build_cos_authorization(
    secret_id: str,
    secret_key: str,
    method: str,
    uri_path: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, str]] = None,
    expire_seconds: int = 600,
) -> str:
    """遵循腾讯云 COS XML API 官方规范生成 HMAC-SHA1 签名字符串。"""
    params = params or {}
    now = int(time.time())
    key_time = f"{now - 60};{now + expire_seconds}"
    sign_key = hmac.new(secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1).hexdigest()

    norm_params = {
        urllib.parse.quote(k.lower(), safe=""): urllib.parse.quote(str(v), safe="")
        for k, v in params.items()
    }
    param_keys = sorted(norm_params.keys())
    param_list = ";".join(param_keys)
    http_params = "&".join(f"{k}={norm_params[k]}" for k in param_keys)

    norm_headers = {
        urllib.parse.quote(k.lower(), safe=""): urllib.parse.quote(str(v).strip(), safe="")
        for k, v in headers.items()
    }
    header_keys = sorted(norm_headers.keys())
    header_list = ";".join(header_keys)
    http_headers = "&".join(f"{k}={norm_headers[k]}" for k in header_keys)

    http_string = f"{method.lower()}\n{uri_path}\n{http_params}\n{http_headers}\n"
    sha1_http = hashlib.sha1(http_string.encode("utf-8")).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{sha1_http}\n"
    signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()

    return (
        f"q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={key_time}"
        f"&q-key-time={key_time}"
        f"&q-header-list={header_list}"
        f"&q-url-param-list={param_list}"
        f"&q-signature={signature}"
    )


def upload_to_cos(
    jpeg_bytes: bytes,
    sku_id: str,
    cos_host: str,
    secret_id: str,
    secret_key: str,
    session: requests.Session,
) -> str:
    """将图片上传至腾讯云 COS 并返回完整公开 URL。"""
    cos_host_clean = cos_host.rstrip("/")
    parsed = urllib.parse.urlparse(cos_host_clean)
    host_netloc = parsed.netloc
    uri_path = f"/ai-sales-assistant/images/{sku_id}.jpg"
    target_url = f"{cos_host_clean}{uri_path}"

    headers = {
        "Host": host_netloc,
        "Content-Type": "image/jpeg",
        "Content-Length": str(len(jpeg_bytes)),
    }
    headers["Authorization"] = build_cos_authorization(
        secret_id=secret_id,
        secret_key=secret_key,
        method="PUT",
        uri_path=uri_path,
        headers=headers,
    )

    resp = session.put(target_url, data=jpeg_bytes, headers=headers, timeout=15)
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"[{sku_id}] COS PUT 失败: HTTP {resp.status_code} - {resp.text[:200]}"
        )
    return target_url


def process_single_sku(
    sku: Dict[str, Any],
    cos_host: str,
    secret_id: str,
    secret_key: str,
    force_refresh: bool,
    existing_manifest: Dict[str, Any],
    claimed_sha256s: Set[str],
    claimed_urls: Set[str],
    lock: threading.Lock,
) -> Tuple[str, Dict[str, Any]]:
    sid = sku["sku_id"]
    local_file = IMAGES_DIR / f"{sid}.jpg"

    session = requests.Session()
    try:
        # 若本地已存在有效图片且 manifest 已有有效记录且未指定 force_refresh，仍验证本地魔数、分辨率与全局唯一 SHA-256
        if (
            not force_refresh
            and local_file.exists()
            and sid in existing_manifest
            and isinstance(existing_manifest[sid], dict)
        ):
            raw = local_file.read_bytes()
            if detect_magic_format(raw) == "JPEG":
                sha = hashlib.sha256(raw).hexdigest()
                entry = existing_manifest[sid]
                if (
                    entry.get("sha256") == sha
                    and entry.get("cos_image_url")
                    and entry.get("source_url")
                    and entry.get("magic_format") == "JPEG"
                    and int(entry.get("width", 0)) >= 200
                    and int(entry.get("height", 0)) >= 150
                ):
                    with lock:
                        if sha not in claimed_sha256s and entry["source_url"] not in claimed_urls:
                            claimed_sha256s.add(sha)
                            claimed_urls.add(entry["source_url"])
                            entry["calibrated_visual_description"] = sku["visual_description"]
                            return sid, entry

        jpeg_bytes, src_url, width, height, sha256_hex = download_valid_sku_image(
            sku=sku,
            session=session,
            claimed_sha256s=claimed_sha256s,
            claimed_urls=claimed_urls,
            lock=lock,
        )
        local_file.write_bytes(jpeg_bytes)

        cos_url = upload_to_cos(
            jpeg_bytes=jpeg_bytes,
            sku_id=sid,
            cos_host=cos_host,
            secret_id=secret_id,
            secret_key=secret_key,
            session=session,
        )

        entry = {
            "sku_id": sid,
            "local_path": f"data/images/{sid}.jpg",
            "image_path": f"/static/images/{sid}.jpg",
            "cos_image_url": cos_url,
            "source_url": src_url,
            "sha256": sha256_hex,
            "magic_format": "JPEG",
            "size_bytes": len(jpeg_bytes),
            "width": width,
            "height": height,
            "calibrated_visual_description": sku["visual_description"],
            "fetched_at": datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds"),
        }
        return sid, entry
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="抓取 60 SKU 商品图并双落盘至本地与腾讯云 COS")
    parser.add_argument("--refresh", action="store_true", help="强制重新抓取并上传全部 60 张图片")
    parser.add_argument("--workers", type=int, default=6, help="并发工作线程数 (默认 6)")
    args = parser.parse_args()

    env = load_env()
    cos_host = env.get("COS_HOST", "").strip()
    secret_id = env.get("COS_SECRET_ID", "").strip()
    secret_key = env.get("COS_SECRET_KEY", "").strip()
    if not (cos_host and secret_id and secret_key):
        print("[ERROR] .env 中缺少 COS_HOST / COS_SECRET_ID / COS_SECRET_KEY 配置", file=sys.stderr)
        return 1

    if not CATALOG_PATH.exists():
        print(f"[ERROR] 找不到商品目录文件: {CATALOG_PATH}", file=sys.stderr)
        return 1

    catalog: List[Dict[str, Any]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    existing_manifest: Dict[str, Any] = {}
    if MANIFEST_PATH.exists():
        try:
            loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_manifest = loaded.get("items", loaded) if isinstance(loaded.get("items"), dict) else loaded
        except Exception:
            existing_manifest = {}

    print(f"[*] 开始处理 {len(catalog)} 条 SKU 的商品图片抓取、魔数校验与腾讯云 COS 上传 (workers={args.workers})...")
    t0 = time.perf_counter()
    results: Dict[str, Dict[str, Any]] = {}
    failures: List[str] = []
    claimed_sha256s: Set[str] = set()
    claimed_urls: Set[str] = set()
    lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_sid = {
            executor.submit(
                process_single_sku,
                sku,
                cos_host,
                secret_id,
                secret_key,
                args.refresh,
                existing_manifest,
                claimed_sha256s,
                claimed_urls,
                lock,
            ): sku["sku_id"]
            for sku in catalog
        }
        for idx, fut in enumerate(concurrent.futures.as_completed(future_to_sid), 1):
            sid = future_to_sid[fut]
            try:
                _, entry = fut.result()
                results[sid] = entry
                print(
                    f"  [{idx:02d}/{len(catalog)}] [OK] {sid} | {entry['width']}x{entry['height']} "
                    f"| {entry['size_bytes']}B | sha256={entry['sha256'][:12]}... -> COS OK"
                )
            except Exception as exc:
                failures.append(f"{sid}: {exc}")
                print(f"  [{idx:02d}/{len(catalog)}] [FAIL] {sid}: {exc}", file=sys.stderr)

    if failures:
        print(f"[ERROR] 共 {len(failures)} 条 SKU 图片处理失败:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    # 按 sku_id 排序写入 data/image_manifest.json
    ordered_manifest = {k: results[k] for k in sorted(results.keys())}
    MANIFEST_PATH.write_text(
        json.dumps(ordered_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    elapsed = time.perf_counter() - t0
    print(f"[OK] 60 张商品图全部落盘并上传 COS 完成 ({elapsed:.2f}s) -> {MANIFEST_PATH}")

    # 调用 generate_catalog.py 将 cos_image_url 与 visual_description_source='calibrated_from_retained_image' 同步回目录
    import generate_catalog

    generate_catalog.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
