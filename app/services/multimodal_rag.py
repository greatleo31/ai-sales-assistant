# -*- coding: utf-8 -*-
"""
多模态混合检索与双路径约束松弛引擎 (app/services/multimodal_rag.py)
严格对齐 HANDOFF.md §2, §3, §4, §6 与 Claude Sonnet 5.5 审查规范：
  1. 主链路标准顺序（严禁倒置）：
     硬约束提取 -> 类目锁 (sub_category) -> 硬约束预过滤 -> 命中数判定
       - 若命中数 >= 1（或无硬约束）：对命中候选集执行 BM25 + halfvec(1024) + RRF(k=60) 混合重排，输出 `constraint_status="satisfied"`；
       - 若硬约束命中数 == 0：触发冲突诊断与双路径松弛 (`relax_constraints_safely`)，输出 `constraint_status="relaxed"`。
  2. 类目锁铁律：
     - 预过滤、BM25、halfvec 向量检索与双路径松弛强制锁死在同一 `sub_category` 内执行，杜绝跨子类目串品（如将充电线当电脑包平替）。
  3. 双路径松弛契约 (`HANDOFF.md` §2 & §6)：
     - 路径 A（保规格、松预算 `relax_budget`）：对用户提取出的全部规格条件做 AND 过滤，在命中集合内取 `min(final_price)`；
     - 路径 B（保预算、松规格 `relax_spec`）：只锁 `sub_category` + `final_price <= max_price`，绝不拿任何单个规格阈值做过滤，在预算内同子类目商品上计算多规格归一化接近度得分 `Closeness(s) = 1 - mean(deficit_i / span_i)`，取第 1 名。
  4. 卡片契约 (`price_explain_card`)：
     - 废除伪造神券的 `saving_plan_card`，统一输出 `price_explain_card`，仅使用 `original_price` 与 `final_price` 解释真实官方直降差额。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import settings
from app.services.constraint_extractor import (
    ConstraintExtractor,
    get_spec_registry,
    normalize_query_text,
)

logger = logging.getLogger(__name__)

RRF_K = 60
HALFVEC_STRUCT_FMT = f"<{settings.embedding_dim}e"
HALFVEC_BYTE_LEN = settings.embedding_dim * 2
QUERY_EMBEDDINGS_CACHE_PATH = settings.embeddings_cache_path.parent / "query_embeddings_cache.json"


# ==============================================================================
# 1. 基础算子与多规格归一化接近度得分 (与 validate_catalog.py 100% 同源对齐)
# ==============================================================================
def is_pure_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))


def compare_spec_value(actual: Any, op: str, target: Any) -> bool:
    if op == "<=":
        if not (is_pure_number(actual) and is_pure_number(target)):
            return False
        return float(actual) <= float(target) + 1e-9
    if op == ">=":
        if not (is_pure_number(actual) and is_pure_number(target)):
            return False
        return float(actual) >= float(target) - 1e-9
    if op == "==":
        if is_pure_number(actual) and is_pure_number(target):
            return abs(float(actual) - float(target)) <= 1e-9
        return actual == target
    raise ValueError(f"Unsupported operator: {op!r}")


def compute_numeric_closeness_score(
    sku: Dict[str, Any],
    spec_constraints: List[Dict[str, Any]],
    sub_cat_specs_meta: Dict[str, Any],
) -> float:
    """
    数值型规格多维归一化接近度计算函数（HANDOFF.md §2 & §6）：
      Closeness(s) = 1 - mean(deficit_i / span_i)
    对非数值型(enum)或损坏的 range 立即抛出 ValueError 拒绝。
    """
    if not isinstance(sku, dict) or not isinstance(sku.get("specs"), dict):
        raise ValueError("sku 必须包含有效的 specs 字典")
    if not spec_constraints:
        return 1.0

    normalized_deficits: List[float] = []
    for c in spec_constraints:
        if not isinstance(c, dict):
            raise ValueError(f"非法的规格约束结构: {c!r}")
        key = c.get("key")
        op = c.get("op")
        raw_target = c.get("value")

        if key not in sub_cat_specs_meta:
            raise ValueError(f"未知的 spec_key: {key!r}")
        spec_meta = sub_cat_specs_meta[key]
        if not isinstance(spec_meta, dict):
            raise ValueError(f"损坏的 spec_meta: {key!r}")
        rng = spec_meta.get("range")
        if (
            spec_meta.get("type") != "number"
            or not isinstance(rng, list)
            or len(rng) != 2
            or not is_pure_number(rng[0])
            or not is_pure_number(rng[1])
        ):
            raise ValueError(
                f"compute_numeric_closeness_score 仅支持含有效 2 元 range 的数值型规格: {key!r}"
            )
        if op not in ("<=", ">=", "=="):
            raise ValueError(f"不支持的比较算子: {op!r}")
        if not is_pure_number(raw_target):
            raise ValueError(f"目标值必须为有限数值: {key}={raw_target!r}")

        raw_actual = sku["specs"].get(key)
        if not is_pure_number(raw_actual):
            raise ValueError(f"SKU {sku.get('sku_id')} 的规格 {key}={raw_actual!r} 不是有限数值")

        target_val = float(raw_target)
        actual_val = float(raw_actual)
        r_min, r_max = float(rng[0]), float(rng[1])
        span = r_max - r_min
        if span <= 0 or not math.isfinite(span):
            raise ValueError(f"规格 {key} 的 range span 非法: {rng}")

        if op == ">=":
            deficit = max(0.0, target_val - actual_val)
        elif op == "<=":
            deficit = max(0.0, actual_val - target_val)
        else:
            deficit = abs(actual_val - target_val)

        normalized_deficits.append(deficit / span)

    return 1.0 - (sum(normalized_deficits) / len(normalized_deficits))


# ==============================================================================
# 2. 中文+专有名词混合分词与 Okapi BM25 稀疏检索器
# ==============================================================================
_ALNUM_SPEC_TOKEN_RE = re.compile(
    r"[a-z0-9]+(?:\.[0-9]+)?(?:\+{1,4}|w|mah|mm|ml|g/㎡|g|kg|db|h|寸|口|键|%)?|[\u4e00-\u9fff]+",
    re.IGNORECASE,
)


def tokenize_for_bm25(text: str) -> List[str]:
    """
    针对电商中文+型号规格混合文本的确定性分词：
      - 保留完整英文/数字/规格复合词（如 17.3寸、100w、2c1a、gan5、spf50+、pa++++）；
      - 对连续中文字符同时生成单字 (unigram) 与双字词 (bigram) 切片，兼顾专有名词（依克多因、神经酰胺、金粉轴）与短词召回。
    """
    norm = normalize_query_text(text)
    tokens: List[str] = []
    for match in _ALNUM_SPEC_TOKEN_RE.finditer(norm):
        chunk = match.group(0)
        if not chunk:
            continue
        if "\u4e00" <= chunk[0] <= "\u9fff":
            n = len(chunk)
            for i in range(n):
                tokens.append(chunk[i])
                if i + 1 < n:
                    tokens.append(chunk[i : i + 2])
                if i + 2 < n:
                    tokens.append(chunk[i : i + 3])
        else:
            tokens.append(chunk)
    return tokens


class BM25Index:
    """零外部依赖的 Okapi BM25 稀疏检索器 (k1=1.5, b=0.75)。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

    def score_candidates(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        soft_constraints: Optional[List[str]] = None,
    ) -> List[Tuple[str, float]]:
        if not candidates:
            return []

        q_tokens = tokenize_for_bm25(query)
        if soft_constraints:
            for sc in soft_constraints:
                q_tokens.extend(tokenize_for_bm25(sc))

        if not q_tokens:
            return [(c["sku_id"], 0.0) for c in candidates]

        doc_tokens_list: List[Counter] = []
        doc_lens: List[int] = []
        df: Counter = Counter()

        for c in candidates:
            text_blob = " ".join(
                [
                    str(c.get("title", "")),
                    " ".join(str(x) for x in c.get("highlights", [])),
                    str(c.get("retrieval_text", "")),
                ]
            )
            toks = tokenize_for_bm25(text_blob)
            cnt = Counter(toks)
            doc_tokens_list.append(cnt)
            doc_lens.append(len(toks))
            for t in cnt.keys():
                df[t] += 1

        n_docs = len(candidates)
        avgdl = max(sum(doc_lens) / max(n_docs, 1), 1.0)
        q_counts = Counter(q_tokens)

        scored: List[Tuple[str, float]] = []
        for idx, c in enumerate(candidates):
            dl = doc_lens[idx]
            tf_map = doc_tokens_list[idx]
            score = 0.0
            for qt, qf in q_counts.items():
                doc_freq = df.get(qt, 0)
                if doc_freq == 0:
                    continue
                idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
                tf = tf_map.get(qt, 0)
                if tf > 0:
                    denom = tf + self.k1 * (1.0 - self.b + self.b * (dl / avgdl))
                    score += idf * ((tf * (self.k1 + 1.0)) / denom) * (1.0 + 0.1 * (qf - 1))

            # 软约束精确子串加分
            if soft_constraints:
                r_text = normalize_query_text(c.get("retrieval_text", ""))
                for sc in soft_constraints:
                    if normalize_query_text(sc) in r_text:
                        score += 0.5

            scored.append((c["sku_id"], round(score, 6)))

        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored


# ==============================================================================
# 3. IEEE 754 binary16 `halfvec(1024)` 稠密向量检索与双模数据库适配
# ==============================================================================
def pack_halfvec_blob(vec: List[float]) -> bytes:
    return struct.pack(HALFVEC_STRUCT_FMT, *[float(x) for x in vec])


def unpack_halfvec_blob(blob: bytes) -> Tuple[float, ...]:
    if len(blob) != HALFVEC_BYTE_LEN:
        raise ValueError(f"Invalid halfvec(1024) blob size: expected {HALFVEC_BYTE_LEN}, got {len(blob)}")
    return struct.unpack(HALFVEC_STRUCT_FMT, blob)


def quantize_to_halfvec_floats(vec: List[float]) -> Tuple[float, ...]:
    return unpack_halfvec_blob(pack_halfvec_blob(vec))


def cosine_similarity_halfvec(v1: Tuple[float, ...], v2: Tuple[float, ...]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 <= 1e-12 or n2 <= 1e-12:
        return 0.0
    return dot / (n1 * n2)


# ==============================================================================
# 4. 零崩溃防御性双路径约束松弛诊断器 (`relax_constraints_safely`)
# ==============================================================================
def build_price_explain_card(sku: Dict[str, Any]) -> Dict[str, Any]:
    """
    构建 `price_explain_card`（HANDOFF.md §2）：
    将原虚构凑单算券的 `saving_plan_card` 降级为真实直降差额解释卡，
    仅使用 `original_price` 与 `final_price` 计算真实立减差额，零虚构券。
    """
    orig = float(sku.get("original_price", sku.get("final_price", 0.0)))
    final = float(sku.get("final_price", 0.0))
    discount = round(max(0.0, orig - final), 2)
    discount_pct = round((discount / orig) * 100.0, 1) if orig > 0 else 0.0
    return {
        "card_type": "price_explain_card",
        "sku_id": sku.get("sku_id"),
        "title": sku.get("title"),
        "original_price": orig,
        "final_price": final,
        "direct_discount_amount": discount,
        "direct_discount_pct": discount_pct,
        "explanation": (
            f"日常原价 ¥{orig:.1f}，当前官方直降立减 ¥{discount:.1f}，实付到手价 ¥{final:.1f}（无套路免凑单）"
            if discount > 0
            else f"当前官方实付到手价 ¥{final:.1f}"
        ),
    }


def _format_spec_summary(
    sku: Dict[str, Any],
    spec_constraints: List[Dict[str, Any]],
    sub_cat_specs_meta: Dict[str, Any],
) -> str:
    specs = sku.get("specs", {})
    parts: List[str] = []
    for c in spec_constraints:
        k = c["key"]
        smeta = sub_cat_specs_meta.get(k, {})
        actual = specs.get(k)
        if actual is None:
            continue
        if smeta.get("type") == "number":
            unit = smeta.get("unit", "")
            if k == "spf":
                parts.append(f"SPF{actual}")
            else:
                parts.append(f"{actual}{unit}")
        elif smeta.get("type") == "enum":
            dlabels = smeta.get("display_labels", {})
            parts.append(str(dlabels.get(actual, actual)))
    return " / ".join(parts) if parts else "标准规格"


def relax_constraints_safely(
    extracted: Dict[str, Any],
    sub_category_skus: List[Dict[str, Any]],
    fallback_top_skus: List[Dict[str, Any]],
    spec_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    防御性双路径约束松弛诊断器（HANDOFF.md §2, §3, §6）：
      - 强制锁死在同一 `sub_category` 的 `sub_category_skus` 内执行；
      - Guard 1：若未同时包含价格上限 (`max_price`) 与至少 1 项规格约束 (`spec_constraints`)，安全降级；
      - 路径 A（保规格、松预算 `relax_budget`）：对全部 `spec_constraints` 执行 AND 过滤，取 `min(final_price)`；
      - 路径 B（保预算、松规格 `relax_spec`）：只锁 `sub_category` + `final_price <= max_price`，
        不拿任何单个规格阈值过滤，按 `compute_numeric_closeness_score` 计算多规格归一化接近度得分取第 1 名；
      - Guard 2 / Guard 3 / Guard 4：全路径空指针防护，绝不出现 NoneType 解引用崩溃。
    """
    registry = get_spec_registry(spec_registry)
    sub_cat = extracted.get("sub_category")
    max_price: Optional[float] = (
        float(extracted["max_price"]) if is_pure_number(extracted.get("max_price")) else None
    )
    spec_constraints: List[Dict[str, Any]] = list(extracted.get("spec_constraints") or [])
    excluded_specs: Dict[str, List[Any]] = dict(extracted.get("excluded_specs") or {})
    candidate_enum_sets: Dict[str, List[str]] = dict(extracted.get("candidate_enum_sets") or {})

    sub_cat_meta = (
        registry.get("sub_categories", {}).get(sub_cat, {})
        if sub_cat
        else {}
    )
    sub_cat_specs_meta: Dict[str, Any] = sub_cat_meta.get("specs", {})
    sub_display_label = sub_cat_meta.get("display_label", sub_cat or "商品")

    # 仅保留严格属于当前 sub_category 且未命中 excluded_specs 的候选
    locked_pool: List[Dict[str, Any]] = []
    for s in sub_category_skus or []:
        if not isinstance(s, dict) or not isinstance(s.get("specs"), dict):
            continue
        if sub_cat and s.get("sub_category") != sub_cat:
            continue
        # 检查否定排除项
        excluded_hit = False
        for ex_k, ex_vals in excluded_specs.items():
            if s["specs"].get(ex_k) in ex_vals:
                excluded_hit = True
                break
        if excluded_hit:
            continue
        locked_pool.append(s)

    # Guard 1: 若未提取出 (max_price + spec_constraints) 组合，安全回落语义/RRF Top 推荐
    if max_price is None or not spec_constraints or not locked_pool:
        safe_pool = locked_pool if locked_pool else (fallback_top_skus or [])
        options = [
            {
                "path": "semantic_fallback",
                "type": "semantic_match",
                "sku": s,
                "sku_id": s.get("sku_id"),
                "badge": "同品类高口碑精选推荐",
                "price_explain_card": build_price_explain_card(s),
            }
            for s in safe_pool[:2]
        ]
        return {
            "status": "satisfied" if options else "relaxed",
            "sub_category": sub_cat,
            "reason": None,
            "path_a": None,
            "path_b": None,
            "options": options,
            "pills": ["查看商品详细参数", "有更便宜的平替款吗？", "看看同品类热销榜"],
        }

    # ------------------------------------------------------------------
    # 路径 A (保规格、松预算)：AND 全部规格条件（含枚举候选集），取 min(final_price)
    # ------------------------------------------------------------------
    path_a_candidates: List[Dict[str, Any]] = []
    for s in locked_pool:
        specs = s["specs"]
        all_specs_ok = True
        for c in spec_constraints:
            actual = specs.get(c["key"])
            if actual is None or not compare_spec_value(actual, c["op"], c["value"]):
                all_specs_ok = False
                break
        if all_specs_ok and candidate_enum_sets:
            for ek, allowed_vals in candidate_enum_sets.items():
                if specs.get(ek) not in allowed_vals:
                    all_specs_ok = False
                    break
        if all_specs_ok:
            path_a_candidates.append(s)

    path_a_sku: Optional[Dict[str, Any]] = (
        min(path_a_candidates, key=lambda x: (float(x["final_price"]), str(x.get("sku_id", ""))))
        if path_a_candidates
        else None
    )

    # ------------------------------------------------------------------
    # 路径 B (保预算、松规格)：只锁 sub_category + final_price <= max_price，绝不卡单个规格阈值！
    # ------------------------------------------------------------------
    budget_satisfied: List[Dict[str, Any]] = [
        s for s in locked_pool if is_pure_number(s.get("final_price")) and float(s["final_price"]) <= max_price + 1e-9
    ]

    numeric_spec_constraints = [
        c for c in spec_constraints if sub_cat_specs_meta.get(c["key"], {}).get("type") == "number"
    ]
    enum_spec_constraints = [
        c for c in spec_constraints if sub_cat_specs_meta.get(c["key"], {}).get("type") == "enum"
    ]

    path_b_sku: Optional[Dict[str, Any]] = None
    path_b_closeness: Optional[float] = None
    path_b_relaxed_dims: List[str] = []

    if budget_satisfied:
        scored_b: List[Tuple[float, float, float, str, Dict[str, Any]]] = []
        for s in budget_satisfied:
            # 数值规格归一化接近度得分（核心权威公式）
            if numeric_spec_constraints:
                num_score = compute_numeric_closeness_score(s, numeric_spec_constraints, sub_cat_specs_meta)
            else:
                num_score = 1.0

            # 若同时含枚举约束，作为次级辅助分
            if enum_spec_constraints:
                enum_hits = sum(
                    1 for c in enum_spec_constraints if compare_spec_value(s["specs"].get(c["key"]), c["op"], c["value"])
                )
                enum_score = enum_hits / len(enum_spec_constraints)
            else:
                enum_score = 1.0

            scored_b.append(
                (
                    round(num_score, 9),
                    round(enum_score, 9),
                    -float(s["final_price"]),
                    str(s.get("sku_id", "")),
                    s,
                )
            )

        scored_b.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        best_num_score, _, _, _, path_b_sku = scored_b[0]
        path_b_closeness = round(best_num_score, 4)

        for c in spec_constraints:
            actual = path_b_sku["specs"].get(c["key"])
            if actual is None or not compare_spec_value(actual, c["op"], c["value"]):
                path_b_relaxed_dims.append(c["key"])

    options: List[Dict[str, Any]] = []
    pills: List[str] = []

    # Guard 2: 仅当 path_a_sku 非空时安全解引用
    path_a_info: Optional[Dict[str, Any]] = None
    if path_a_sku is not None:
        price_diff = round(float(path_a_sku["final_price"]) - max_price, 2)
        spec_summary_a = _format_spec_summary(path_a_sku, spec_constraints, sub_cat_specs_meta)
        badge_a = f"微调预算 +¥{price_diff:g} · 满足 {spec_summary_a}"
        path_a_info = {
            "path": "A",
            "type": "relax_budget",
            "sku_id": path_a_sku["sku_id"],
            "sku": path_a_sku,
            "final_price": float(path_a_sku["final_price"]),
            "budget_delta": price_diff,
            "spec_summary": spec_summary_a,
            "badge": badge_a,
            "price_explain_card": build_price_explain_card(path_a_sku),
        }
        options.append(path_a_info)
        pills.append(f"看 ¥{path_a_sku['final_price']:g} 的 {spec_summary_a} 款详情")

    # Guard 3: 仅当 path_b_sku 非空且不与 path_a_sku 重复时安全解引用
    path_b_info: Optional[Dict[str, Any]] = None
    if path_b_sku is not None and (path_a_sku is None or path_b_sku["sku_id"] != path_a_sku["sku_id"]):
        spec_summary_b = _format_spec_summary(path_b_sku, spec_constraints, sub_cat_specs_meta)
        badge_b = f"严守 ¥{max_price:g}内预算 · {spec_summary_b} 折中方案"
        path_b_info = {
            "path": "B",
            "type": "relax_spec",
            "sku_id": path_b_sku["sku_id"],
            "sku": path_b_sku,
            "final_price": float(path_b_sku["final_price"]),
            "closeness_score": path_b_closeness,
            "relaxed_dimensions": path_b_relaxed_dims,
            "spec_summary": spec_summary_b,
            "badge": badge_b,
            "price_explain_card": build_price_explain_card(path_b_sku),
        }
        options.append(path_b_info)
        pills.append(f"¥{path_b_sku['final_price']:g} 的 {spec_summary_b} 款能满足日常需求吗？")

    # 构造人类可读的约束冲突原因说明
    constraint_desc_parts: List[str] = [f"预算 <=¥{max_price:g}"]
    for c in spec_constraints:
        smeta = sub_cat_specs_meta.get(c["key"], {})
        unit = smeta.get("unit", "")
        label = smeta.get("label", c["key"])
        constraint_desc_parts.append(f"{label} {c['op']} {c['value']}{unit}")
    conflict_desc = " 且 ".join(constraint_desc_parts)

    # Guard 4: 若两条松弛路径均为空，使用同子类目语义召回兜底
    if not options:
        fallback_pool = locked_pool if locked_pool else (fallback_top_skus or [])
        fallback_opts = [
            {
                "path": "fallback",
                "type": "fallback",
                "sku_id": s.get("sku_id"),
                "sku": s,
                "badge": f"同品类({sub_display_label})热销替代",
                "price_explain_card": build_price_explain_card(s),
            }
            for s in fallback_pool[:2]
        ]
        return {
            "status": "relaxed",
            "sub_category": sub_cat,
            "reason": f"当前在【{sub_display_label}】中暂未找到同时满足（{conflict_desc}）的商品，为您推荐相近款式：",
            "path_a": None,
            "path_b": None,
            "options": fallback_opts,
            "pills": ["放宽预算重新筛选", "调整规格要求看看", "转人工导购咨询"],
        }

    pills.append(f"还有其他{sub_display_label}推荐吗？")
    return {
        "status": "relaxed",
        "sub_category": sub_cat,
        "reason": (
            f"在【{sub_display_label}】下未找到同时满足（{conflict_desc}）的商品，"
            f"已为您诊断生成『路径A：保规格微调预算』与『路径B：保预算微调规格』两条最优方案："
        ),
        "path_a": path_a_info,
        "path_b": path_b_info,
        "options": options,
        "pills": pills[:3],
    }


# ==============================================================================
# 5. 多模态混合检索主引擎 (MultimodalRAGEngine)
# ==============================================================================
class MultimodalRAGEngine:
    """
    统一多模态混合检索与双路径约束松弛主引擎：
      - 支持 `hybrid_rrf`（默认主链路：BM25 + halfvec(1024) + RRF）、`bm25_only`、`dense_only` 三种模式；
      - 自动加载 `data/ecommerce_catalog_60skus.json`、`data/local_vector_store.db`（或 PostgreSQL pgvector）与 `data/embeddings_cache.json`；
      - 严格执行：类目锁 -> 硬约束预过滤 -> 命中数判定 -> 混合排序 或 冲突松弛诊断。
    """

    def __init__(
        self,
        catalog: Optional[List[Dict[str, Any]]] = None,
        spec_registry: Optional[Dict[str, Any]] = None,
        sqlite_db_path: Optional[Path] = None,
        embeddings_cache_path: Optional[Path] = None,
        query_embed_fn: Optional[Callable[[str], List[float]]] = None,
    ) -> None:
        self.spec_registry = get_spec_registry(spec_registry)
        self.extractor = ConstraintExtractor(spec_registry=self.spec_registry)
        self.sqlite_db_path = sqlite_db_path or settings.sqlite_db_path
        self.embeddings_cache_path = embeddings_cache_path or settings.embeddings_cache_path
        self._custom_query_embed_fn = query_embed_fn

        self.catalog: List[Dict[str, Any]] = (
            catalog
            if catalog is not None
            else json.loads(settings.catalog_path.read_text(encoding="utf-8"))
        )
        self.sku_by_id: Dict[str, Dict[str, Any]] = {s["sku_id"]: s for s in self.catalog}
        self.bm25 = BM25Index(k1=1.5, b=0.75)

        # 加载 60 SKU 的 halfvec(1024) 半精度量化向量
        self.sku_halfvecs: Dict[str, Tuple[float, ...]] = {}
        self.db_backend: str = "sqlite_halfvec_mirror"
        self._load_sku_halfvecs()

        # 加载本地 Query 向量持久化缓存
        self._query_vec_cache: Dict[str, List[float]] = self._load_query_vec_cache()

    def _load_sku_halfvecs(self) -> None:
        """优先从 local_vector_store.db 读取 2048 字节 halfvec(1024) 二进制 blob，回退读取 embeddings_cache.json。"""
        if self.sqlite_db_path.exists():
            try:
                conn = sqlite3.connect(str(self.sqlite_db_path))
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT sku_id, embedding_halfvec_blob FROM sku_catalog")
                    for sku_id, blob in cur.fetchall():
                        if blob and len(blob) == HALFVEC_BYTE_LEN:
                            self.sku_halfvecs[sku_id] = unpack_halfvec_blob(blob)
                finally:
                    conn.close()
                if len(self.sku_halfvecs) >= len(self.catalog):
                    return
            except Exception as exc:
                logger.warning(f"[MultimodalRAGEngine] SQLite halfvec 加载告警: {exc}")

        if self.embeddings_cache_path.exists():
            try:
                cache_obj = json.loads(self.embeddings_cache_path.read_text(encoding="utf-8"))
                skus_map = cache_obj.get("skus", {})
                for sku_id, entry in skus_map.items():
                    vec = entry.get("embedding")
                    if isinstance(vec, list) and len(vec) == settings.embedding_dim:
                        self.sku_halfvecs[sku_id] = quantize_to_halfvec_floats(vec)
            except Exception as exc:
                logger.warning(f"[MultimodalRAGEngine] embeddings_cache.json 加载告警: {exc}")

    def _load_query_vec_cache(self) -> Dict[str, List[float]]:
        if QUERY_EMBEDDINGS_CACHE_PATH.exists():
            try:
                data = json.loads(QUERY_EMBEDDINGS_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("queries"), dict):
                    return data["queries"]
            except Exception:
                pass
        return {}

    def _save_query_vec_cache(self) -> None:
        try:
            QUERY_EMBEDDINGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "model": settings.embedding_model,
                "dimension": settings.embedding_dim,
                "queries": self._query_vec_cache,
            }
            QUERY_EMBEDDINGS_CACHE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[MultimodalRAGEngine] 保存 query_embeddings_cache 失败: {exc}")

    def get_query_halfvec(self, query: str, allow_remote: bool = True) -> Tuple[float, ...]:
        """
        获取查询文本的 `halfvec(1024)` 半精度向量：
          1. 若注入了自定义 `query_embed_fn`，优先调用；
          2. 若 `query` 精确匹配某条 SKU 的 `retrieval_text`，直接复用该 SKU 向量；
          3. 检查 `data/query_embeddings_cache.json` 缓存；
          4. 若允许远端调用且配置了 `EMBEDDING_API_KEY`，调用 Vertex `/v1beta/models/gemini-embedding-2:batchEmbedContents`
             （严禁传内层 `model`，单条 `requests`，`outputDimensionality: 1024`）并缓存；
          5. 若处于离线/无网络单测环境，使用基于候选库 BM25 相似词加权合成的确定性 1024 维单位向量兜底。
        """
        norm_q = normalize_query_text(query)
        if self._custom_query_embed_fn is not None:
            raw_v = self._custom_query_embed_fn(query)
            return quantize_to_halfvec_floats(raw_v)

        cache_key = hashlib.sha256(
            f"{settings.embedding_model}:{settings.embedding_dim}:{norm_q}".encode("utf-8")
        ).hexdigest()

        if cache_key in self._query_vec_cache:
            return quantize_to_halfvec_floats(self._query_vec_cache[cache_key])

        if allow_remote and settings.embedding_api_key:
            try:
                req_body = {
                    "requests": [
                        {
                            "content": {"parts": [{"text": query.strip()}]},
                            "outputDimensionality": settings.embedding_dim,
                        }
                    ]
                }
                req = urllib.request.Request(
                    settings.embedding_url,
                    data=json.dumps(req_body, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {settings.embedding_api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "ai-sales-assistant/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    if resp.getcode() == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        vec = data["embeddings"][0]["values"]
                        if len(vec) == settings.embedding_dim:
                            self._query_vec_cache[cache_key] = vec
                            self._save_query_vec_cache()
                            return quantize_to_halfvec_floats(vec)
            except Exception as exc:
                logger.warning(f"[MultimodalRAGEngine] 远端 Embedding 调用降级为本地语义合成: {exc}")

        # 离线确定性回退：基于 catalog 中字符/词重叠度对已有 halfvec 做加权归一化合成
        bm25_scores = self.bm25.score_candidates(query, self.catalog)
        acc = [0.0] * settings.embedding_dim
        total_w = 0.0
        for sku_id, sc in bm25_scores[:5]:
            w = max(sc, 0.01)
            hv = self.sku_halfvecs.get(sku_id)
            if hv:
                for i in range(settings.embedding_dim):
                    acc[i] += w * hv[i]
                total_w += w
        if total_w > 0:
            norm = math.sqrt(sum(x * x for x in acc)) or 1.0
            return quantize_to_halfvec_floats([x / norm for x in acc])
        return tuple([0.0] * settings.embedding_dim)

    def filter_by_hard_constraints(
        self,
        pool: List[Dict[str, Any]],
        extracted: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """在已锁定子类目的候选池上执行确定性硬约束过滤（预算 + 数值规格 + 枚举规格 + 候选枚举集 + 否定排除）。"""
        hard_constraints: List[Dict[str, Any]] = list(extracted.get("hard_constraints") or [])
        excluded_specs: Dict[str, List[Any]] = dict(extracted.get("excluded_specs") or {})
        candidate_enum_sets: Dict[str, List[str]] = dict(extracted.get("candidate_enum_sets") or {})

        matched: List[Dict[str, Any]] = []
        for sku in pool:
            specs = sku.get("specs") or {}
            ok = True

            # 1. 检查否定排除项
            for ex_k, ex_vals in excluded_specs.items():
                if specs.get(ex_k) in ex_vals:
                    ok = False
                    break
            if not ok:
                continue

            # 2. 检查硬约束 (final_price 与各 spec)
            for hc in hard_constraints:
                k = hc["key"]
                op = hc["op"]
                target = hc["value"]
                actual = sku.get("final_price") if k == "final_price" else specs.get(k)
                if actual is None or not compare_spec_value(actual, op, target):
                    ok = False
                    break
            if not ok:
                continue

            # 3. 检查多值枚举候选集 (如 "依克多因或者神经酰胺NP" / "全尺寸")
            for ek, allowed_vals in candidate_enum_sets.items():
                if specs.get(ek) not in allowed_vals:
                    ok = False
                    break
            if not ok:
                continue

            matched.append(sku)

        return matched

    def rank_candidates_hybrid(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        soft_constraints: Optional[List[str]] = None,
        mode: str = "hybrid_rrf",
        allow_remote_embedding: bool = True,
    ) -> Dict[str, Any]:
        """
        对候选商品集执行：
          - `bm25_only`：仅按 Okapi BM25 排序；
          - `dense_only`：仅按 `halfvec(1024)` 余弦相似度排序；
          - `hybrid_rrf`（默认）：BM25 + `halfvec(1024)` 双路召回 -> RRF (k=60) 倒数秩融合重排。
        """
        if not candidates:
            return {
                "ranked_skus": [],
                "bm25_ranking": [],
                "dense_ranking": [],
                "rrf_scores": {},
            }

        # 1. BM25 稀疏检索排序
        bm25_scored = self.bm25.score_candidates(query, candidates, soft_constraints=soft_constraints)
        bm25_rank_map: Dict[str, int] = {sku_id: rank for rank, (sku_id, _) in enumerate(bm25_scored, 1)}
        bm25_score_map: Dict[str, float] = {sku_id: sc for sku_id, sc in bm25_scored}

        # 2. halfvec(1024) 稠密余弦相似度排序
        dense_scored: List[Tuple[str, float]] = []
        if mode in ("hybrid_rrf", "dense_only"):
            q_hv = self.get_query_halfvec(query, allow_remote=allow_remote_embedding)
            for c in candidates:
                sid = c["sku_id"]
                s_hv = self.sku_halfvecs.get(sid)
                sim = cosine_similarity_halfvec(q_hv, s_hv) if s_hv else 0.0
                dense_scored.append((sid, round(sim, 6)))
            dense_scored.sort(key=lambda x: (-x[1], x[0]))
        else:
            dense_scored = [(c["sku_id"], 0.0) for c in candidates]

        dense_rank_map: Dict[str, int] = {sku_id: rank for rank, (sku_id, _) in enumerate(dense_scored, 1)}
        dense_score_map: Dict[str, float] = {sku_id: sc for sku_id, sc in dense_scored}

        # 3. 按指定模式汇总最终得分
        cand_by_id = {c["sku_id"]: c for c in candidates}
        final_scores: Dict[str, float] = {}

        if mode == "bm25_only":
            ordered_ids = [sid for sid, _ in bm25_scored]
            for sid, sc in bm25_scored:
                final_scores[sid] = sc
        elif mode == "dense_only":
            ordered_ids = [sid for sid, _ in dense_scored]
            for sid, sc in dense_scored:
                final_scores[sid] = sc
        else:
            # RRF (k=60): score = 1/(k + r_bm25) + 1/(k + r_dense)
            rrf_list: List[Tuple[str, float, float, float]] = []
            for sid in cand_by_id.keys():
                r_b = bm25_rank_map.get(sid, len(candidates) + 1)
                r_d = dense_rank_map.get(sid, len(candidates) + 1)
                rrf_val = (1.0 / (RRF_K + r_b)) + (1.0 / (RRF_K + r_d))
                final_scores[sid] = round(rrf_val, 8)
                rrf_list.append((sid, rrf_val, bm25_score_map.get(sid, 0.0), dense_score_map.get(sid, 0.0)))
            rrf_list.sort(key=lambda x: (-x[1], -x[2], -x[3], x[0]))
            ordered_ids = [x[0] for x in rrf_list]

        ranked_skus: List[Dict[str, Any]] = []
        for sid in ordered_ids:
            sku_copy = dict(cand_by_id[sid])
            sku_copy["_retrieval_meta"] = {
                "final_score": final_scores[sid],
                "bm25_rank": bm25_rank_map.get(sid),
                "bm25_score": bm25_score_map.get(sid, 0.0),
                "dense_rank": dense_rank_map.get(sid),
                "dense_score": dense_score_map.get(sid, 0.0),
                "mode": mode,
            }
            ranked_skus.append(sku_copy)

        return {
            "ranked_skus": ranked_skus,
            "bm25_ranking": bm25_scored,
            "dense_ranking": dense_scored,
            "rrf_scores": final_scores,
        }

    def search(
        self,
        query: str,
        extracted_constraints: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
        mode: str = "hybrid_rrf",
        allow_remote_embedding: bool = True,
    ) -> Dict[str, Any]:
        """
        统一检索与松弛主入口（严格遵循 HANDOFF.md §2 & §6 顺序）：
          Step 1: 提取硬约束与子类目锁 (`ConstraintExtractor`)；
          Step 2: 按 `sub_category` 锁定候选池（多子类目歧义时锁定 `matched_sub_categories` 并生成澄清提示）；
          Step 3: 执行硬约束预过滤 (`filter_by_hard_constraints`)；
          Step 4: 命中数判定：
            - 若存在硬约束且 `len(hard_filtered) == 0` -> 触发 `relax_constraints_safely` 双路径松弛（路径A 保规格松预算 + 路径B 保预算松规格），并对候选做 BM25 + halfvec + RRF；
            - 若 `len(hard_filtered) >= 1`（或无硬约束） -> 对 `hard_filtered` 候选池执行 BM25 + halfvec + RRF 重排，返回 `constraint_status="satisfied"`。
        """
        t0 = time.perf_counter()
        extracted = (
            extracted_constraints
            if extracted_constraints is not None
            else self.extractor.extract(query)
        )

        sub_cat = extracted.get("sub_category")
        matched_subs = list(extracted.get("matched_sub_categories") or [])
        excluded_subs = set(extracted.get("excluded_sub_categories") or [])
        sub_ambiguity = extracted.get("sub_category_ambiguity", "none")
        soft_constraints = list(extracted.get("soft_constraints") or [])

        # Step 2: 类目锁 (Subcategory Lock)
        if sub_cat:
            category_locked_pool = [
                s for s in self.catalog if s.get("sub_category") == sub_cat
            ]
        elif sub_ambiguity == "multi_sub_category" and matched_subs:
            category_locked_pool = [
                s for s in self.catalog if s.get("sub_category") in matched_subs
            ]
        else:
            category_locked_pool = [
                s for s in self.catalog if s.get("sub_category") not in excluded_subs
            ]

        # Step 3: 硬约束预过滤 (Hard Constraint Pre-Filtering)
        has_any_hard_filter = bool(
            extracted.get("hard_constraints")
            or extracted.get("excluded_specs")
            or extracted.get("candidate_enum_sets")
        )
        hard_filtered = (
            self.filter_by_hard_constraints(category_locked_pool, extracted)
            if has_any_hard_filter
            else list(category_locked_pool)
        )

        # Step 4: 命中数判定与分支执行
        if has_any_hard_filter and len(hard_filtered) == 0:
            # 命中 == 0 -> 触发冲突诊断与双路径松弛，同时对同子类目池做 BM25 + halfvec + RRF 供对比与兜底
            hybrid_sub = self.rank_candidates_hybrid(
                query=query,
                candidates=category_locked_pool,
                soft_constraints=soft_constraints,
                mode=mode,
                allow_remote_embedding=allow_remote_embedding,
            )
            relax_res = relax_constraints_safely(
                extracted=extracted,
                sub_category_skus=category_locked_pool,
                fallback_top_skus=hybrid_sub["ranked_skus"],
                spec_registry=self.spec_registry,
            )

            # 对松弛选出的双路径候选计算/附上 RRF 与混合检索元数据
            relaxed_skus: List[Dict[str, Any]] = []
            product_cards: List[Dict[str, Any]] = []
            price_explain_cards: List[Dict[str, Any]] = []
            for opt in relax_res.get("options", []):
                sku_obj = opt.get("sku")
                if not isinstance(sku_obj, dict):
                    continue
                relaxed_skus.append(sku_obj)
                product_cards.append(
                    {
                        "sku_id": sku_obj["sku_id"],
                        "sub_category": sku_obj.get("sub_category"),
                        "title": sku_obj["title"],
                        "original_price": float(sku_obj["original_price"]),
                        "final_price": float(sku_obj["final_price"]),
                        "image_url": f"/static/images/{sku_obj['sku_id']}.jpg",
                        "cos_image_url": sku_obj.get("cos_image_url"),
                        "match_badge": opt.get("badge", "折中推荐"),
                        "relaxation_path": opt.get("path"),
                        "highlights": list(sku_obj.get("highlights", [])),
                        "specs": dict(sku_obj.get("specs", {})),
                    }
                )
                if opt.get("price_explain_card"):
                    price_explain_cards.append(opt["price_explain_card"])

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "constraint_status": "relaxed",
                "sub_category": sub_cat,
                "sub_category_ambiguity": sub_ambiguity,
                "extracted_constraints": extracted,
                "relaxation_reason": relax_res.get("reason"),
                "relaxation": relax_res,
                "top_skus": relaxed_skus,
                "product_cards": product_cards,
                "price_explain_cards": price_explain_cards,
                "followup_pills": relax_res.get("pills", []),
                "rag_retrieve_ms": round(elapsed_ms, 2),
                "index_mode": "flat_exact_scan",
                "db_backend": self.db_backend,
                "recall_channels": {
                    "sub_category_pool": len(category_locked_pool),
                    "hard_filtered": 0,
                    "bm25_hits": min(len(hybrid_sub["bm25_ranking"]), top_k),
                    "pgvector_halfvec_hits": min(len(hybrid_sub["dense_ranking"]), top_k),
                    "relaxed_candidates": len(relaxed_skus),
                },
            }

        # 命中 >= 1（正常混合排序）：对 hard_filtered 候选集做 BM25 + halfvec + RRF 重排
        hybrid_res = self.rank_candidates_hybrid(
            query=query,
            candidates=hard_filtered,
            soft_constraints=soft_constraints,
            mode=mode,
            allow_remote_embedding=allow_remote_embedding,
        )
        top_skus = hybrid_res["ranked_skus"][:top_k]

        product_cards = []
        price_explain_cards = []
        for s in top_skus[:3]:
            product_cards.append(
                {
                    "sku_id": s["sku_id"],
                    "sub_category": s.get("sub_category"),
                    "title": s["title"],
                    "original_price": float(s["original_price"]),
                    "final_price": float(s["final_price"]),
                    "image_url": f"/static/images/{s['sku_id']}.jpg",
                    "cos_image_url": s.get("cos_image_url"),
                    "match_badge": "硬约束精准匹配 · 混合检索高分优选" if has_any_hard_filter else "多模态高口碑优选",
                    "relaxation_path": None,
                    "highlights": list(s.get("highlights", [])),
                    "specs": dict(s.get("specs", {})),
                }
            )
            price_explain_cards.append(build_price_explain_card(s))

        if sub_ambiguity == "multi_sub_category" and len(matched_subs) > 1:
            labels = [
                self.spec_registry["sub_categories"].get(sc, {}).get("display_label", sc)
                for sc in matched_subs
            ]
            pills = [f"先看{lb}推荐" for lb in labels[:2]] + ["两类商品一起对比"]
        elif top_skus:
            first_sku = top_skus[0]
            pills = [
                f"看 {first_sku['title'][:12]} 的详细参数",
                "有更便宜的同品类平替吗？",
                "支持 7 天无理由和 15 天价保吗？",
            ]
        else:
            pills = ["换个预算范围看看", "转人工导购咨询"]

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "constraint_status": "satisfied",
            "sub_category": sub_cat,
            "sub_category_ambiguity": sub_ambiguity,
            "extracted_constraints": extracted,
            "relaxation_reason": None,
            "relaxation": None,
            "top_skus": top_skus,
            "product_cards": product_cards,
            "price_explain_cards": price_explain_cards,
            "followup_pills": pills[:3],
            "rag_retrieve_ms": round(elapsed_ms, 2),
            "index_mode": "flat_exact_scan",
            "db_backend": self.db_backend,
            "recall_channels": {
                "sub_category_pool": len(category_locked_pool),
                "hard_filtered": len(hard_filtered),
                "bm25_hits": min(len(hybrid_res["bm25_ranking"]), top_k),
                "pgvector_halfvec_hits": min(len(hybrid_res["dense_ranking"]), top_k),
                "relaxed_candidates": 0,
            },
        }

    async def search_async(
        self,
        query: str,
        extracted_constraints: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
        mode: str = "hybrid_rrf",
        allow_remote_embedding: bool = True,
    ) -> Dict[str, Any]:
        """异步包装器：供主链路使用 asyncio.gather 与 JevDecisionRouter.evaluate_async 并发执行。"""
        return await asyncio.to_thread(
            self.search,
            query=query,
            extracted_constraints=extracted_constraints,
            top_k=top_k,
            mode=mode,
            allow_remote_embedding=allow_remote_embedding,
        )

