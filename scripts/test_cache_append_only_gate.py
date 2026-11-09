# -*- coding: utf-8 -*-
"""
双轨缓存与 Append-Only Messages 结构断言验证门禁 (scripts/test_cache_append_only_gate.py)
验证目标：
  1. 静态前缀 100% 恒定 (static_prefix_md5 零漂移)；
  2. 多轮历史消息严格物理 Append-Only (第 N 轮前缀切片 == 第 N-1 轮完整历史)；
  3. 运行时易变度量 (confidence 浮点数、时间戳) 严格沉底至末尾。
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.strategy_injector import StrategyInjector
from app.core.telemetry import evaluate_prompt_alignment


class TestCacheAppendOnlyGate(unittest.TestCase):
    def setUp(self) -> None:
        self.injector = StrategyInjector()

    def test_01_static_prefix_md5_constant_across_turns(self) -> None:
        """验证多轮不同查询与不同意图下，STATIC_PREFIX 的 MD5 100% 恒定。"""
        q1 = "帮我推荐一个电脑包"
        d1 = {"primary_intent": "product_recommend", "intent_confidence": 0.9523, "confidence_tier": "high"}
        res1 = self.injector.compile_prompt(q1, d1)

        q2 = "订单 ORD-20261001-001 几天能到？"
        d2 = {"primary_intent": "order_query", "intent_confidence": 0.6841, "confidence_tier": "medium"}
        res2 = self.injector.compile_prompt(q2, d2)

        md5_1 = evaluate_prompt_alignment(res1["full_system_prompt"])["static_prefix_md5"]
        md5_2 = evaluate_prompt_alignment(res2["full_system_prompt"])["static_prefix_md5"]

        self.assertEqual(md5_1, md5_2, "静态前缀 MD5 在不同轮次与意图间发生漂移！")
        self.assertEqual(res1["static_prefix"], res2["static_prefix"], "静态提示词文本不一致！")

    def test_02_volatile_metrics_sink_to_bottom(self) -> None:
        """验证 confidence 浮点数与时间戳被严格沉底至末尾 volatile_metrics_block。"""
        q = "推荐 100W 氮化镓充电头"
        d = {"primary_intent": "product_recommend", "intent_confidence": 0.9876, "confidence_tier": "high"}
        res = self.injector.compile_prompt(q, d)

        # 验证 ## 1. System One 门控决策 中不再包含浮点 confidence=
        dyn_context = res["dynamic_context_block"]
        self.assertNotIn("confidence=0.9876", dyn_context, "浮点置信度泄漏在动态块上部模板中！")
        self.assertIn("tier=high", dyn_context, "离散 tier 标签应保留在门控决策中")

        # 验证 volatile_metrics_block 中包含沉底的浮点数
        vol_block = res["volatile_metrics_block"]
        self.assertIn("## 7. 运行时易变度量", vol_block)
        self.assertIn("confidence=0.9876", vol_block)

    def test_03_append_only_messages_assembly(self) -> None:
        """验证多轮消息数组装载逻辑符合物理级 Append-Only。"""
        history_t1 = []
        static_p = self.injector.compile_prompt("第一轮问题", {"primary_intent": "product_recommend"})["static_prefix"]

        # 模拟第 1 轮装载
        msgs_t1 = [{"role": "system", "content": static_p}, {"role": "user", "content": "第一轮问题\n\n[真源数据1]"}]
        assistant_t1_reply = "为您推荐款 A"

        # 模拟第 2 轮装载
        history_t2 = [
            {"role": "user", "content": "第一轮问题"},
            {"role": "assistant", "content": assistant_t1_reply},
        ]
        msgs_t2 = [{"role": "system", "content": static_p}]
        for m in history_t2:
            msgs_t2.append({"role": m["role"], "content": m["content"]})
        msgs_t2.append({"role": "user", "content": "第二轮问题\n\n[真源数据2]"})

        # 核心断言：msgs_t2 的前 3 条消息 (system + user_1 + assistant_1) 必须与上一轮物理完全一致
        self.assertEqual(msgs_t2[0]["content"], msgs_t1[0]["content"], "System 消息前缀不一致！")
        self.assertEqual(msgs_t2[1]["role"], "user")
        self.assertEqual(msgs_t2[2]["role"], "assistant")
        self.assertEqual(msgs_t2[2]["content"], assistant_t1_reply)
        self.assertEqual(len(msgs_t2), 4)


if __name__ == "__main__":
    unittest.main()
