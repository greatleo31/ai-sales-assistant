# -*- coding: utf-8 -*-
r"""
后台管理后端纯净重构与真实接口闭环验收测试套件
(scripts/test_clean_admin_backend.py)

验证范围：
  1. 数据库零数据纯净初始状态断言（0 条虚假会话、0 条虚假反馈、0 条虚假工单、0 条虚假心愿单）；
  2. 后台全套观测治理接口在零数据下的高健壮性（HTTP 200，空列表，零计数值，零异常）；
  3. C 端真实流式对话 (POST /api/v1/chat/stream) -> 后台会话 Trace 真实入库闭环；
  4. C 端真实好差评反馈 (POST /api/v1/feedback) -> 后台反馈中心实时聚合与管理员批注流转；
  5. 真实 12315 维权情绪与 handoff_guard 触发 -> 自动生成真实 P0 工单与工单办结流转；
  6. 真实缺货品类心愿单 (POST /api/v1/wishlist) -> 后台心愿单列表与状态更新；
  7. 全景数据看板、RAG 检索松弛分析、Token 算力账单完全由真实交互驱动更新。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi.testclient import TestClient
from app.main import app
from app.services.admin_analytics_service import (
    get_admin_analytics_service,
    get_static_constitution_metrics,
)


def _parse_sse(raw_text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        ev = "message"
        data_s = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data_s = line[5:].strip()
        if data_s == "[DONE]":
            payload: Any = "[DONE]"
        else:
            try:
                payload = json.loads(data_s)
            except Exception:
                payload = data_s
        events.append({"event": ev, "data": payload})
    return events


class TestCleanAdminBackend(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.analytics = get_admin_analytics_service()
        # 确保以 100% 纯净零数据基线启动
        cls.analytics.reset_to_baseline()

    def test_01_verify_pure_clean_baseline_zero_data(self) -> None:
        """测试 1：验证数据库纯净初始化状态，绝无任何 mock 假数据残留。"""
        # 检查各表统计
        overview = self.analytics.get_dashboard_overview()
        summary = overview["summary"]
        self.assertEqual(summary["total_messages"], 0, "会话总数必须为 0")
        self.assertEqual(summary["total_sessions"], 0, "Session 总数必须为 0")
        self.assertEqual(summary["total_tokens"], 0, "Token 总数必须为 0")
        self.assertEqual(summary["feedback_total"], 0, "反馈总数必须为 0")
        self.assertEqual(summary["handoff_turns"], 0, "工单触发数必须为 0")
        self.assertEqual(overview["daily_trend"], [], "每日趋势必须为空列表")
        self.assertEqual(overview["intent_distribution"], [], "意图分布必须为空列表")
        self.assertEqual(overview["top_active_ips"], [], "Top IP 必须为空列表")

        # 检查审计日志、反馈、工单、心愿单
        self.assertEqual(self.analytics.list_audit_logs()["total"], 0)
        self.assertEqual(self.analytics.list_feedbacks()["total"], 0)
        self.assertEqual(self.analytics.list_handoff_tickets()["summary"]["total_tickets"], 0)
        self.assertEqual(self.analytics.list_wishlist_hooks()["summary"]["total_categories"], 0)

        # 验证静态前缀 MD5 为动态真实计算值
        real_md5, real_tokens = get_static_constitution_metrics()
        self.assertEqual(summary["static_prefix_md5"], real_md5)
        self.assertEqual(len(real_md5), 32)
        self.assertGreater(real_tokens, 2000)

    def test_02_all_admin_endpoints_return_200_on_zero_data(self) -> None:
        """测试 2：验证所有后台监控与管理 API 在零数据下均正常响应 HTTP 200 且返回结构健全。"""
        endpoints = [
            "/api/v1/admin/dashboard",
            "/api/v1/admin/conversations",
            "/api/v1/admin/feedbacks",
            "/api/v1/admin/handoffs",
            "/api/v1/admin/rag-analytics",
            "/api/v1/admin/token-analytics",
            "/api/v1/admin/wishlists",
            "/api/v1/admin/runtime-status",
            "/api/v1/admin/context-manifest",
            "/api/v1/admin/catalog",
            "/api/v1/admin/eval-summary",
            "/api/v1/admin/benchmark-queries",
        ]
        for ep in endpoints:
            resp = self.client.get(ep)
            self.assertEqual(resp.status_code, 200, f"接口 {ep} 应该返回 200，实际返回 {resp.status_code}")
            data = resp.json()
            self.assertIsInstance(data, dict, f"接口 {ep} 返回必须为 dict")

        # 校验 runtime-status 的真实动态性
        run_res = self.client.get("/api/v1/admin/runtime-status").json()
        real_md5, real_tokens = get_static_constitution_metrics()
        self.assertEqual(run_res["status"], "ok")
        self.assertEqual(run_res["static_prefix_md5"], real_md5)
        self.assertEqual(run_res["static_prefix_tokens"], real_tokens)
        self.assertEqual(run_res["sku_count"], 60)

    def test_03_real_chat_stream_populates_audit_trace(self) -> None:
        """测试 3：发起真实导购对话，验证后台 Trace 审计真实录入客户端 IP、意图、SKU 及耗时。"""
        client_ip = "113.108.99.66"  # 深圳电信
        resp = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
                "session_id": "real-test-session-001",
                "allow_remote_llm": False,  # 确定性高质量零延迟生成
            },
            headers={"X-Forwarded-For": client_ip, "User-Agent": "TestClient/Real-Audit-Agent"},
        )
        self.assertEqual(resp.status_code, 200)
        events = _parse_sse(resp.text)
        self.assertTrue(any(e["event"] == "decision_meta" for e in events))
        self.assertTrue(any(e["event"] == "ui_cards" for e in events))
        self.assertTrue(any(e["event"] == "telemetry" for e in events))

        meta = next(e["data"] for e in events if e["event"] == "decision_meta")
        msg_id = meta["message_id"]

        # 后台审计接口查询
        logs_res = self.client.get("/api/v1/admin/conversations").json()
        self.assertEqual(logs_res["total"], 1, "必须真实新增 1 条审计日志")
        log_item = logs_res["items"][0]
        self.assertEqual(log_item["message_id"], msg_id)
        self.assertEqual(log_item["client_ip"], client_ip)
        self.assertIn("深圳", log_item["ip_region"])
        self.assertEqual(log_item["primary_intent"], "product_recommend")
        self.assertEqual(log_item["constraint_status"], "relaxed")
        self.assertIn("SKU-3C-008", log_item["top_skus"])
        self.assertGreater(log_item["total_tokens"], 2000)

    def test_04_real_customer_feedback_flow(self) -> None:
        """测试 4：客户对上一条会话进行真实好评点赞，验证反馈中心实时统计。"""
        # 读取刚才生成的 message_id
        logs = self.client.get("/api/v1/admin/conversations").json()
        self.assertGreaterEqual(logs["total"], 1)
        msg_id = logs["items"][0]["message_id"]

        fb_resp = self.client.post(
            "/api/v1/feedback",
            json={
                "message_id": msg_id,
                "session_id": "real-test-session-001",
                "rating": "like",
                "reason_tags": ["推荐方案合理", "参数对比直观"],
                "comment": "双路径松弛非常专业！",
            },
            headers={"X-Forwarded-For": "113.108.99.66"},
        )
        self.assertEqual(fb_resp.status_code, 200)
        fb_id = fb_resp.json()["feedback"]["feedback_id"]

        # 查后台反馈中心
        feedbacks = self.client.get("/api/v1/admin/feedbacks").json()
        self.assertEqual(feedbacks["total"], 1)
        self.assertEqual(feedbacks["likes"], 1)
        self.assertEqual(feedbacks["dislikes"], 0)
        self.assertEqual(feedbacks["satisfaction_rate_pct"], 100.0)
        self.assertEqual(len(feedbacks["reason_tag_breakdown"]), 2)

        # 管理员审核批注
        patch_res = self.client.patch(
            f"/api/v1/admin/feedbacks/{fb_id}",
            json={"status": "resolved", "admin_note": "选品算法表现优异已归档"},
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["feedback"]["status"], "resolved")

    def test_05_real_handoff_and_ticket_resolution(self) -> None:
        """测试 5：真实 12315 维权情绪触发 handoff_guard，验证 P0 工单自动生成与处理。"""
        angry_resp = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "充电器刚用就发烫冒烟了！怀疑是假货！立刻找你们店长出来赔钱，不然我马上打12315消协举报！",
                "session_id": "real-angry-session-002",
                "allow_remote_llm": False,
            },
            headers={"X-Forwarded-For": "223.72.88.19"},
        )
        self.assertEqual(angry_resp.status_code, 200)

        # 查后台工单台
        tickets_res = self.client.get("/api/v1/admin/handoffs").json()
        self.assertEqual(tickets_res["summary"]["total_tickets"], 1)
        self.assertEqual(tickets_res["summary"]["open_tickets"], 1)
        ticket = tickets_res["items"][0]
        self.assertEqual(ticket["priority"], "P0_URGENT")
        self.assertEqual(ticket["status"], "open")
        self.assertTrue(
            "维权" in ticket["trigger_reason"] or "handoff_guard" in ticket["trigger_reason"],
            f"trigger_reason 应该包含维权或 handoff_guard，实际为: {ticket['trigger_reason']}"
        )

        # 管理员办结工单
        patch_ticket = self.client.patch(
            f"/api/v1/admin/handoffs/{ticket['ticket_id']}",
            json={
                "status": "resolved",
                "assigned_specialist": "VIP售后主管-林夏",
                "resolution_note": "已为客户安排全额退赔与礼品关怀",
            },
        )
        self.assertEqual(patch_ticket.status_code, 200)
        self.assertEqual(patch_ticket.json()["ticket"]["status"], "resolved")

    def test_06_real_wishlist_flow(self) -> None:
        """测试 6：真实缺货心愿单登记与后台采买状态流转。"""
        wish_res = self.client.post(
            "/api/v1/wishlist",
            json={
                "missing_item": "可视空气炸锅 5L",
                "user_query": "请问有 5L 的可视空气炸锅吗？",
                "expected_budget": "¥200 - ¥350",
                "user_subscribed": True,
            },
            headers={"X-Forwarded-For": "101.81.55.22"},
        )
        self.assertEqual(wish_res.status_code, 200)
        hook_id = wish_res.json()["wishlist"]["hook_id"]

        # 后台心愿单列表查询
        wish_list = self.client.get("/api/v1/admin/wishlists").json()
        self.assertEqual(wish_list["summary"]["total_categories"], 1)
        self.assertEqual(wish_list["summary"]["subscribed_categories"], 1)

        # 管理员更新采买进展
        update_wish = self.client.patch(
            f"/api/v1/admin/wishlists/{hook_id}",
            json={"status": "sourcing", "admin_note": "已联系九阳官方买手排期"},
        )
        self.assertEqual(update_wish.status_code, 200)
        self.assertEqual(update_wish.json()["wishlist"]["status"], "sourcing")

    def test_07_dashboard_and_analytics_driven_by_real_data(self) -> None:
        """测试 7：验证全景看板、RAG 松弛分析与 Token 账单均完全由上述真实数据驱动计算。"""
        overview = self.client.get("/api/v1/admin/dashboard").json()
        summary = overview["summary"]
        # 刚才共发起 2 次流式对话（1 次选品松弛，1 次 12315 维权）
        self.assertEqual(summary["total_messages"], 2, "总请求数必须严格等于真实的 2 次")
        self.assertEqual(summary["total_sessions"], 2)
        self.assertEqual(summary["unique_ips"], 2)
        self.assertEqual(summary["relaxed_turns"], 1)
        self.assertEqual(summary["handoff_turns"], 1)
        self.assertEqual(summary["feedback_total"], 1)
        self.assertEqual(summary["feedback_likes"], 1)
        self.assertEqual(summary["satisfaction_rate_pct"], 100.0)
        self.assertEqual(len(overview["top_active_ips"]), 2)

        # RAG 分析接口
        rag_res = self.client.get("/api/v1/admin/rag-analytics").json()
        self.assertEqual(rag_res["summary"]["total_rag_queries"], 1)
        self.assertEqual(rag_res["summary"]["relaxed_hits"], 1)
        self.assertEqual(rag_res["summary"]["relaxation_trigger_rate_pct"], 100.0)

        # Token 账单接口
        token_res = self.client.get("/api/v1/admin/token-analytics").json()
        self.assertEqual(token_res["summary"]["total_turns"], 2)
        self.assertGreater(token_res["summary"]["total_tokens"], 4000)
        self.assertEqual(token_res["summary"]["md5_alignment_rate_pct"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
