# -*- coding: utf-8 -*-
"""
确定性推理引擎：产生式规则正反向链推导与支持集启发式剪枝 (app/reasoning/deterministic_engine.py)

学术与工程特性：
  1. 形式化产生式系统：严格遵循 IF <Conditions> THEN <Consequence> 架构；
  2. 全链路白盒证明树 (Proof Tree)：每个结论均记录前件事实依据、触发规则 ID 与推导步，支持可视化溯源；
  3. 双向推理能力：
     - 正向链 (Forward Chaining)：数据驱动，自底向上多跳连锁推演；
     - 反向链 (Backward Chaining)：目标驱动，自顶向下递归验证子目标与反向提问；
  4. 进阶优化（25分考核点）：
     - 支持集策略 (Set-of-Support, SoS)：隔离无关通用规则，聚焦目标关联子集；
     - 一票否决优先级剪枝 (Heuristic Veto Pruning)：优先执行高判别度门禁规则，大幅降低搜索空间。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


@dataclass
class Fact:
    """产生式事实原子。"""
    key: str
    value: Any
    confidence: float = 1.0


class Op(str, Enum):
    EQ = "=="
    NE = "!="
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"


@dataclass
class Condition:
    """产生式规则的前件原子条件。"""
    fact_key: str
    op: Op
    target_value: Any = None

    def evaluate(self, working_memory: Dict[str, Any]) -> bool:
        if self.op == Op.IS_TRUE:
            return bool(working_memory.get(self.fact_key) is True)
        if self.op == Op.IS_FALSE:
            return bool(working_memory.get(self.fact_key) is False)

        if self.fact_key not in working_memory:
            return False

        val = working_memory[self.fact_key]
        tgt = self.target_value

        try:
            if self.op == Op.EQ:
                return val == tgt
            elif self.op == Op.NE:
                return val != tgt
            elif self.op == Op.GT:
                return float(val) > float(tgt)
            elif self.op == Op.GTE:
                return float(val) >= float(tgt)
            elif self.op == Op.LT:
                return float(val) < float(tgt)
            elif self.op == Op.LTE:
                return float(val) <= float(tgt)
            elif self.op == Op.IN:
                return val in tgt
            elif self.op == Op.NOT_IN:
                return val not in tgt
            elif self.op == Op.CONTAINS:
                return tgt in val
        except (ValueError, TypeError):
            return False
        return False

    def to_readable(self) -> str:
        if self.op in (Op.IS_TRUE, Op.IS_FALSE):
            return f"{self.fact_key} {self.op.value}"
        return f"{self.fact_key} {self.op.value} {self.target_value}"


@dataclass
class Consequence:
    """产生式规则后件派生结论。"""
    derived_fact_key: str
    derived_value: Any
    is_terminal: bool = False  # 是否为终结状态（决策落地）
    decision_status: Optional[str] = None  # PASS, REJECT, DISPUTE


@dataclass
class Rule:
    """形式化产生式规则。"""
    rule_id: str
    name: str
    category: str  # 规则类别，用于支持集 SoS 划分
    priority: int  # 优先级（数值越高越先评估）
    conditions: List[Condition]
    consequence: Consequence
    explanation_template: str
    is_veto: bool = False  # 是否为一票否决门禁规则（剪枝关键）

    def check(self, working_memory: Dict[str, Any]) -> bool:
        return all(cond.evaluate(working_memory) for cond in self.conditions)


@dataclass
class ProofStep:
    """证明树推导步骤记录。"""
    step_index: int
    rule_id: str
    rule_name: str
    matched_conditions: List[str]
    derived_fact: str
    derived_value: Any
    explanation: str


@dataclass
class ProofTree:
    """白盒形式化证明树。"""
    goal: Optional[str]
    is_solved: bool
    terminal_decision: Optional[str]
    steps: List[ProofStep] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def render_ascii(self) -> str:
        """渲染成清晰可解释的 ASCII 证明树。"""
        lines = [
            f"=== 白盒产生式推导证明树 [目标: {self.goal or '全局决策'}] ===",
            f"最终裁决: {self.terminal_decision or '无终结结论'} (推导求解成功: {self.is_solved})",
            "--------------------------------------------------"
        ]
        if not self.steps:
            lines.append(" (无规则触发，使用初始事实推导失败)")
        for step in self.steps:
            lines.append(f"[{step.step_index}] 命中规则 <{step.rule_id}: {step.rule_name}>")
            lines.append(f"    ├─ 前件依据: {', '.join(step.matched_conditions)}")
            lines.append(f"    ├─ 产生事实: {step.derived_fact} = {step.derived_value}")
            lines.append(f"    └─ 逻辑解释: {step.explanation}")
        lines.append("--------------------------------------------------")
        lines.append(
            f"性能指标: 规则总数={self.metrics.get('total_rules', 0)} | "
            f"匹配探测={self.metrics.get('match_attempts', 0)} 次 | "
            f"有效触发={self.metrics.get('fired_rules', 0)} 次 | "
            f"剪枝跳过={self.metrics.get('pruned_rules', 0)} 条 | "
            f"推理耗时={self.metrics.get('execution_time_us', 0):.2f} μs"
        )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "is_solved": self.is_solved,
            "terminal_decision": self.terminal_decision,
            "steps": [
                {
                    "step": s.step_index,
                    "rule_id": s.rule_id,
                    "rule_name": s.rule_name,
                    "conditions": s.matched_conditions,
                    "fact": s.derived_fact,
                    "value": s.derived_value,
                    "explanation": s.explanation,
                }
                for s in self.steps
            ],
            "metrics": self.metrics,
        }


class DeterministicInferenceEngine:
    """
    确定性产生式推理机核心：
      支持正向链、反向链、支持集过滤（Set-of-Support）与一票否决优先级剪枝。
    """

    def __init__(self, rules: Optional[List[Rule]] = None) -> None:
        self.rules: List[Rule] = rules or []
        if not self.rules:
            self._load_default_ecommerce_rules()

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)

    def _load_default_ecommerce_rules(self) -> None:
        """加载电商售后与合规核心公理知识库。"""
        self.rules = [
            # 1. 终极一票否决（高优先级剪枝门禁）
            Rule(
                rule_id="R_VETO_NOT_SIGNED",
                name="未签收一票否决",
                category="aftersales_gate",
                priority=100,
                is_veto=True,
                conditions=[
                    Condition("order_status", Op.NE, "delivered")
                ],
                consequence=Consequence("return_eligibility", "REJECT_NOT_SIGNED", is_terminal=True, decision_status="REJECT"),
                explanation_template="订单当前物流状态为 '{order_status}'，尚未签收，不支持7天无理由退货（可申请物流拦截）。",
            ),
            Rule(
                rule_id="R_VETO_EXCEED_30D",
                name="严重超时超30天一票否决",
                category="aftersales_gate",
                priority=95,
                is_veto=True,
                conditions=[
                    Condition("elapsed_hours", Op.GT, 720.0)
                ],
                consequence=Consequence("return_eligibility", "REJECT_EXCEED_30D", is_terminal=True, decision_status="REJECT"),
                explanation_template="订单签收已超 720 小时（30天），严重超出平台无理由与售后关怀期限，系统硬性驳回。",
            ),
            Rule(
                rule_id="R_VETO_COUNTERFEIT",
                name="恶意调包欺诈黑名单否决",
                category="security_gate",
                priority=90,
                is_veto=True,
                conditions=[
                    Condition("buyer_risk_level", Op.EQ, "HIGH_FRAUD_RISK")
                ],
                consequence=Consequence("return_eligibility", "REJECT_FRAUD_LOCK", is_terminal=True, decision_status="REJECT"),
                explanation_template="买家账户命中平台恶意欺诈/调包黑名单规则，冻结无理由退货权限，转人工风控专员核验。",
            ),

            # 2. 确定性合规放行
            Rule(
                rule_id="R_STANDARD_PASS_7D",
                name="标准7天无理由完全合规放行",
                category="standard_aftersales",
                priority=60,
                conditions=[
                    Condition("order_status", Op.EQ, "delivered"),
                    Condition("elapsed_hours", Op.LTE, 168.0),
                    Condition("is_opened", Op.IS_FALSE),
                ],
                consequence=Consequence("return_eligibility", "ELIGIBLE_7D_STANDARD", is_terminal=True, decision_status="PASS"),
                explanation_template="订单签收时间（{elapsed_hours:.1f}h <= 168h）且商品完好未拆封，100% 满足平台7天无理由退换条款。",
            ),

            # 3. 确定性拆封超期违规
            Rule(
                rule_id="R_EXCEED_168H_UNOPENED",
                name="未拆封但超168小时冷酷违规",
                category="standard_aftersales",
                priority=50,
                conditions=[
                    Condition("elapsed_hours", Op.GT, 168.0),
                    Condition("elapsed_hours", Op.LTE, 720.0),
                    Condition("is_opened", Op.IS_FALSE),
                    Condition("elapsed_hours", Op.GT, 240.0),  # 超出较大（>10天）
                ],
                consequence=Consequence("return_eligibility", "REJECT_WINDOW_EXPIRED", is_terminal=True, decision_status="REJECT"),
                explanation_template="商品虽未拆封，但签收距今已满 {elapsed_hours:.1f} 小时，超出平台 168 小时（7天）受理窗口期。",
            ),

            # 4. 争议与模糊区间派生规则（触发后续不确定性推理中枢）
            Rule(
                rule_id="R_DISPUTE_MARGINAL_OVERDUE",
                name="微超期争议衍生规则",
                category="dispute_arbitration",
                priority=40,
                conditions=[
                    Condition("elapsed_hours", Op.GT, 168.0),
                    Condition("elapsed_hours", Op.LTE, 240.0),  # 7~10天之间微超期
                ],
                consequence=Consequence("dispute_category", "MARGINAL_OVERDUE_DISPUTE", is_terminal=False, decision_status="DISPUTE"),
                explanation_template="订单签收达 {elapsed_hours:.1f} 小时，处于微超期边缘区间（168h~240h），派生争议状态供二次仲裁。",
            ),
            Rule(
                rule_id="R_DISPUTE_OPENED_DEFECT",
                name="拆封但主张性能故障争议衍生",
                category="dispute_arbitration",
                priority=40,
                conditions=[
                    Condition("is_opened", Op.IS_TRUE),
                    Condition("issue_type", Op.IN, ["quality_defect", "performance_failure", "damaged_on_arrival"]),
                ],
                consequence=Consequence("dispute_category", "QUALITY_DISPUTE_OPENED", is_terminal=False, decision_status="DISPUTE"),
                explanation_template="商品已拆封但用户反馈存在外观损伤或性能故障，超出无理由范畴，派生质量争议状态供证据合成。",
            ),
            Rule(
                rule_id="R_TRIGGER_UNCERTAIN_ARBITRATION",
                name="综合争议转入不确定性仲裁网关",
                category="dispute_arbitration",
                priority=20,
                conditions=[
                    Condition("dispute_category", Op.IN, ["MARGINAL_OVERDUE_DISPUTE", "QUALITY_DISPUTE_OPENED"]),
                ],
                consequence=Consequence("return_eligibility", "UNCERTAIN_DISPUTE", is_terminal=True, decision_status="DISPUTE"),
                explanation_template="事实命中了争议复合前件，确定性规则无法单一裁决，移交不确定性双引擎（D-S融合 + 模糊推理）。",
            ),

            # 5. 15天价保判定规则
            Rule(
                rule_id="R_PRICE_DROP_ELIGIBLE",
                name="15天保价有效退差判定",
                category="price_guarantee",
                priority=50,
                conditions=[
                    Condition("elapsed_hours", Op.LTE, 360.0),
                    Condition("price_diff", Op.GT, 0.0),
                ],
                consequence=Consequence("price_guarantee_eligibility", "REFUND_PRICE_DIFF", is_terminal=True, decision_status="PASS"),
                explanation_template="订单签收时间（{elapsed_hours:.1f}h <= 360h）且商品产生降价正差额（直降 {price_diff} 元），支持退差。",
            ),
            Rule(
                rule_id="R_NO_PRICE_DROP",
                name="无降价差额价保驳回",
                category="price_guarantee",
                priority=50,
                conditions=[
                    Condition("price_diff", Op.LTE, 0.0),
                ],
                consequence=Consequence("price_guarantee_eligibility", "NO_PRICE_DROP", is_terminal=True, decision_status="REJECT"),
                explanation_template="当前商品售价未低于历史下单实付价（差额为 0 或上涨），暂不触发价保补差。",
            ),
        ]

    def forward_chain(
        self,
        facts: Dict[str, Any],
        target_predicate: Optional[str] = None,
        enable_pruning: bool = True,
        sos_category: Optional[str] = None,
    ) -> ProofTree:
        """
        正向链推导算法 (Forward Chaining)：
          - 数据驱动，自底向上循环扫描；
          - 结合优先级与支持集 (Set-of-Support) 剪枝；
          - 遇到一票否决 (Veto) 规则直接短路退出，显著减少迭代步数。
        """
        start_time = time.perf_counter_ns()
        working_memory = dict(facts)
        fired_rules_set: Set[str] = set()
        proof_steps: List[ProofStep] = []
        terminal_decision: Optional[str] = None
        is_solved = False

        # 统计计数器
        match_attempts = 0
        fired_count = 0
        pruned_count = 0

        # 候选规则过滤（支持集策略 Set-of-Support）
        if enable_pruning and sos_category:
            candidate_rules = [r for r in self.rules if r.category == sos_category or r.is_veto]
            pruned_count += (len(self.rules) - len(candidate_rules))
        else:
            candidate_rules = list(self.rules)

        # 启发式排序：按 priority 降序（优先匹配高优先级一票否决规则）
        if enable_pruning:
            candidate_rules.sort(key=lambda r: r.priority, reverse=True)

        step_idx = 1
        changed = True

        while changed:
            changed = False
            for rule in candidate_rules:
                if rule.rule_id in fired_rules_set:
                    continue

                match_attempts += 1
                if rule.check(working_memory):
                    # 触发规则 (Fire Rule)
                    fired_rules_set.add(rule.rule_id)
                    fired_count += 1
                    changed = True

                    # 记录推导证据步
                    c = rule.consequence
                    working_memory[c.derived_fact_key] = c.derived_value

                    matched_cond_strs = [
                        cond.to_readable() for cond in rule.conditions
                    ]
                    # 格式化解释
                    try:
                        expl = rule.explanation_template.format(**working_memory)
                    except Exception:
                        expl = rule.explanation_template

                    proof_steps.append(
                        ProofStep(
                            step_index=step_idx,
                            rule_id=rule.rule_id,
                            rule_name=rule.name,
                            matched_conditions=matched_cond_strs,
                            derived_fact=c.derived_fact_key,
                            derived_value=c.derived_value,
                            explanation=expl,
                        )
                    )
                    step_idx += 1

                    # 检查是否满足终结条件
                    if c.is_terminal:
                        terminal_decision = str(c.derived_value)
                        is_solved = True
                        if enable_pruning:
                            # 一票否决或终结规则命中，立即短路剪枝剩余所有规则！
                            remaining_rules = [r for r in candidate_rules if r.rule_id not in fired_rules_set]
                            pruned_count += len(remaining_rules)
                            changed = False
                            break

                    if target_predicate and c.derived_fact_key == target_predicate:
                        is_solved = True
                        if enable_pruning:
                            changed = False
                            break

            if is_solved and enable_pruning:
                break

        end_time = time.perf_counter_ns()
        execution_time_us = (end_time - start_time) / 1000.0

        metrics = {
            "total_rules": len(self.rules),
            "candidate_rules": len(candidate_rules),
            "match_attempts": match_attempts,
            "fired_rules": fired_count,
            "pruned_rules": pruned_count,
            "execution_time_us": execution_time_us,
            "enable_pruning": enable_pruning,
        }

        return ProofTree(
            goal=target_predicate,
            is_solved=is_solved,
            terminal_decision=terminal_decision,
            steps=proof_steps,
            metrics=metrics,
        )

    def backward_chain(
        self,
        goal_fact: str,
        goal_value: Any,
        facts: Dict[str, Any],
    ) -> Tuple[bool, List[str], List[str]]:
        """
        反向链推导算法 (Backward Chaining)：
          - 目标驱动，自顶向下递归搜索规则子目标；
          - 返回: (是否证明成功, 证明路径规则列表, 缺失的事实条件列表)。
        """
        working_memory = dict(facts)
        missing_conditions: List[str] = []
        path: List[str] = []

        def _prove(fact_k: str, fact_v: Any, visited_goals: Set[str]) -> bool:
            # 1. 如果事实已在工作内存中直接满足
            if fact_k in working_memory:
                return working_memory[fact_k] == fact_v

            state_key = f"{fact_k}={fact_v}"
            if state_key in visited_goals:
                return False  # 防死循环回路
            visited_goals.add(state_key)

            # 2. 寻找后件能产生该事实的规则
            matching_rules = [
                r for r in self.rules
                if r.consequence.derived_fact_key == fact_k and r.consequence.derived_value == fact_v
            ]

            if not matching_rules:
                missing_conditions.append(f"{fact_k} 应为 {fact_v} (未在已知事实中)")
                return False

            for rule in matching_rules:
                rule_supported = True
                for cond in rule.conditions:
                    if not cond.evaluate(working_memory):
                        # 尝试反向将未满足的前件作为子目标递归求证
                        if cond.target_value is not None:
                            sub_ok = _prove(cond.fact_key, cond.target_value, visited_goals)
                            if not sub_ok:
                                rule_supported = False
                                break
                        else:
                            missing_conditions.append(cond.to_readable())
                            rule_supported = False
                            break
                if rule_supported:
                    path.append(rule.rule_id)
                    working_memory[fact_k] = fact_v
                    return True

            return False

        success = _prove(goal_fact, goal_value, set())
        return success, path, missing_conditions
