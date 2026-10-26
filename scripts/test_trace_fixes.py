# -*- coding: utf-8 -*-
"""
验证之前真实 Trace 发现的 3 大核心问题彻底修复测试套件 (scripts/test_trace_fixes.py)
1. 单品代词追问漂移修复（17寸包追问现货/发货，锁定 ROG 17.3寸，严禁漂移到小米 16寸）
2. 多轮候选比价锁定修复（氮化镓追问“那个更便宜的呢？”，锁定安克35元/努比亚45元，严禁宣称倍思89元最便宜）
3. 情绪累加与连续叹号转人工（“发货这么慢”紧跟“!!”与“!闭嘴”，连续升级100%触发 handoff_guard）
"""
import unittest
import asyncio
from app.services.rule_governance import EmotionTracker
from app.core.chat_orchestrator import ShoppingAgentOrchestrator


class TestTraceFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orchestrator = ShoppingAgentOrchestrator(context_window_limit=272000)

    def test_01_emotion_punctuation_and_consecutive_complaint_escalation(self):
        """测试连续抱怨与连续叹号触发 handoff_guard"""
        # 1. 单独的 '发货这么慢'
        res1 = EmotionTracker.detect([], current_query="发货这么慢")
        self.assertEqual(res1.emotion_type, "negative")
        # 2. 紧接着发 '!!' 连续叹号催促 -> 必须触发 handoff
        history = [
            {"role": "user", "content": "发货这么慢"},
            {"role": "assistant", "content": "您的订单 ORD-20261001-001 目前运输中..."},
        ]
        res2 = EmotionTracker.detect(history, current_query="!!")
        self.assertTrue(res2.trigger_handoff, "连续叹号未触发转人工")
        self.assertEqual(res2.emotion_type, "negative")
        self.assertGreaterEqual(res2.intensity, 0.90)

        # 3. 发 '!闭嘴' -> 必须触发 handoff
        res3 = EmotionTracker.detect(history, current_query="!闭嘴")
        self.assertTrue(res3.trigger_handoff, "!闭嘴未触发转人工")
        self.assertEqual(res3.emotion_type, "negative")

    def test_02_pronoun_single_item_locking(self):
        """测试单品代词追问锁定上一轮主推款，杜绝漂移到小米 16寸"""
        async def _run():
            ses_id = "test-pronoun-fix-ses-01"
            # Turn 1: 推荐 17寸电脑包
            events_1 = []
            async for ev_raw in self.orchestrator.stream_chat(
                query="推荐一款17寸双肩电脑包",
                session_id=ses_id,
                allow_remote_llm=False,
            ):
                events_1.append(ev_raw)

            # Turn 2: 这款包有现货吗？一般从哪发货、几天能到
            events_2 = []
            async for ev_raw in self.orchestrator.stream_chat(
                query="这款包有现货吗？一般从哪发货、几天能到",
                session_id=ses_id,
                history=[
                    {"role": "user", "content": "推荐一款17寸双肩电脑包"},
                    {"role": "assistant", "content": "针对17寸双肩电脑包，推荐ROG硬壳装甲防震双肩包(SKU-3C-007)..."},
                ],
                allow_remote_llm=False,
            ):
                events_2.append(ev_raw)

            # 解析 Turn 2 的 ui_cards
            cards_ev = None
            for ev in events_2:
                if ev.startswith("event: ui_cards"):
                    lines = ev.strip().split("\n")
                    for l in lines:
                        if l.startswith("data: "):
                            import json
                            cards_ev = json.loads(l[6:])
                            break

            self.assertIsNotNone(cards_ev)
            p_cards = cards_ev.get("product_cards", [])
            self.assertEqual(len(p_cards), 1, f"锁定款式时不应发散多张卡片: {len(p_cards)}")
            self.assertEqual(p_cards[0]["sku_id"], "SKU-3C-007", f"款式错误漂移到了: {p_cards[0]['sku_id']}")
            self.assertIn("ROG", p_cards[0]["title"])
            self.assertNotIn("小米", p_cards[0]["title"], "代词追问被错误偷换为小米 16寸包")

        asyncio.run(_run())

    def test_03_comparative_cheaper_item_locking(self):
        """测试比价代词追问锁定前序更便宜候选，杜绝颠倒价格"""
        async def _run():
            ses_id = "test-cheaper-fix-ses-02"
            # Turn 1: 推荐氮化镓充电头
            events_1 = []
            async for ev_raw in self.orchestrator.stream_chat(
                query="找个性价比高的氮化镓充电头，不要超过 100 块钱，",
                session_id=ses_id,
                allow_remote_llm=False,
            ):
                events_1.append(ev_raw)

            # Turn 2: 那个更便宜的呢？
            events_2 = []
            async for ev_raw in self.orchestrator.stream_chat(
                query="那个更便宜的呢？",
                session_id=ses_id,
                history=[
                    {"role": "user", "content": "找个性价比高的氮化镓充电头，不要超过 100 块钱，"},
                    {"role": "assistant", "content": "推荐安克Nano3 30W(¥35)、努比亚65W(¥45)、倍思65W(¥89)..."},
                ],
                allow_remote_llm=False,
            ):
                events_2.append(ev_raw)

            # 解析 Turn 2 的 ui_cards
            cards_ev = None
            for ev in events_2:
                if ev.startswith("event: ui_cards"):
                    lines = ev.strip().split("\n")
                    for l in lines:
                        if l.startswith("data: "):
                            import json
                            cards_ev = json.loads(l[6:])
                            break

            self.assertIsNotNone(cards_ev)
            p_cards = cards_ev.get("product_cards", [])
            self.assertGreaterEqual(len(p_cards), 1)
            # 首款必须是最便宜的安克 Nano3 30W (SKU-3C-001, ¥35)
            self.assertEqual(p_cards[0]["sku_id"], "SKU-3C-001")
            self.assertEqual(p_cards[0]["final_price"], 35.0)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
