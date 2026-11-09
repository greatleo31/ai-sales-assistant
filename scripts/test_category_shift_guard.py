# -*- coding: utf-8 -*-
"""
跨轮品类状态树与重置守卫自动化验证测试 (scripts/test_category_shift_guard.py)
验证：
  1. 品类跃迁 (Category Shift): 从电脑包 (laptop_bag) 跳转到冲锋衣/耳机等新类目时：
     - 活跃候选集 `evaluated_skus` 自动物理清空并移入 `archived_skus`；
     - 焦点实体 `focused_sku_id` 重置为 None；
     - 强绑定的旧规格约束（如 17.3寸游戏本屏幕）物理清空，杜绝串扰新类目；
     - 决策阶段自动回退至 EXPLORATION。
  2. 端到端多轮流式会话品类隔离：
     - 第 1 轮推荐 17寸电脑包；
     - 第 2 轮显式要求“换个冲锋衣”；
     - 第 3 轮使用单品代词“这款有现货吗”，严禁指代回溯至第 1 轮的电脑包。
  3. Token 计量引擎对 ds4.1-flash (DeepSeek) 的校准支持与 100% 离线确定性。
"""

import unittest
import asyncio
from app.core.context_harness import (
    DecisionLedgerState,
    DecisionStage,
    EvaluatedSKU,
    HierarchicalContextHarness,
)
from app.core.chat_orchestrator import ShoppingAgentOrchestrator
from app.core.telemetry import (
    count_tokens_exact,
    evaluate_prompt_alignment,
    reset_baseline_prefix_md5,
    BOUNDARY_MARKER,
)
from scripts.test_module4_pipeline import parse_sse_stream_text


class TestCategoryShiftGuard(unittest.TestCase):
    def test_01_ledger_state_category_shift_isolation(self):
        """测试账本在品类跃迁时物理清空候选池与旧品类规格"""
        ledger = DecisionLedgerState(
            session_id="test_cat_shift_01",
            turn_count=1,
            decision_stage=DecisionStage.OBJECTION,
            locked_sub_category="laptop_bag",
            budget_max=250.0,
            specs_constraints={"screen_size": 17.3, "waterproof": True},
            excluded_specs=["手提"],
            evaluated_skus=[
                EvaluatedSKU(
                    sku_id="SKU-3C-008",
                    title="酷态科 17.3寸加厚气囊防震游戏本电脑包",
                    final_price=29.9,
                    status="focused",
                )
            ],
            focused_sku_id="SKU-3C-008",
        )

        # 模拟用户在第 2 轮转向“冲锋衣”
        new_constraints = {
            "sub_category": "outdoor_jacket",
            "budget_max": 500.0,
            "numeric_specs": {},
            "enum_specs": {"color": "黑色"},
        }

        updated_ledger = HierarchicalContextHarness.evolve_ledger(
            current_ledger=ledger,
            session_id="test_cat_shift_01",
            current_query="换一款冲锋衣吧，要黑色的",
            detected_intent="product_recommend",
            extracted_constraints=new_constraints,
            recommended_cards=None,
        )

        # 1. 类目正确切换
        self.assertEqual(updated_ledger.locked_sub_category, "outdoor_jacket")
        # 2. 活跃候选池已物理清空
        self.assertEqual(len(updated_ledger.evaluated_skus), 0)
        # 3. 历史电脑包移入归档池
        self.assertEqual(len(updated_ledger.archived_skus), 1)
        self.assertEqual(updated_ledger.archived_skus[0].sku_id, "SKU-3C-008")
        # 4. 焦点实体被重置
        self.assertIsNone(updated_ledger.focused_sku_id)
        # 5. 电脑包 17.3 寸规格已被物理清空，仅保留当前冲锋衣颜色规格
        self.assertNotIn("screen_size", updated_ledger.specs_constraints)
        self.assertEqual(updated_ledger.specs_constraints.get("color"), "黑色")
        # 6. 排除项清空
        self.assertEqual(len(updated_ledger.excluded_specs), 0)
        # 7. 决策阶段回退至 EXPLORATION
        self.assertEqual(updated_ledger.decision_stage, DecisionStage.EXPLORATION)

    def test_02_e2e_cross_category_pronoun_no_drift(self):
        """测试端到端会话在品类切换后，单品代词绝不回溯旧品类商品"""
        orchestrator = ShoppingAgentOrchestrator(context_window_limit=272000)

        async def _run():
            ses_id = "test-shift-ses-99"
            # Turn 1: 推荐 17寸电脑包
            events_1 = []
            async for ev in orchestrator.stream_chat(
                query="推荐一款17寸双肩电脑包",
                session_id=ses_id,
                allow_remote_llm=False,
            ):
                events_1.append(ev)

            # Turn 2: 换成冲锋衣
            events_2 = []
            async for ev in orchestrator.stream_chat(
                query="不要电脑包了，帮我找一件户外防风防泼水冲锋衣",
                session_id=ses_id,
                history=[
                    {"role": "user", "content": "推荐一款17寸双肩电脑包"},
                    {"role": "assistant", "content": "为您推荐ROG 17.3寸游戏本电脑包..."},
                ],
                allow_remote_llm=False,
            ):
                events_2.append(ev)

            # 解析 Turn 2 卡片
            parsed_2 = parse_sse_stream_text("".join(events_2))
            ev_map_2 = {k: v for k, v in parsed_2}
            cards_2 = ev_map_2.get("ui_cards", {}).get("product_cards", [])
            # 确认 Turn 2 绝无电脑包
            for c in cards_2:
                self.assertNotIn("电脑包", c.get("title", ""), "冲锋衣轮次中混入了上一轮电脑包")

            # Turn 3: 这款几天能到
            events_3 = []
            async for ev in orchestrator.stream_chat(
                query="这款几天能到？从哪里发货",
                session_id=ses_id,
                history=[
                    {"role": "user", "content": "推荐一款17寸双肩电脑包"},
                    {"role": "assistant", "content": "为您推荐ROG 17.3寸游戏本电脑包..."},
                    {"role": "user", "content": "不要电脑包了，帮我找一件户外防风防泼水冲锋衣"},
                    {"role": "assistant", "content": "针对户外冲锋衣，为您推荐伯希和户外硬壳冲锋衣..."},
                ],
                allow_remote_llm=False,
            ):
                events_3.append(ev)

            parsed_3 = parse_sse_stream_text("".join(events_3))
            ev_map_3 = {k: v for k, v in parsed_3}
            cards_3 = ev_map_3.get("ui_cards", {}).get("product_cards", [])
            # 断言：第 3 轮代词追问锁定款式时，绝不能是电脑包
            for c in cards_3:
                self.assertNotIn("电脑包", c.get("title", ""), "第 3 轮代词追问被错误指代回第 1 轮电脑包")

        asyncio.run(_run())

    def test_03_telemetry_deepseek_and_offline_exact_counting(self):
        """测试 Token 计量器对 ds4.1-flash / DeepSeek 模型的适配支持与前缀对齐率"""
        test_text = "帮我找一个 15.6 英寸防震双肩电脑包，预算 100 元以内"
        base_tokens = count_tokens_exact(test_text)
        ds_tokens = count_tokens_exact(test_text, model="ds4.1-flash")

        self.assertGreater(base_tokens, 10)
        self.assertGreater(ds_tokens, 8)
        # DeepSeek 对中文常用词具有更高压缩率，计数值应合理校准
        self.assertLessEqual(ds_tokens, base_tokens)

        # 验证 Boundary 切分对齐保持 100% 恒定
        reset_baseline_prefix_md5(None)
        full_prompt = (
            "【静态导购宪法】保持绝对一致\n"
            + BOUNDARY_MARKER
            + "\n【动态上下文】当前轮次"
        )
        metrics = evaluate_prompt_alignment(full_prompt)
        self.assertTrue(metrics["prefix_cache_aligned"])
        self.assertEqual(metrics["tokenizer"], "tiktoken_cl100k_base")


if __name__ == "__main__":
    unittest.main()
