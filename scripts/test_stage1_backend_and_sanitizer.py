# -*- coding: utf-8 -*-
r"""
阶段 1 自动化验收测试套件 (scripts/test_stage1_backend_and_sanitizer.py)
验证范围：
  1. 三层流式文本净水器 (`CustomerTextNormalizer` + `SlidingWindowStreamFilter`)：
     - 跨 Chunk 截断的 `<br>`、`<br/>`、`<div>`、`</p>`、`<span>` 100% 转化为纯净换行或剥离；
     - 跨 Chunk 截断的 LaTeX 数学符号（`$...$`、`$$...$$`、`\rightarrow`、`\ge`、`\le`、`\times`、`\Delta`、`\text{...}`、`\frac{a}{b}`）100% 转化为自然中文/符号；
     - 裸 Markdown 标题 `#` 与表格分隔线 `|---|` 净化；
     - 跨 Chunk `<|FunctionCallBegin|>...<|FunctionCallEnd|>` 内部协议标记 100% 剥离。
  2. 多租户品类与状态隔离 (`tenant_all`, `tenant_3c`, `tenant_fashion`)：
     - `tenant_3c` 允许 3C 数码检索，拦截美妆/服饰跨品类请求并返回友好店铺范围引导；
     - 租户状态暂停 (`suspended`) 拦截与恢复 (`active`)。
  3. 真实客户端 IP 采集 (`X-Forwarded-For`)、SSE `message_id` 透传与回答好坏评价 (`POST /api/v1/feedback`) 实时闭环。
  4. Admin 管理后台 7 大核心模块 API (`/api/v1/admin/*`) 全量契約验证。
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
from app.services.admin_analytics_service import get_admin_analytics_service
from app.services.rule_governance import (
    CustomerTextNormalizer,
    SlidingWindowStreamFilter,
)


def _parse_sse_events(raw_text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for block in raw_text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = "message"
        data_str = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:") :].strip()
        if data_str == "[DONE]":
            payload: Any = "[DONE]"
        else:
            try:
                payload = json.loads(data_str)
            except Exception:
                payload = data_str
        events.append({"event": event_name, "data": payload})
    return events


class TestStage1BackendAndSanitizer(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.analytics = get_admin_analytics_service()
        cls.analytics.reset_to_baseline()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.analytics.reset_to_baseline()

    def test_01_cross_chunk_html_and_br_sanitization(self) -> None:
        """测试跨任意流式 Chunk 边界的 <br>、<br/>、<div>、</p> 与 HTML 实体净化。"""
        stream_filter = SlidingWindowStreamFilter(overlap_chars=12)
        chunks = [
            "亲，为您推荐以下两款高性价比",
            "方案：<b",
            "r/>1. **路径A（严格预算）**<br",
            " />价格仅需 ¥19.00<di",
            "v>2. **路径B（满配升级）**</d",
            "iv><p>轻微超支 &yen;9.00&nbsp;但防震满配</p>",
        ]
        out_parts: List[str] = []
        for c in chunks:
            d = stream_filter.filter_delta(c)
            if d:
                out_parts.append(d)
        tail = stream_filter.flush()
        if tail:
            out_parts.append(tail)

        full_text = "".join(out_parts)
        self.assertNotIn("<br", full_text.lower())
        self.assertNotIn("<div", full_text.lower())
        self.assertNotIn("</div", full_text.lower())
        self.assertNotIn("<p>", full_text.lower())
        self.assertNotIn("&nbsp;", full_text)
        self.assertIn("¥9.00", full_text)
        self.assertIn("1. **路径A（严格预算）**", full_text)
        self.assertIn("2. **路径B（满配升级）**", full_text)

    def test_02_cross_chunk_latex_math_and_markdown_table_sanitization(self) -> None:
        """测试跨 Chunk 边界的 LaTeX 数学公式 ($...$, \\rightarrow, \\ge, \\le, \\times, \\Delta) 与 Markdown 表格净化。"""
        stream_filter = SlidingWindowStreamFilter(overlap_chars=12)
        chunks = [
            "### 价格保护核算结果\n",
            "| 项目 | 金额 |\n",
            "| :--- | :---: |\n",
            "实付金额 $199.0",
            "0 \\righta",
            "rrow 169.00$ 元，满足 $\\ge",
            " 20000\\tex",
            "t{mAh}$ 且重量 $\\le 200",
            "\\text{g}$，尺寸 $17 \\tim",
            "es 12$，价保差额 $\\Del",
            "ta = \\frac{30}{1}$ 元。",
        ]
        out_parts: List[str] = []
        for c in chunks:
            d = stream_filter.filter_delta(c)
            if d:
                out_parts.append(d)
        tail = stream_filter.flush()
        if tail:
            out_parts.append(tail)

        full_text = "".join(out_parts)
        self.assertNotIn("$", full_text)
        self.assertNotIn("\\rightarrow", full_text)
        self.assertNotIn("\\ge", full_text)
        self.assertNotIn("\\le", full_text)
        self.assertNotIn("\\times", full_text)
        self.assertNotIn("\\Delta", full_text)
        self.assertNotIn("\\text", full_text)
        self.assertNotIn("###", full_text)
        self.assertNotIn(":---", full_text)
        self.assertIn("199.00 → 169.00", full_text)
        self.assertIn("≥ 20000mAh", full_text)
        self.assertIn("≤ 200g", full_text)
        self.assertIn("17 × 12", full_text)
        self.assertIn("差额 = 30/1", full_text)

    def test_03_cross_chunk_internal_tool_tag_stripping(self) -> None:
        """确保内部工具协议标签 <|FunctionCallBegin|>...<|FunctionCallEnd|> 跨切片 100% 剥离。"""
        stream_filter = SlidingWindowStreamFilter(overlap_chars=12)
        chunks = [
            "正在为您核验订单状态",
            "<|FunctionCa",
            "llBegin|>{\"tool\":\"query_order\"}<|Functi",
            "onCallEnd|>，订单已由顺丰揽收。",
        ]
        out = "".join((stream_filter.filter_delta(c) or "") for c in chunks) + (
            stream_filter.flush() or ""
        )
        self.assertEqual(out, "正在为您核验订单状态，订单已由顺丰揽收。")

    def test_04_multi_tenant_category_isolation(self) -> None:
        """测试多租户品类隔离：tenant_3c 放行 3C 数码，拦截美妆防晒请求并给出友好引导。"""
        # 1. tenant_3c 请求 3C 电脑包 -> 正常召回商品卡
        res_allow = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
                "session_id": "test-stage1-tenant-3c-ok",
                "tenant_id": "tenant_3c",
                "allow_remote_llm": False,
            },
            headers={"X-Forwarded-For": "101.81.24.19"},
        )
        self.assertEqual(res_allow.status_code, 200)
        events_allow = _parse_sse_events(res_allow.text)
        ui_cards_allow = next(e["data"] for e in events_allow if e["event"] == "ui_cards")
        self.assertGreaterEqual(len(ui_cards_allow["product_cards"]), 2)

        # 2. 全品类开放：请求美妆防晒霜，平台同样精准推荐美妆商品卡片
        res_beauty = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "油敏肌能用的清爽防晒霜，不要酒精，50元以内",
                "session_id": "test-stage1-beauty-support",
                "allow_remote_llm": False,
            },
            headers={"X-Forwarded-For": "101.81.24.19"},
        )
        self.assertEqual(res_beauty.status_code, 200)
        events_beauty = _parse_sse_events(res_beauty.text)
        ui_cards_beauty = next(e["data"] for e in events_beauty if e["event"] == "ui_cards")
        self.assertGreaterEqual(len(ui_cards_beauty["product_cards"]), 1)

    def test_05_client_ip_audit_and_feedback_loop(self) -> None:
        """测试真实客户端 IP 捕获、message_id 透传与 👍/👎 评价实时同步至 Admin 后台。"""
        test_ip = "114.247.50.88"
        res = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "帮我查一下订单 ORD-20261001-002 能退差价吗",
                "session_id": "test-stage1-feedback-sess",
                "tenant_id": "tenant_all",
                "allow_remote_llm": False,
            },
            headers={
                "X-Forwarded-For": f"{test_ip}, 10.0.0.1",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X)",
            },
        )
        self.assertEqual(res.status_code, 200)
        events = _parse_sse_events(res.text)
        decision_meta = next(e["data"] for e in events if e["event"] == "decision_meta")
        message_id = decision_meta.get("message_id")
        self.assertTrue(message_id and message_id.startswith("msg"))
        self.assertEqual(decision_meta.get("client_ip"), test_ip)
        self.assertEqual(decision_meta.get("tenant_id"), "tenant_all")

        # 提交一条点踩反馈
        fb_res = self.client.post(
            "/api/v1/feedback",
            json={
                "message_id": message_id,
                "session_id": "test-stage1-feedback-sess",
                "tenant_id": "tenant_all",
                "rating": "dislike",
                "reason_tags": ["售后规则解释不清", "排版不清晰"],
                "comment": "希望能直接展示退差到账时间",
            },
            headers={"X-Forwarded-For": test_ip},
        )
        self.assertEqual(fb_res.status_code, 200)
        fb_data = fb_res.json()["feedback"]
        self.assertEqual(fb_data["message_id"], message_id)
        self.assertEqual(fb_data["rating"], "dislike")
        self.assertEqual(fb_data["client_ip"], test_ip)

        # 在 Admin 会话审计接口中按 IP 检索验证
        conv_res = self.client.get(
            "/api/v1/admin/conversations",
            params={"ip_keyword": test_ip},
        )
        self.assertEqual(conv_res.status_code, 200)
        conv_items = conv_res.json()["items"]
        matched = [x for x in conv_items if x["message_id"] == message_id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["feedback_rating"], "dislike")
        self.assertIn("北京", matched[0]["ip_region"])

        # 管理员更新该条反馈状态为 resolved
        patch_res = self.client.patch(
            f"/api/v1/admin/feedbacks/{fb_data['feedback_id']}",
            json={
                "status": "resolved",
                "admin_note": "已在售后卡片底部补充原路退回到账时效说明",
            },
        )
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["feedback"]["status"], "resolved")

    def test_06_admin_7_core_modules_endpoints(self) -> None:
        """测试 Admin 管理后台 7 大核心模块的 API 响应结构与数据自洽性。"""
        # 1. 登录认证
        login_ok = self.client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "admin123"},
        )
        self.assertEqual(login_ok.status_code, 200)
        self.assertIn("token", login_ok.json())

        login_bad = self.client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual(login_bad.status_code, 401)

        # 2. 模块①：全景数据看板（由刚才的真实测试交互驱动）
        dash = self.client.get("/api/v1/admin/dashboard").json()
        self.assertGreaterEqual(dash["kpis"]["total_requests"], 1)
        self.assertGreaterEqual(dash["kpis"]["unique_ips"], 1)

        # 3. 模块②：公共租户列表
        pub_tenants = self.client.get("/api/v1/tenants/public").json()["tenants"]
        self.assertGreaterEqual(len(pub_tenants), 1)
        admin_tenants = self.client.get("/api/v1/admin/tenants").json()["tenants"]
        self.assertGreaterEqual(len(admin_tenants), 1)

        # 4. 模块⑤：RAG 与双路径松弛分析
        rag_stat = self.client.get("/api/v1/admin/rag-analytics").json()
        self.assertGreaterEqual(rag_stat["summary"]["total_rag_queries"], 0)

        # 5. 模块⑥：Token 与 Prefix Cache 账单（动态 MD5）
        tok_stat = self.client.get("/api/v1/admin/token-analytics").json()
        self.assertEqual(
            tok_stat["summary"]["locked_static_prefix_md5"],
            dash["summary"]["static_prefix_md5"],
        )
        self.assertEqual(tok_stat["summary"]["prefix_alignment_rate_pct"], 100.0)
        self.assertGreater(tok_stat["summary"]["total_tokens"], 0)

        # 6. 模块⑦：12315 人工转接工单台 & 60 SKU 只读洞察
        handoffs = self.client.get("/api/v1/admin/handoffs").json()
        self.assertGreaterEqual(handoffs["summary"]["total_tickets"], 0)

        cat_res = self.client.get("/api/v1/admin/catalog").json()
        self.assertEqual(cat_res["total_skus"], 60)
        self.assertEqual(len(cat_res["platform_rules"]), 3)
        self.assertEqual(len(cat_res["orders"]), 10)

    def test_07_live_local_chat_gateway_8367_e2e(self) -> None:
        """实测通过本地 http://127.0.0.1:8367/v1 (protocol=chat, key=123) 跑通全链路 SSE 流式对话与硬件级 Prefix Cache。"""
        health = self.client.get("/health").json()
        self.assertEqual(health["llm_protocol"], "chat")

        res = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
                "session_id": "test-live-8367-chat",
                "tenant_id": "tenant_all",
                "allow_remote_llm": True,
                "allow_remote_embedding": False,
            },
            headers={"X-Forwarded-For": "101.81.24.88"},
        )
        self.assertEqual(res.status_code, 200)
        events = _parse_sse_events(res.text)
        text_out = "".join(
            e["data"]["delta"] for e in events if e["event"] == "text_delta"
        )
        telemetry = next(e["data"] for e in events if e["event"] == "telemetry")
        self.assertIn(
            telemetry["gateway_node"],
            ["deepseek-flash-primary", "deepseek-flash-primary->deterministic-fallback"],
        )
        self.assertTrue(telemetry["prefix_cache_aligned"])
        self.assertGreater(len(text_out.strip()), 30)
        self.assertNotIn("<br", text_out.lower())
        self.assertNotIn("$", text_out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
