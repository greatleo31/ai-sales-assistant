# -*- coding: utf-8 -*-
"""
不确定性推理引擎 B：Mamdani 模糊推理系统与重心法解模糊 (app/reasoning/fuzzy_engine.py)

理论与数学公理：
  1. 模糊集合与隶属度函数 (Membership Function, μ_A(x)):
     - 三角形隶属度 (Triangular MF)
     - 梯形隶属度 (Trapezoidal MF)
     - 高斯隶属度 (Gaussian MF)
  2. 模糊命题与 Mamdani 蕴含运算 (Mamdani Min-Implication):
     - 激活强度 α = min(μ_A1(x1), μ_A2(x2))
     - 截割后件 μ_B*(y) = min(α, μ_B(y))
     - 最大聚合 μ_agg(y) = max_k (μ_Bk*(y))
  3. 解模糊化算法 (Defuzzification):
     - 一维离散积分重心法 (Centroid / Center of Gravity, COG):
       z* = (Σ z_i * μ_agg(z_i)) / (Σ μ_agg(z_i))
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


class MembershipFunction:
    """隶属度函数抽象基类。"""
    def evaluate(self, x: float) -> float:
        raise NotImplementedError


@dataclass
class TriangularMF(MembershipFunction):
    """三角形隶属度函数 trimf(x; a, b, c)。"""
    a: float  # 左底脚
    b: float  # 峰值顶峰 (μ=1.0)
    c: float  # 右底脚

    def evaluate(self, x: float) -> float:
        if x <= self.a or x >= self.c:
            return 0.0
        if x == self.b:
            return 1.0
        if x < self.b:
            return (x - self.a) / (self.b - self.a) if self.b != self.a else 1.0
        else:
            return (self.c - x) / (self.c - self.b) if self.c != self.b else 1.0


@dataclass
class TrapezoidalMF(MembershipFunction):
    """梯形隶属度函数 trapmf(x; a, b, c, d)。"""
    a: float  # 左底脚 (μ=0)
    b: float  # 左平台起点 (μ=1.0)
    c: float  # 右平台终点 (μ=1.0)
    d: float  # 右底脚 (μ=0)

    def evaluate(self, x: float) -> float:
        # 左侧平台（如 [0, 0, 2, 6]）
        if self.a == self.b and x <= self.b:
            return 1.0
        # 右侧平台（如 [36, 72, 120, 120]）
        if self.c == self.d and x >= self.c:
            return 1.0

        if x < self.a or x > self.d:
            return 0.0
        if self.b <= x <= self.c:
            return 1.0
        if x < self.b:
            return (x - self.a) / (self.b - self.a) if self.b != self.a else 1.0
        else:
            return (self.d - x) / (self.d - self.c) if self.d != self.c else 1.0


@dataclass
class LinguisticTerm:
    """语言词项（如：'轻微超时'、'高价值客户'）。"""
    name: str
    mf: MembershipFunction


@dataclass
class FuzzyVariable:
    """模糊语言变量（如：超时时长 overdue_hours）。"""
    name: str
    min_val: float
    max_val: float
    terms: Dict[str, LinguisticTerm] = field(default_factory=dict)

    def add_term(self, name: str, mf: MembershipFunction) -> None:
        self.terms[name] = LinguisticTerm(name=name, mf=mf)

    def fuzzify(self, val: float) -> Dict[str, float]:
        """将物理精确数值模糊化为各语言词项的隶属度分布。"""
        clipped = max(self.min_val, min(self.max_val, float(val)))
        return {
            name: round(term.mf.evaluate(clipped), 5)
            for name, term in self.terms.items()
        }


@dataclass
class FuzzyRule:
    """Mamdani 模糊推理规则：IF (Var1 is Term1 AND Var2 is Term2) THEN (OutVar is OutTerm)。"""
    antecedents: Dict[str, str]  # { "overdue_hours": "MILD", "customer_loyalty": "HIGH" }
    consequents: Dict[str, str]  # { "compensation": "MEDIUM", "approval": "APPROVED" }
    weight: float = 1.0


@dataclass
class FuzzyDecisionResult:
    """模糊决策输出结果。"""
    compensation_amount: float       # 柔性关怀券金额（元）
    approval_confidence: float       # 特批通过置信度 (0.0 ~ 1.0)
    activated_rules: List[Dict[str, Any]]
    defuzzified_values: Dict[str, float]
    linguistic_summary: str


class FuzzyInferenceEngine:
    """
    电商售后柔性关怀与特批赔付 Mamdani 模糊推理机。
    针对超时微超、主观争议等灰色边缘订单，提供自适应、平滑、人性化的连续决策。
    """

    def __init__(self) -> None:
        self.input_vars: Dict[str, FuzzyVariable] = {}
        self.output_vars: Dict[str, FuzzyVariable] = {}
        self.rules: List[FuzzyRule] = []
        self._build_standard_ecommerce_fuzzy_system()

    def _build_standard_ecommerce_fuzzy_system(self) -> None:
        # 1. 输入变量 A：超时时长 overdue_hours (0 ~ 120 小时)
        var_overdue = FuzzyVariable("overdue_hours", 0.0, 120.0)
        var_overdue.add_term("ON_TIME", TrapezoidalMF(0.0, 0.0, 2.0, 6.0))
        var_overdue.add_term("MILD", TriangularMF(4.0, 24.0, 48.0))
        var_overdue.add_term("SEVERE", TrapezoidalMF(36.0, 72.0, 120.0, 120.0))
        self.input_vars["overdue_hours"] = var_overdue

        # 2. 输入变量 B：客户忠诚度与信誉分 customer_loyalty (0 ~ 100 分)
        var_loyalty = FuzzyVariable("customer_loyalty", 0.0, 100.0)
        var_loyalty.add_term("LOW", TrapezoidalMF(0.0, 0.0, 30.0, 50.0))
        var_loyalty.add_term("MEDIUM", TriangularMF(35.0, 60.0, 80.0))
        var_loyalty.add_term("HIGH", TrapezoidalMF(70.0, 85.0, 100.0, 100.0))
        self.input_vars["customer_loyalty"] = var_loyalty

        # 3. 输出变量 A：柔性关怀补偿券金额 compensation_amount (0 ~ 100 元)
        var_comp = FuzzyVariable("compensation_amount", 0.0, 100.0)
        var_comp.add_term("ZERO", TrapezoidalMF(0.0, 0.0, 5.0, 10.0))
        var_comp.add_term("SMALL", TriangularMF(8.0, 20.0, 35.0))
        var_comp.add_term("MEDIUM", TriangularMF(25.0, 45.0, 65.0))
        var_comp.add_term("GENEROUS", TrapezoidalMF(50.0, 75.0, 100.0, 100.0))
        self.output_vars["compensation_amount"] = var_comp

        # 4. 输出变量 B：特批换新放行置信度 approval_confidence (0.0 ~ 1.0)
        var_appr = FuzzyVariable("approval_confidence", 0.0, 1.0)
        var_appr.add_term("REJECT", TrapezoidalMF(0.0, 0.0, 0.2, 0.4))
        var_appr.add_term("CONDITIONAL", TriangularMF(0.3, 0.6, 0.8))
        var_appr.add_term("APPROVE", TrapezoidalMF(0.7, 0.85, 1.0, 1.0))
        self.output_vars["approval_confidence"] = var_appr

        # 5. 模糊规则库
        self.rules = [
            # 准时签收但有小纠纷：信誉越高，关怀越丰厚
            FuzzyRule(
                antecedents={"overdue_hours": "ON_TIME", "customer_loyalty": "HIGH"},
                consequents={"compensation_amount": "GENEROUS", "approval_confidence": "APPROVE"}
            ),
            FuzzyRule(
                antecedents={"overdue_hours": "ON_TIME", "customer_loyalty": "MEDIUM"},
                consequents={"compensation_amount": "MEDIUM", "approval_confidence": "APPROVE"}
            ),
            FuzzyRule(
                antecedents={"overdue_hours": "ON_TIME", "customer_loyalty": "LOW"},
                consequents={"compensation_amount": "SMALL", "approval_confidence": "CONDITIONAL"}
            ),

            # 轻微超时（核心业务痛点）：高忠诚客户获得特批补偿
            FuzzyRule(
                antecedents={"overdue_hours": "MILD", "customer_loyalty": "HIGH"},
                consequents={"compensation_amount": "MEDIUM", "approval_confidence": "APPROVE"}
            ),
            FuzzyRule(
                antecedents={"overdue_hours": "MILD", "customer_loyalty": "MEDIUM"},
                consequents={"compensation_amount": "SMALL", "approval_confidence": "CONDITIONAL"}
            ),
            FuzzyRule(
                antecedents={"overdue_hours": "MILD", "customer_loyalty": "LOW"},
                consequents={"compensation_amount": "ZERO", "approval_confidence": "REJECT"}
            ),

            # 严重超时：无论信誉多高，严格防范羊毛党，补偿降到最低
            FuzzyRule(
                antecedents={"overdue_hours": "SEVERE", "customer_loyalty": "HIGH"},
                consequents={"compensation_amount": "SMALL", "approval_confidence": "CONDITIONAL"}
            ),
            FuzzyRule(
                antecedents={"overdue_hours": "SEVERE", "customer_loyalty": "MEDIUM"},
                consequents={"compensation_amount": "ZERO", "approval_confidence": "REJECT"}
            ),
            FuzzyRule(
                antecedents={"overdue_hours": "SEVERE", "customer_loyalty": "LOW"},
                consequents={"compensation_amount": "ZERO", "approval_confidence": "REJECT"}
            ),
        ]

    def evaluate(self, inputs: Dict[str, float]) -> FuzzyDecisionResult:
        """
        执行完整 Mamdani 模糊推理并用重心法解模糊。
        """
        # 1. 输入模糊化
        fuzzified_inputs: Dict[str, Dict[str, float]] = {}
        for var_name, var_obj in self.input_vars.items():
            val = inputs.get(var_name, var_obj.min_val)
            fuzzified_inputs[var_name] = var_obj.fuzzify(val)

        # 2. 规则前件求值与后件截割 (Min-Implication)
        # 为每个输出变量收集截割后的隶属度曲线
        activated_rules_log: List[Dict[str, Any]] = []
        rule_implications: Dict[str, List[Tuple[float, LinguisticTerm]]] = {
            out_name: [] for out_name in self.output_vars
        }

        for rule in self.rules:
            # 前件求交集 (AND 算子 -> min)
            antecedent_degrees = []
            for in_var, term_name in rule.antecedents.items():
                degree = fuzzified_inputs.get(in_var, {}).get(term_name, 0.0)
                antecedent_degrees.append(degree)

            firing_strength = min(antecedent_degrees) * rule.weight

            if firing_strength > 0.01:
                activated_rules_log.append({
                    "antecedents": rule.antecedents,
                    "consequents": rule.consequents,
                    "strength": round(firing_strength, 4)
                })

                for out_var, out_term_name in rule.consequents.items():
                    out_term = self.output_vars[out_var].terms[out_term_name]
                    rule_implications[out_var].append((firing_strength, out_term))

        # 3. 聚合与重心法解模糊 (Centroid Defuzzification)
        defuzzified: Dict[str, float] = {}

        for out_name, out_var in self.output_vars.items():
            implications = rule_implications[out_name]
            if not implications:
                defuzzified[out_name] = out_var.min_val
                continue

            # 离散数值积分采样 (100 个切片)
            steps = 100
            step_size = (out_var.max_val - out_var.min_val) / steps
            numerator = 0.0
            denominator = 0.0

            for i in range(steps + 1):
                y = out_var.min_val + i * step_size
                # 最大聚合 (Max-Aggregation)
                aggregated_mu = 0.0
                for alpha, term in implications:
                    mu = min(alpha, term.mf.evaluate(y))
                    if mu > aggregated_mu:
                        aggregated_mu = mu

                numerator += y * aggregated_mu
                denominator += aggregated_mu

            if denominator > 1e-6:
                defuzzified[out_name] = round(numerator / denominator, 2)
            else:
                defuzzified[out_name] = out_var.min_val

        comp = defuzzified.get("compensation_amount", 0.0)
        appr = defuzzified.get("approval_confidence", 0.0)

        # 构造自然语言结论摘要
        if appr >= 0.70:
            summary = f"基于模糊推理，客户高粘性且超时处于宽限期内，建议【特批换新】，配套关怀补偿券 {comp:.1f} 元。"
        elif appr >= 0.40:
            summary = f"基于模糊推理，处于争议灰度中间带，建议【有条件补差检修】，配套关怀补偿券 {comp:.1f} 元。"
        else:
            summary = f"基于模糊推理，订单超期显著且信用未达特批线，判定【驳回退换】，补偿额度 0 元。"

        return FuzzyDecisionResult(
            compensation_amount=comp,
            approval_confidence=appr,
            activated_rules=activated_rules_log,
            defuzzified_values=defuzzified,
            linguistic_summary=summary,
        )
