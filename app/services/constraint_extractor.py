# -*- coding: utf-8 -*-
"""
确定性硬约束与消歧提取器 (app/services/constraint_extractor.py)
严格对齐 HANDOFF.md §2, §6 与 Claude Sonnet 5.5 审查规范 (R5 / S5 / D6)：
  1. 以 `data/spec_registry.json` 为唯一权威元数据真源，覆盖全部 17 个子类目、数值规格与封闭枚举词表；
  2. 否定意图守卫（D6/S5）：
     - 兼容否定前缀（不要/不需要/并非/不是/不选/排除/不买/别推/拒绝/不考虑）+ 空白字符 + 0~2 个常见单字助词/动词（`[用带选买要想看推配有是个款种的]`）；
     - 准确拦截“不要用红轴”、“不要 红轴”、“不需要带青轴”并将其归入 `excluded_specs`，绝不误提为正向硬过滤条件；
     - 同时保护“非常想要红轴”、“我不是非要红轴的键盘”以及数值上下界比较词（“不超过 200 克”、“不要超过 50 元”）；
  3. 最长匹配优先 (`longest_match_first`) 与边界守卫：
     - `PA++++` 优先消费区间，绝不误触发 `PA+` / `PA++` / `PA+++`；
     - `有声段落轴` 优先消费区间，绝不误触发 `段落轴`；
     - `混油皮` 优先消费区间，绝不误触发 `油皮`；
  4. 模糊词安全降级 (`ambiguous_terms`)：
     - `108键`、`全尺寸`、`全肤质`、`普通肤质`、`敏感肌`、`USB3.2` 按策略执行候选集/软偏好/澄清，绝不强填单一硬过滤枚举；
     - 复合词 `连帽卫衣开衫` 归属 `hoodie`；多子类目同时出现时标记 `sub_category_ambiguity="multi_sub_category"`。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

ZH_NUM_MAP: Dict[str, int] = {
    "单": 1,
    "一": 1,
    "双": 2,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
}

# 比较算子前缀：区分“不要超过200克”（数值上界 <=）与“不要200克”（否定排除）
UPPER_BOUND_PREFIX_RE = re.compile(
    r"(?:不超过|不要超过|不高于|不大于|不多于|最多|至多|最高|低于|小于|以内|以下|轻到|控制在|<=|≤|<)\s*$"
)
LOWER_BOUND_PREFIX_RE = re.compile(
    r"(?:不少于|不低于|不小于|至少|最少|最低|大于|高于|达到|>=|≥|>)\s*$"
)
UPPER_BOUND_SUFFIX_RE = re.compile(r"^\s*(?:以内|以下|之内|内|封顶)")
LOWER_BOUND_SUFFIX_RE = re.compile(r"^\s*(?:以上|起|起步)")


def normalize_query_text(text: str) -> str:
    """按 spec_registry.matching_policy 对输入文本做确定性规范化（NFKC + ASCII 小写）。"""
    return unicodedata.normalize("NFKC", text or "").lower().strip()


def _load_default_spec_registry() -> Dict[str, Any]:
    path: Path = settings.spec_registry_path
    return json.loads(path.read_text(encoding="utf-8"))


_CACHED_SPEC_REGISTRY: Optional[Dict[str, Any]] = None


def get_spec_registry(spec_registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _CACHED_SPEC_REGISTRY
    if spec_registry is not None:
        return spec_registry
    if _CACHED_SPEC_REGISTRY is None:
        _CACHED_SPEC_REGISTRY = _load_default_spec_registry()
    return _CACHED_SPEC_REGISTRY


def build_negation_regex(spec_registry: Dict[str, Any]) -> Optional[re.Pattern]:
    """
    构建 D6/S5 兼容的否定前缀正则：
      支持否定词 + 任意空白 + 0~2 个常见动词/助词 ([用带选买要想看推配有是个款种的])。
      不将“非”放入助词集，从而保护“非常想要红轴”与“不是非要红轴”。
    """
    policy = spec_registry.get("matching_policy", {})
    neg_prefixes = [
        normalize_query_text(p)
        for p in policy.get("negation_prefixes", [])
        if isinstance(p, str) and p.strip()
    ]
    if not neg_prefixes:
        return None
    sorted_prefixes = sorted(neg_prefixes, key=len, reverse=True)
    return re.compile(
        r"(?:"
        + "|".join(re.escape(np) for np in sorted_prefixes)
        + r")\s*[用带选买要想看推配有是个款种的]{0,2}\s*$"
    )


def is_span_negated(
    norm_q: str,
    start_idx: int,
    neg_regex: Optional[re.Pattern],
    neg_win: int = 4,
) -> bool:
    """判断 norm_q[start_idx:] 的匹配实体是否处于否定上下文中（同时排除“不要超过”这类数值比较词）。"""
    if neg_regex is None or start_idx <= 0:
        return False
    prefix_slice = norm_q[max(0, start_idx - max(neg_win + 4, 8)) : start_idx]
    # 若紧邻前缀是“不要超过 / 不超过 / 不高于 / 不少于 / 不低于”，属于数值上下界算子而非否定排除
    if UPPER_BOUND_PREFIX_RE.search(prefix_slice) or LOWER_BOUND_PREFIX_RE.search(prefix_slice):
        return False
    return bool(neg_regex.search(prefix_slice))


def valid_token_boundary(norm_q: str, alias_norm: str, start: int, end: int) -> bool:
    """PA+ 右侧加号守卫与数字词左侧数字守卫。"""
    if alias_norm.startswith("pa+"):
        if end < len(norm_q) and norm_q[end] == "+":
            return False
    if alias_norm and alias_norm[0].isdigit() and start > 0 and norm_q[start - 1].isdigit():
        return False
    return True


def _infer_numeric_op(norm_q: str, start: int, end: int, default_op: str) -> str:
    """根据数值前后的比较词推断算子（<= / >= / ==），未显式声明时采用 spec_registry 默认方向。"""
    before = norm_q[max(0, start - 10) : start]
    after = norm_q[end : min(len(norm_q), end + 6)]
    if UPPER_BOUND_PREFIX_RE.search(before) or UPPER_BOUND_SUFFIX_RE.search(after):
        return "<="
    if LOWER_BOUND_PREFIX_RE.search(before) or LOWER_BOUND_SUFFIX_RE.search(after):
        return ">="
    return default_op


def _cast_spec_number(val: float, is_integer: bool) -> int | float:
    """按 spec_registry 的 integer 标记将数值转为纯 int 或纯 float。"""
    if is_integer or abs(val - round(val)) <= 1e-9:
        return int(round(val))
    return round(float(val), 4)


class ConstraintExtractor:
    """确定性硬约束与消歧策略提取器。"""

    def __init__(self, spec_registry: Optional[Dict[str, Any]] = None) -> None:
        self.spec_registry = get_spec_registry(spec_registry)
        self.neg_regex = build_negation_regex(self.spec_registry)
        self.neg_win = int(self.spec_registry.get("matching_policy", {}).get("negation_window_chars", 4))

    def extract(
        self,
        query: str,
        sub_category_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        norm_q = normalize_query_text(query)
        ambiguous_defs = self.spec_registry.get("ambiguous_terms", {})
        sub_cats_meta = self.spec_registry.get("sub_categories", {})

        # ------------------------------------------------------------------
        # Step 1: 检测 ambiguous_terms（最长优先）
        # ------------------------------------------------------------------
        triggered_ambiguous: Dict[str, Any] = {}
        consumed_ambig_spans: List[Tuple[int, int]] = []
        sorted_ambig = sorted(
            ambiguous_defs.items(),
            key=lambda kv: len(normalize_query_text(kv[0])),
            reverse=True,
        )
        for raw_term, meta in sorted_ambig:
            t_norm = normalize_query_text(raw_term)
            for m in re.finditer(re.escape(t_norm), norm_q):
                s_idx, e_idx = m.start(), m.end()
                if not valid_token_boundary(norm_q, t_norm, s_idx, e_idx):
                    continue
                if is_span_negated(norm_q, s_idx, self.neg_regex, self.neg_win):
                    continue
                if t_norm == "usb3.2" and norm_q[s_idx:].startswith(("usb3.2gen2", "usb3.2 gen2")):
                    continue
                if t_norm == "敏感肌" and s_idx >= 2 and norm_q[s_idx - 2 : e_idx] in ("干敏肌", "油敏肌"):
                    continue
                if t_norm == "敏感肌" and s_idx >= 3 and norm_q[s_idx - 3 : e_idx] == "干性敏感肌":
                    continue
                triggered_ambiguous[raw_term] = meta
                consumed_ambig_spans.append((s_idx, e_idx))

        # ------------------------------------------------------------------
        # Step 2: 子类目匹配（最长优先 + 区间互斥 + 否定排除）
        # ------------------------------------------------------------------
        sub_candidates: List[Tuple[str, str]] = []
        for sc_name, sc_meta in sub_cats_meta.items():
            for a in sc_meta.get("zh_aliases", []):
                sub_candidates.append((normalize_query_text(a), sc_name))
        sub_candidates.sort(key=lambda x: len(x[0]), reverse=True)

        occupied_sub_spans: List[Tuple[int, int]] = []
        matched_subs: List[Tuple[int, str]] = []
        excluded_sub_categories: List[str] = []

        for raw_term, meta in triggered_ambiguous.items():
            if meta.get("action") == "compound_sub_category":
                t_norm = normalize_query_text(raw_term)
                for m in re.finditer(re.escape(t_norm), norm_q):
                    occupied_sub_spans.append((m.start(), m.end()))
                    matched_subs.append((m.start(), meta["primary_sub_category"]))

        for alias_norm, sc_name in sub_candidates:
            for m in re.finditer(re.escape(alias_norm), norm_q):
                s_idx, e_idx = m.start(), m.end()
                if not valid_token_boundary(norm_q, alias_norm, s_idx, e_idx):
                    continue
                if any(not (e_idx <= os or s_idx >= oe) for os, oe in occupied_sub_spans):
                    continue
                if is_span_negated(norm_q, s_idx, self.neg_regex, self.neg_win):
                    if sc_name not in excluded_sub_categories:
                        excluded_sub_categories.append(sc_name)
                    occupied_sub_spans.append((s_idx, e_idx))
                    continue
                occupied_sub_spans.append((s_idx, e_idx))
                matched_subs.append((s_idx, sc_name))

        matched_subs.sort(key=lambda x: x[0])
        unique_subs: List[str] = []
        for _, sc in matched_subs:
            if sc not in unique_subs:
                unique_subs.append(sc)

        # 若未直接命中子类目词，但命中特定独有规格/型号关键词，做安全辅助推断
        if not unique_subs and not sub_category_override:
            inferred = self._infer_sub_category_from_unique_cues(norm_q)
            if inferred:
                unique_subs.append(inferred)

        if sub_category_override:
            target_sub: Optional[str] = sub_category_override
            sub_ambiguity = "single"
        elif len(unique_subs) == 1:
            target_sub = unique_subs[0]
            sub_ambiguity = "single"
        elif len(unique_subs) > 1:
            target_sub = None
            sub_ambiguity = "multi_sub_category"
        else:
            target_sub = None
            sub_ambiguity = "none"

        target_category: Optional[str] = (
            sub_cats_meta[target_sub]["category"]
            if target_sub and target_sub in sub_cats_meta
            else None
        )

        # ------------------------------------------------------------------
        # Step 3: 提取预算上限 (final_price <= max_price)
        # ------------------------------------------------------------------
        max_price: Optional[float] = None
        if "百元内" in norm_q or "百元以内" in norm_q or "一百元以内" in norm_q or "一百块以内" in norm_q:
            max_price = 100.0
        else:
            # 必须携带明确货币单位（元/块/块钱/rmb/¥/￥），或以“预算”开头且右侧不紧跟物理规格单位
            price_patterns = [
                re.compile(
                    r"(?:预算|不超过|不要超过|不高于|低于|以内|最多|控制在|<=|≤)?\s*(?:¥|￥)?\s*(\d+(?:\.\d+)?)\s*(?:元|块|块钱|rmb)(?:\s*(?:以内|以下|之内|内|左右|封顶))?",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"(?:¥|￥)\s*(\d+(?:\.\d+)?)(?!\s*(?:g|kg|克|千克|公斤|mah|毫安|w|瓦|寸|英寸|mm|ml|毫升|db|h|小时|口|键|%))\s*(?:以内|以下|内|左右)?",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"预算\s*(\d+(?:\.\d+)?)(?!\s*(?:g|kg|克|千克|公斤|mah|毫安|w|瓦|寸|英寸|mm|ml|毫升|db|h|小时|口|键|%))",
                    re.IGNORECASE,
                ),
            ]
            for pat in price_patterns:
                for m_price in pat.finditer(norm_q):
                    s_p = m_price.start()
                    if not is_span_negated(norm_q, s_p, self.neg_regex, self.neg_win):
                        max_price = float(m_price.group(1))
                        break
                if max_price is not None:
                    break

        hard_constraints: List[Dict[str, Any]] = []
        if max_price is not None:
            hard_constraints.append(
                {
                    "key": "final_price",
                    "op": "<=",
                    "value": _cast_spec_number(max_price, is_integer=False),
                }
            )

        # ------------------------------------------------------------------
        # Step 4: 提取目标子类目下的数值型规格约束 (Numeric Specs)
        # ------------------------------------------------------------------
        numeric_specs_extracted: Dict[str, Dict[str, Any]] = {}
        excluded_specs: Dict[str, List[Any]] = {}
        candidate_enum_sets: Dict[str, List[str]] = {}
        soft_constraints: List[str] = []

        if target_sub and target_sub in sub_cats_meta:
            sc_def = sub_cats_meta[target_sub]
            specs_meta = sc_def.get("specs", {})

            for skey, sdef in specs_meta.items():
                if sdef.get("type") != "number":
                    continue
                default_op = sdef.get("op", ">=")
                is_int = bool(sdef.get("integer", False))

                pos_hit, neg_hits = self._extract_numeric_spec_all(
                    norm_q=norm_q,
                    spec_key=skey,
                    default_op=default_op,
                    is_integer=is_int,
                )
                if neg_hits:
                    excluded_specs.setdefault(skey, []).extend(
                        [v for v in neg_hits if v not in excluded_specs.get(skey, [])]
                    )
                if pos_hit is not None:
                    val, op_used = pos_hit
                    spec_item = {"key": skey, "op": op_used, "value": val}
                    numeric_specs_extracted[skey] = spec_item
                    hard_constraints.append(spec_item)

            # ------------------------------------------------------------------
            # Step 5: 提取目标子类目下的封闭枚举规格约束 (Enum Specs，带否定守卫与最长优先)
            # ------------------------------------------------------------------
            for skey, sdef in specs_meta.items():
                if sdef.get("type") != "enum":
                    continue
                cand_list: List[Tuple[str, str]] = []
                for eval_name, aliases in sdef.get("zh_aliases", {}).items():
                    for a in aliases:
                        cand_list.append((normalize_query_text(a), eval_name))
                cand_list.sort(key=lambda x: len(x[0]), reverse=True)

                # 先将被 ambiguous_terms 占用的区间放入已消费区间，避免模糊词内部子串触发硬枚举
                occupied_spans: List[Tuple[int, int]] = list(consumed_ambig_spans)
                pos_vals: List[Tuple[int, str]] = []
                neg_vals: List[str] = []

                for alias_norm, eval_name in cand_list:
                    for m in re.finditer(re.escape(alias_norm), norm_q):
                        s_idx, e_idx = m.start(), m.end()
                        if not valid_token_boundary(norm_q, alias_norm, s_idx, e_idx):
                            continue
                        if any(not (e_idx <= os or s_idx >= oe) for os, oe in occupied_spans):
                            continue
                        occupied_spans.append((s_idx, e_idx))
                        if is_span_negated(norm_q, s_idx, self.neg_regex, self.neg_win):
                            if eval_name not in neg_vals:
                                neg_vals.append(eval_name)
                        else:
                            pos_vals.append((s_idx, eval_name))

                if neg_vals:
                    excluded_specs[skey] = neg_vals

                pos_vals.sort(key=lambda x: x[0])
                dedup_pos: List[str] = []
                for _, v in pos_vals:
                    if v not in dedup_pos and v not in neg_vals:
                        dedup_pos.append(v)

                if len(dedup_pos) == 1:
                    hard_constraints.append({"key": skey, "op": "==", "value": dedup_pos[0]})
                elif len(dedup_pos) > 1:
                    # 用户在同一枚举维度提出“A或B”（如“依克多因或者神经酰胺NP”），作为候选枚举集合过滤
                    candidate_enum_sets[skey] = dedup_pos

            # 处理 ambiguous_terms 针对当前子类目的动作
            for raw_term, meta in triggered_ambiguous.items():
                scope = str(meta.get("scope", ""))
                action = meta.get("action")
                if scope.startswith(f"{target_sub}."):
                    skey = scope.split(".", 1)[1]
                    if action == "candidate_set":
                        cands = list(meta.get("candidates", []))
                        if cands and skey not in candidate_enum_sets:
                            candidate_enum_sets[skey] = cands
                    elif action == "soft_preference":
                        skw = meta.get("soft_keyword") or raw_term
                        if skw not in soft_constraints:
                            soft_constraints.append(skw)

            # Step 6: 提取软约束关键词
            for sc_soft in sc_def.get("soft_constraints", []):
                sc_soft_norm = normalize_query_text(sc_soft)
                if sc_soft_norm in norm_q and sc_soft not in soft_constraints:
                    soft_constraints.append(sc_soft)

        # 构建扁平化的 legacy 兼容字典（供 telemetry / decision_meta 展示）
        flat_constraints: Dict[str, Any] = {}
        if target_category:
            flat_constraints["category"] = target_category
        if target_sub:
            flat_constraints["sub_category"] = target_sub
        if max_price is not None:
            flat_constraints["max_price"] = _cast_spec_number(max_price, is_integer=False)
        for hc in hard_constraints:
            if hc["key"] != "final_price":
                flat_constraints[hc["key"]] = hc["value"]
        if excluded_specs:
            flat_constraints["excluded_specs"] = excluded_specs
        if candidate_enum_sets:
            flat_constraints["candidate_enum_sets"] = candidate_enum_sets

        return {
            "category": target_category,
            "sub_category": target_sub,
            "matched_sub_categories": unique_subs,
            "excluded_sub_categories": excluded_sub_categories,
            "sub_category_ambiguity": sub_ambiguity,
            "max_price": _cast_spec_number(max_price, is_integer=False) if max_price is not None else None,
            "hard_constraints": hard_constraints,
            "spec_constraints": [hc for hc in hard_constraints if hc["key"] != "final_price"],
            "excluded_specs": excluded_specs,
            "candidate_enum_sets": candidate_enum_sets,
            "soft_constraints": soft_constraints,
            "ambiguous_hits": triggered_ambiguous,
            "flat_constraints": flat_constraints,
        }

    def _infer_sub_category_from_unique_cues(self, norm_q: str) -> Optional[str]:
        """当用户未直接说出子类目名，但说出了唯一指向某子类目的专有型号/属性时，辅助推断子类目。"""
        cue_map: List[Tuple[re.Pattern, str]] = [
            (re.compile(r"(金粉轴|快银轴|红轴|茶轴|青轴|线性轴|段落轴|87键|98键|104键|gasket)", re.I), "mechanical_keyboard"),
            (re.compile(r"(spf\s*\d+|pa\+{1,4})", re.I), "sunscreen"),
            (re.compile(r"(神经酰胺|依克多因)", re.I), "facial_cream"),
            (re.compile(r"(2c1a|3c1a|1c1a|gan5)", re.I), "gan_charger"),
            (re.compile(r"(\d+\s*万\s*(?:mah|毫安)|\d{4,5}\s*(?:mah|毫安))", re.I), "power_bank"),
            (re.compile(r"(\d{4,5}\s*mm\s*防水|防水指数)", re.I), "windbreaker"),
            (re.compile(r"(主动降噪|\d{2}\s*db\s*降噪)", re.I), "nc_headphones"),
            (re.compile(r"(雷电4|thunderbolt\s*4)", re.I), "usb_hub_cable"),
        ]
        matched: List[str] = []
        for pat, sc in cue_map:
            if pat.search(norm_q) and sc not in matched:
                matched.append(sc)
        return matched[0] if len(matched) == 1 else None

    def _extract_numeric_spec_all(
        self,
        norm_q: str,
        spec_key: str,
        default_op: str,
        is_integer: bool,
    ) -> Tuple[Optional[Tuple[int | float, str]], List[int | float]]:
        """
        提取指定数值规格的正向约束 (value, op) 与否定排除列表 [neg_val, ...]。
        使用 finditer 遍历全部命中项，避免前序否定数值（如“不要30W的，要100W的”）遮蔽后续正向数值。
        """
        pos_hit: Optional[Tuple[int | float, str]] = None
        neg_hits: List[int | float] = []

        def _collect(
            pattern: re.Pattern,
            val_fn: Any,
            use_default_op_only: bool = False,
            occupied: Optional[List[Tuple[int, int]]] = None,
        ) -> None:
            nonlocal pos_hit
            for m in pattern.finditer(norm_q):
                s_i, e_i = m.start(), m.end()
                if occupied is not None and any(not (e_i <= os or s_i >= oe) for os, oe in occupied):
                    continue
                if occupied is not None:
                    occupied.append((s_i, e_i))
                val = val_fn(m)
                if is_span_negated(norm_q, s_i, self.neg_regex, self.neg_win):
                    if val not in neg_hits:
                        neg_hits.append(val)
                elif pos_hit is None:
                    op = default_op if use_default_op_only else _infer_numeric_op(norm_q, s_i, e_i, default_op)
                    pos_hit = (val, op)

        if spec_key == "screen_size":
            _collect(
                re.compile(r"(?<!\d)(\d{2}(?:\.\d+)?)\s*(?:寸|英寸|inch)", re.I),
                lambda m: _cast_spec_number(float(m.group(1)), is_integer=is_integer),
            )

        elif spec_key in ("power_w", "output_w"):
            _collect(
                re.compile(r"(?<!\d)(\d{2,3}(?:\.\d+)?)\s*(?:w|瓦)(?![hH])", re.I),
                lambda m: _cast_spec_number(float(m.group(1)), is_integer=is_integer),
            )

        elif spec_key in ("ports", "port_count"):
            occ: List[Tuple[int, int]] = []
            _collect(
                re.compile(r"(?<!\d)(\d+)\s*c\s*(\d+)\s*a(?![a-z])", re.I),
                lambda m: int(m.group(1)) + int(m.group(2)),
                use_default_op_only=True,
                occupied=occ,
            )
            _collect(
                re.compile(r"(?<!\d)(\d+|[单双两一二三四五六七八])\s*(?:个)?(?:快充|扩展)?(?:接口|口)"),
                lambda m: int(m.group(1)) if m.group(1).isdigit() else ZH_NUM_MAP.get(m.group(1), 1),
                occupied=occ,
            )
            if spec_key == "port_count":
                _collect(
                    re.compile(r"(?<!\d)(\d+)\s*合\s*1"),
                    lambda m: int(m.group(1)),
                    use_default_op_only=True,
                    occupied=occ,
                )

        elif spec_key == "capacity_mah":
            occ = []
            _collect(
                re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*万\s*(?:mah|毫安时|毫安)(?![a-z])", re.I),
                lambda m: _cast_spec_number(float(m.group(1)) * 10000.0, is_integer=True),
                occupied=occ,
            )
            _collect(
                re.compile(r"(?<!\d)(\d{4,5})\s*(?:mah|毫安时|毫安)(?![a-z])", re.I),
                lambda m: int(m.group(1)),
                occupied=occ,
            )

        elif spec_key == "weight_g":
            occ = []
            _collect(
                re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:kg|千克|公斤)(?!\s*/\s*㎡|[a-z])", re.I),
                lambda m: _cast_spec_number(float(m.group(1)) * 1000.0, is_integer=is_integer),
                occupied=occ,
            )
            _collect(
                re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:g|克)(?!\s*/\s*㎡|[a-z])", re.I),
                lambda m: _cast_spec_number(float(m.group(1)), is_integer=is_integer),
                occupied=occ,
            )

        elif spec_key == "fabric_weight_gm2":
            _collect(
                re.compile(r"(?<!\d)(\d{3}(?:\.\d+)?)\s*(?:(?:g|克)\s*/\s*㎡|g|克)", re.I),
                lambda m: _cast_spec_number(float(m.group(1)), is_integer=is_integer),
            )

        elif spec_key == "waterproof_mm":
            _collect(
                re.compile(r"(?<!\d)(\d{3,5})\s*mm", re.I),
                lambda m: int(m.group(1)),
            )

        elif spec_key == "spf":
            _collect(
                re.compile(r"spf\s*(\d{2,3})\+?", re.I),
                lambda m: int(m.group(1)),
            )

        elif spec_key == "volume_ml":
            _collect(
                re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:ml|毫升)(?![a-z])", re.I),
                lambda m: _cast_spec_number(float(m.group(1)), is_integer=is_integer),
            )

        elif spec_key == "anc_db":
            _collect(
                re.compile(r"(?<!\d)(\d{2})\s*db", re.I),
                lambda m: int(m.group(1)),
            )

        elif spec_key == "battery_h":
            _collect(
                re.compile(r"(?<!\d)(\d{2})\s*(?:h|小时)", re.I),
                lambda m: int(m.group(1)),
            )

        elif spec_key == "concentration_pct":
            _collect(
                re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*%"),
                lambda m: _cast_spec_number(float(m.group(1)), is_integer=is_integer),
            )

        return pos_hit, neg_hits


def extract_hard_constraints(
    query: str,
    sub_category_override: Optional[str] = None,
    spec_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """模块级便捷函数：提取结构化硬约束、排除项、模糊词命中与软约束。"""
    extractor = ConstraintExtractor(spec_registry=spec_registry)
    return extractor.extract(query=query, sub_category_override=sub_category_override)
