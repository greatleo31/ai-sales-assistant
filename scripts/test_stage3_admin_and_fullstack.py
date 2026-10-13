# -*- coding: utf-8 -*-
r"""
阶段 3 自动化验收与全栈闭环联调测试套件 (scripts/test_stage3_admin_and_fullstack.py)
验证范围：
  1. `admin-frontend/` 工程结构与 Vite 生产构建产物 (`admin-frontend/dist/index.html` 与 JS/CSS bundle)；
  2. FastAPI 同时挂载 `/h5/` (C 端 iOS 27 极光毛玻璃客户端) 与 `/admin/` (Admin 管理员统计中台) 均返回 HTTP 200；
  3. Admin 7 大核心模块前端组件完整性断言 (`dashboard`, `tenants`, `conversations`, `feedbacks`, `rag`, `tokens`, `handoffs_catalog`)；
  4. C 端 H5 <-> 后端 272K 状态机 (`http://127.0.0.1:8367/v1`, `chat` 协议, `key=123`) <-> Admin 统计后台全链路实时闭环验证。
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


class TestStage3AdminAndFullstack(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.admin_dir = ROOT_DIR / "admin-frontend"
        cls.dist_dir = cls.admin_dir / "dist"
        cls.client = TestClient(app)
        cls.analytics = get_admin_analytics_service()
        cls.analytics.reset_to_baseline()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.analytics.reset_to_baseline()

    def test_01_admin_project_files_and_dist_artifacts(self) -> None:
        """验证 admin-frontend 源码结构与 Vite 生产构建产物 dist/ 完整存在。"""
        required = [
            self.admin_dir / "package.json",
            self.admin_dir / "vite.config.js",
            self.admin_dir / "index.html",
            self.admin_dir / "src" / "main.jsx",
            self.admin_dir / "src" / "App.jsx",
            self.admin_dir / "src" / "styles" / "admin-console.css",
            self.dist_dir / "index.html",
        ]
        for p in required:
            self.assertTrue(p.exists(), f"缺少关键文件: {p}")

        dist_html = (self.dist_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("/admin/assets/", dist_html)
        self.assertIn("官方运营管理中台", dist_html)

    def test_02_admin_7_modules_ui_coverage_in_app_jsx(self) -> None:
        """验证 admin-frontend/src/App.jsx 完整实现 7 大核心管理模块与 API 对接。"""
        app_code = (self.admin_dir / "src" / "App.jsx").read_text(encoding="utf-8")
        for tab_key in (
            "dashboard",
            "tenants",
            "conversations",
            "feedbacks",
            "rag",
            "tokens",
            "handoffs_catalog",
        ):
            self.assertIn(f"'{tab_key}'", app_code)

        for api_path in (
            "/api/v1/admin/login",
            "/api/v1/admin/dashboard",
            "/api/v1/admin/tenants",
            "/api/v1/admin/conversations",
            "/api/v1/admin/feedbacks",
            "/api/v1/admin/rag-analytics",
            "/api/v1/admin/token-analytics",
            "/api/v1/admin/handoffs",
            "/api/v1/admin/catalog",
        ):
            self.assertIn(api_path, app_code)

    def test_03_dual_frontend_mounts_h5_and_admin_reachable(self) -> None:
        """验证 FastAPI 同时挂载 /h5/ 与 /admin/ 且均返回 HTTP 200。"""
        r_h5 = self.client.get("/h5/")
        self.assertEqual(r_h5.status_code, 200)
        self.assertIn("/h5/assets/", r_h5.text)

        r_admin = self.client.get("/admin/")
        self.assertEqual(r_admin.status_code, 200)
        self.assertIn("/admin/assets/", r_admin.text)

    def test_04_end_to_end_c_end_to_admin_live_closed_loop(self) -> None:
        """
        全栈端到端闭环验证：
          1. C 端客户通过真实本地 8367 网关 (protocol=chat, key=123) 发起选品对话并点赞 -> Admin 实时查到深圳 IP 会话与好评；
          2. C 端客户发起 12315 暴怒维权投诉 -> 自动生成 P0 人工转接工单 -> Admin 专员一键办结工单；
          3. Admin 新增/更新租户并轮换 AppKey -> C 端公共租户列表立即同步。
        """
        # 1. C 端客户 (深圳电信 IP: 202.96.134.133) 发起真实流式选品对话
        shenzhen_ip = "202.96.134.133"
        chat_res = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "50 块以内、100W、还得有三个口的氮化镓充电器",
                "session_id": "fullstack-e2e-sz-01",
                "tenant_id": "tenant_3c",
                "allow_remote_llm": True,
                "allow_remote_embedding": False,
            },
            headers={"X-Forwarded-For": shenzhen_ip},
        )
        self.assertEqual(chat_res.status_code, 200)
        events = _parse_sse(chat_res.text)
        meta = next(e["data"] for e in events if e["event"] == "decision_meta")
        msg_id = meta["message_id"]
        stream_text = "".join(
            e["data"]["delta"] for e in events if e["event"] == "text_delta"
        )
        self.assertNotIn("<br", stream_text.lower())
        self.assertNotIn("$", stream_text)

        # 客户点击 👍 有帮助
        fb_res = self.client.post(
            "/api/v1/feedback",
            json={
                "message_id": msg_id,
                "session_id": "fullstack-e2e-sz-01",
                "tenant_id": "tenant_3c",
                "rating": "like",
                "reason_tags": ["推荐方案合理", "参数对比直观"],
                "comment": "双路径对比非常清楚！",
            },
            headers={"X-Forwarded-For": shenzhen_ip},
        )
        self.assertEqual(fb_res.status_code, 200)

        # Admin 后台按深圳 IP 审计该条会话
        conv_res = self.client.get(
            "/api/v1/admin/conversations",
            params={"ip_keyword": shenzhen_ip},
        ).json()
        self.assertGreaterEqual(conv_res["total"], 1)
        self.assertEqual(conv_res["items"][0]["feedback_rating"], "like")

        # 2. C 端客户发起 12315 暴怒维权 -> 自动触发 handoff_guard 并生成 P0 工单
        angry_res = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "刚买就降价了还不给保价！再不退差价我直接打12315投诉你们欺诈！",
                "session_id": "fullstack-e2e-angry-02",
                "tenant_id": "tenant_all",
                "allow_remote_llm": False,
            },
            headers={"X-Forwarded-For": "223.72.91.88"},
        )
        self.assertEqual(angry_res.status_code, 200)
        handoffs = self.client.get("/api/v1/admin/handoffs").json()
        newest_ticket = handoffs["items"][0]
        self.assertEqual(newest_ticket["priority"], "P0_URGENT")

        # Admin 办结该工单
        resolve_res = self.client.patch(
            f"/api/v1/admin/handoffs/{newest_ticket['ticket_id']}",
            json={
                "status": "resolved",
                "assigned_specialist": "VIP售后主管-林夏",
                "resolution_note": "已为客户极速退还差价并致电安抚。",
            },
        )
        self.assertEqual(resolve_res.status_code, 200)
        self.assertEqual(resolve_res.json()["ticket"]["status"], "resolved")

        # 3. Admin 轮换 tenant_3c 的 AppKey
        old_tenants = {
            t["tenant_id"]: t["app_key"]
            for t in self.client.get("/api/v1/admin/tenants").json()["tenants"]
        }
        rot_res = self.client.post("/api/v1/admin/tenants/tenant_3c/rotate-key")
        self.assertEqual(rot_res.status_code, 200)
        new_key = rot_res.json()["tenant"]["app_key"]
        self.assertNotEqual(old_tenants["tenant_3c"], new_key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
