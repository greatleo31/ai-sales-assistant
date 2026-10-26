# -*- coding: utf-8 -*-
"""
推理内核全套单元测试与理论公理实测 (scripts/test_reasoning_core.py)
验证内容：
  1. 确定性推理：正向链推导、证明树生成、反向链目标反查、支持集/优先级剪枝效果；
  2. D-S 证据理论：经典 Dempster 合成、Zadeh 悖论复现、Murphy 算法修正对比；
  3. 模糊推理：三角形/梯形隶属度函数、Mamdani 蕴含聚合、离散积分重心法解模糊；
  4. 双轨混合推理：确定性门禁与不确定性争议动态裁决全链路。
"""

import sys
import unittest
from pathlib import Path

# 将项目根目录加入模块搜索路径
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.reasoning.deterministic_engine import (
    Condition,
    Consequence,
    DeterministicInferenceEngine,
    Op,
    Rule,
)
from app.reasoning.ds_evidence import (
    DSEvidenceEngine,
    EcommerceEvidenceFactory,
    ELEM_BUYER,
    ELEM_LOGISTICS,
    ELEM_SELLER,
    FULL_FRAME,
    MassFunction,
    MurphyEvidenceCombiner,
)
from app.reasoning.fuzzy_engine import (
    FuzzyInferenceEngine,
    TrapezoidalMF,
    TriangularMF,
)
from app.reasoning.hybrid_arbitrator import (
    HybridArbitrator,
    get_hybrid_arbitrator,
)


class TestDeterministicEngine(unittest.TestCase):
    """测试确定性产生式推理机与剪枝算法。"""

    def setUp(self) -> None:
        self.engine = DeterministicInferenceEngine()

    def test_standard_7d_pass(self) -> None:
        """测试 168h 内且未拆封的标准放行。"""
        facts = {
            "order_id": "ORD-20261001-001",
            "order_status": "delivered",
            "elapsed_hours": 48.0,
            "is_opened": False,
        }
        tree = self.engine.forward_chain(facts, target_predicate="return_eligibility", enable_pruning=True)
        self.assertTrue(tree.is_solved)
        self.assertEqual(tree.terminal_decision, "ELIGIBLE_7D_STANDARD")
        self.assertGreaterEqual(len(tree.steps), 1)
        # 验证证明树生成可读文本
        rendered = tree.render_ascii()
        self.assertIn("标准7天无理由完全合规放行", rendered)

    def test_veto_not_signed_pruning(self) -> None:
        """测试未签收一票否决规则短路剪枝。"""
        facts = {
            "order_id": "ORD-20261001-002",
            "order_status": "shipped",
            "elapsed_hours": 10.0,
            "is_opened": False,
        }
        # 有剪枝
        tree_pruned = self.engine.forward_chain(facts, target_predicate="return_eligibility", enable_pruning=True)
        self.assertEqual(tree_pruned.terminal_decision, "REJECT_NOT_SIGNED")
        self.assertGreater(tree_pruned.metrics["pruned_rules"], 0)

        # 无剪枝
        tree_no_prune = self.engine.forward_chain(facts, target_predicate="return_eligibility", enable_pruning=False)
        self.assertEqual(tree_no_prune.terminal_decision, "REJECT_NOT_SIGNED")
        self.assertGreaterEqual(tree_no_prune.metrics["match_attempts"], tree_pruned.metrics["match_attempts"])

    def test_backward_chain_goal_driven(self) -> None:
        """测试反向链目标驱动推理。"""
        # 已知事实缺少 is_opened
        facts = {
            "order_status": "delivered",
            "elapsed_hours": 24.0,
        }
        # 试图证明通过退换
        ok, path, missing = self.engine.backward_chain(
            goal_fact="return_eligibility",
            goal_value="ELIGIBLE_7D_STANDARD",
            facts=facts,
        )
        self.assertFalse(ok)
        self.assertTrue(any("is_opened" in m for m in missing))


class TestDSEvidenceAndZadeh(unittest.TestCase):
    """测试 D-S 证据理论与 Zadeh 悖论消融。"""

    def test_zadeh_paradox_and_murphy_fix(self) -> None:
        """
        学术核心经典测试：Zadeh 悖论复现与 Murphy 修正。
        设定识别框架：{B, S, L}
        证据 1 强烈认为责任在商家 S: m1(S)=0.99, m1(B)=0.01, m1(L)=0.0
        证据 2 强烈认为责任在物流 L: m2(L)=0.99, m2(B)=0.01, m2(S)=0.0
        """
        m1 = MassFunction({
            frozenset([ELEM_SELLER]): 0.99,
            frozenset([ELEM_BUYER]): 0.01,
        }, name="Doctor1")

        m2 = MassFunction({
            frozenset([ELEM_LOGISTICS]): 0.99,
            frozenset([ELEM_BUYER]): 0.01,
        }, name="Doctor2")

        # 1. 经典 Dempster 合成
        combined_dempster, conflict_k = DSEvidenceEngine.dempster_combine_two(m1, m2)
        # 冲突因子高达 0.9998 (1 - 0.0001)
        self.assertGreater(conflict_k, 0.98)
        # 经典规则荒谬地判定责任 100% 在买家！(Zadeh 悖论)
        buyer_belief_dempster = combined_dempster.belief(frozenset([ELEM_BUYER]))
        self.assertAlmostEqual(buyer_belief_dempster, 1.0, places=2)

        # 2. Murphy 加权平均修正算法介入
        murphy_fused = MurphyEvidenceCombiner.combine([m1, m2])
        buyer_belief_murphy = murphy_fused.belief(frozenset([ELEM_BUYER]))
        seller_belief_murphy = murphy_fused.belief(frozenset([ELEM_SELLER]))
        logistics_belief_murphy = murphy_fused.belief(frozenset([ELEM_LOGISTICS]))

        # Murphy 修正后，买家责任回归极低（约 1%），商家与物流势均力敌（各约 49%）！彻底打破悖论！
        self.assertLess(buyer_belief_murphy, 0.05)
        self.assertAlmostEqual(seller_belief_murphy, logistics_belief_murphy, places=2)
        self.assertGreater(seller_belief_murphy, 0.40)


class TestFuzzyEngine(unittest.TestCase):
    """测试 Mamdani 模糊推理系统与重心法解模糊。"""

    def setUp(self) -> None:
        self.fuzzy = FuzzyInferenceEngine()

    def test_mild_overdue_high_loyalty(self) -> None:
        """测试轻微超时但高价值忠诚客户的柔性关怀。"""
        inputs = {
            "overdue_hours": 12.0,       # 轻微超期 12 小时
            "customer_loyalty": 92.0,    # 优质高价值 VIP
        }
        res = self.fuzzy.evaluate(inputs)
        # 补偿金应在 20~55 元区间
        self.assertGreater(res.compensation_amount, 20.0)
        self.assertLess(res.compensation_amount, 60.0)
        # 放行置信度高
        self.assertGreater(res.approval_confidence, 0.65)
        self.assertIn("特批换新", res.linguistic_summary)

    def test_severe_overdue_low_loyalty(self) -> None:
        """测试严重超时且低信誉客户的防羊毛拦截。"""
        inputs = {
            "overdue_hours": 80.0,       # 严重超时
            "customer_loyalty": 25.0,    # 风险账号
        }
        res = self.fuzzy.evaluate(inputs)
        self.assertLessEqual(res.compensation_amount, 10.0)
        self.assertLess(res.approval_confidence, 0.35)
        self.assertIn("驳回退换", res.linguistic_summary)


class TestHybridArbitrator(unittest.TestCase):
    """测试双轨神经-符号混合推理仲裁器。"""

    def setUp(self) -> None:
        self.arbitrator = get_hybrid_arbitrator()

    def test_hybrid_flow_standard_pass(self) -> None:
        """测试第一阶段确定性直接通过。"""
        ctx = {
            "order_id": "ORD-PASS-001",
            "order_status": "delivered",
            "elapsed_hours": 36.0,
            "is_opened": False,
        }
        decision = self.arbitrator.arbitrate(ctx)
        self.assertEqual(decision.reasoning_mode, "DETERMINISTIC")
        self.assertTrue(decision.is_eligible)
        self.assertEqual(decision.decision_code, "ELIGIBLE_7D_STANDARD")

    def test_hybrid_flow_dispute_arbitration(self) -> None:
        """测试争议订单激活第二阶段不确定性仲裁（D-S + 模糊）。"""
        ctx = {
            "order_id": "ORD-DISPUTE-002",
            "order_status": "delivered",
            "elapsed_hours": 175.0,           # 超期 7 小时 (微超期)
            "is_opened": True,                 # 已拆封
            "issue_type": "performance_failure", # 机器性能故障
            "buyer_credit_score": 90.0,       # 买家信誉优良
            "sensor_shock_g": 2.0,            # 运输平稳
            "defect_confidence": 0.85,        # 质检确认为出厂电路缺陷
            "defect_kind": "internal_circuit",
        }
        decision = self.arbitrator.arbitrate(ctx)
        self.assertEqual(decision.reasoning_mode, "HYBRID_NEURO_SYMBOLIC")
        self.assertTrue(decision.is_eligible)
        self.assertEqual(decision.decision_code, "SPECIAL_APPROVED")
        # 验证商家责任最高
        self.assertGreater(decision.responsibility_distribution["seller_fault"], 0.40)
        self.assertGreater(decision.compensation_amount, 10.0)
        self.assertIn("双轨智能仲裁", decision.human_summary)


if __name__ == "__main__":
    unittest.main()
