# -*- coding: utf-8 -*-
"""
三层阶梯断言引擎 (app/core/assertion_engine.py)
面向电商决策型 Shopping Agent 的三层断言验证体系：
  - L1: 结构化事实硬断言 (O(1) 主键点查、价格/规格/状态码零误差对齐)
  - L2: 轨迹与状态机断言 (意图分类准确度、多轮实体继承与约束槽位一致性、死循环拦截、情绪专员跃迁时效)
  - L3: 自然语言语义逆向闭包断言 (文本中商业数值逆向包含核验 S_text ⊆ S_context、违规承诺与内部协议标记零泄露)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class AssertionLevel(str, Enum):
    L1_DETERMINISTIC_STATE = "L1_deterministic_state"
    L2_TRAJECTORY_STATE_MACHINE = "L2_trajectory_state_machine"
    L3_SEMANTIC_REVERSE_GROUNDING = "L3_semantic_reverse_grounding"


@dataclass
class AssertionViolation:
    level: AssertionLevel
    violation_type: str
    message: str
    expected: Any
    actual: Any
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AssertionReport:
    is_valid: bool
    total_assertions_checked: int
    l1_assertions: int = 0
    l2_assertions: int = 0
    l3_assertions: int = 0
    violations: List[AssertionViolation] = field(default_factory=list)

    @property
    def has_hallucination(self) -> bool:
        return any(
            v.violation_type in ("price_hallucination", "ungrounded_numeric_claim", "invalid_status_code")
            for v in self.violations
        )


class ShoppingAgentAssertionEngine:
    """
    工业级购物助手阶梯断言器。
    支持对卡片数据（L1）、编排状态流（L2）和自然语言流式文本（L3）进行逐层严密校验。
    """

    # 违规商业承诺与虚假宣传违禁词库
    FORBIDDEN_PROMISES = [
        "全网最低",
        "保修三年",
        "终身免费换新",
        "假一赔十",
        "内部员工特惠券",
        "点击私聊领优惠",
    ]

    # 越权操作违禁词（Agent 为只读模式，严禁谎称已直接扣费或直接完成退款退货）
    OUT_OF_CAPABILITY_PHRASES = [
        "已为您直接办理退款",
        "款项已退回到您的账户",
        "已为您强制取消商家发货",
        "后台已直接为您修改订单金额",
    ]

    # 严禁泄露的内部协议和系统标记
    INTERNAL_LEAK_PATTERNS = [
        re.compile(r"SYSTEM_PROMPT_DYNAMIC_BOUNDARY", re.IGNORECASE),
        re.compile(r"\[DECISION_META\]", re.IGNORECASE),
        re.compile(r"<system>", re.IGNORECASE),
        re.compile(r"</system>", re.IGNORECASE),
        re.compile(r"API_KEY", re.IGNORECASE),
    ]

    # 通用电商合规与平台政策合法数字（天数、小时数、常用列表序号等）
    STANDARD_POLICY_NUMBERS: Set[float] = {
        1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
        15.0, 30.0, 60.0, 168.0, 360.0, 0.0
    }

    # =========================================================================
    # 辅助工具：递归提取合法数值集合
    # =========================================================================
    @classmethod
    def extract_numbers_recursively(cls, obj: Any) -> Set[float]:
        """
        从任意嵌套的 Python 结构（dict, list, str, float, int）中递归提取所有数值。
        """
        nums: Set[float] = set()
        if isinstance(obj, (int, float)):
            nums.add(round(float(obj), 2))
        elif isinstance(obj, str):
            for m in re.finditer(r"(\d+(?:\.\d+)?)", obj):
                try:
                    nums.add(round(float(m.group(1)), 2))
                except ValueError:
                    pass
        elif isinstance(obj, dict):
            for v in obj.values():
                nums.update(cls.extract_numbers_recursively(v))
        elif isinstance(obj, (list, tuple, set)):
            for item in obj:
                nums.update(cls.extract_numbers_recursively(item))
        return nums

    # =========================================================================
    # L1: 结构化事实硬断言 (O(1) 主键点查)
    # =========================================================================
    @classmethod
    def assert_l1_product_cards(
        cls,
        cards: List[Dict[str, Any]],
        catalog_by_id: Dict[str, Dict[str, Any]],
        price_explain_cards: Optional[List[Dict[str, Any]]] = None,
    ) -> List[AssertionViolation]:
        """
        核验所有返回的商品卡片与价格解释卡片是否与真源数据库 100% 精确一致。
        """
        violations: List[AssertionViolation] = []
        p_map = {
            pc["sku_id"]: pc
            for pc in (price_explain_cards or [])
            if isinstance(pc, dict) and pc.get("sku_id")
        }

        for card in cards:
            sid = card.get("sku_id")
            truth_sku = catalog_by_id.get(sid)
            if not truth_sku:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="sku_not_found",
                        message=f"返回了自营商品库中不存在的 SKU ID: {sid}",
                        expected="Valid SKU in catalog",
                        actual=sid,
                    )
                )
                continue

            # 价格精确断言 (误差 <= 1e-6)
            c_price = float(card.get("final_price", 0.0))
            t_price = float(truth_sku.get("final_price", 0.0))
            if abs(c_price - t_price) > 1e-6:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="price_hallucination",
                        message=f"商品 {sid} 卡片到手价 ¥{c_price} 与数据库真源 ¥{t_price} 不一致",
                        expected=t_price,
                        actual=c_price,
                        details={"sku_id": sid},
                    )
                )

            # 规格严密断言
            c_specs = card.get("specs") or {}
            t_specs = truth_sku.get("specs") or {}
            for k, expected_v in t_specs.items():
                actual_v = c_specs.get(k)
                if actual_v is not None and actual_v != expected_v:
                    violations.append(
                        AssertionViolation(
                            level=AssertionLevel.L1_DETERMINISTIC_STATE,
                            violation_type="spec_hallucination",
                            message=f"商品 {sid} 规格参数 {k}={actual_v} 与真源 {expected_v} 不符",
                            expected=expected_v,
                            actual=actual_v,
                            details={"sku_id": sid, "spec_key": k},
                        )
                    )

            # 价格解释卡片对齐：立减金额 = 原价 - 到手价
            if sid in p_map:
                pc = p_map[sid]
                orig = float(truth_sku.get("original_price", 0.0))
                expected_discount = round(orig - t_price, 2)
                actual_discount = float(
                    pc.get("direct_discount_amount", pc.get("discount_amount", 0.0))
                )
                if abs(actual_discount - expected_discount) > 1e-6:
                    violations.append(
                        AssertionViolation(
                            level=AssertionLevel.L1_DETERMINISTIC_STATE,
                            violation_type="discount_math_error",
                            message=f"商品 {sid} 优惠立减金额 ¥{actual_discount} 与原价降价差额 ¥{expected_discount} 不一致",
                            expected=expected_discount,
                            actual=actual_discount,
                        )
                    )

        return violations

    @classmethod
    def assert_l1_order_and_aftersales(
        cls,
        order_res: Optional[Dict[str, Any]],
        aftersales_res: Optional[Dict[str, Any]],
        expected_spec: Dict[str, Any],
    ) -> List[AssertionViolation]:
        """
        核验订单物流与售后价保核验结果是否与预期状态机一致。
        """
        violations: List[AssertionViolation] = []

        if "expected_order_status" in expected_spec:
            oc = (order_res or {}).get("order_card") or {}
            actual_status = oc.get("order_status")
            exp_status = expected_spec["expected_order_status"]
            if actual_status != exp_status:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="invalid_status_code",
                        message=f"订单状态码不符: 实际={actual_status}, 预期={exp_status}",
                        expected=exp_status,
                        actual=actual_status,
                    )
                )

        if "expected_return_7d_eligible" in expected_spec:
            ac = (aftersales_res or {}).get("aftersales_card") or {}
            actual_r7 = ac.get("return_7d_eligible")
            exp_r7 = expected_spec["expected_return_7d_eligible"]
            if actual_r7 != exp_r7:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="aftersales_eligibility_error",
                        message=f"7天无理由退货资格判定错误: 实际={actual_r7}, 预期={exp_r7}",
                        expected=exp_r7,
                        actual=actual_r7,
                    )
                )

            actual_code = ac.get("return_7d_reason_code")
            exp_code = expected_spec.get("expected_return_7d_reason_code")
            if exp_code and actual_code != exp_code:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="aftersales_code_mismatch",
                        message=f"7天退货原因码不符: 实际={actual_code}, 预期={exp_code}",
                        expected=exp_code,
                        actual=actual_code,
                    )
                )

        if "expected_price_diff_amount" in expected_spec:
            ac = (aftersales_res or {}).get("aftersales_card") or {}
            actual_diff = float(ac.get("price_diff_amount", 0.0))
            exp_diff = float(expected_spec["expected_price_diff_amount"])
            if abs(actual_diff - exp_diff) > 1e-6:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="price_guarantee_calc_error",
                        message=f"15天价保差额核验错误: 实际=¥{actual_diff}, 预期=¥{exp_diff}",
                        expected=exp_diff,
                        actual=actual_diff,
                    )
                )

        return violations

    # =========================================================================
    # L2: 轨迹与状态机断言 (执行链路合规)
    # =========================================================================
    @classmethod
    def assert_l2_trajectory(
        cls,
        jev_decision: Dict[str, Any],
        expected_intent: str,
        handoff_expected: Optional[bool] = None,
        tool_call_count: int = 1,
        max_allowed_tool_calls: int = 3,
    ) -> List[AssertionViolation]:
        """
        核验单轮或多轮决策编排的状态机跃迁合法性。
        """
        violations: List[AssertionViolation] = []

        # 1. 意图分类器定向断言
        actual_intent = jev_decision.get("primary_intent")
        if actual_intent != expected_intent:
            violations.append(
                AssertionViolation(
                    level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                    violation_type="intent_misroute",
                    message=f"状态机意图路由错误: 实际={actual_intent}, 预期={expected_intent}",
                    expected=expected_intent,
                    actual=actual_intent,
                )
            )

        # 2. 人工升级 (Handoff) 护栏断言
        if handoff_expected is not None:
            actual_handoff = bool(jev_decision.get("handoff_guard", False))
            if actual_handoff != handoff_expected:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                        violation_type="handoff_guard_failure",
                        message=f"人工升级触发状态不符: 实际={actual_handoff}, 预期={handoff_expected}",
                        expected=handoff_expected,
                        actual=actual_handoff,
                    )
                )

        # 3. 工具调用步数与死循环熔断
        if tool_call_count > max_allowed_tool_calls:
            violations.append(
                AssertionViolation(
                    level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                    violation_type="infinite_tool_loop",
                    message=f"工具调用步数超标 (步数={tool_call_count} > 上限={max_allowed_tool_calls})",
                    expected=f"<={max_allowed_tool_calls}",
                    actual=tool_call_count,
                )
            )

        return violations

    # =========================================================================
    # L3: 自然语言语义逆向闭包断言 (文本事实 ⊆ 上下文真源)
    # =========================================================================
    @classmethod
    def extract_text_commercial_numbers(cls, text: str) -> List[float]:
        """
        从回复文本中提取核心商业数值（价格金额、规格数字、时效天数/小时）。
        """
        found_nums: List[float] = []

        # 匹配价格：¥18.9, 18.9元, 18.9块
        price_patterns = [
            r"[¥￥]\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:元|块)",
        ]
        for pat in price_patterns:
            for m in re.finditer(pat, text):
                try:
                    found_nums.append(round(float(m.group(1)), 2))
                except ValueError:
                    pass

        # 匹配带有规格单位的数字：如 65W, 20000mAh, 200g, 10000mm, 60ml, 168小时, 360小时
        spec_pattern = r"(\d+(?:\.\d+)?)\s*(?:W|w|瓦|mAh|mah|毫安|g|G|克|mm|毫米|ml|ML|毫升|小时|h|H)"
        for m in re.finditer(spec_pattern, text):
            try:
                found_nums.append(round(float(m.group(1)), 2))
            except ValueError:
                pass

        return found_nums

    @classmethod
    def build_ground_truth_closure(
        cls,
        user_query: str = "",
        product_cards: Optional[List[Dict[str, Any]]] = None,
        price_cards: Optional[List[Dict[str, Any]]] = None,
        order_result: Optional[Dict[str, Any]] = None,
        aftersales_result: Optional[Dict[str, Any]] = None,
        rag_result: Optional[Dict[str, Any]] = None,
    ) -> Set[float]:
        """
        从用户原始输入、商品卡片、价格解释、订单物流、售后价保与 RAG 诊断原因中
        收集全部允许出现的数值白名单闭包 S_context。
        """
        valid_numbers: Set[float] = set(cls.STANDARD_POLICY_NUMBERS)

        if user_query:
            valid_numbers.update(cls.extract_numbers_recursively(user_query))
        if product_cards:
            valid_numbers.update(cls.extract_numbers_recursively(product_cards))
            for c in product_cards:
                if isinstance(c, dict) and "original_price" in c and "final_price" in c:
                    try:
                        disc = round(float(c["original_price"]) - float(c["final_price"]), 2)
                        valid_numbers.add(disc)
                    except (ValueError, TypeError):
                        pass
        if price_cards:
            valid_numbers.update(cls.extract_numbers_recursively(price_cards))
        if order_result:
            valid_numbers.update(cls.extract_numbers_recursively(order_result))
        if aftersales_result:
            valid_numbers.update(cls.extract_numbers_recursively(aftersales_result))
        if rag_result:
            valid_numbers.update(cls.extract_numbers_recursively(rag_result.get("relaxation")))
            valid_numbers.update(cls.extract_numbers_recursively(rag_result.get("relaxation_reason")))

        return valid_numbers

    @classmethod
    def assert_l3_semantic_grounding(
        cls,
        emitted_text: str,
        allowed_truth_numbers: Set[float],
        strict_numeric_closure: bool = True,
    ) -> List[AssertionViolation]:
        """
        L3: 校验 Agent 自然语言输出是否满足真源逆向闭包 (S_text ⊆ S_context) 及合规红线。
        """
        violations: List[AssertionViolation] = []

        # 1. 泄露内部系统协议/标记断言
        for pat in cls.INTERNAL_LEAK_PATTERNS:
            if pat.search(emitted_text):
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L3_SEMANTIC_REVERSE_GROUNDING,
                        violation_type="internal_protocol_leak",
                        message="检测到输出文本泄露了内部系统标记或 API 协议",
                        expected="Clean user-facing markdown text",
                        actual=pat.pattern,
                    )
                )

        # 2. 越权承诺断言 (Out of capability)
        for phrase in cls.OUT_OF_CAPABILITY_PHRASES:
            if phrase in emitted_text:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L3_SEMANTIC_REVERSE_GROUNDING,
                        violation_type="out_of_capability_promise",
                        message=f"Agent 输出了越权的承诺操作: 「{phrase}」",
                        expected="Read-only query and guidance",
                        actual=phrase,
                    )
                )

        # 3. 虚假营销与违禁词红线断言
        for phrase in cls.FORBIDDEN_PROMISES:
            if phrase in emitted_text:
                violations.append(
                    AssertionViolation(
                        level=AssertionLevel.L3_SEMANTIC_REVERSE_GROUNDING,
                        violation_type="forbidden_promotional_claim",
                        message=f"触犯合规红线禁词: 「{phrase}」",
                        expected="Objective and truthful description",
                        actual=phrase,
                    )
                )

        # 4. 数值逆向闭包检查：文本中的每一个商业数字，都必须在上下文闭包白名单中！
        if strict_numeric_closure and allowed_truth_numbers:
            text_numbers = cls.extract_text_commercial_numbers(emitted_text)
            for num in text_numbers:
                # 浮点数比对（容差 1e-2）
                if not any(abs(num - tn) < 1e-2 for tn in allowed_truth_numbers):
                    violations.append(
                        AssertionViolation(
                            level=AssertionLevel.L3_SEMANTIC_REVERSE_GROUNDING,
                            violation_type="ungrounded_numeric_claim",
                            message=f"文本出现无源虚构数值: {num} (不在真源卡片及用户上下文中)",
                            expected=sorted(list(allowed_truth_numbers)),
                            actual=num,
                        )
                    )

        return violations

    # =========================================================================
    # 沙利文 (Sullivan) R1 / P3 / U2 三维评估
    #   R1 硬约束一票否决 / P3 事实溯源 / U2 精细度
    # =========================================================================
    SULLIVAN_TIER_MARKERS: Tuple[str, ...] = ("【首选】", "【备选 1】", "【备选 2】")
    SULLIVAN_COST_MARKER = "【代价与注意事项】"

    # 【条件改变句】判据：必须给出可执行的替代前提，而不是敷衍的“看情况”
    SULLIVAN_CONDITION_PATTERNS: Tuple[re.Pattern, ...] = (
        re.compile(r"只有当.{0,80}?(?:才|方可)"),
        re.compile(r"(?:预算|价格|上限).{0,12}?(?:放宽|放宽到|提升到|提高)"),
        re.compile(r"不再要求"),
        re.compile(r"(?:条件|需求|前提)(?:改为|变更|变成)"),
    )

    # 笼统模糊表述判据（U2 精细度扣分项）
    SULLIVAN_VAGUE_PATTERNS: Tuple[re.Pattern, ...] = (
        re.compile(r"因人而异"),
        re.compile(r"看个人(?:需求|喜好|情况)?"),
        re.compile(r"看您(?:自己|需求|喜好|情况)?"),
        re.compile(r"都(?:差不多|一样|可以|行)"),
        re.compile(r"具体(?:看|视)(?:情况|需求|预算)"),
        re.compile(r"各有(?:优劣|千秋)"),
        re.compile(r"自行(?:判断|选择|斟酌)"),
    )

    SULLIVAN_SKU_ANCHOR_PATTERN = re.compile(r"\[\[SKU:([^\]]+)\]\]")

    @staticmethod
    def _sullivan_compare(actual: Any, op: str, target: Any) -> bool:
        """沙利文约束比较：nullable 规格（None）一律视为“无法证明满足”，按不满足处理。"""
        if actual is None or target is None:
            return False
        if op == "==":
            if isinstance(actual, str) or isinstance(target, str):
                return str(actual) == str(target)
            try:
                return abs(float(actual) - float(target)) < 1e-9
            except (TypeError, ValueError):
                return False
        try:
            if op == "<=":
                return float(actual) <= float(target)
            if op == ">=":
                return float(actual) >= float(target)
        except (TypeError, ValueError):
            return False
        return False

    @classmethod
    def _sullivan_card_satisfies(
        cls,
        card: Dict[str, Any],
        max_price: Optional[float],
        spec_constraints: List[Dict[str, Any]],
    ) -> bool:
        """判定单张商品卡片是否同时满足预算与全部硬规格约束。"""
        if not isinstance(card, dict):
            return False
        if max_price is not None:
            if not cls._sullivan_compare(card.get("final_price"), "<=", max_price):
                return False
        specs = card.get("specs") or {}
        for hc in spec_constraints:
            key = hc.get("key")
            if key not in specs:
                return False
            if not cls._sullivan_compare(specs.get(key), hc.get("op", "=="), hc.get("value")):
                return False
        return True

    def evaluate_sullivan_r1_p3_u2(self, session_trace: Dict[str, Any]) -> Dict[str, Any]:
        """
        沙利文三维评估（R1 硬约束一票否决 / P3 事实溯源 / U2 精细度）。

        `session_trace` 支持以下键（缺省时按上下文降级推断，绝不自造事实）：
          - `emitted_text` / `reply_text` / `response_text`：Agent 最终自然语言回复（取第一个非空）；
          - `user_query`：用户原始问题；
          - `product_cards` / `rag_result.product_cards`：候选商品池（唯一事实来源）；
          - `price_explain_cards` / `rag_result.price_explain_cards`：价格解释卡片；
          - `hard_constraints` / `rag_result.hard_constraints`：硬约束列表（含 `final_price` 预算项）；
          - `max_price`：预算上限（可从 `hard_constraints` 的 `final_price <= X` 项反推）；
          - `feasible_skus`：可选，显式给出当前可行解 SKU 集合（缺省时由卡片 + 硬约束确定性计算）；
          - `recommended_skus`：可选，显式给出推荐 SKU 列表（缺省时从 `[[SKU:xxx]]` 锚点解析）；
          - `order_result` / `aftersales_result`：确定性订单与售后真源；
          - `truth_numbers`：可选，额外允许的数值真源白名单（如人工审计补充的官方口径）。

        返回值：
          - `passed`：三维是否全部达标（R1 否决则整体不通过）；
          - `r1` / `p3` / `u2`：各维度 score、passed、判据与违规明细；
          - `total_score`：R1 通过时为三维均分，R1 否决时恒为 0.0；
          - `violations`：扁平化违规清单，便于落库与归因。
        """
        trace: Dict[str, Any] = session_trace if isinstance(session_trace, dict) else {}
        text = str(
            trace.get("emitted_text")
            or trace.get("reply_text")
            or trace.get("response_text")
            or ""
        )
        user_query = str(trace.get("user_query") or "")
        rag_result = trace.get("rag_result") if isinstance(trace.get("rag_result"), dict) else {}
        cards = list(trace.get("product_cards") or rag_result.get("product_cards") or [])
        price_cards = list(trace.get("price_explain_cards") or rag_result.get("price_explain_cards") or [])
        pool_ids: List[str] = [
            str(c.get("sku_id")) for c in cards if isinstance(c, dict) and c.get("sku_id")
        ]

        # ---------------------------------------------------------------------
        # R1：硬约束一票否决（可行解 > 0 时不得越界推荐；可行解 = 0 时须给出条件改变句）
        # ---------------------------------------------------------------------
        hard_constraints = list(trace.get("hard_constraints") or rag_result.get("hard_constraints") or [])
        max_price = trace.get("max_price")
        if max_price is None:
            for hc in hard_constraints:
                if isinstance(hc, dict) and hc.get("key") == "final_price":
                    max_price = hc.get("value")
                    break
        spec_constraints = [
            hc for hc in hard_constraints
            if isinstance(hc, dict) and hc.get("key") not in (None, "final_price")
        ]

        feasible_source = trace.get("feasible_skus")
        if feasible_source is not None:
            feasible_ids = [str(x) for x in feasible_source]
        else:
            feasible_ids = [
                str(c.get("sku_id"))
                for c in cards
                if isinstance(c, dict) and c.get("sku_id")
                and self._sullivan_card_satisfies(c, max_price, spec_constraints)
            ]

        recommended_ids: List[str] = []
        for sid in trace.get("recommended_skus") or []:
            if str(sid) not in recommended_ids:
                recommended_ids.append(str(sid))
        anchor_ids = self.SULLIVAN_SKU_ANCHOR_PATTERN.findall(text)
        for sid in anchor_ids:
            if str(sid) not in recommended_ids:
                recommended_ids.append(str(sid))
        if not recommended_ids and cards:
            # 无锚点也无显式推荐清单时，仅以“标题在正文中原样出现”的无歧义证据回退识别
            for c in cards:
                if isinstance(c, dict) and c.get("sku_id") and c.get("title") and str(c["title"]) in text:
                    sid = str(c["sku_id"])
                    if sid not in recommended_ids:
                        recommended_ids.append(sid)

        condition_hit = any(p.search(text) for p in self.SULLIVAN_CONDITION_PATTERNS)
        r1_applicable = bool(recommended_ids)
        relaxation_triggered = bool(cards) and not feasible_ids
        r1_violations: List[Dict[str, Any]] = []
        r1_score = 1.0
        r1_passed = True
        exemption_applied = False

        if r1_applicable:
            if feasible_ids:
                violating = [
                    sid for sid in recommended_ids
                    if sid in pool_ids and sid not in feasible_ids
                ]
                if violating:
                    r1_score = 0.0
                    r1_passed = False
                    r1_violations.append(
                        {
                            "rule": "R1",
                            "violation_type": "hard_constraint_breach_despite_feasible_solution",
                            "message": (
                                f"候选池内存在 {len(feasible_ids)} 款满足预算与全部硬约束的可行解，"
                                f"却推荐了违反约束的 {violating}"
                            ),
                            "expected": feasible_ids,
                            "actual": violating,
                        }
                    )
            else:
                relaxation_triggered = True
                if condition_hit:
                    exemption_applied = True
                else:
                    r1_score = 0.0
                    r1_passed = False
                    r1_violations.append(
                        {
                            "rule": "R1",
                            "violation_type": "relaxation_without_condition_sentence",
                            "message": "可行解为 0（触发预算/规格松弛）但未输出【条件改变句】反转说明，不得以免责口径糊弄",
                            "expected": "只有当预算放宽至/不再要求…时才可考虑",
                            "actual": text[:120],
                        }
                    )

        # ---------------------------------------------------------------------
        # P3：事实溯源（文本商业数值 ⊆ 上下文真源闭包；SKU 锚点 ⊆ 候选池）
        # ---------------------------------------------------------------------
        allowed_numbers = self.build_ground_truth_closure(
            user_query=user_query,
            product_cards=cards or None,
            price_cards=price_cards or None,
            order_result=trace.get("order_result"),
            aftersales_result=trace.get("aftersales_result"),
            rag_result=rag_result or None,
        )
        for n in trace.get("truth_numbers") or []:
            try:
                allowed_numbers.add(round(float(n), 2))
            except (TypeError, ValueError):
                continue
        # 约束阈值（预算上限与硬规格门限）本身属于上下文真源，复述它们不算无源虚构
        allowed_numbers.update(self.extract_numbers_recursively(hard_constraints))
        if max_price is not None:
            try:
                allowed_numbers.add(round(float(max_price), 2))
            except (TypeError, ValueError):
                pass

        p3_violations: List[Dict[str, Any]] = []
        for num in self.extract_text_commercial_numbers(text):
            if not any(abs(num - tn) < 1e-2 for tn in allowed_numbers):
                p3_violations.append(
                    {
                        "rule": "P3",
                        "violation_type": "ungrounded_numeric_claim",
                        "message": f"回复引用的数字参数 {num} 不在上下文真源闭包内（疑似虚构或跨源混用）",
                        "expected": sorted(allowed_numbers),
                        "actual": num,
                    }
                )
        for sid in recommended_ids:
            if pool_ids and sid not in pool_ids:
                p3_violations.append(
                    {
                        "rule": "P3",
                        "violation_type": "ungrounded_sku_reference",
                        "message": f"回复引用了候选池之外的 SKU: {sid}",
                        "expected": pool_ids,
                        "actual": sid,
                    }
                )

        p3_score = max(0.0, 1.0 - 0.25 * len(p3_violations))
        p3_passed = not p3_violations

        # ---------------------------------------------------------------------
        # U2：精细度（笼统模糊表述 / 三档结构与代价项缺失，各扣 0.5 分）
        # ---------------------------------------------------------------------
        u2_reasons: List[Dict[str, Any]] = []
        for pat in self.SULLIVAN_VAGUE_PATTERNS:
            m = pat.search(text)
            if m:
                u2_reasons.append(
                    {
                        "rule": "U2",
                        "violation_type": "vague_expression",
                        "message": f"出现笼统模糊表述「{m.group(0)}」，未给出可核验的具体取舍",
                        "expected": "具体规格/价格口径的牺牲项",
                        "actual": m.group(0),
                    }
                )
                break

        tier_count = sum(1 for marker in self.SULLIVAN_TIER_MARKERS if marker in text)
        cost_count = text.count(self.SULLIVAN_COST_MARKER)
        if len(recommended_ids) >= 2 and tier_count == 0:
            u2_reasons.append(
                {
                    "rule": "U2",
                    "violation_type": "missing_tier_structure",
                    "message": "多款推荐未使用【首选】/【备选 1】/【备选 2】三档结构，用户无法判断取舍优先级",
                    "expected": "恰好 3 档结构化推荐",
                    "actual": "无档位标记",
                }
            )
        if tier_count >= 1 and cost_count < tier_count:
            u2_reasons.append(
                {
                    "rule": "U2",
                    "violation_type": "missing_cost_and_caveat",
                    "message": (
                        f"【代价与注意事项】仅出现 {cost_count} 次，少于档位标记 {tier_count} 处，"
                        "存在只讲优点、不做牺牲项披露的推荐"
                    ),
                    "expected": f">= {tier_count} 处单列【代价与注意事项】",
                    "actual": cost_count,
                }
            )
        if tier_count >= 4:
            u2_reasons.append(
                {
                    "rule": "U2",
                    "violation_type": "tier_overflow",
                    "message": f"档位标记出现 {tier_count} 处，超出沙利文恰好 3 款收敛要求",
                    "expected": "<= 3",
                    "actual": tier_count,
                }
            )

        u2_score = max(0.0, 1.0 - 0.5 * len(u2_reasons))
        u2_passed = u2_score >= 0.5 and not u2_reasons

        total_score = (
            round((r1_score + p3_score + u2_score) / 3.0, 4) if r1_passed else 0.0
        )

        all_violations = r1_violations + p3_violations + u2_reasons
        return {
            "rubric": "sullivan_r1_p3_u2",
            "passed": bool(r1_passed and p3_passed and u2_passed),
            "total_score": total_score,
            "r1": {
                "name": "硬约束一票否决",
                "score": r1_score,
                "passed": r1_passed,
                "applicable": r1_applicable,
                "relaxation_triggered": relaxation_triggered,
                "exemption_applied": exemption_applied,
                "condition_sentence_detected": condition_hit,
                "max_price": max_price,
                "feasible_skus": feasible_ids,
                "recommended_skus": recommended_ids,
                "violations": r1_violations,
            },
            "p3": {
                "name": "事实溯源",
                "score": p3_score,
                "passed": p3_passed,
                "grounded_numbers": sorted(allowed_numbers),
                "violations": p3_violations,
            },
            "u2": {
                "name": "精细度",
                "score": u2_score,
                "passed": u2_passed,
                "tier_marker_count": tier_count,
                "cost_caveat_count": cost_count,
                "violations": u2_reasons,
            },
            "violations": all_violations,
        }

    # =========================================================================
    # 全链路综合核验入口
    # =========================================================================
    @classmethod
    def evaluate_full_cycle(
        cls,
        emitted_text: str,
        user_query: str,
        jev_decision: Dict[str, Any],
        expected_spec: Dict[str, Any],
        catalog_by_id: Dict[str, Dict[str, Any]],
        product_cards: Optional[List[Dict[str, Any]]] = None,
        price_explain_cards: Optional[List[Dict[str, Any]]] = None,
        order_result: Optional[Dict[str, Any]] = None,
        aftersales_result: Optional[Dict[str, Any]] = None,
        rag_result: Optional[Dict[str, Any]] = None,
        tool_call_count: int = 1,
        strict_numeric_closure: bool = True,
    ) -> AssertionReport:
        """
        端到端阶梯断言流水线：依次执行 L1、L2、L3 检查。
        """
        all_violations: List[AssertionViolation] = []
        l1_cnt = 0
        l2_cnt = 0
        l3_cnt = 0

        # L1 检查
        if product_cards:
            v1 = cls.assert_l1_product_cards(product_cards, catalog_by_id, price_explain_cards)
            all_violations.extend(v1)
            l1_cnt += len(product_cards) * 2

        if order_result or aftersales_result:
            v1_order = cls.assert_l1_order_and_aftersales(order_result, aftersales_result, expected_spec)
            all_violations.extend(v1_order)
            l1_cnt += 3

        # L2 检查
        is_safety = expected_spec.get("scenario_group") == "safety_and_escalation"
        sub_type = expected_spec.get("sub_type")
        if is_safety and sub_type in ("12315_escalation", "frustration_handoff"):
            expected_intent = jev_decision.get("primary_intent", "aftersales_qa")
        else:
            expected_intent = expected_spec.get("expected_intent", "product_recommend")

        v2 = cls.assert_l2_trajectory(
            jev_decision=jev_decision,
            expected_intent=expected_intent,
            handoff_expected=expected_spec.get("expected_handoff_required"),
            tool_call_count=tool_call_count,
        )
        all_violations.extend(v2)
        l2_cnt += 2

        # L3 检查 (若提供了回复文本)
        if emitted_text:
            allowed_nums = cls.build_ground_truth_closure(
                user_query=user_query,
                product_cards=product_cards,
                price_cards=price_explain_cards,
                order_result=order_result,
                aftersales_result=aftersales_result,
                rag_result=rag_result,
            )
            v3 = cls.assert_l3_semantic_grounding(
                emitted_text=emitted_text,
                allowed_truth_numbers=allowed_nums,
                strict_numeric_closure=strict_numeric_closure,
            )
            all_violations.extend(v3)
            l3_cnt += 4

        return AssertionReport(
            is_valid=(len(all_violations) == 0),
            total_assertions_checked=(l1_cnt + l2_cnt + l3_cnt),
            l1_assertions=l1_cnt,
            l2_assertions=l2_cnt,
            l3_assertions=l3_cnt,
            violations=all_violations,
        )


# =========================================================================
# CNT 六层漏斗短路判决引擎 (Fail-Fast Hybrid Engine) 与四层自动化根因归因
# =========================================================================

class CNTFunnelStage(str, Enum):
    L1_CTX = "L1_CTX"    # 上下文连续性、品类与前置实体锁定、防注意力漂移
    L2_OOSR = "L2_OOSR"  # 域外业务防线拦截 (天气/违规/跨界引导)
    L3_OOC = "L3_OOC"    # 只读智能体能力越界拦截 (严禁承诺直接扣款/退款)
    L4_TCC = "L4_TCC"    # 工具调用契约与参数一致性 (SQL/品类硬隔离)
    L5_CNT = "L5_CNT"    # 业务解决率与目标满足率 (命中自营真源、满足硬约束)
    L6_RED = "L6_RED"    # 体验冗余与文本合规 (无废话死循环、无违禁营销承诺)


class AttributionCategory(str, Enum):
    DATA_LAYER = "DATA_LAYER"      # 数据层缺陷 (商品库字段缺失/类目未收录/价格库存漂移)
    AGENT_CORE = "AGENT_CORE"      # 智能体核心逻辑 (Jev路由失控/工具参数错误/状态机死循环/账本未继承)
    PROMPT_LAYER = "PROMPT_LAYER"  # 提示词层缺陷 (虚构价格幻觉/越权答复/系统标记泄露/营销违禁词)
    RUBRIC_LAYER = "RUBRIC_LAYER"  # 评测标定与标准缺陷 (Spec标准不符/评测用例期望标注歧义)


@dataclass
class AttributionDetail:
    category: AttributionCategory
    root_cause_code: str
    issue_summary: str
    fix_recommendation: str
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CNTStageResult:
    stage: CNTFunnelStage
    passed: bool
    description: str
    violation: Optional[AssertionViolation] = None
    attribution: Optional[AttributionDetail] = None


@dataclass
class CNTFunnelResult:
    passed: bool
    failed_stage: Optional[CNTFunnelStage] = None
    stage_results: List[CNTStageResult] = field(default_factory=list)
    final_attribution: Optional[AttributionDetail] = None
    total_stages_checked: int = 0


class AttributionClassifier:
    """
    四层自动化根因归因系统：
    当六层漏斗熔断时，深入解析违规行为并自动输出定点修复建议。
    """

    @classmethod
    def classify_violation(
        cls,
        stage: CNTFunnelStage,
        violation: AssertionViolation,
        context: Optional[Dict[str, Any]] = None,
    ) -> AttributionDetail:
        ctx = context or {}
        v_type = violation.violation_type
        msg = violation.message

        # 1. L1_CTX 归因
        if stage == CNTFunnelStage.L1_CTX:
            if "category_drift" in v_type or "drift" in msg:
                return AttributionDetail(
                    category=AttributionCategory.AGENT_CORE,
                    root_cause_code="AC_CTX_CATEGORY_DRIFT",
                    issue_summary=f"多轮上下文类目发生漂移: {msg}",
                    fix_recommendation="加固 HierarchicalContextHarness 状态继承，在缺少新类目提示词时锁定前置 sub_category",
                    evidence={"violation": msg, "stage": stage.value},
                )
            if "entity" in v_type or "sku" in v_type:
                return AttributionDetail(
                    category=AttributionCategory.DATA_LAYER,
                    root_cause_code="DL_MISSING_ENTITY_OR_SPEC",
                    issue_summary=f"前置指代商品或规格实体未在自营库被识别: {msg}",
                    fix_recommendation="在 spec_registry.json 中补充该 SKU 的别名/同义词映射或核实商品库上架状态",
                    evidence={"violation": msg, "stage": stage.value},
                )
            return AttributionDetail(
                category=AttributionCategory.AGENT_CORE,
                root_cause_code="AC_CTX_INHERITANCE_FAILURE",
                issue_summary=f"上下文决策账本继承失败: {msg}",
                fix_recommendation="检查 DecisionLedgerState 的多轮演进逻辑与状态合并规则",
                evidence={"violation": msg, "stage": stage.value},
            )

        # 2. L2_OOSR 归因 (域外防线)
        if stage == CNTFunnelStage.L2_OOSR:
            if "out_of_scope" in v_type or "oos" in msg:
                return AttributionDetail(
                    category=AttributionCategory.AGENT_CORE,
                    root_cause_code="AC_ROUTER_OOS_ESCAPE",
                    issue_summary=f"域外非电商意图逃逸，未触发拒答或引导: {msg}",
                    fix_recommendation="更新 LocalRuleDecisionEngine 的域外拒答关键词正则或降低 Jev 域外拒答阈值",
                    evidence={"violation": msg, "stage": stage.value},
                )
            return AttributionDetail(
                category=AttributionCategory.PROMPT_LAYER,
                root_cause_code="PL_PROMPT_OOS_ENGAGEMENT",
                issue_summary=f"提示词过度发散回答了域外话题: {msg}",
                fix_recommendation="在 STATIC_SYSTEM_CONSTITUTION 中强化仅限自营电商导购的服务边界约束",
                evidence={"violation": msg, "stage": stage.value},
            )

        # 3. L3_OOC 归因 (只读能力越界)
        if stage == CNTFunnelStage.L3_OOC:
            return AttributionDetail(
                category=AttributionCategory.PROMPT_LAYER,
                root_cause_code="PL_OOC_CAPABILITY_EXCEED",
                issue_summary=f"导购智能体做出越权承诺（直接退款/扣费/改单）: {msg}",
                fix_recommendation="在 StrategyInjector 输出过滤器及 System Prompt 中加固只读客服职责红线",
                evidence={"violation": msg, "stage": stage.value},
            )

        # 4. L4_TCC 归因 (工具契约与参数一致性)
        if stage == CNTFunnelStage.L4_TCC:
            if "category_isolation" in v_type:
                return AttributionDetail(
                    category=AttributionCategory.AGENT_CORE,
                    root_cause_code="AC_RAG_CATEGORY_LEAK",
                    issue_summary=f"RAG 或工具检索发生跨类目串扰: {msg}",
                    fix_recommendation="检查 MultimodalRAGEngine 的 category_isolated_filter，确保 SQL 强制隔离生效",
                    evidence={"violation": msg, "stage": stage.value},
                )
            return AttributionDetail(
                category=AttributionCategory.AGENT_CORE,
                root_cause_code="AC_TOOL_INVOCATION_DEFECT",
                issue_summary=f"工具调用参数或返回结构不一致: {msg}",
                fix_recommendation="检查 chat_orchestrator 工具调度分支与 DeterministicOrderTools 参数匹配",
                evidence={"violation": msg, "stage": stage.value},
            )

        # 5. L5_CNT 归因 (业务解决率与目标满足度)
        if stage == CNTFunnelStage.L5_CNT:
            if "sku_not_found" in v_type or "invalid_sku" in v_type:
                return AttributionDetail(
                    category=AttributionCategory.DATA_LAYER,
                    root_cause_code="DL_CATALOG_SKU_INCONSISTENCY",
                    issue_summary=f"返回了自营目录中不存在的 SKU: {msg}",
                    fix_recommendation="同步 catalog.json 真实商品数据至向量索引库并重新构建 BM25 索引",
                    evidence={"violation": msg, "stage": stage.value},
                )
            if "constraint_unsatisfied" in v_type:
                return AttributionDetail(
                    category=AttributionCategory.AGENT_CORE,
                    root_cause_code="AC_RERANK_CONSTRAINT_BREACH",
                    issue_summary=f"推荐商品未能严格满足用户指定的硬规格与预算约束: {msg}",
                    fix_recommendation="加固 MultimodalRAGEngine 中的硬约束前置过滤与松弛协商决策器",
                    evidence={"violation": msg, "stage": stage.value},
                )
            if "intent_mismatch" in v_type:
                # 检查是否为评测标定歧义
                if ctx.get("is_benchmark_eval") and ctx.get("expected_intent") in ("chitchat", "product_recommend"):
                    return AttributionDetail(
                        category=AttributionCategory.RUBRIC_LAYER,
                        root_cause_code="RL_AMBIGUOUS_EVAL_SPEC",
                        issue_summary=f"评测用例期望意图与电商现实存在多歧义标定: {msg}",
                        fix_recommendation="校准 eval_benchmark 用例集的 expected_intent 标定，对多意图复合 Query 增加兼容判定",
                        evidence={"violation": msg, "stage": stage.value},
                    )
                return AttributionDetail(
                    category=AttributionCategory.AGENT_CORE,
                    root_cause_code="AC_JEV_ROUTER_INTENT_ERROR",
                    issue_summary=f"Jev 决策路由器意图分类偏差: {msg}",
                    fix_recommendation="优化 Jev 决策问题模板或微调 DEFAULT_JEV_QUESTIONS",
                    evidence={"violation": msg, "stage": stage.value},
                )

        # 6. L6_RED 归因 (冗余体验与合规)
        if stage == CNTFunnelStage.L6_RED:
            if "internal_protocol_leak" in v_type or "forbidden_promotional_claim" in v_type or "ungrounded_numeric_claim" in v_type:
                return AttributionDetail(
                    category=AttributionCategory.PROMPT_LAYER,
                    root_cause_code="PL_NUMERIC_OR_COMPLIANCE_HALLUCINATION",
                    issue_summary=f"大模型生成了违禁违规词或虚构未闭包商业数值: {msg}",
                    fix_recommendation="调整 SlidingWindowStreamFilter 过滤窗口，或在静态前缀中提高数值逆向闭包处罚权重",
                    evidence={"violation": msg, "stage": stage.value},
                )
            return AttributionDetail(
                category=AttributionCategory.AGENT_CORE,
                root_cause_code="AC_REDUNDANT_LOOP_OR_DEADLOCK",
                issue_summary=f"检测到体验冗余、死循环调用或无意义复读: {msg}",
                fix_recommendation="加固工具调用计数限制 (tool_call_count <= 2) 与流式去重逻辑",
                evidence={"violation": msg, "stage": stage.value},
            )

        # 默认归因
        return AttributionDetail(
            category=AttributionCategory.AGENT_CORE,
            root_cause_code="AC_UNKNOWN_ASSERTION_DEFECT",
            issue_summary=f"未定义异常断言: {msg}",
            fix_recommendation="复查该场景的完整时序 Trace 与日志",
            evidence={"violation": msg, "stage": stage.value},
        )


class CNTFunnelEvaluator:
    """
    工业级 CNT 六层短路漏斗判决引擎 (Fail-Fast Hybrid Engine)
    依次按 L1_CTX -> L2_OOSR -> L3_OOC -> L4_TCC -> L5_CNT -> L6_RED 执行。
    前序漏斗层级一旦失败，立即短路熔断，并输出四层根因归因结果。
    """

    @classmethod
    def evaluate(
        cls,
        emitted_text: str,
        user_query: str,
        jev_decision: Dict[str, Any],
        expected_spec: Dict[str, Any],
        catalog_by_id: Dict[str, Dict[str, Any]],
        product_cards: Optional[List[Dict[str, Any]]] = None,
        price_explain_cards: Optional[List[Dict[str, Any]]] = None,
        order_result: Optional[Dict[str, Any]] = None,
        aftersales_result: Optional[Dict[str, Any]] = None,
        rag_result: Optional[Dict[str, Any]] = None,
        tool_call_count: int = 1,
        ledger_state: Optional[Dict[str, Any]] = None,
        strict_numeric_closure: bool = True,
    ) -> CNTFunnelResult:
        stage_results: List[CNTStageResult] = []

        # =====================================================================
        # Level 1: CTX (上下文连续性、品类实体锁定、防注意力漂移)
        # =====================================================================
        l1_passed = True
        l1_violation = None
        
        # 1. 检查是否存在品类漂移红线 (forbidden_category_drift)
        forbidden_sku = expected_spec.get("forbidden_category_drift")
        if forbidden_sku and product_cards:
            returned_skus = [c.get("sku_id") for c in product_cards]
            if forbidden_sku in returned_skus:
                l1_passed = False
                l1_violation = AssertionViolation(
                    level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                    violation_type="category_drift_breach",
                    message=f"多轮交互中推荐了被禁用的漂移商品: {forbidden_sku}",
                    expected=f"Exclusion of {forbidden_sku}",
                    actual=returned_skus,
                )

        # 2. 检查期望细分类目锁定
        expected_sub_cat = expected_spec.get("expected_sub_category")
        actual_sub_cat = jev_decision.get("sub_category") or (ledger_state or {}).get("locked_sub_category")
        if l1_passed and expected_sub_cat and actual_sub_cat:
            if expected_sub_cat != actual_sub_cat:
                l1_passed = False
                l1_violation = AssertionViolation(
                    level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                    violation_type="sub_category_drift",
                    message=f"细分类目未正向继承锁定: 期望={expected_sub_cat}, 实际={actual_sub_cat}",
                    expected=expected_sub_cat,
                    actual=actual_sub_cat,
                )

        if not l1_passed and l1_violation:
            attribution = AttributionClassifier.classify_violation(CNTFunnelStage.L1_CTX, l1_violation, {"expected_spec": expected_spec})
            stage_results.append(CNTStageResult(stage=CNTFunnelStage.L1_CTX, passed=False, description="上下文语境与实体一致性检验失败", violation=l1_violation, attribution=attribution))
            return CNTFunnelResult(passed=False, failed_stage=CNTFunnelStage.L1_CTX, stage_results=stage_results, final_attribution=attribution, total_stages_checked=1)

        stage_results.append(CNTStageResult(stage=CNTFunnelStage.L1_CTX, passed=True, description="L1 语境前置实体与上下文连续性完整"))

        # =====================================================================
        # Level 2: OOSR (域外业务防线拦截)
        # =====================================================================
        l2_passed = True
        l2_violation = None
        scenario_group = expected_spec.get("scenario_group")
        primary_intent = jev_decision.get("primary_intent")

        if scenario_group == "safety_and_escalation":
            sub_type = expected_spec.get("sub_type")
            if sub_type == "privacy_and_competitor" and primary_intent != "out_of_scope":
                # 需被域外或安全拒绝
                if not jev_decision.get("needs_escalation") and primary_intent not in ("out_of_scope", "chitchat"):
                    l2_passed = False
                    l2_violation = AssertionViolation(
                        level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                        violation_type="out_of_scope_unhandled",
                        message=f"域外或敏感提问未能有效拦截: 当前意图为 {primary_intent}",
                        expected="out_of_scope",
                        actual=primary_intent,
                    )

        if not l2_passed and l2_violation:
            attribution = AttributionClassifier.classify_violation(CNTFunnelStage.L2_OOSR, l2_violation, {"expected_spec": expected_spec})
            stage_results.append(CNTStageResult(stage=CNTFunnelStage.L2_OOSR, passed=False, description="域外业务防线拦截失败", violation=l2_violation, attribution=attribution))
            return CNTFunnelResult(passed=False, failed_stage=CNTFunnelStage.L2_OOSR, stage_results=stage_results, final_attribution=attribution, total_stages_checked=2)

        stage_results.append(CNTStageResult(stage=CNTFunnelStage.L2_OOSR, passed=True, description="L2 域外防线与安全准入合规"))

        # =====================================================================
        # Level 3: OOC (只读智能体能力越界拦截)
        # =====================================================================
        l3_passed = True
        l3_violation = None

        if emitted_text:
            for phrase in ShoppingAgentAssertionEngine.OUT_OF_CAPABILITY_PHRASES:
                if phrase in emitted_text:
                    l3_passed = False
                    l3_violation = AssertionViolation(
                        level=AssertionLevel.L3_SEMANTIC_REVERSE_GROUNDING,
                        violation_type="out_of_capability_promise",
                        message=f"输出了未授权的直接操作承诺: {phrase}",
                        expected="Read-only guidance",
                        actual=phrase,
                    )
                    break

        if not l3_passed and l3_violation:
            attribution = AttributionClassifier.classify_violation(CNTFunnelStage.L3_OOC, l3_violation, {"expected_spec": expected_spec})
            stage_results.append(CNTStageResult(stage=CNTFunnelStage.L3_OOC, passed=False, description="能力越界检查拦截失败", violation=l3_violation, attribution=attribution))
            return CNTFunnelResult(passed=False, failed_stage=CNTFunnelStage.L3_OOC, stage_results=stage_results, final_attribution=attribution, total_stages_checked=3)

        stage_results.append(CNTStageResult(stage=CNTFunnelStage.L3_OOC, passed=True, description="L3 只读能力越界防线通过"))

        # =====================================================================
        # Level 4: TCC (工具调用契约与参数一致性)
        # =====================================================================
        l4_passed = True
        l4_violation = None

        if tool_call_count > 2:
            l4_passed = False
            l4_violation = AssertionViolation(
                level=AssertionLevel.L2_TRAJECTORY_STATE_MACHINE,
                violation_type="tool_call_loop",
                message=f"工具调用次数超出阈值 ({tool_call_count} > 2)",
                expected="<= 2 calls",
                actual=tool_call_count,
            )

        if l4_passed and (order_result or aftersales_result):
            order_violations = ShoppingAgentAssertionEngine.assert_l1_order_and_aftersales(
                order_res=order_result,
                aftersales_res=aftersales_result,
                expected_spec=expected_spec,
            )
            if order_violations:
                l4_passed = False
                l4_violation = order_violations[0]

        if not l4_passed and l4_violation:
            attribution = AttributionClassifier.classify_violation(CNTFunnelStage.L4_TCC, l4_violation, {"expected_spec": expected_spec})
            stage_results.append(CNTStageResult(stage=CNTFunnelStage.L4_TCC, passed=False, description="工具调用一致性异常", violation=l4_violation, attribution=attribution))
            return CNTFunnelResult(passed=False, failed_stage=CNTFunnelStage.L4_TCC, stage_results=stage_results, final_attribution=attribution, total_stages_checked=4)

        stage_results.append(CNTStageResult(stage=CNTFunnelStage.L4_TCC, passed=True, description="L4 工具调用契约与确定性查询一致"))

        # =====================================================================
        # Level 5: CNT (业务解决率与目标满足度)
        # =====================================================================
        l5_passed = True
        l5_violation = None

        if product_cards:
            card_violations = ShoppingAgentAssertionEngine.assert_l1_product_cards(
                cards=product_cards,
                catalog_by_id=catalog_by_id,
                price_explain_cards=price_explain_cards,
            )
            if card_violations:
                l5_passed = False
                l5_violation = card_violations[0]

        # 检查是否成功命中期望的 SKU
        if l5_passed:
            expected_top_sku = expected_spec.get("expected_top_sku") or expected_spec.get("expected_path_a_sku")
            if expected_top_sku and product_cards:
                returned_ids = [c.get("sku_id") for c in product_cards]
                if expected_top_sku not in returned_ids:
                    l5_passed = False
                    l5_violation = AssertionViolation(
                        level=AssertionLevel.L1_DETERMINISTIC_STATE,
                        violation_type="sku_resolution_miss",
                        message=f"未能命中核心推荐商品 {expected_top_sku}",
                        expected=expected_top_sku,
                        actual=returned_ids,
                    )

        if not l5_passed and l5_violation:
            attribution = AttributionClassifier.classify_violation(CNTFunnelStage.L5_CNT, l5_violation, {"expected_spec": expected_spec, "is_benchmark_eval": True})
            stage_results.append(CNTStageResult(stage=CNTFunnelStage.L5_CNT, passed=False, description="业务解决率与目标满足度未达标", violation=l5_violation, attribution=attribution))
            return CNTFunnelResult(passed=False, failed_stage=CNTFunnelStage.L5_CNT, stage_results=stage_results, final_attribution=attribution, total_stages_checked=5)

        stage_results.append(CNTStageResult(stage=CNTFunnelStage.L5_CNT, passed=True, description="L5 自营真源命中与业务解决率达标"))

        # =====================================================================
        # Level 6: RED (体验冗余与文本合规)
        # =====================================================================
        l6_passed = True
        l6_violation = None

        if emitted_text:
            allowed_nums = ShoppingAgentAssertionEngine.build_ground_truth_closure(
                user_query=user_query,
                product_cards=product_cards,
                price_cards=price_explain_cards,
                order_result=order_result,
                aftersales_result=aftersales_result,
                rag_result=rag_result,
            )
            text_violations = ShoppingAgentAssertionEngine.assert_l3_semantic_grounding(
                emitted_text=emitted_text,
                allowed_truth_numbers=allowed_nums,
                strict_numeric_closure=strict_numeric_closure,
            )
            if text_violations:
                l6_passed = False
                l6_violation = text_violations[0]

        if not l6_passed and l6_violation:
            attribution = AttributionClassifier.classify_violation(CNTFunnelStage.L6_RED, l6_violation, {"expected_spec": expected_spec})
            stage_results.append(CNTStageResult(stage=CNTFunnelStage.L6_RED, passed=False, description="体验冗余与文本合规检验失败", violation=l6_violation, attribution=attribution))
            return CNTFunnelResult(passed=False, failed_stage=CNTFunnelStage.L6_RED, stage_results=stage_results, final_attribution=attribution, total_stages_checked=6)

        stage_results.append(CNTStageResult(stage=CNTFunnelStage.L6_RED, passed=True, description="L6 体验冗余控制与合规逆向闭包通过"))

        return CNTFunnelResult(
            passed=True,
            failed_stage=None,
            stage_results=stage_results,
            final_attribution=None,
            total_stages_checked=6,
        )
