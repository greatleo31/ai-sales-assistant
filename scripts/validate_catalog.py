#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
60 SKU 商品目录、规格注册表、硬约束冲突对与售后时间契约自洽性闸门 (Self-Consistency Gate)
依据 HANDOFF.md §2 & §6 与模块一复审要求 (R1~R8)：
  - 数据生成后、灌库前必须运行本脚本且 100% 断言通过；
  - 禁止在脚本中硬编码冲突对数值，统一从 data/spec_registry.json 与 data/conflict_pairs.json 读取；
  - 全部 5 个核心交付文件必须存在且结构、类型、跨文件引用、时间顺序、反污染与排序唯一性全部校验通过。
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

SHANGHAI_TZ = timezone(timedelta(hours=8))

# 固定基准的各维度默认比较方向（用于检测 spec_registry.json 自身定义是否发生意外篡改漂移；
# 注意：用户 Query 的显式比较方向不必恒等于默认方向）
CANONICAL_SPEC_OPS: Dict[Tuple[str, str], str] = {
    ("laptop_bag", "screen_size"): ">=",
    ("gan_charger", "power_w"): ">=",
    ("gan_charger", "ports"): ">=",
    ("mechanical_keyboard", "switch_type"): "==",
    ("mechanical_keyboard", "layout"): "==",
    ("mechanical_keyboard", "connectivity"): "==",
    ("nc_headphones", "anc_db"): ">=",
    ("nc_headphones", "battery_h"): ">=",
    ("nc_headphones", "weight_g"): "<=",
    ("power_bank", "capacity_mah"): ">=",
    ("power_bank", "output_w"): ">=",
    ("power_bank", "weight_g"): "<=",
    ("usb_hub_cable", "port_count"): ">=",
    ("usb_hub_cable", "protocol"): "==",
    ("hoodie", "fabric_weight_gm2"): ">=",
    ("knit_cardigan", "fabric_weight_gm2"): ">=",
    ("windbreaker", "waterproof_mm"): ">=",
    ("windbreaker", "weight_g"): "<=",
    ("cargo_pants", "material"): "==",
    ("cargo_pants", "fit"): "==",
    ("tshirt", "fabric_weight_gm2"): ">=",
    ("facial_cream", "key_ingredient"): "==",
    ("facial_cream", "skin_type"): "==",
    ("facial_cream", "volume_ml"): ">=",
    ("amino_cleanser", "volume_ml"): ">=",
    ("sunscreen", "spf"): ">=",
    ("sunscreen", "pa"): "==",
    ("sunscreen", "volume_ml"): ">=",
    ("b5_serum", "volume_ml"): ">=",
    ("salicylic_pads", "concentration_pct"): "<=",
    ("lip_balm", "volume_ml"): ">=",
}

ALLOWED_OPS = {"<=", ">=", "=="}
ALLOWED_ORDER_STATUSES = {"pending", "shipped", "delivered", "refunded"}
REQUIRED_SEED_ORDER_IDS = {
    "ORD-20261001-001",
    "ORD-20261001-002",
    "ORD-20261001-003",
    "ORD-20261001-004",
    "ORD-20261001-005",
    "ORD-20261001-006",
    "ORD-20261001-007",
    "ORD-20261001-008",
    "ORD-20261001-009",
    "ORD-20261001-010",
}
FORBIDDEN_OVERSTRONG_ALIASES = {"108键", "全肤质", "普通肤质", "敏感肌", "usb3.2"}
REQUIRED_AMBIGUOUS_TERMS = {"108键", "全尺寸", "全肤质", "普通肤质", "敏感肌", "usb3.2", "连帽卫衣开衫"}
ALLOWED_AMBIGUOUS_ACTIONS = {
    "soft_or_clarify",
    "no_hard_filter",
    "soft_preference",
    "candidate_set",
    "compound_sub_category",
}
ALLOWED_VISUAL_DESCRIPTION_SOURCES = {
    "synthetic_draft_pending_image_calibration",
    "calibrated_from_retained_image",
}
COS_URL_PATTERN = re.compile(
    r"^(?:https://[a-z0-9-]+-\d+\.cos\.[a-z0-9-]+\.myqcloud\.com/.+\.(?:jpg|jpeg|png)|/static/images/.+\.(?:jpg|jpeg|png))$",
    re.IGNORECASE,
)
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

# 【S3 / F3 / D3 / D4 中文语境量纲与容量数值校验正则（不依赖 \b 单词边界，忽略大小写覆盖 g/G/克/kg/Kg/kG/KG/千克/公斤）】
BARE_GRAM_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:g|克)(?!\s*/\s*㎡|[a-zA-Z])", re.IGNORECASE)
KG_WEIGHT_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:kg|千克|公斤)(?!\s*/\s*㎡|[a-zA-Z])",
    re.IGNORECASE,
)
WEIGHT_UNIT_IN_TEXT_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:kg|g|千克|公斤|克)(?!\s*/\s*㎡|[a-zA-Z])",
    re.IGNORECASE,
)
ML_CAPACITY_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:ml|毫升)(?![a-zA-Z])", re.IGNORECASE)
FABRIC_GM2_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:g|克)\s*/\s*㎡", re.IGNORECASE)

# 【F1 / D1 / D2 / D3 数值规格与商品文案一致性校验正则】
SCREEN_SIZE_TEXT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:寸|英寸)")
POWER_W_TEXT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[wW瓦](?![hH])")
PORTS_TEXT_PATTERN = re.compile(r"(?<!\d)(\d+|[单双两一二三四五六])\s*(?:快充)?口")
PORT_CA_CODE_PATTERN = re.compile(r"(?<!\d)(\d+)\s*[cC]\s*(\d+)\s*[aA](?![a-zA-Z])")
PORT_TYPEC_USBA_PAIR_PATTERN = re.compile(
    r"(\d+)\s*个\s*Type-C(?:接口|口)?\s*(?:与|和|加|\+)\s*(\d+)\s*个[^，。；]{0,15}?USB-A(?:接口|口)?",
    re.IGNORECASE,
)
CAPACITY_MAH_TEXT_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:mAh|毫安时|毫安)(?![a-zA-Z])",
    re.IGNORECASE,
)
CAPACITY_WAN_MAH_TEXT_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*万\s*(?:mAh|毫安时|毫安)(?![a-zA-Z])",
    re.IGNORECASE,
)
WATERPROOF_MM_TEXT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*mm", re.IGNORECASE)
SPF_TEXT_PATTERN = re.compile(r"SPF\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
CONCENTRATION_PCT_TEXT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*%")
ANC_DB_TEXT_PATTERN = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)
ZH_PORT_NUM_MAP: Dict[str, int] = {
    "单": 1,
    "一": 1,
    "双": 2,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
}

# 【S2 / A2 独立中性标签基准常量】：防止通过篡改 spec_registry.json 的 display_label / display_labels 绕过 R1 反污染检查
CANONICAL_SUB_DISPLAY_LABELS: Dict[str, str] = {
    "laptop_bag": "笔记本电脑包",
    "gan_charger": "氮化镓快充充电器",
    "mechanical_keyboard": "机械键盘",
    "nc_headphones": "头戴式降噪耳机",
    "power_bank": "移动电源/充电宝",
    "usb_hub_cable": "Type-C拓展坞",
    "hoodie": "卫衣",
    "knit_cardigan": "针织开衫",
    "windbreaker": "户外冲锋衣",
    "cargo_pants": "工装裤",
    "tshirt": "短袖T恤",
    "facial_cream": "保湿修护面霜",
    "amino_cleanser": "氨基酸洁面",
    "sunscreen": "防晒霜",
    "b5_serum": "B5修护精华液",
    "salicylic_pads": "水杨酸棉片",
    "lip_balm": "润唇膏",
}

CANONICAL_ENUM_DISPLAY_LABELS: Dict[Tuple[str, str, str], str] = {
    ("mechanical_keyboard", "switch_type", "linear"): "线性轴",
    ("mechanical_keyboard", "switch_type", "tactile"): "段落轴",
    ("mechanical_keyboard", "switch_type", "clicky"): "有声段落轴",
    ("mechanical_keyboard", "layout", "87_keys"): "87键配列",
    ("mechanical_keyboard", "layout", "98_keys"): "98键配列",
    ("mechanical_keyboard", "layout", "104_keys"): "104键全尺寸配列",
    ("mechanical_keyboard", "connectivity", "wired"): "有线单模",
    ("mechanical_keyboard", "connectivity", "bluetooth"): "蓝牙双模",
    ("mechanical_keyboard", "connectivity", "2.4g"): "2.4G无线",
    ("mechanical_keyboard", "connectivity", "triple_mode"): "三模无线",
    ("usb_hub_cable", "protocol", "usb2.0"): "USB2.0协议",
    ("usb_hub_cable", "protocol", "usb3.0"): "USB3.0(5Gbps)",
    ("usb_hub_cable", "protocol", "usb3.2gen2"): "USB3.2 Gen2(10Gbps)",
    ("usb_hub_cable", "protocol", "thunderbolt4"): "雷电4(40Gbps)",
    ("cargo_pants", "material", "cotton"): "纯棉",
    ("cargo_pants", "material", "polyester"): "聚酯纤维",
    ("cargo_pants", "material", "cotton_blend"): "棉混纺",
    ("cargo_pants", "material", "nylon"): "防撕裂尼龙",
    ("cargo_pants", "fit", "slim"): "修身版型",
    ("cargo_pants", "fit", "regular"): "合身直筒",
    ("cargo_pants", "fit", "loose"): "宽松阔腿",
    ("facial_cream", "key_ingredient", "ceramide_np"): "神经酰胺NP",
    ("facial_cream", "key_ingredient", "ectoin"): "依克多因",
    ("facial_cream", "key_ingredient", "hyaluronic_acid"): "透明质酸(玻尿酸)",
    ("facial_cream", "key_ingredient", "vitamin_b5"): "维生素B5(泛醇)",
    ("facial_cream", "key_ingredient", "salicylic_acid"): "水杨酸",
    ("facial_cream", "skin_type", "dry_sensitive"): "干敏肌",
    ("facial_cream", "skin_type", "oily_acne"): "油痘肌",
    ("facial_cream", "skin_type", "combination"): "混合肌",
    ("facial_cream", "skin_type", "neutral"): "中性肌",
    ("sunscreen", "pa", "pa_plus"): "PA+",
    ("sunscreen", "pa", "pa_plus_plus"): "PA++",
    ("sunscreen", "pa", "pa_plus_plus_plus"): "PA+++",
    ("sunscreen", "pa", "pa_plus_plus_plus_plus"): "PA++++",
}


def is_pure_number(val: Any) -> bool:
    """判定是否为严格有限 JSON 数值（排除 Python 中 bool 是 int 子类及 NaN/Inf 的陷阱）。"""
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))


def is_pure_int(val: Any) -> bool:
    """判定是否为严格整数（排除 bool 与非整数 float）。"""
    return isinstance(val, int) and not isinstance(val, bool)


def is_non_empty_str(val: Any) -> bool:
    """判定是否为非空字符串。"""
    return isinstance(val, str) and len(val.strip()) > 0


def parse_aware_iso_datetime(val: Any) -> Optional[datetime]:
    """【F2 防御】：解析带显式 UTC 时区偏移的 ISO-8601 时间戳；无时区或格式非法统一返回 None，绝不抛未处理异常。"""
    if not is_non_empty_str(val):
        return None
    try:
        dt = datetime.fromisoformat(str(val))
    except Exception:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def compare_op(actual: Any, op: str, target: Any) -> bool:
    """执行确定性比较操作。"""
    if op not in ALLOWED_OPS:
        raise ValueError(f"Unsupported operator: {op}")
    if op in ("<=", ">="):
        if not (is_pure_number(actual) and is_pure_number(target)):
            return False
        if op == "<=":
            return float(actual) <= float(target) + 1e-9
        return float(actual) >= float(target) - 1e-9
    # op == "=="
    if is_pure_number(actual) and is_pure_number(target):
        return abs(float(actual) - float(target)) <= 1e-9
    return actual == target


def compute_numeric_closeness_score(
    sku: Dict[str, Any],
    spec_constraints: List[Dict[str, Any]],
    sub_cat_specs_meta: Dict[str, Any],
) -> float:
    """
    【R7 明确作用域】：仅用于数值型规格(type=='number')的多规格归一化接近度基准计算函数：
      Closeness(s) = 1 - mean(deficit_i / span_i)
    若传入枚举型(type=='enum')或未声明 range 的规格约束，入口立即抛出 ValueError 拒绝。
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
            raise ValueError(f"compute_numeric_closeness_score 收到未知的 spec_key: {key!r}")
        spec_meta = sub_cat_specs_meta[key]
        if not isinstance(spec_meta, dict):
            raise ValueError(f"compute_numeric_closeness_score 收到损坏的 spec_meta: {key!r}={spec_meta!r}")
        rng = spec_meta.get("range")
        if (
            spec_meta.get("type") != "number"
            or not isinstance(rng, list)
            or len(rng) != 2
            or not is_pure_number(rng[0])
            or not is_pure_number(rng[1])
        ):
            raise ValueError(
                f"compute_numeric_closeness_score 仅支持数值型(number)规格(且须含有效 2 元数值 range)，不支持枚举或损坏的 range: {key!r} (type={spec_meta.get('type')!r}, range={rng!r})"
            )
        if op not in ("<=", ">=", "=="):
            raise ValueError(f"compute_numeric_closeness_score 不支持比较符: {op!r}")
        if not is_pure_number(raw_target):
            raise ValueError(f"compute_numeric_closeness_score 目标值必须为有限数值: {key}={raw_target!r}")

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


# 保留向后兼容别名
compute_closeness_score = compute_numeric_closeness_score


def normalize_query_text(text: str) -> str:
    """按 spec_registry.matching_policy 对输入文本做确定性规范化（NFKC + 小写）。"""
    return unicodedata.normalize("NFKC", text).lower().strip()


def match_aliases_with_policy(
    query: str,
    spec_registry: Dict[str, Any],
    sub_category: Optional[str] = None,
) -> Dict[str, Any]:
    """
    【R5 / S5 / D6 消歧策略实现】：
      1. NFKC + ASCII 小写规范化；
      2. 否定前缀守卫（支持跳过空白字符及常见单字助词 [用带选买要想看推配有是个款种的]，且避免“非常想要红轴”/“不是非要红轴”被误伤）；
      3. PA+ 右侧加号边界守卫 + 数字词左侧数字边界守卫；
      4. 同一文本区间最长匹配优先 (longest_match_first)，长词消费区间后禁止子区间短词重复触发；
      5. 模糊/软性词（108键、全肤质、敏感肌、USB3.2、连帽卫衣开衫）走 ambiguous_terms 策略，不强填硬过滤枚举；
      6. 多子类目同时命中时返回 matched_sub_categories 列表并标记 multi_sub_category。
    """
    norm_q = normalize_query_text(query)
    policy = spec_registry.get("matching_policy", {})
    neg_prefixes = [normalize_query_text(p) for p in policy.get("negation_prefixes", []) if is_non_empty_str(p)]
    neg_win = int(policy.get("negation_window_chars", 4))
    ambiguous_defs = spec_registry.get("ambiguous_terms", {})

    neg_regex = (
        re.compile(
            r"(?:"
            + "|".join(re.escape(np) for np in sorted(neg_prefixes, key=len, reverse=True))
            + r")\s*[用带选买要想看推配有是个款种的]{0,2}\s*$"
        )
        if neg_prefixes
        else None
    )

    def is_negated(start_idx: int) -> bool:
        if neg_regex is None:
            return False
        prefix_slice = norm_q[max(0, start_idx - max(neg_win + 4, 8)) : start_idx]
        return bool(neg_regex.search(prefix_slice))

    def valid_boundary(alias_norm: str, start: int, end: int) -> bool:
        # PA+ 边界：右侧不能紧跟 '+'
        if alias_norm.startswith("pa+"):
            if end < len(norm_q) and norm_q[end] == "+":
                return False
        # 数字开头或结尾边界
        if alias_norm and alias_norm[0].isdigit() and start > 0 and norm_q[start - 1].isdigit():
            return False
        return True

    # 1. 检测 ambiguous_terms
    triggered_ambiguous: Dict[str, Any] = {}
    consumed_ambig_spans: List[Tuple[int, int]] = []
    sorted_ambig = sorted(ambiguous_defs.items(), key=lambda kv: len(normalize_query_text(kv[0])), reverse=True)
    for raw_term, meta in sorted_ambig:
        t_norm = normalize_query_text(raw_term)
        for m in re.finditer(re.escape(t_norm), norm_q):
            s_idx, e_idx = m.start(), m.end()
            if not valid_boundary(t_norm, s_idx, e_idx) or is_negated(s_idx):
                continue
            # 若为 USB3.2，需排除已被更长的 USB3.2Gen2 覆盖的情况
            if t_norm == "usb3.2" and norm_q[s_idx:].startswith(("usb3.2gen2", "usb3.2 gen2")):
                continue
            # 若为 敏感肌，需排除已被 干敏肌 / 干性敏感肌 覆盖的情况
            if t_norm == "敏感肌" and s_idx >= 2 and norm_q[s_idx - 2 : e_idx] in ("干敏肌", "油敏肌"):
                continue
            if t_norm == "敏感肌" and s_idx >= 3 and norm_q[s_idx - 3 : e_idx] == "干性敏感肌":
                continue
            triggered_ambiguous[raw_term] = meta
            if meta.get("action") == "compound_sub_category":
                consumed_ambig_spans.append((s_idx, e_idx))

    # 2. 子类目匹配（最长匹配优先 + 区间互斥）
    sub_candidates: List[Tuple[str, str]] = []
    for sc_name, sc_meta in spec_registry["sub_categories"].items():
        for a in sc_meta.get("zh_aliases", []):
            sub_candidates.append((normalize_query_text(a), sc_name))
    sub_candidates.sort(key=lambda x: len(x[0]), reverse=True)

    occupied_sub: List[Tuple[int, int]] = list(consumed_ambig_spans)
    matched_subs: List[Tuple[int, str]] = []
    for raw_term, meta in triggered_ambiguous.items():
        if meta.get("action") == "compound_sub_category":
            matched_subs.append((0, meta["primary_sub_category"]))

    for alias_norm, sc_name in sub_candidates:
        for m in re.finditer(re.escape(alias_norm), norm_q):
            s_idx, e_idx = m.start(), m.end()
            if not valid_boundary(alias_norm, s_idx, e_idx) or is_negated(s_idx):
                continue
            if any(not (e_idx <= os or s_idx >= oe) for os, oe in occupied_sub):
                continue
            occupied_sub.append((s_idx, e_idx))
            matched_subs.append((s_idx, sc_name))

    matched_subs.sort(key=lambda x: x[0])
    unique_subs: List[str] = []
    for _, sc in matched_subs:
        if sc not in unique_subs:
            unique_subs.append(sc)

    target_sub = sub_category or (unique_subs[0] if len(unique_subs) == 1 else None)
    sub_ambiguity = "multi_sub_category" if len(unique_subs) > 1 else ("single" if len(unique_subs) == 1 else "none")

    # 3. 枚举规格匹配（同一枚举维度按最长匹配优先消费字符区间）
    enum_matches: Dict[str, List[str]] = {}
    if target_sub and target_sub in spec_registry["sub_categories"]:
        specs_meta = spec_registry["sub_categories"][target_sub]["specs"]
        for skey, sdef in specs_meta.items():
            if sdef.get("type") != "enum":
                continue
            cand_list: List[Tuple[str, str]] = []
            for eval_name, aliases in sdef.get("zh_aliases", {}).items():
                for a in aliases:
                    cand_list.append((normalize_query_text(a), eval_name))
            cand_list.sort(key=lambda x: len(x[0]), reverse=True)

            occupied_spans: List[Tuple[int, int]] = []
            found_vals: List[Tuple[int, str]] = []
            for alias_norm, eval_name in cand_list:
                for m in re.finditer(re.escape(alias_norm), norm_q):
                    s_idx, e_idx = m.start(), m.end()
                    if not valid_boundary(alias_norm, s_idx, e_idx) or is_negated(s_idx):
                        continue
                    if any(not (e_idx <= os or s_idx >= oe) for os, oe in occupied_spans):
                        continue
                    occupied_spans.append((s_idx, e_idx))
                    found_vals.append((s_idx, eval_name))
            found_vals.sort(key=lambda x: x[0])
            dedup_vals: List[str] = []
            for _, v in found_vals:
                if v not in dedup_vals:
                    dedup_vals.append(v)
            if dedup_vals:
                enum_matches[skey] = dedup_vals

    return {
        "matched_sub_categories": unique_subs,
        "sub_category_ambiguity": sub_ambiguity,
        "resolved_sub_category": target_sub,
        "hard_enum_matches": enum_matches,
        "ambiguous_or_soft_hits": triggered_ambiguous,
    }


def evaluate_order_eligibility(
    order: Dict[str, Any],
    target_sku: Dict[str, Any],
    rules_by_type: Dict[str, Dict[str, Any]],
    ref_dt: datetime,
) -> Dict[str, Any]:
    """
    【R6 / S4 / F2 确定性售后与价保核验函数】：
    真正动态消费 platform_rules.json 的全部机器参数，并对无时区或损坏时间锚点抛出结构化 ValueError(F2)：
      - window_hours: 滚动小时窗口
      - anchor_field: 主起算时间锚点字段名（如 'signed_at'）
      - fallback_anchor_field: 未签收且 allow_unsigned=True 时的回退时间锚点字段名（如 'paid_at'）
      - include_boundary: 是否包含窗口整点边界（True 用 <=，False 用 <）
      - allow_unsigned: 是否允许未签收订单参与判定
      - require_unopened: 是否要求未拆封（is_opened == False）
    """
    if not isinstance(ref_dt, datetime) or ref_dt.tzinfo is None or ref_dt.utcoffset() is None:
        raise ValueError(f"ref_dt 必须包含显式 UTC 时区偏移(F2): {ref_dt!r}")

    aftersales_rule = rules_by_type["aftersales"]
    price_rule = rules_by_type["price_guarantee"]

    status = order["order_status"]
    is_opened = bool(order["is_opened"])

    # 1. 7天无理由退货判定 (RULE-AFTER-001)
    return_window_hours = float(aftersales_rule["window_hours"])
    af_anchor_field = aftersales_rule.get("anchor_field", "signed_at")
    af_fallback_field = aftersales_rule.get("fallback_anchor_field")
    af_inc_boundary = bool(aftersales_rule.get("include_boundary", True))
    af_allow_unsigned = bool(aftersales_rule.get("allow_unsigned", False))
    af_req_unopened = bool(aftersales_rule.get("require_unopened", True))

    af_anchor_str: Optional[str] = None
    af_unsigned_rejected = False
    if status == "delivered" and is_non_empty_str(af_anchor_field) and order.get(af_anchor_field):
        af_anchor_str = order.get(af_anchor_field)
    else:
        if not af_allow_unsigned:
            af_unsigned_rejected = True
        else:
            fb_key = af_fallback_field if is_non_empty_str(af_fallback_field) else "paid_at"
            af_anchor_str = order.get(fb_key) if isinstance(fb_key, str) else None

    if af_unsigned_rejected:
        return_ok = False
        return_reason = "NOT_SIGNED_YET"
    elif not af_anchor_str:
        return_ok = False
        return_reason = "MISSING_ANCHOR_TIME"
    else:
        signed_dt = parse_aware_iso_datetime(af_anchor_str)
        if signed_dt is None:
            raise ValueError(f"订单退货锚点时间格式非法或缺少 UTC 时区偏移(F2): {af_anchor_str!r}")
        elapsed_signed_h = (ref_dt - signed_dt).total_seconds() / 3600.0
        if elapsed_signed_h < -1e-9:
            return_ok = False
            return_reason = "INVALID_FUTURE_SIGNED_AT"
        else:
            if af_inc_boundary:
                in_window = elapsed_signed_h <= return_window_hours + 1e-9
            else:
                in_window = elapsed_signed_h < return_window_hours - 1e-9
            unopened_violated = is_opened if af_req_unopened else False
            if in_window and not unopened_violated:
                return_ok = True
                return_reason = "ELIGIBLE"
            elif in_window and unopened_violated:
                return_ok = False
                return_reason = "OPENED_NOT_ALLOWED"
            elif (not in_window) and (not unopened_violated):
                return_ok = False
                return_reason = "WINDOW_EXPIRED_7D"
            else:
                return_ok = False
                return_reason = "EXPIRED_AND_OPENED"

    # 2. 15天价格保护判定 (RULE-PRICE-001)
    price_window_hours = float(price_rule["window_hours"])
    pg_anchor_field = price_rule.get("anchor_field", "signed_at")
    pg_fallback_field = price_rule.get("fallback_anchor_field")
    pg_inc_boundary = bool(price_rule.get("include_boundary", True))
    pg_allow_unsigned = bool(price_rule.get("allow_unsigned", True))
    pg_req_unopened = bool(price_rule.get("require_unopened", False))
    has_pg = bool(order.get("has_price_guarantee", False))

    pg_anchor_str: Optional[str] = None
    pg_unsigned_rejected = False
    if status == "delivered" and is_non_empty_str(pg_anchor_field) and order.get(pg_anchor_field):
        pg_anchor_str = order.get(pg_anchor_field)
    else:
        if not pg_allow_unsigned:
            pg_unsigned_rejected = True
        else:
            pg_anchor_str = order.get(pg_fallback_field) if is_non_empty_str(pg_fallback_field) else None

    if not has_pg:
        pg_ok = False
        pg_reason = "NO_PRICE_GUARANTEE_TAG"
        diff_amt = 0.0
    elif pg_unsigned_rejected:
        pg_ok = False
        pg_reason = "NOT_SIGNED_YET"
        diff_amt = 0.0
    elif not pg_anchor_str:
        pg_ok = False
        pg_reason = "MISSING_ANCHOR_TIME"
        diff_amt = 0.0
    else:
        anchor_dt = parse_aware_iso_datetime(pg_anchor_str)
        if anchor_dt is None:
            raise ValueError(f"订单价保锚点时间格式非法或缺少 UTC 时区偏移(F2): {pg_anchor_str!r}")
        elapsed_pg_h = (ref_dt - anchor_dt).total_seconds() / 3600.0
        raw_diff = round(float(order["pay_amount"]) - float(target_sku["final_price"]), 2)
        if elapsed_pg_h < -1e-9:
            pg_ok = False
            pg_reason = "INVALID_FUTURE_ANCHOR_TIME"
            diff_amt = 0.0
        else:
            if pg_inc_boundary:
                in_pg_window = elapsed_pg_h <= price_window_hours + 1e-9
            else:
                in_pg_window = elapsed_pg_h < price_window_hours - 1e-9
            pg_unopened_violated = is_opened if pg_req_unopened else False
            if not in_pg_window:
                pg_ok = False
                pg_reason = "WINDOW_EXPIRED_15D"
                diff_amt = 0.0
            elif pg_unopened_violated:
                pg_ok = False
                pg_reason = "OPENED_NOT_ALLOWED"
                diff_amt = 0.0
            elif raw_diff <= 1e-9:
                pg_ok = False
                pg_reason = "NO_PRICE_DROP"
                diff_amt = 0.0
            else:
                pg_ok = True
                pg_reason = "ELIGIBLE"
                diff_amt = raw_diff

    return {
        "return_7d_eligible": return_ok,
        "return_7d_reason_code": return_reason,
        "price_guarantee_15d_eligible": pg_ok,
        "price_guarantee_15d_reason_code": pg_reason,
        "price_diff_amount": diff_amt,
    }


def build_expected_retrieval_text(sku: Dict[str, Any], sub_cat_meta: Dict[str, Any]) -> str:
    """根据 SKU 结构化数据与中性标签重建期望的 retrieval_text，用于校验语料无别名污染。"""
    spec_parts: List[str] = []
    specs_def = sub_cat_meta.get("specs", {})
    for k, v in sku["specs"].items():
        s_meta = specs_def.get(k, {})
        label = s_meta.get("label", k)
        unit = s_meta.get("unit", "")
        if s_meta.get("type") == "enum":
            display_labels = s_meta.get("display_labels", {})
            neutral_label = display_labels.get(str(v), str(v))
            spec_parts.append(f"{label}:{v}({neutral_label})")
        else:
            spec_parts.append(f"{label}:{v}{unit}")

    sub_display_label = sub_cat_meta.get("display_label", sku["sub_category"])
    highlights_str = "；".join(sku["highlights"])
    discount = round(float(sku["original_price"]) - float(sku["final_price"]), 2)

    return (
        f"【商品】{sku['title']} "
        f"【品牌】{sku['brand']} "
        f"【类目】{sku['category']}/{sku['sub_category']} ({sub_display_label}) "
        f"【价格】到手价¥{sku['final_price']} (原价¥{sku['original_price']}，直降¥{discount}) "
        f"【核心规格】{' | '.join(spec_parts)} "
        f"【核心卖点】{highlights_str} "
        f"【视觉外观】{sku['visual_description']}"
    )


def validate_all(data_dir: Optional[Path] = None) -> Tuple[bool, List[str], List[str]]:
    target_dir = data_dir if data_dir is not None else DATA_DIR
    errors: List[str] = []
    passed_checks: List[str] = []

    spec_registry_path = target_dir / "spec_registry.json"
    conflict_pairs_path = target_dir / "conflict_pairs.json"
    catalog_path = target_dir / "ecommerce_catalog_60skus.json"
    orders_path = target_dir / "mock_orders.json"
    rules_path = target_dir / "platform_rules.json"

    # 【R4 修复】：5 个核心交付文件全部为强制必检项，缺失任何一个直接失败
    required_files = [
        spec_registry_path,
        conflict_pairs_path,
        catalog_path,
        rules_path,
        orders_path,
    ]
    for p in required_files:
        if not p.exists():
            errors.append(f"核心交付数据文件缺失: {p.name}")
    if errors:
        return False, passed_checks, errors

    try:
        spec_registry = json.loads(spec_registry_path.read_text(encoding="utf-8"))
        conflict_data = json.loads(conflict_pairs_path.read_text(encoding="utf-8"))
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        orders = json.loads(orders_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"JSON 解析失败: {exc}")
        return False, passed_checks, errors

    # 顶层容器类型校验
    if not isinstance(spec_registry, dict):
        errors.append("spec_registry.json 顶层必须为 JSON object")
    if not isinstance(conflict_data, dict):
        errors.append("conflict_pairs.json 顶层必须为 JSON object")
    if not isinstance(catalog, list):
        errors.append("ecommerce_catalog_60skus.json 顶层必须为 JSON array")
    if not isinstance(rules, list):
        errors.append("platform_rules.json 顶层必须为 JSON array")
    if not isinstance(orders, list):
        errors.append("mock_orders.json 顶层必须为 JSON array")
    if errors:
        return False, passed_checks, errors

    # ==================== 1. spec_registry.json 完整性、词表闭环与默认方向基准校验 ====================
    sub_categories_meta = spec_registry.get("sub_categories")
    categories_map = spec_registry.get("categories")
    if not isinstance(sub_categories_meta, dict) or len(sub_categories_meta) != 17:
        errors.append(
            f"spec_registry.json sub_categories 必须为包含 17 个子类目的字典，实际: {len(sub_categories_meta) if isinstance(sub_categories_meta, dict) else type(sub_categories_meta)}"
        )
        return False, passed_checks, errors
    if not isinstance(categories_map, dict) or set(categories_map.keys()) != {"3c_digital", "apparel", "beauty"}:
        errors.append("spec_registry.json categories 必须包含 3c_digital / apparel / beauty")
        return False, passed_checks, errors

    passed_checks.append("spec_registry.json 包含完整 17 个子类目定义")

    registry_schema_ok = True
    seen_sub_aliases: Dict[str, str] = {}
    for sub_cat, meta in sub_categories_meta.items():
        if not isinstance(meta, dict):
            errors.append(f"子类目 [{sub_cat}] 定义必须为字典")
            registry_schema_ok = False
            continue
        if meta.get("category") not in categories_map:
            errors.append(f"子类目 [{sub_cat}] category 字段非法或缺失(F2): {meta.get('category')!r}")
            registry_schema_ok = False
        if not is_pure_int(meta.get("count")) or int(meta.get("count")) <= 0:
            errors.append(f"子类目 [{sub_cat}] count 必须为正整数(F2): {meta.get('count')!r}")
            registry_schema_ok = False

        sub_dl = meta.get("display_label")
        if not is_non_empty_str(sub_dl):
            errors.append(f"子类目 [{sub_cat}] 缺少非空字符串 display_label")
            registry_schema_ok = False
        else:
            expected_sub_dl = CANONICAL_SUB_DISPLAY_LABELS.get(sub_cat)
            if expected_sub_dl and sub_dl != expected_sub_dl:
                errors.append(
                    f"子类目 [{sub_cat}] display_label 偏离中性基准(S2/A2): 期望 {expected_sub_dl!r}，实际 {sub_dl!r}"
                )
                registry_schema_ok = False

        aliases = meta.get("zh_aliases")
        if not isinstance(aliases, list) or not aliases or not all(is_non_empty_str(a) for a in aliases):
            errors.append(f"子类目 [{sub_cat}] zh_aliases 必须为非空字符串列表")
            registry_schema_ok = False
        else:
            for alias in aliases:
                norm_a = normalize_query_text(alias)
                if norm_a in seen_sub_aliases and seen_sub_aliases[norm_a] != sub_cat:
                    errors.append(
                        f"子类目完全相同 alias 冲突: '{alias}' 同时出现在 {seen_sub_aliases[norm_a]} 与 {sub_cat}"
                    )
                    registry_schema_ok = False
                seen_sub_aliases[norm_a] = sub_cat

        specs_dict = meta.get("specs")
        if not isinstance(specs_dict, dict) or not specs_dict:
            errors.append(f"子类目 [{sub_cat}] specs 必须为非空字典")
            registry_schema_ok = False
            continue

        for skey, sdef in specs_dict.items():
            if not isinstance(sdef, dict):
                errors.append(f"[{sub_cat}.{skey}] 规格定义必须为字典")
                registry_schema_ok = False
                continue
            stype = sdef.get("type")
            sop = sdef.get("op")
            if sop not in ALLOWED_OPS:
                errors.append(f"[{sub_cat}.{skey}] 非法的默认比较符 op={sop!r}")
                registry_schema_ok = False
            expected_default_op = CANONICAL_SPEC_OPS.get((sub_cat, skey))
            if expected_default_op and sop != expected_default_op:
                errors.append(
                    f"[{sub_cat}.{skey}] 注册表默认比较方向漂移: 期望 {expected_default_op!r}，实际 {sop!r}"
                )
                registry_schema_ok = False

            if stype == "number":
                rng = sdef.get("range")
                if (
                    not isinstance(rng, list)
                    or len(rng) != 2
                    or not (is_pure_number(rng[0]) and is_pure_number(rng[1]))
                    or float(rng[1]) <= float(rng[0])
                ):
                    errors.append(f"[{sub_cat}.{skey}.range] 数值规格 range 必须为 [min, max] 且 max > min(F2): {rng!r}")
                    registry_schema_ok = False
                if not isinstance(sdef.get("integer"), bool):
                    errors.append(f"[{sub_cat}.{skey}] 数值规格必须显式声明布尔字段 integer (True/False)")
                    registry_schema_ok = False
            elif stype == "enum":
                vocab = sdef.get("vocabulary")
                if (
                    not isinstance(vocab, list)
                    or not vocab
                    or not all(is_non_empty_str(v) for v in vocab)
                    or len(set(vocab)) != len(vocab)
                ):
                    errors.append(f"[{sub_cat}.{skey}.vocabulary] 枚举 vocabulary 必须为无重复的非空字符串列表(F2)")
                    registry_schema_ok = False
                    continue

                vocab_set = set(vocab)
                zh_map = sdef.get("zh_aliases")
                if not isinstance(zh_map, dict):
                    errors.append(f"[{sub_cat}.{skey}] 枚举规格缺少 zh_aliases 字典")
                    registry_schema_ok = False
                else:
                    zh_keys = set(zh_map.keys())
                    missing_vocab = vocab_set - zh_keys
                    unknown_vocab = zh_keys - vocab_set
                    if missing_vocab:
                        errors.append(
                            f"[{sub_cat}.{skey}] zh_aliases 遗漏了 vocabulary 中的枚举值: {sorted(missing_vocab)}"
                        )
                        registry_schema_ok = False
                    if unknown_vocab:
                        errors.append(
                            f"[{sub_cat}.{skey}] zh_aliases 出现了不在 vocabulary 中的未知目标: {sorted(unknown_vocab)}"
                        )
                        registry_schema_ok = False

                    seen_enum_aliases: Dict[str, str] = {}
                    for enum_val, e_aliases in zh_map.items():
                        if (
                            not isinstance(e_aliases, list)
                            or not e_aliases
                            or not all(is_non_empty_str(ea) for ea in e_aliases)
                        ):
                            errors.append(f"[{sub_cat}.{skey}.{enum_val}] zh_aliases 必须为非空字符串列表")
                            registry_schema_ok = False
                            continue
                        for ea in e_aliases:
                            norm_ea = normalize_query_text(ea)
                            if norm_ea in FORBIDDEN_OVERSTRONG_ALIASES:
                                errors.append(
                                    f"[{sub_cat}.{skey}.{enum_val}] 包含禁止强行硬映射的非等价词 '{ea}'（应移入 ambiguous_terms）"
                                )
                                registry_schema_ok = False
                            if norm_ea in seen_enum_aliases and seen_enum_aliases[norm_ea] != enum_val:
                                errors.append(
                                    f"枚举 [{sub_cat}.{skey}] 完全相同 alias 冲突: '{ea}' 同时映射到 {seen_enum_aliases[norm_ea]} 与 {enum_val}"
                                )
                                registry_schema_ok = False
                            seen_enum_aliases[norm_ea] = enum_val

                disp_map = sdef.get("display_labels")
                if not isinstance(disp_map, dict) or set(disp_map.keys()) != vocab_set:
                    errors.append(f"[{sub_cat}.{skey}] display_labels 必须完整覆盖全部 vocabulary 枚举值")
                    registry_schema_ok = False
                elif not all(is_non_empty_str(disp_map[k]) for k in vocab):
                    errors.append(f"[{sub_cat}.{skey}] display_labels 的值必须全部为非空字符串")
                    registry_schema_ok = False
                else:
                    for k in vocab:
                        expected_enum_dl = CANONICAL_ENUM_DISPLAY_LABELS.get((sub_cat, skey, k))
                        if expected_enum_dl and disp_map[k] != expected_enum_dl:
                            errors.append(
                                f"[{sub_cat}.{skey}.{k}] display_label 偏离中性基准(S2/A2): 期望 {expected_enum_dl!r}，实际 {disp_map[k]!r}"
                            )
                            registry_schema_ok = False
            else:
                errors.append(f"[{sub_cat}.{skey}] 未知的规格类型声明: {stype!r}")
                registry_schema_ok = False

    # 【S10 / F4 修复】：matching_policy 与 ambiguous_terms 深度 Schema 完整性校验
    mp = spec_registry.get("matching_policy")
    at = spec_registry.get("ambiguous_terms")
    if not isinstance(mp, dict) or not isinstance(at, dict):
        errors.append("spec_registry.json 必须包含 matching_policy 与 ambiguous_terms 策略声明")
        registry_schema_ok = False
    else:
        if mp.get("span_resolution") != "longest_match_first":
            errors.append(
                f"matching_policy.span_resolution 必须为 'longest_match_first'，实际: {mp.get('span_resolution')!r}"
            )
            registry_schema_ok = False
        neg_p = mp.get("negation_prefixes")
        if (
            not isinstance(neg_p, list)
            or len(neg_p) < 5
            or not all(is_non_empty_str(x) and len(x.strip()) >= 2 for x in neg_p)
        ):
            errors.append(
                f"matching_policy.negation_prefixes 必须为不少于 5 项且每项长度 >=2 的双字否定前缀列表(S5/S10)，实际: {neg_p!r}"
            )
            registry_schema_ok = False
        if not is_pure_int(mp.get("negation_window_chars")) or int(mp["negation_window_chars"]) <= 0:
            errors.append("matching_policy.negation_window_chars 必须为正整数")
            registry_schema_ok = False
        if not isinstance(mp.get("boundary_rules"), dict) or len(mp.get("boundary_rules", {})) < 2:
            errors.append("matching_policy.boundary_rules 必须为包含至少 2 项边界规则的非空字典")
            registry_schema_ok = False

        norm_at_keys: Dict[str, str] = {}
        for k in at.keys():
            if not is_non_empty_str(k):
                errors.append(f"ambiguous_terms 包含空或非字符串键(D7/F4): {k!r}")
                registry_schema_ok = False
                continue
            nk = normalize_query_text(k)
            if nk in norm_at_keys:
                errors.append(
                    f"ambiguous_terms 存在大小写或空白规范化后重名的冲突键(D7/F4): {norm_at_keys[nk]!r} 与 {k!r}"
                )
                registry_schema_ok = False
            else:
                norm_at_keys[nk] = k
        missing_at = REQUIRED_AMBIGUOUS_TERMS - set(norm_at_keys.keys())
        if missing_at:
            errors.append(f"ambiguous_terms 缺少必须声明的模糊/复合词条目(S10/F4): {sorted(missing_at)}")
            registry_schema_ok = False
        for raw_k, meta_v in at.items():
            if not isinstance(meta_v, dict):
                errors.append(f"ambiguous_terms[{raw_k!r}] 必须为字典(F4)")
                registry_schema_ok = False
                continue
            act = meta_v.get("action")
            rsn = meta_v.get("reason")
            scp = meta_v.get("scope")
            cands = meta_v.get("candidates")
            if not is_non_empty_str(act) or act not in ALLOWED_AMBIGUOUS_ACTIONS:
                errors.append(
                    f"ambiguous_terms[{raw_k!r}].action 非法(F4): 期望属于 {sorted(ALLOWED_AMBIGUOUS_ACTIONS)}，实际 {act!r}"
                )
                registry_schema_ok = False
            if not is_non_empty_str(rsn):
                errors.append(f"ambiguous_terms[{raw_k!r}].reason 必须为非空字符串(F4)")
                registry_schema_ok = False
            if not is_non_empty_str(scp):
                errors.append(f"ambiguous_terms[{raw_k!r}].scope 必须为非空字符串(F4)")
                registry_schema_ok = False
                valid_cands_pool: set[str] = set()
            elif scp == "sub_category":
                valid_cands_pool = set(sub_categories_meta.keys())
            elif "." in scp:
                sc_part, sk_part = scp.split(".", 1)
                sc_obj = sub_categories_meta.get(sc_part)
                sk_obj = sc_obj.get("specs", {}).get(sk_part) if isinstance(sc_obj, dict) else None
                if not isinstance(sk_obj, dict) or sk_obj.get("type") != "enum" or not isinstance(sk_obj.get("vocabulary"), list):
                    errors.append(
                        f"ambiguous_terms[{raw_k!r}].scope={scp!r} 必须指向已注册的枚举规格 '<sub_category>.<enum_spec>' (F4)"
                    )
                    registry_schema_ok = False
                    valid_cands_pool = set()
                else:
                    valid_cands_pool = set(sk_obj["vocabulary"])
            else:
                errors.append(f"ambiguous_terms[{raw_k!r}].scope={scp!r} 格式非法(F4)")
                registry_schema_ok = False
                valid_cands_pool = set()

            if not isinstance(cands, list) or len(cands) == 0 or not all(is_non_empty_str(c) for c in cands):
                errors.append(f"ambiguous_terms[{raw_k!r}].candidates 必须为非空字符串列表(F4)")
                registry_schema_ok = False
            elif valid_cands_pool:
                unknown_cands = [c for c in cands if c not in valid_cands_pool]
                if unknown_cands:
                    errors.append(
                        f"ambiguous_terms[{raw_k!r}].candidates 包含不在注册表词表/子类目中的非法目标 {unknown_cands} (F4)"
                    )
                    registry_schema_ok = False

            if act == "compound_sub_category":
                prim_sub = meta_v.get("primary_sub_category")
                if not is_non_empty_str(prim_sub) or prim_sub not in sub_categories_meta or (isinstance(cands, list) and prim_sub not in cands):
                    errors.append(
                        f"ambiguous_terms[{raw_k!r}] action='compound_sub_category' 必须声明合法的 primary_sub_category 且属于 candidates(F4)，实际: {prim_sub!r}"
                    )
                    registry_schema_ok = False
            elif act == "soft_preference":
                if not is_non_empty_str(meta_v.get("soft_keyword")):
                    errors.append(
                        f"ambiguous_terms[{raw_k!r}] action='soft_preference' 必须声明非空字符串 soft_keyword(F4)"
                    )
                    registry_schema_ok = False

    if registry_schema_ok:
        passed_checks.append(
            "spec_registry.json 全词表覆盖、中性基准标签(S2/A2)、消歧策略 Schema 保护(S10/F4)、默认比较方向与整数约束声明 100% 通过"
        )

    # ==================== 2. 全局与类目配额校验 ====================
    if len(catalog) != 60:
        errors.append(f"SKU 总数错误: 期望 60，实际 {len(catalog)}")
    else:
        passed_checks.append("全局 SKU 总数 == 60")

    if not all(isinstance(item, dict) for item in catalog):
        errors.append("ecommerce_catalog_60skus.json 中存在非 JSON object 的元素")
        return False, passed_checks, errors

    sku_ids = [item.get("sku_id") for item in catalog]
    if not all(is_non_empty_str(sid) for sid in sku_ids) or len(set(sku_ids)) != 60:
        dup = [k for k, v in Counter(sku_ids).items() if v > 1]
        errors.append(f"sku_id 非法或存在重复: {dup}")
    else:
        passed_checks.append("60 条 sku_id 100% 唯一且格式合法")

    expected_cat_counts = {"3c_digital": 25, "apparel": 20, "beauty": 15}
    actual_cat_counts = Counter(item.get("category") for item in catalog)
    if dict(actual_cat_counts) != expected_cat_counts:
        errors.append(f"一级类目计数不匹配: 期望 {expected_cat_counts}，实际 {dict(actual_cat_counts)}")
    else:
        passed_checks.append("三大一级类目配额精确匹配 (3c_digital=25, apparel=20, beauty=15)")

    actual_sub_counts = Counter(item.get("sub_category") for item in catalog)
    sub_quota_ok = True
    for sub_cat, meta in sub_categories_meta.items():
        if not isinstance(meta, dict) or not is_pure_int(meta.get("count")):
            sub_quota_ok = False
            continue
        expected_cnt = int(meta["count"])
        actual_cnt = actual_sub_counts.get(sub_cat, 0)
        if actual_cnt != expected_cnt:
            errors.append(f"子类目 [{sub_cat}] 数量不匹配: 期望 {expected_cnt}，实际 {actual_cnt}")
            sub_quota_ok = False
    if sub_quota_ok:
        passed_checks.append("17 个子类目 SKU 配额 (25+20+15=60) 100% 匹配")

    # ==================== 3. SKU 字段类型、价格、量纲一致性(S3)、跨枚举无矛盾(S1)、溯源校验(S9)与反污染(S2) ====================
    required_str_fields = (
        "sku_id",
        "category",
        "sub_category",
        "title",
        "brand",
        "visual_description",
        "search_query",
        "retrieval_text",
        "image_path",
    )

    manifest_path = target_dir / "image_manifest.json"
    manifest_map: Dict[str, Dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(raw_manifest, dict):
                items_obj = raw_manifest.get("items", raw_manifest)
                if isinstance(items_obj, dict):
                    manifest_map = {k: v for k, v in items_obj.items() if isinstance(v, dict)}
                elif isinstance(items_obj, list):
                    manifest_map = {
                        x["sku_id"]: x for x in items_obj if isinstance(x, dict) and is_non_empty_str(x.get("sku_id"))
                    }
        except Exception as exc:
            errors.append(f"image_manifest.json 解析失败: {exc}")

    spec_contract_ok = True
    price_contract_ok = True
    fact_isolation_ok = True
    provenance_ok = True

    for sku in catalog:
        sid = sku.get("sku_id") if is_non_empty_str(sku.get("sku_id")) else "<UNKNOWN>"
        for sf in required_str_fields:
            if not is_non_empty_str(sku.get(sf)):
                errors.append(f"[{sid}] 字段 {sf} 必须为非空字符串，实际: {sku.get(sf)!r}")
                spec_contract_ok = False

        hl = sku.get("highlights")
        if not isinstance(hl, list) or len(hl) == 0 or not all(is_non_empty_str(x) for x in hl):
            errors.append(f"[{sid}] highlights 必须为非空字符串列表，实际: {hl!r}")
            spec_contract_ok = False

        # 【S9 修复】：溯源字段 is_synthetic_benchmark / visual_description_source / cos_image_url 强制校验
        if sku.get("is_synthetic_benchmark") is not True:
            errors.append(
                f"[{sid}] is_synthetic_benchmark 必须显式为布尔值 True (S9)，实际: {sku.get('is_synthetic_benchmark')!r}"
            )
            provenance_ok = False

        vds = sku.get("visual_description_source")
        if not is_non_empty_str(vds) or vds not in ALLOWED_VISUAL_DESCRIPTION_SOURCES:
            errors.append(
                f"[{sid}] visual_description_source 必须属于 {sorted(ALLOWED_VISUAL_DESCRIPTION_SOURCES)} (S9)，实际: {vds!r}"
            )
            provenance_ok = False
        elif vds == "calibrated_from_retained_image":
            m_entry = manifest_map.get(sid)
            if (
                not isinstance(m_entry, dict)
                or not is_non_empty_str(m_entry.get("sha256"))
                or not SHA256_HEX_PATTERN.match(str(m_entry.get("sha256")))
                or not is_non_empty_str(m_entry.get("source_url"))
                or m_entry.get("magic_format") not in ("JPEG", "PNG")
            ):
                errors.append(
                    f"[{sid}] visual_description_source 宣称为 'calibrated_from_retained_image'，但 image_manifest.json 中缺少该 SKU 的有效 sha256/source_url/magic_format 证据(S9)"
                )
                provenance_ok = False

        cos_url = sku.get("cos_image_url")
        if cos_url is not None and (not is_non_empty_str(cos_url) or not COS_URL_PATTERN.match(str(cos_url))):
            errors.append(
                f"[{sid}] cos_image_url 必须为 null 或合法的腾讯云 COS 域名图片地址(S9)，实际: {cos_url!r}"
            )
            provenance_ok = False

        # 价格校验
        orig_p = sku.get("original_price")
        final_p = sku.get("final_price")
        if not (is_pure_number(orig_p) and is_pure_number(final_p)):
            errors.append(f"[{sid}] 价格必须为纯数字: original_price={orig_p!r}, final_price={final_p!r}")
            price_contract_ok = False
        elif float(final_p) <= 0 or float(final_p) > float(orig_p):
            errors.append(f"[{sid}] 价格违背 0 < final_price <= original_price: {final_p} vs {orig_p}")
            price_contract_ok = False

        # 静态图片路径契约
        expected_img = f"/static/images/{sid}.jpg"
        if sku.get("image_path") != expected_img:
            errors.append(f"[{sid}] image_path 格式不符: 期望 {expected_img}，实际 {sku.get('image_path')!r}")
            spec_contract_ok = False

        # 子类目与一级类目从属校验
        sub_cat = sku.get("sub_category")
        if not is_non_empty_str(sub_cat) or sub_cat not in sub_categories_meta:
            errors.append(f"[{sid}] 未知的 sub_category: {sub_cat!r}")
            spec_contract_ok = False
            continue

        sub_meta = sub_categories_meta[sub_cat]
        if not isinstance(sub_meta, dict):
            errors.append(f"[{sid}] 子类目 [{sub_cat}] 元数据损坏(F2)")
            spec_contract_ok = False
            continue
        if sku.get("category") != sub_meta.get("category"):
            errors.append(
                f"[{sid}] category({sku.get('category')}) 与 sub_category({sub_cat}) 声明的父类目({sub_meta.get('category')})不一致"
            )
            spec_contract_ok = False

        # specs 严格契约校验（防御 specs=null 或非 dict，以及损坏的 spec_registry 缺 range/vocabulary F2）
        declared_specs = sub_meta.get("specs")
        if not isinstance(declared_specs, dict):
            errors.append(f"[{sid}] 子类目 [{sub_cat}] specs 定义损坏或缺失(F2)")
            spec_contract_ok = False
            continue
        actual_specs = sku.get("specs")
        if not isinstance(actual_specs, dict):
            errors.append(f"[{sid}] specs 必须为 JSON object (dict)，实际为 {type(actual_specs).__name__}")
            spec_contract_ok = False
            continue

        if set(actual_specs.keys()) != set(declared_specs.keys()):
            errors.append(
                f"[{sid}] specs 键集合不匹配: 期望 {set(declared_specs.keys())}，实际 {set(actual_specs.keys())}"
            )
            spec_contract_ok = False
            continue

        sku_specs_valid = True
        for skey, sdef in declared_specs.items():
            if not isinstance(sdef, dict):
                sku_specs_valid = False
                spec_contract_ok = False
                continue
            sval = actual_specs[skey]
            stype = sdef.get("type")
            if stype == "number":
                if not is_pure_number(sval):
                    errors.append(f"[{sid}] 数值规格 {skey}={sval!r} 不是纯数字(禁止字符串/单位/bool/NaN)")
                    spec_contract_ok = False
                    sku_specs_valid = False
                else:
                    if sdef.get("integer") is True and not is_pure_int(sval):
                        errors.append(
                            f"[{sid}] 离散整数规格 {skey}={sval!r} 必须为整数(int)，禁止小数或非整型"
                        )
                        spec_contract_ok = False
                        sku_specs_valid = False
                    rng = sdef.get("range")
                    if (
                        isinstance(rng, list)
                        and len(rng) == 2
                        and is_pure_number(rng[0])
                        and is_pure_number(rng[1])
                    ):
                        r_min, r_max = rng
                        if not (float(r_min) - 1e-9 <= float(sval) <= float(r_max) + 1e-9):
                            errors.append(
                                f"[{sid}] 数值规格 {skey}={sval} 超出 spec_registry 声明范围 [{r_min}, {r_max}]"
                            )
                            spec_contract_ok = False
                            sku_specs_valid = False
                    else:
                        sku_specs_valid = False
                        spec_contract_ok = False
            elif stype == "enum":
                vocab = sdef.get("vocabulary")
                if isinstance(vocab, list):
                    if not isinstance(sval, str) or sval not in vocab:
                        errors.append(f"[{sid}] 枚举规格 {skey}={sval!r} 不在封闭词表 {vocab} 内")
                        spec_contract_ok = False
                        sku_specs_valid = False
                else:
                    sku_specs_valid = False
                    spec_contract_ok = False
            else:
                sku_specs_valid = False
                spec_contract_ok = False

        # 【R2 / S3 / F3 / F1 中文语境量纲一致性、容量正向校验与全数值规格文本同步检查（覆盖 title/highlights/visual_description/search_query）】：
        if (
            sku_specs_valid
            and is_non_empty_str(sku.get("title"))
            and isinstance(sku.get("highlights"), list)
            and is_non_empty_str(sku.get("visual_description"))
            and is_non_empty_str(sku.get("search_query"))
        ):
            text_fields_map = {
                "title": sku["title"],
                "highlights": " ".join(str(x) for x in sku["highlights"]),
                "visual_description": sku["visual_description"],
                "search_query": sku["search_query"],
            }
            # 1) volume_ml 规格商品：禁止在任何字段写 Xg/G/克/kg/千克/公斤，且 title 必须包含等于 volume_ml 的 ml/毫升数字
            if "volume_ml" in actual_specs:
                vol_val = float(actual_specs["volume_ml"])
                title_ml_nums = [float(x) for x in ML_CAPACITY_PATTERN.findall(sku["title"])]
                if not title_ml_nums:
                    errors.append(
                        f"[{sid}] 量纲错配(R2/S3/F3): specs.volume_ml={actual_specs['volume_ml']}，但 title 中未找到合法的 ml/毫升 容量标注"
                    )
                    spec_contract_ok = False
                for fn, ftext in text_fields_map.items():
                    bad_w = WEIGHT_UNIT_IN_TEXT_PATTERN.search(ftext)
                    if bad_w:
                        errors.append(
                            f"[{sid}] 量纲错配(R2/S3/F3): specs.volume_ml={actual_specs['volume_ml']}(ml)，但 {fn} 出现了重量单位 '{bad_w.group(0)}'"
                        )
                        spec_contract_ok = False
                    for m_str in ML_CAPACITY_PATTERN.findall(ftext):
                        if abs(float(m_str) - vol_val) > 1e-6:
                            errors.append(
                                f"[{sid}] 容量数值不一致(R2/S3/F1): specs.volume_ml={actual_specs['volume_ml']}，但 {fn} 标注了 '{m_str}ml'"
                            )
                            spec_contract_ok = False

            # 2) fabric_weight_gm2 规格商品：任何克重在 4 个文本字段中必须带 /㎡ 且数值等于 fabric_weight_gm2，禁止裸 g/克/kg/公斤/千克
            if "fabric_weight_gm2" in actual_specs:
                fw_val = float(actual_specs["fabric_weight_gm2"])
                for fn, ftext in text_fields_map.items():
                    bad_bare_w = WEIGHT_UNIT_IN_TEXT_PATTERN.search(ftext)
                    if bad_bare_w:
                        errors.append(
                            f"[{sid}] 量纲歧义(R2/S3/F3): 面料克重商品在 {fn} 中出现了未带 '/㎡' 的重量标注 '{bad_bare_w.group(0)}'"
                        )
                        spec_contract_ok = False
                    for gm_str in FABRIC_GM2_PATTERN.findall(ftext):
                        if abs(float(gm_str) - fw_val) > 1e-6:
                            errors.append(
                                f"[{sid}] 面料克重数值不一致(R2/S3/F1): specs.fabric_weight_gm2={actual_specs['fabric_weight_gm2']}，但 {fn} 标注了 '{gm_str}g/㎡'"
                            )
                            spec_contract_ok = False

            # 3) 【F1 / D1 / D2 / D3 数值规格与商品文本事实同步检查】：防止修改 conflict_pairs 真源或 specs 后，文案仍残留旧数值造成双值漂移
            numeric_text_rules: List[Tuple[str, re.Pattern[str], str]] = [
                ("screen_size", SCREEN_SIZE_TEXT_PATTERN, "寸"),
                ("power_w", POWER_W_TEXT_PATTERN, "W"),
                ("output_w", POWER_W_TEXT_PATTERN, "W"),
                ("capacity_mah", CAPACITY_MAH_TEXT_PATTERN, "mAh/毫安"),
                ("weight_g", BARE_GRAM_PATTERN, "g"),
                ("waterproof_mm", WATERPROOF_MM_TEXT_PATTERN, "mm"),
                ("spf", SPF_TEXT_PATTERN, "SPF"),
                ("concentration_pct", CONCENTRATION_PCT_TEXT_PATTERN, "%"),
                ("anc_db", ANC_DB_TEXT_PATTERN, "dB"),
            ]
            for n_key, n_pat, n_unit in numeric_text_rules:
                if n_key in actual_specs:
                    exp_num = float(actual_specs[n_key])
                    for fn, ftext in text_fields_map.items():
                        for m_num in n_pat.findall(ftext):
                            if abs(float(m_num) - exp_num) > 1e-6:
                                errors.append(
                                    f"[{sid}] 文本与结构化规格数值漂移(F1): specs.{n_key}={actual_specs[n_key]}，但 {fn} 出现了不一致的数值标注 '{m_num}{n_unit}'"
                                )
                                spec_contract_ok = False
                                fact_isolation_ok = False

            if "capacity_mah" in actual_specs:
                exp_mah = float(actual_specs["capacity_mah"])
                for fn, ftext in text_fields_map.items():
                    for m_wan in CAPACITY_WAN_MAH_TEXT_PATTERN.findall(ftext):
                        if abs(float(m_wan) * 10000.0 - exp_mah) > 1e-3:
                            errors.append(
                                f"[{sid}] 文本与结构化规格数值漂移(D2/F1): specs.capacity_mah={actual_specs['capacity_mah']}，但 {fn} 出现了 '{m_wan}万毫安'"
                            )
                            spec_contract_ok = False
                            fact_isolation_ok = False

            if "weight_g" in actual_specs:
                exp_wg = float(actual_specs["weight_g"])
                for fn, ftext in text_fields_map.items():
                    for m_kg in KG_WEIGHT_PATTERN.findall(ftext):
                        if abs(float(m_kg) * 1000.0 - exp_wg) > 1e-3:
                            errors.append(
                                f"[{sid}] 文本与结构化规格数值漂移(D3/F1): specs.weight_g={actual_specs['weight_g']}g，但 {fn} 出现了不一致的千克重量标注 '{m_kg}kg/公斤' (折合 {float(m_kg)*1000:g}g)"
                            )
                            spec_contract_ok = False
                            fact_isolation_ok = False

            if "ports" in actual_specs:
                exp_ports = int(actual_specs["ports"])
                for fn, ftext in text_fields_map.items():
                    for m_port in PORTS_TEXT_PATTERN.findall(ftext):
                        p_val = int(m_port) if m_port.isdigit() else ZH_PORT_NUM_MAP.get(m_port, -1)
                        if p_val != exp_ports:
                            errors.append(
                                f"[{sid}] 文本与结构化规格数值漂移(F1): specs.ports={exp_ports}，但 {fn} 出现了 '{m_port}口'"
                            )
                            spec_contract_ok = False
                            fact_isolation_ok = False
                    for c_str, a_str in PORT_CA_CODE_PATTERN.findall(ftext):
                        ca_sum = int(c_str) + int(a_str)
                        if ca_sum != exp_ports:
                            errors.append(
                                f"[{sid}] 接口组成编码与 specs.ports 不一致(D1/F1): specs.ports={exp_ports}，但 {fn} 出现了 '{c_str}C{a_str}A' (合计 {ca_sum} 口 != {exp_ports} 口)"
                            )
                            spec_contract_ok = False
                            fact_isolation_ok = False
                    for c_cnt, a_cnt in PORT_TYPEC_USBA_PAIR_PATTERN.findall(ftext):
                        pair_sum = int(c_cnt) + int(a_cnt)
                        if pair_sum != exp_ports:
                            errors.append(
                                f"[{sid}] 视觉/文本 Type-C 与 USB-A 接口数量之和与 specs.ports 不一致(D1/F1): specs.ports={exp_ports}，但 {fn} 描述了 {c_cnt}个Type-C + {a_cnt}个USB-A (合计 {pair_sum} 口 != {exp_ports} 口)"
                            )
                            spec_contract_ok = False
                            fact_isolation_ok = False

            # 【S1 / D5 跨枚举别名矛盾扫描（全局最长优先匹配原则）】：
            # 收集当前维度下所有枚举值的全部别名与标签，按长度降序全局决胜；
            # 绝不允许当前值的短别名（如 "段落轴"、"PA+++"）抢占区间从而遮掩更长的其他枚举别名（如 "有声段落轴"、"PA++++"）。
            for skey, sdef in declared_specs.items():
                if sdef.get("type") != "enum" or not isinstance(sdef.get("vocabulary"), list):
                    continue
                actual_eval = str(actual_specs[skey])
                all_enum_candidates: List[Tuple[str, str]] = []
                for eval_item in sdef.get("vocabulary", []):
                    if not is_non_empty_str(eval_item):
                        continue
                    eval_str = str(eval_item)
                    raw_terms = list(sdef.get("zh_aliases", {}).get(eval_str, [])) + [
                        sdef.get("display_labels", {}).get(eval_str, "")
                    ]
                    if eval_str == actual_eval:
                        raw_terms.append(eval_str)
                    for rt in raw_terms:
                        if is_non_empty_str(rt):
                            nt = normalize_query_text(rt)
                            if nt:
                                all_enum_candidates.append((nt, eval_str))

                for fn, ftext in text_fields_map.items():
                    norm_ft = normalize_query_text(ftext)
                    raw_hits: List[Tuple[int, int, int, str, str]] = []
                    for term_norm, eval_str in all_enum_candidates:
                        for m in re.finditer(re.escape(term_norm), norm_ft):
                            si, ei = m.start(), m.end()
                            if term_norm.startswith("pa+") and ei < len(norm_ft) and norm_ft[ei] == "+":
                                continue
                            if term_norm[0].isdigit() and si > 0 and norm_ft[si - 1].isdigit():
                                continue
                            raw_hits.append((si, ei, ei - si, eval_str, term_norm))

                    # 按 (长度降序, 起始位置升序, 非当前枚举优先) 排序
                    raw_hits.sort(
                        key=lambda item: (-item[2], item[0], 0 if item[3] != actual_eval else 1)
                    )
                    accepted_spans: List[Tuple[int, int, int, str, str]] = []
                    for si, ei, slen, eval_str, term_norm in raw_hits:
                        if any(
                            not (ei <= asi or si >= aei) and alen > slen
                            for asi, aei, alen, _, _ in accepted_spans
                        ):
                            continue
                        accepted_spans.append((si, ei, slen, eval_str, term_norm))
                        if eval_str != actual_eval:
                            errors.append(
                                f"[{sid}] 违反 S1 跨枚举属性一致性(D5)：specs.{skey}='{actual_eval}'，但 {fn} 出现了其他枚举值 '{eval_str}' 的别名 '{term_norm}'"
                            )
                            fact_isolation_ok = False

        # 【R1 / S2 非自引用反污染与 retrieval_text 一致性检查】：
        if (
            sku_specs_valid
            and is_non_empty_str(sku.get("title"))
            and is_non_empty_str(sku.get("brand"))
            and isinstance(sku.get("highlights"), list)
            and is_non_empty_str(sku.get("visual_description"))
            and is_non_empty_str(sku.get("retrieval_text"))
            and is_pure_number(orig_p)
            and is_pure_number(final_p)
        ):
            expected_rt = build_expected_retrieval_text(sku, sub_meta)
            if sku["retrieval_text"] != expected_rt:
                errors.append(
                    f"[{sid}] retrieval_text 与结构化事实/中性标签不一致（可能混入了查询 alias 或未同步重生成）"
                )
                fact_isolation_ok = False

            # 【S2 消除自引用】：own_facts_blob 严格只取商品自身文案 (title + highlights + visual_description)，
            # 绝不拼入 sub_meta['display_label'] 或 sdef['display_labels']
            own_facts_blob = " ".join(
                [
                    sku["title"],
                    " ".join(sku["highlights"]),
                    sku["visual_description"],
                ]
            )
            canonical_sub_dl = CANONICAL_SUB_DISPLAY_LABELS.get(sub_cat, "")
            for skey, sdef in declared_specs.items():
                if sdef.get("type") == "enum":
                    sval = str(actual_specs[skey])
                    canonical_enum_dl = CANONICAL_ENUM_DISPLAY_LABELS.get((sub_cat, skey, sval), "")
                    for alias in sdef.get("zh_aliases", {}).get(sval, []):
                        if (
                            alias not in own_facts_blob
                            and alias not in canonical_enum_dl
                            and alias in sku["retrieval_text"]
                        ):
                            errors.append(
                                f"[{sid}] 违反 R1/S2 事实隔离：未在商品自身事实中声明的枚举查询别名 '{alias}' 被注入了 retrieval_text"
                            )
                            fact_isolation_ok = False

            for sub_alias in sub_meta.get("zh_aliases", []):
                if (
                    sub_alias not in own_facts_blob
                    and sub_alias not in canonical_sub_dl
                    and sub_alias in sku["retrieval_text"]
                ):
                    errors.append(
                        f"[{sid}] 违反 R1/S2 事实隔离：未在商品自身事实中声明的子类目查询别名 '{sub_alias}' 被注入了 retrieval_text"
                    )
                    fact_isolation_ok = False

    if price_contract_ok:
        passed_checks.append("全部 60 条 SKU 满足 0 < final_price <= original_price 纯数值契约")
    if spec_contract_ok:
        passed_checks.append("全部 60 条 SKU 的字段类型、离散整数、数值范围、封闭枚举、物理量纲(S3/F3)与数值文本同步(F1) 100% 通过")
    if fact_isolation_ok:
        passed_checks.append("全部 60 条 SKU 的跨枚举无矛盾(S1)与非自引用 retrieval_text 事实语料隔离(R1/S2) 100% 通过")
    if provenance_ok:
        passed_checks.append("全部 60 条 SKU 的溯源标记(is_synthetic_benchmark/visual_description_source/cos_image_url) 校验(R8/S9/F4) 100% 通过")

    # 若基础 SKU 结构有严重错误，后续冲突与订单引用检查前先保护
    sku_by_id: Dict[str, Dict[str, Any]] = {
        s["sku_id"]: s for s in catalog if isinstance(s, dict) and is_non_empty_str(s.get("sku_id"))
    }

    # ==================== 4. C1~C5 硬约束冲突对与松弛算法自洽性校验 ====================
    pairs = conflict_data.get("pairs")
    expected_pair_ids = ["C1", "C2", "C3", "C4", "C5"]
    if not isinstance(pairs, list) or len(pairs) != 5:
        errors.append(
            f"conflict_pairs.json 冲突对数量应为 5 (C1~C5)，实际为 {len(pairs) if isinstance(pairs, list) else type(pairs)}"
        )
        return False, passed_checks, errors

    actual_pair_ids = [p.get("id") if isinstance(p, dict) else None for p in pairs]
    if actual_pair_ids != expected_pair_ids:
        errors.append(
            f"conflict_pairs.json 冲突对 ID 列表必须严格为 {expected_pair_ids}（禁止重复 ID），实际为 {actual_pair_ids}"
        )
        return False, passed_checks, errors

    for pair in pairs:
        cid = pair["id"]
        sub_cat = pair.get("sub_category")
        if not is_non_empty_str(sub_cat) or sub_cat not in sub_categories_meta:
            errors.append(f"[{cid}] 无效的 sub_category: {sub_cat!r}")
            continue

        sub_cat_obj = sub_categories_meta[sub_cat]
        sub_specs_meta = sub_cat_obj.get("specs") if isinstance(sub_cat_obj, dict) else None
        if not isinstance(sub_specs_meta, dict):
            errors.append(f"[{cid}] 子类目 [{sub_cat}] specs 元数据损坏(F2)")
            continue
        hard_constraints = pair.get("hard_constraints")
        soft_constraints = pair.get("soft_constraints", [])
        if not isinstance(hard_constraints, list) or len(hard_constraints) < 2:
            errors.append(f"[{cid}] hard_constraints 必须为至少包含预算与 1 项规格的列表")
            continue
        if not isinstance(soft_constraints, list) or not all(is_non_empty_str(w) for w in soft_constraints):
            errors.append(f"[{cid}] soft_constraints 必须为非空字符串列表")
            continue

        price_constraints: List[Dict[str, Any]] = []
        spec_constraints: List[Dict[str, Any]] = []
        hc_valid = True
        for c in hard_constraints:
            if not isinstance(c, dict):
                errors.append(f"[{cid}] hard_constraints 元素必须为字典: {c!r}")
                hc_valid = False
                continue
            ckey = c.get("key")
            cop = c.get("op")
            cval = c.get("value")
            if cop not in ALLOWED_OPS:
                errors.append(f"[{cid}] 硬约束 {ckey} 比较符非法: {cop!r}")
                hc_valid = False
                continue
            if ckey == "final_price":
                if cop != "<=" or not is_pure_number(cval) or float(cval) <= 0:
                    errors.append(f"[{cid}] 预算约束必须为 final_price <= 正数，实际: {c!r}")
                    hc_valid = False
                price_constraints.append(c)
            else:
                if ckey not in sub_specs_meta or not isinstance(sub_specs_meta[ckey], dict):
                    errors.append(f"[{cid}] 硬约束规格键 {ckey!r} 不在子类目 {sub_cat} 的 spec_registry 中")
                    hc_valid = False
                    continue
                sdef = sub_specs_meta[ckey]
                if sdef.get("type") == "number":
                    if not is_pure_number(cval):
                        errors.append(f"[{cid}] 数值规格约束 {ckey} 的阈值必须为有限数字: {cval!r}")
                        hc_valid = False
                else:
                    vocab_list = sdef.get("vocabulary")
                    if not isinstance(vocab_list, list) or not isinstance(cval, str) or cval not in vocab_list:
                        errors.append(f"[{cid}] 枚举规格约束 {ckey} 的值不在词表内: {cval!r}")
                        hc_valid = False
                spec_constraints.append(c)

        if not hc_valid or len(price_constraints) != 1 or len(spec_constraints) == 0:
            errors.append(f"[{cid}] 硬约束结构校验失败 (price={len(price_constraints)}, specs={len(spec_constraints)})")
            continue

        expected_expect_keys = {"final_price"} | {sc["key"] for sc in spec_constraints}
        price_c = price_constraints[0]
        max_price = float(price_c["value"])
        sub_skus = [
            s
            for s in catalog
            if isinstance(s, dict) and s.get("sub_category") == sub_cat and isinstance(s.get("specs"), dict)
        ]

        # 断言 4.1: 目录中不存在同时满足全部 hard_constraints 的 SKU
        fully_matched: List[str] = []
        for s in sub_skus:
            p_ok = compare_op(s.get("final_price"), price_c["op"], max_price)
            s_ok = all(
                compare_op(s["specs"].get(sc["key"]), sc["op"], sc["value"])
                for sc in spec_constraints
            )
            if p_ok and s_ok:
                fully_matched.append(s["sku_id"])

        if fully_matched:
            errors.append(
                f"[{cid}] 违反断言1（零交集冲突）：存在同时满足全部硬约束的 SKU {fully_matched}"
            )
        else:
            passed_checks.append(f"[{cid} ({sub_cat})] 断言1通过：全表同时满足全部硬约束的 SKU 数 == 0")

        # 断言 4.2: 校验 path_a 满足全部规格条件且仅违反预算，且在 Path A (AND全部规格 + min(final_price)) 中严格唯一排第 1
        pa_branch = pair.get("path_a")
        pb_branch = pair.get("path_b")
        if not isinstance(pa_branch, dict) or not isinstance(pb_branch, dict):
            errors.append(f"[{cid}] path_a 与 path_b 必须为字典")
            continue

        pa_id = pa_branch.get("sku_id")
        pa_expect = pa_branch.get("expect")
        if not is_non_empty_str(pa_id) or pa_id not in sku_by_id or not isinstance(pa_expect, dict):
            errors.append(f"[{cid}] path_a 定义非法或 SKU 不存在: {pa_branch!r}")
            continue
        if set(pa_expect.keys()) != expected_expect_keys:
            errors.append(
                f"[{cid}] path_a.expect 键集合必须严格等于 {sorted(expected_expect_keys)} (F1)，实际: {sorted(pa_expect.keys())}"
            )
        pa_sku = sku_by_id[pa_id]

        if pa_sku.get("sub_category") != sub_cat:
            errors.append(f"[{cid}] path_a ({pa_id}) 的 sub_category 不匹配: {pa_sku.get('sub_category')} != {sub_cat}")

        for ek, ev in pa_expect.items():
            actual_v = pa_sku.get("final_price") if ek == "final_price" else pa_sku.get("specs", {}).get(ek)
            if not compare_op(actual_v, "==", ev):
                errors.append(f"[{cid}] path_a ({pa_id}) 字段 {ek} 期望 {ev}，实际 {actual_v}")

        pa_specs_ok = all(
            compare_op(pa_sku.get("specs", {}).get(sc["key"]), sc["op"], sc["value"])
            for sc in spec_constraints
        )
        pa_price_violated = not compare_op(pa_sku.get("final_price"), price_c["op"], max_price)
        if not (pa_specs_ok and pa_price_violated):
            errors.append(
                f"[{cid}] 违反断言2：path_a ({pa_id}) 必须满足全部规格({pa_specs_ok})且仅违反预算({pa_price_violated})"
            )

        path_a_candidates = [
            s
            for s in sub_skus
            if is_pure_number(s.get("final_price"))
            and all(compare_op(s["specs"].get(sc["key"]), sc["op"], sc["value"]) for sc in spec_constraints)
        ]
        path_a_candidates.sort(key=lambda x: (float(x["final_price"]), x["sku_id"]))
        if not path_a_candidates:
            errors.append(f"[{cid}] Path A 松弛算法候选集为空，期望 {pa_id}")
        elif (
            len(path_a_candidates) >= 2
            and abs(float(path_a_candidates[0]["final_price"]) - float(path_a_candidates[1]["final_price"])) <= 1e-9
        ):
            errors.append(
                f"[{cid}] Path A 最低价存在并列歧义: {path_a_candidates[0]['sku_id']} 与 {path_a_candidates[1]['sku_id']}"
            )
        elif path_a_candidates[0]["sku_id"] != pa_id:
            top_a = path_a_candidates[0]["sku_id"]
            errors.append(f"[{cid}] Path A 松弛算法选出第1名为 {top_a}，与期望的 {pa_id} 不一致")
        else:
            passed_checks.append(
                f"[{cid} ({sub_cat})] 断言2通过：path_a={pa_id} (¥{pa_sku['final_price']}) 满足全部规格且为 Path A 严格唯一最低价解"
            )

        # 断言 4.3: 校验 path_b 满足预算且至少违反一个规格条件，且在 Path B 数值归一化接近度排序中严格唯一排第 1
        pb_id = pb_branch.get("sku_id")
        pb_expect = pb_branch.get("expect")
        if not is_non_empty_str(pb_id) or pb_id not in sku_by_id or not isinstance(pb_expect, dict):
            errors.append(f"[{cid}] path_b 定义非法或 SKU 不存在: {pb_branch!r}")
            continue
        if set(pb_expect.keys()) != expected_expect_keys:
            errors.append(
                f"[{cid}] path_b.expect 键集合必须严格等于 {sorted(expected_expect_keys)} (F1)，实际: {sorted(pb_expect.keys())}"
            )
        if pb_id == pa_id:
            errors.append(f"[{cid}] path_a 与 path_b 不能指向同一个 SKU: {pa_id}")
        pb_sku = sku_by_id[pb_id]

        if pb_sku.get("sub_category") != sub_cat:
            errors.append(f"[{cid}] path_b ({pb_id}) 的 sub_category 不匹配: {pb_sku.get('sub_category')} != {sub_cat}")

        for ek, ev in pb_expect.items():
            actual_v = pb_sku.get("final_price") if ek == "final_price" else pb_sku.get("specs", {}).get(ek)
            if not compare_op(actual_v, "==", ev):
                errors.append(f"[{cid}] path_b ({pb_id}) 字段 {ek} 期望 {ev}，实际 {actual_v}")

        pb_price_ok = compare_op(pb_sku.get("final_price"), price_c["op"], max_price)
        violated_specs = [
            sc["key"]
            for sc in spec_constraints
            if not compare_op(pb_sku.get("specs", {}).get(sc["key"]), sc["op"], sc["value"])
        ]
        if not (pb_price_ok and len(violated_specs) >= 1):
            errors.append(
                f"[{cid}] 违反断言3：path_b ({pb_id}) 必须满足预算({pb_price_ok})且至少违反1项规格(实际违反:{violated_specs})"
            )

        path_b_candidates = [
            s for s in sub_skus if compare_op(s.get("final_price"), price_c["op"], max_price)
        ]
        try:
            path_b_scored = [
                (compute_numeric_closeness_score(s, spec_constraints, sub_specs_meta), s)
                for s in path_b_candidates
            ]
        except Exception as exc:
            errors.append(f"[{cid}] Path B 接近度计算异常(D8/F2): {exc}")
            continue

        path_b_scored.sort(key=lambda item: (-item[0], float(item[1]["final_price"]), item[1]["sku_id"]))

        if not path_b_scored:
            errors.append(f"[{cid}] Path B 松弛算法候选集为空，期望 {pb_id}")
        elif len(path_b_scored) >= 2 and abs(path_b_scored[0][0] - path_b_scored[1][0]) <= 1e-9:
            errors.append(
                f"[{cid}] Path B 接近度得分存在并列歧义: {path_b_scored[0][1]['sku_id']} 与 {path_b_scored[1][1]['sku_id']}"
            )
        elif path_b_scored[0][1]["sku_id"] != pb_id:
            top_b = path_b_scored[0][1]["sku_id"]
            errors.append(f"[{cid}] Path B 松弛算法选出第1名为 {top_b}，与期望的 {pb_id} 不一致")
        else:
            top_score = path_b_scored[0][0]
            passed_checks.append(
                f"[{cid} ({sub_cat})] 断言3通过：path_b={pb_id} (¥{pb_sku['final_price']}, Closeness={top_score:.3f}, 松弛维度={violated_specs}) 为 Path B 严格唯一最优解"
            )

        # 软约束词覆盖检查
        for sc_word in soft_constraints:
            if not is_non_empty_str(pa_sku.get("retrieval_text")) or sc_word not in pa_sku["retrieval_text"]:
                errors.append(f"[{cid}] path_a ({pa_id}) 的 retrieval_text 未覆盖软约束词 '{sc_word}'")

    # ==================== 5. 平台规则 (platform_rules.json) 严格结构、无重复覆盖(S6/A1)、机器参数与正文绑定(S4/S7) ====================
    if len(rules) < 3:
        errors.append(f"platform_rules.json 至少应包含 3 条核心规则，实际为 {len(rules)}")

    rules_ok = True
    seen_rule_keys: set[Tuple[str, str]] = set()
    valid_rule_versions: set[str] = set()
    rules_by_type: Dict[str, Dict[str, Any]] = {}
    rules_by_id: Dict[str, Dict[str, Any]] = {}

    for idx, r in enumerate(rules):
        if not isinstance(r, dict):
            errors.append(f"platform_rules[{idx}] 必须为字典")
            rules_ok = False
            continue
        rid = r.get("rule_id")
        rtype = r.get("rule_type")
        rver = r.get("rule_version")
        rtitle = r.get("title")
        max_days = r.get("max_days")
        win_hours = r.get("window_hours")
        anchor = r.get("anchor_field")
        fb_anchor = r.get("fallback_anchor_field")
        inc_b = r.get("include_boundary")
        allow_u = r.get("allow_unsigned")
        req_unopen = r.get("require_unopened")
        content = r.get("content")
        src_ref = r.get("source_reference")

        if not (
            is_non_empty_str(rid)
            and is_non_empty_str(rtype)
            and is_non_empty_str(rver)
            and is_non_empty_str(rtitle)
            and is_non_empty_str(content)
        ):
            errors.append(f"platform_rules[{idx}] 缺少必填非空字符串字段 (rule_id/rule_type/rule_version/title/content)")
            rules_ok = False
            continue

        if (rid, rver) in seen_rule_keys:
            errors.append(f"platform_rules 存在重复的 (rule_id, rule_version): ({rid}, {rver})")
            rules_ok = False
        seen_rule_keys.add((rid, rver))
        valid_rule_versions.add(rver)

        # 【S6 / A1 修复】：禁止同一 rule_type 出现多条规则静默覆盖
        if rtype in rules_by_type:
            prev_rid = rules_by_type[rtype].get("rule_id")
            errors.append(
                f"platform_rules 存在重复的 rule_type={rtype!r} (S6/A1): {prev_rid!r} 与 {rid!r} 冲突（禁止同类型多条规则静默覆盖）"
            )
            rules_ok = False
        else:
            rules_by_type[rtype] = r
        rules_by_id[rid] = r

        if not is_pure_int(max_days) or int(max_days) <= 0:
            errors.append(f"[{rid}] max_days 必须为正整数，实际: {max_days!r}")
            rules_ok = False
        if not is_pure_int(win_hours) or (is_pure_int(max_days) and int(win_hours) != int(max_days) * 24):
            errors.append(f"[{rid}] window_hours({win_hours!r}) 必须等于 max_days({max_days!r}) * 24")
            rules_ok = False
        if anchor != "signed_at":
            errors.append(f"[{rid}] anchor_field 必须为 'signed_at'，实际: {anchor!r}")
            rules_ok = False
        if not (isinstance(inc_b, bool) and isinstance(allow_u, bool) and isinstance(req_unopen, bool)):
            errors.append(f"[{rid}] include_boundary / allow_unsigned / require_unopened 必须全部为布尔值")
            rules_ok = False
        else:
            # 【S4 修复】：当 fallback_anchor_field 非空时，必须为订单中真实存在的时间锚点字段
            if fb_anchor is not None and fb_anchor not in ("paid_at", "created_at"):
                errors.append(
                    f"[{rid}] fallback_anchor_field 必须为 null 或合法订单时间字段 ('paid_at' / 'created_at') (S4)，实际: {fb_anchor!r}"
                )
                rules_ok = False

        # 【S7 修复】：规则标题/正文必须真实包含 max_days 与 window_hours 对应数字，防止参数与文本脱钩
        if is_pure_int(max_days) and is_pure_int(win_hours):
            combined_rule_text = f"{rtitle} {content}"
            if not any(
                tok in combined_rule_text for tok in (f"{max_days}天", f"{max_days} 天", f"{max_days}日", f"{max_days} 日")
            ):
                errors.append(
                    f"[{rid}] 机器参数 max_days={max_days} 与规则文本脱钩(S7)：标题与正文均未出现 '{max_days}天'"
                )
                rules_ok = False
            if f"{win_hours} 小时" not in content and f"{win_hours}小时" not in content:
                errors.append(
                    f"[{rid}] 机器参数 window_hours={win_hours} 与规则正文 content 脱钩(S7)：正文未出现 '{win_hours} 小时'"
                )
                rules_ok = False

        # 【R6(a) 防口径冲突】：规则正文中严禁出现“次日零时”与滚动小时混用的歧义表达
        if "次日零时" in content:
            errors.append(f"[{rid}] 规则正文包含 '次日零时'，与滚动 {win_hours} 小时契约矛盾")
            rules_ok = False

        if not isinstance(src_ref, dict) or not all(
            is_non_empty_str(src_ref.get(k))
            for k in ("platform", "source_title", "source_url", "retrieved_at", "mock_simplification_note")
        ):
            errors.append(f"[{rid}] 缺少完整的 source_reference 来源与简化边界说明")
            rules_ok = False

    required_rule_types = {"aftersales", "price_guarantee", "shipping"}
    if set(rules_by_type.keys()) != required_rule_types:
        errors.append(
            f"platform_rules.json rule_type 集合必须为 {required_rule_types}，实际为 {set(rules_by_type.keys())}"
        )
        rules_ok = False
    else:
        # 【S7 修复】：shipping 规则必须显式声明 depends_on_rule_id 指向 aftersales 规则且时间窗口一致
        ship_rule = rules_by_type["shipping"]
        af_rule = rules_by_type["aftersales"]
        dep_rid = ship_rule.get("depends_on_rule_id")
        if dep_rid != af_rule.get("rule_id"):
            errors.append(
                f"[{ship_rule.get('rule_id')}] shipping 规则必须声明 depends_on_rule_id={af_rule.get('rule_id')!r} (S7)，实际: {dep_rid!r}"
            )
            rules_ok = False
        for shared_k in ("max_days", "window_hours", "anchor_field"):
            if ship_rule.get(shared_k) != af_rule.get(shared_k):
                errors.append(
                    f"[{ship_rule.get('rule_id')}] shipping 规则字段 {shared_k}={ship_rule.get(shared_k)!r} 与所依赖的 [{af_rule.get('rule_id')}] ({af_rule.get(shared_k)!r}) 不一致(S7)"
                )
                rules_ok = False

    if rules_ok and len(rules) >= 3:
        passed_checks.append(
            f"platform_rules.json ({len(rules)} 条规则) 结构、无重复覆盖(S6/A1)、机器参数与正文绑定(S4/S7)、168h/360h 滚动时间契约与来源说明 100% 通过"
        )

    # ==================== 6. 测试订单 (mock_orders.json) 引用、时间顺序(含 refunded S8/F2)与售后核验真值(S4/S11)校验 ====================
    if len(orders) < len(REQUIRED_SEED_ORDER_IDS):
        errors.append(
            f"mock_orders.json 订单数量不足: 期望至少 {len(REQUIRED_SEED_ORDER_IDS)} 条，实际 {len(orders)}"
        )

    orders_ok = True
    seen_order_ids: set[str] = set()
    can_eval_eligibility = (
        "aftersales" in rules_by_type
        and "price_guarantee" in rules_by_type
        and is_pure_number(rules_by_type["aftersales"].get("window_hours"))
        and is_pure_number(rules_by_type["price_guarantee"].get("window_hours"))
    )

    for idx, od in enumerate(orders):
        if not isinstance(od, dict):
            errors.append(f"mock_orders[{idx}] 必须为字典")
            orders_ok = False
            continue

        oid = od.get("order_id")
        if not is_non_empty_str(oid):
            errors.append(f"mock_orders[{idx}] 缺少有效的 order_id")
            orders_ok = False
            continue
        if oid in seen_order_ids:
            errors.append(f"mock_orders 存在重复的 order_id: {oid}")
            orders_ok = False
        seen_order_ids.add(oid)

        osid = od.get("sku_id")
        if not is_non_empty_str(osid) or osid not in sku_by_id:
            errors.append(f"订单 [{oid}] 外键关联的 SKU {osid!r} 不存在")
            orders_ok = False
            continue
        target_sku = sku_by_id[osid]

        if od.get("sku_title") != target_sku.get("title"):
            errors.append(
                f"订单 [{oid}] sku_title({od.get('sku_title')!r}) 与商品当前 title({target_sku.get('title')!r}) 不一致"
            )
            orders_ok = False

        # 金额校验（拦截 NaN / Inf / bool / 负数，且历史实付金额满足 final_price <= pay_amount <= original_price）
        pay_amt = od.get("pay_amount")
        if not is_pure_number(pay_amt) or float(pay_amt) <= 0:
            errors.append(f"订单 [{oid}] pay_amount={pay_amt!r} 必须为有限正数")
            orders_ok = False
        elif is_pure_number(target_sku.get("final_price")) and is_pure_number(target_sku.get("original_price")):
            sku_fp = float(target_sku["final_price"])
            sku_op = float(target_sku["original_price"])
            if not (sku_fp - 1e-6 <= float(pay_amt) <= sku_op + 1e-6):
                errors.append(
                    f"订单 [{oid}] pay_amount({pay_amt}) 必须在商品 [final_price({sku_fp}), original_price({sku_op})] 范围内"
                )
                orders_ok = False

        # 布尔与枚举状态校验
        if not isinstance(od.get("is_opened"), bool):
            errors.append(f"订单 [{oid}] is_opened={od.get('is_opened')!r} 必须为严格布尔值(bool)")
            orders_ok = False
        if not isinstance(od.get("has_price_guarantee"), bool) or not isinstance(
            od.get("has_freight_insurance"), bool
        ):
            errors.append(f"订单 [{oid}] has_price_guarantee 与 has_freight_insurance 必须为严格布尔值(bool)")
            orders_ok = False

        ostatus = od.get("order_status")
        if ostatus not in ALLOWED_ORDER_STATUSES:
            errors.append(f"订单 [{oid}] order_status={ostatus!r} 非法")
            orders_ok = False

        rver = od.get("rule_version")
        if not is_non_empty_str(rver) or rver not in valid_rule_versions:
            errors.append(f"订单 [{oid}] 引用了未知的 rule_version={rver!r}")
            orders_ok = False

        # 时间契约与顺序校验（【F2 修复】：强制要求显式 UTC 时区偏移，拒绝无时区时间，绝不抛未处理 TypeError）
        ref_str = od.get("reference_time")
        c_off = od.get("created_offset_hours")
        p_off = od.get("paid_offset_hours")
        s_off = od.get("signed_offset_hours")
        c_at = od.get("created_at")
        p_at = od.get("paid_at")
        s_at = od.get("signed_at")

        ref_dt = parse_aware_iso_datetime(ref_str)
        if ref_dt is None:
            errors.append(f"订单 [{oid}] reference_time 格式非法或缺少 UTC 时区偏移(F2): {ref_str!r}")
            orders_ok = False
            continue

        if not (is_pure_number(c_off) and is_pure_number(p_off)):
            errors.append(f"订单 [{oid}] created_offset_hours 与 paid_offset_hours 必须为有限数字")
            orders_ok = False
            continue

        if not (float(c_off) <= float(p_off) <= 0.0):
            errors.append(
                f"订单 [{oid}] 违反时间先后顺序 created_offset({c_off}) <= paid_offset({p_off}) <= 0"
            )
            orders_ok = False

        if parse_aware_iso_datetime(c_at) is None or parse_aware_iso_datetime(p_at) is None:
            errors.append(f"订单 [{oid}] created_at/paid_at 格式非法或缺少 UTC 时区偏移(F2)")
            orders_ok = False

        expected_c_at = (ref_dt + timedelta(hours=float(c_off))).astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")
        expected_p_at = (ref_dt + timedelta(hours=float(p_off))).astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")
        if c_at != expected_c_at or p_at != expected_p_at:
            errors.append(f"订单 [{oid}] 物化时间戳 created_at/paid_at 与相对偏移不一致")
            orders_ok = False

        if ostatus in ("pending", "shipped"):
            if s_off is not None or s_at is not None:
                errors.append(f"订单 [{oid}] 状态为 {ostatus}，signed_offset_hours 与 signed_at 必须为 null")
                orders_ok = False
        elif ostatus in ("delivered", "refunded"):
            # 【S8 / F2 修复】：delivered 与 refunded 均强制校验 signed_offset_hours 与带时区物化 signed_at
            if not is_pure_number(s_off):
                errors.append(f"订单 [{oid}] 状态为 {ostatus}，signed_offset_hours 必须为有限数字(S8)")
                orders_ok = False
            else:
                if float(s_off) > 0.0:
                    errors.append(f"订单 [{oid}] 存在未来签收时间偏移 signed_offset_hours={s_off} > 0")
                    orders_ok = False
                if float(s_off) < float(p_off):
                    errors.append(
                        f"订单 [{oid}] 签收时间 signed_offset({s_off}) 早于付款时间 paid_offset({p_off})"
                    )
                    orders_ok = False
                if parse_aware_iso_datetime(s_at) is None:
                    errors.append(f"订单 [{oid}] signed_at 格式非法或缺少 UTC 时区偏移(F2): {s_at!r}")
                    orders_ok = False
                expected_s_at = (
                    (ref_dt + timedelta(hours=float(s_off))).astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")
                )
                if s_at != expected_s_at:
                    errors.append(f"订单 [{oid}] 物化 signed_at({s_at!r}) 与期望({expected_s_at!r})不一致(S8)")
                    orders_ok = False

        # 校验确定性售后与价保判定真值
        exp_elig = od.get("expected_eligibility")
        if not isinstance(exp_elig, dict):
            errors.append(f"订单 [{oid}] 缺少 expected_eligibility 字典")
            orders_ok = False
        elif can_eval_eligibility and isinstance(od.get("is_opened"), bool) and is_pure_number(pay_amt):
            try:
                actual_elig = evaluate_order_eligibility(od, target_sku, rules_by_type, ref_dt)
                if actual_elig != exp_elig:
                    errors.append(
                        f"订单 [{oid}] 售后/价保核验结果与 expected_eligibility 不匹配(S4): 实际={actual_elig}, 期望={exp_elig}"
                    )
                    orders_ok = False
            except Exception as exc:
                errors.append(f"订单 [{oid}] 售后/价保核验执行异常: {exc}")
                orders_ok = False

    missing_seeds = REQUIRED_SEED_ORDER_IDS - seen_order_ids
    if missing_seeds:
        errors.append(f"mock_orders.json 缺少约定的基准/边界测试订单 ID: {sorted(missing_seeds)}")
        orders_ok = False

    if orders_ok:
        passed_checks.append(
            f"mock_orders.json ({len(orders)} 条基准与边界订单) 外键、有限金额、时间顺序(含 refunded S8)与售后/价保动态规则核验(S4/S11) 100% 匹配"
        )

    return len(errors) == 0, passed_checks, errors


def main() -> int:
    ok, passed, errors = validate_all()
    print("=" * 76)
    print("  60 SKU Catalog & Conflict Pairs Self-Consistency Gate (validate_catalog.py)")
    print("=" * 76)
    for msg in passed:
        print(f"  [PASS] {msg}")
    if errors:
        print("-" * 76)
        for err in errors:
            print(f"  [FAIL] {err}")
        print("=" * 76)
        print(f"RESULT: FAILED ({len(errors)} errors found)")
        return 1

    print("=" * 76)
    print(f"RESULT: ALL {len(passed)} DATA ASSERTIONS PASSED (100% GREEN)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
