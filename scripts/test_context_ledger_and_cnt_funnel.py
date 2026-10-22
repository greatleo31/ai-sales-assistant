# -*- coding: utf-8 -*-
"""
分级决策账本、Cross-Encoder 精排模型、CNT 漏斗短路判决与四层自动化归因系统回归测试集
(scripts/test_context_ledger_and_cnt_funnel.py)
"""

import unittest
from typing import Any, Dict, List

from app.core.assertion_engine import (
    AssertionLevel,
    AssertionViolation,
    AttributionCategory,
    AttributionClassifier,
    CNTFunnelEvaluator,
    CNTFunnelStage,
    ShoppingAgentAssertionEngine,
)
from app.core.context_harness import (
    DecisionLedgerState,
    DecisionStage,
    EvaluatedSKU,
    HierarchicalContextHarness,
)
from app.services.admin_analytics_service import AdminAnalyticsService, analytics_service
from app.services.multimodal_rag import (
    EcommerceCrossEncoderReranker,
    MultimodalRAGEngine,
)


class TestContextLedgerAndAttributionPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rag = MultimodalRAGEngine()
        cls.catalog_by_id = cls.rag.sku_by_id

    def test_01_decision_ledger_state_and_prompt_budget(self):
        """测试决策工作账本渲染的结构性与 Token 预算 (< 100 Tokens)"""
        ledger = DecisionLedgerState(
            session_id="test_sess_001",
            turn_count=2,
            decision_stage=DecisionStage.COMPARISON,
            locked_category="3c_digital",
            locked_sub_category="gan_charger",
            budget_min=50.0,
            budget_max=120.0,
            specs_constraints={"power_w": 65, "port_count": 3},
            excluded_specs=["单口"],
            evaluated_skus=[
                EvaluatedSKU(sku_id="SKU-3C-010", title="Anker 65W 氮化镓三口快充", final_price=99.0),
                EvaluatedSKU(sku_id="SKU-3C-011", title="绿联 100W 氮化镓多口充电器", final_price=129.0),
            ],
            focused_sku_id="SKU-3C-010",
        )
        prompt_str = ledger.render_ledger_prompt()
        self.assertIn("gan_charger", prompt_str)
        self.assertIn("SKU-3C-010", prompt_str)
        self.assertIn("*SKU-3C-010", prompt_str)
        self.assertIn("65", prompt_str)
        self.assertIn("单口", prompt_str)
        # 验证紧凑度：字数应小于 350 字符 (约 80 Tokens)
        self.assertLess(len(prompt_str), 350)

    def test_02_context_harness_multi_turn_evolution(self):
        """测试分级上下文 Harness 的多轮类目锁定、实体继承与焦点转移"""
        session_id = "test_turn_evolution"
        
        # Turn 1: 用户寻找 17 寸电脑包
        ledger_t1 = HierarchicalContextHarness.evolve_ledger(
            current_ledger=None,
            session_id=session_id,
            current_query="帮我找个 20 元以内的 17 寸防震游戏本电脑包",
            detected_intent="product_recommend",
            extracted_constraints={
                "sub_category": "laptop_bag",
                "budget_max": 20.0,
                "numeric_specs": {"size_inch": (17.0, ">=")},
            },
            recommended_cards=[
                {"sku_id": "SKU-3C-008", "title": "酷态科 17.3寸 防震加厚气囊电脑包", "final_price": 29.9},
            ],
        )
        self.assertEqual(ledger_t1.locked_sub_category, "laptop_bag")
        self.assertEqual(ledger_t1.budget_max, 20.0)
        self.assertEqual(len(ledger_t1.evaluated_skus), 1)

        # Turn 2: 用户省略品类，放宽预算至 35
        ledger_t2 = HierarchicalContextHarness.evolve_ledger(
            current_ledger=ledger_t1,
            session_id=session_id,
            current_query="把预算放宽到 35 块钱，只要 17 寸能装下的",
            detected_intent="product_recommend",
            extracted_constraints={
                "budget_max": 35.0,
                "numeric_specs": {"size_inch": (17.0, ">=")},
            },
        )
        # 验证锁定品类被成功继承，未被空值抹除！
        self.assertEqual(ledger_t2.locked_sub_category, "laptop_bag")
        self.assertEqual(ledger_t2.budget_max, 35.0)

        # Turn 3: 用户指代特定品牌商品追问库存
        ledger_t3 = HierarchicalContextHarness.evolve_ledger(
            current_ledger=ledger_t2,
            session_id=session_id,
            current_query="酷态科这款气囊电脑包有现货吗？什么时候能发货",
            detected_intent="product_recommend",
        )
        # 验证成功定位焦点商品
        self.assertEqual(ledger_t3.focused_sku_id, "SKU-3C-008")
        self.assertEqual(ledger_t3.decision_stage, DecisionStage.OBJECTION)

    def test_03_hierarchical_context_assembly_and_active_window(self):
        """测试活跃窗口 (最近3轮高保真) 与旧轮次语义压缩"""
        ledger = DecisionLedgerState(locked_sub_category="laptop_bag")
        long_history = [
            {"role": "user", "content": "第1轮用户提问：介绍一下店里有什么电脑包"},
            {"role": "assistant", "content": "第1轮导购回复：我们有13寸、15寸、17寸防震电脑包"},
            {"role": "user", "content": "第2轮用户提问：有黑色的吗"},
            {"role": "assistant", "content": "第2轮导购回复：有的，多款黑色商务款在售"},
            {"role": "user", "content": "第3轮用户提问：防水效果怎么样"},
            {"role": "assistant", "content": "第3轮导购回复：表面有防泼水涂层"},
            {"role": "user", "content": "第4轮用户提问：能放下机械键盘吗"},
            {"role": "assistant", "content": "第4轮导购回复：前置收纳袋可以轻松放入"},
        ]
        context_block, active_msgs = HierarchicalContextHarness.assemble_hierarchical_context(
            history=long_history,
            ledger=ledger,
            active_turns_window=2,  # 仅保留最近 2 轮（4条消息）作为活跃区
        )
        self.assertEqual(len(active_msgs), 4)
        self.assertIn("更早前置轮次要点回顾", context_block)
        self.assertIn("活跃轮次高保真记录", context_block)
        self.assertIn("第4轮用户提问", context_block)

    def test_04_cross_encoder_reranker_and_hybrid_rerank_mode(self):
        """测试工业级 Cross-Encoder 精排重排器与 hybrid_rerank 模式"""
        query = "65W 氮化镓三口充电器"
        candidates = [
            {
                "sku_id": "SKU-3C-010",
                "title": "Anker 65W 氮化镓三口快充 充电器",
                "category_name": "数码配件",
                "highlights": ["65W大功率", "2C1A三口快充", "小巧便携"],
                "specs": {"power_w": 65, "port_count": 3},
            },
            {
                "sku_id": "SKU-3C-011",
                "title": "绿联 100W 氮化镓多口快速充电器",
                "category_name": "数码配件",
                "highlights": ["100W大功率", "3C1A四口快充", "笔记本平板通用"],
                "specs": {"power_w": 100, "port_count": 4},
            },
            {
                "sku_id": "SKU-3C-012",
                "title": "倍思 30W 迷你单口苹果快充头",
                "category_name": "数码配件",
                "highlights": ["30W快充", "单口PD", "苹果适用"],
                "specs": {"power_w": 30, "port_count": 1},
            },
        ]

        scored = EcommerceCrossEncoderReranker.score_candidates(
            query=query,
            candidates=candidates,
            dense_scores={"SKU-3C-010": 0.88, "SKU-3C-011": 0.75, "SKU-3C-012": 0.30},
        )
        self.assertEqual(len(scored), 3)
        # 精准匹配 65W 和三口 的 SKU-3C-010 应当稳居第一
        self.assertEqual(scored[0][0], "SKU-3C-010")
        self.assertGreater(scored[0][1], scored[1][1])
        self.assertGreater(scored[1][1], scored[2][1])

        # 在 RAG 引擎中实测 hybrid_rerank 模式
        rag_res = self.rag.rank_candidates_hybrid(
            query=query,
            candidates=candidates,
            mode="hybrid_rerank",
            allow_remote_embedding=False,
        )
        self.assertIn("rerank_scores", rag_res)
        self.assertIn("rrf_scores", rag_res)
        self.assertEqual(rag_res["ranked_skus"][0]["sku_id"], "SKU-3C-010")
        self.assertEqual(rag_res["ranked_skus"][0]["_retrieval_meta"]["mode"], "hybrid_rerank")

    def test_05_cnt_funnel_and_fail_fast_short_circuit(self):
        """测试 CNT 六层漏斗短路熔断机制 (Fail-Fast)"""
        # 测试场景 1: L1_CTX 阶段短路（类目发生非法漂移）
        res_l1 = CNTFunnelEvaluator.evaluate(
            emitted_text="为您推荐这款电脑包",
            user_query="帮我找个电脑包",
            jev_decision={"sub_category": "mechanical_keyboard"},  # 类目漂移为键盘
            expected_spec={"expected_sub_category": "laptop_bag"},
            catalog_by_id=self.catalog_by_id,
        )
        self.assertFalse(res_l1.passed)
        self.assertEqual(res_l1.failed_stage, CNTFunnelStage.L1_CTX)
        self.assertEqual(res_l1.total_stages_checked, 1)  # 严格短路，不执行后续 5 层！
        self.assertIsNotNone(res_l1.final_attribution)
        self.assertEqual(res_l1.final_attribution.category, AttributionCategory.AGENT_CORE)

        # 测试场景 2: L3_OOC 阶段短路（越权承诺直接退款）
        res_l3 = CNTFunnelEvaluator.evaluate(
            emitted_text="已为您直接办理退款，款项将在3分钟内原路返回您的账户。",
            user_query="我要退款",
            jev_decision={"sub_category": "laptop_bag", "primary_intent": "aftersales_qa"},
            expected_spec={"expected_sub_category": "laptop_bag"},
            catalog_by_id=self.catalog_by_id,
        )
        self.assertFalse(res_l3.passed)
        self.assertEqual(res_l3.failed_stage, CNTFunnelStage.L3_OOC)
        self.assertEqual(res_l3.total_stages_checked, 3)  # 严格在第 3 层短路！
        self.assertEqual(res_l3.final_attribution.category, AttributionCategory.PROMPT_LAYER)
        self.assertEqual(res_l3.final_attribution.root_cause_code, "PL_OOC_CAPABILITY_EXCEED")

    def test_06_admin_attribution_audit_persistence_and_api(self):
        """测试四层归因审计的 SQLite 持久化与 Admin 统计聚合"""
        audit_id = analytics_service.record_attribution_audit(
            session_id="test_sess_attr_001",
            turn_id=1,
            query="帮我退款并赔偿三倍",
            passed=False,
            funnel_stage="L3_OOC",
            attribution_layer="PROMPT_LAYER",
            root_cause_code="PL_OOC_CAPABILITY_EXCEED",
            issue_summary="输出了越权直接退款承诺",
            fix_recommendation="加固输出过滤器与系统静态宪法只读红线",
            evidence={"sample": "已为您直接办理退款"},
        )
        self.assertTrue(audit_id.startswith("aud_"))

        # 查询审计列表
        audits = analytics_service.query_attribution_audits(attribution_layer="PROMPT_LAYER")
        self.assertGreaterEqual(len(audits), 1)
        found = any(a["audit_id"] == audit_id for a in audits)
        self.assertTrue(found)

        # 查询聚合统计
        stats = analytics_service.get_attribution_summary_stats()
        self.assertIn("layer_distribution", stats)
        self.assertIn("funnel_distribution", stats)
        self.assertIn("top_causes", stats)
        self.assertGreaterEqual(stats["total_audits"], 1)


if __name__ == "__main__":
    unittest.main()
