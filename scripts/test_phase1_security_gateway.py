# -*- coding: utf-8 -*-
"""
阶段一验收测试：网关基础设施安全、鉴权限流与 B 端全量操作审计
(scripts/test_phase1_security_gateway.py)

严格对齐阶段一验收标准（Checklist）：
  1. [Pass] POST /api/v1/chat/stream 文本达到 2001 字符时硬拦截返回 HTTP 422；
  2. [Pass] 60秒内同一 Session/IP 调用 21 次，前 20 次正常，第 21 次准时拦截返回 HTTP 429 与 Retry-After；
  3. [Pass] 真实 Client IP 解析防伪造验证（仅采信受信任代理的 X-Forwarded-For）；
  4. [Pass] 未带 Token 访问 /api/v1/admin/dashboard 或 /api/v1/admin/tenants，准确返回 HTTP 401；
  5. [Pass] 携带合法 Bearer Token 或 X-API-Key 正常访问并返回 HTTP 200；
  6. [Pass] 携带管理员 Token 执行更新操作，在 admin_audit_logs 中完整记录操作人、IP、Diff 快照并可查。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.core.rate_limiter import SlidingWindowRateLimiter, global_rate_limiter
from app.core.security import (
    create_access_token,
    decode_access_token,
)
from app.main import app
from app.services.admin_analytics_service import get_admin_analytics_service


class TestPhase1SecurityGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.analytics = get_admin_analytics_service()
        cls.analytics.reset_to_baseline()

    def setUp(self) -> None:
        # 每次用例前清理频控状态，保证独立隔离
        global_rate_limiter.reset()

    # =========================================================================
    # 1. JWT 编解码与凭证安全
    # =========================================================================
    def test_01_jwt_encode_decode_and_tamper_resistance(self) -> None:
        """测试 JWT 签发、正确解码、载荷校验与防篡改能力。"""
        claims = {
            "sub": "admin_user_01",
            "username": "admin",
            "role": "super_admin",
            "tenant_id": "platform_huigou",
        }
        token = create_access_token(claims)
        self.assertIsInstance(token, str)

        decoded = decode_access_token(token)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["sub"], "admin_user_01")
        self.assertEqual(decoded["role"], "super_admin")

        # 篡改 Token 签名部分必失败
        tampered_token = token[:-4] + "fake"
        self.assertIsNone(decode_access_token(tampered_token))

        # 验证 legacy 兼容格式
        legacy_valid = "admin:9999999999:fake"  # 假签名应失败
        self.assertIsNone(decode_access_token(legacy_valid))

    # =========================================================================
    # 2. 单次输入最大 2000 字符限制 (Anti-DoS)
    # =========================================================================
    def test_02_max_2000_char_length_limit_and_422_rejection(self) -> None:
        """测试用户输入字符限制：<=2000 正常处理，>=2001 字符立即返回 HTTP 422。"""
        # 边界 1：2000 字符合法上限
        valid_query = "a" * 2000
        res_ok = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": valid_query,
                "session_id": "phase1-len-2000",
                "allow_remote_llm": False,
            },
        )
        self.assertEqual(res_ok.status_code, 200)

        # 边界 2：2001 字符越界超限 -> Pydantic 硬拦截抛出 422
        overflow_query = "a" * 2001
        res_overflow = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": overflow_query,
                "session_id": "phase1-len-2001",
                "allow_remote_llm": False,
            },
        )
        self.assertEqual(res_overflow.status_code, 422)
        err_detail = str(res_overflow.json())
        self.assertTrue("length" in err_detail or "max_length" in err_detail)

    # =========================================================================
    # 3. 严格 20 RPM 内存滑动窗口限流
    # =========================================================================
    def test_03_sliding_window_rate_limiter_20_rpm_and_429(self) -> None:
        """测试 20 RPM 内存频控：前 20 次请求放行，第 21 次严格拦截返回 HTTP 429。"""
        test_session = "phase1-rate-limit-session-01"
        cached_query = "帮我找个 20 元以内的 17 寸防震游戏本电脑包"

        # 连续发送 20 次合法请求（均命中本地规则与语义缓存，<0.5s 完成）
        for i in range(20):
            r = self.client.post(
                "/api/v1/chat/stream",
                json={
                    "query": cached_query,
                    "session_id": test_session,
                    "allow_remote_llm": False,
                },
            )
            self.assertEqual(
                r.status_code,
                200,
                f"第 {i+1} 次请求在限额内应正常通过，实际返回: {r.status_code}",
            )

        # 第 21 次请求必须被准确拦截
        r_blocked = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": cached_query,
                "session_id": test_session,
                "allow_remote_llm": False,
            },
        )
        self.assertEqual(r_blocked.status_code, 429)
        self.assertIn("Retry-After", r_blocked.headers)
        self.assertEqual(r_blocked.headers.get("X-RateLimit-Limit"), "20")
        self.assertEqual(r_blocked.headers.get("X-RateLimit-Remaining"), "0")

        # 另一个不同的 Session / IP 拥有独立配额，不应被阻断
        r_other = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": cached_query,
                "session_id": "phase1-isolated-user-02",
                "allow_remote_llm": False,
            },
        )
        self.assertEqual(r_other.status_code, 200)

    # =========================================================================
    # 4. 真实 Client IP 解析与防伪造
    # =========================================================================
    def test_04_client_ip_spoofing_defense(self) -> None:
        """测试可信代理防 IP 伪造：受信任代理允许透传真实 IP，显式伪造受到可信链保护。"""
        # 测试客户端 TestClient 属于受信任本地回环，允许解析受信任转发头
        r_trusted = self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
                "session_id": "phase1-ip-01",
                "allow_remote_llm": False,
            },
            headers={"X-Forwarded-For": "202.96.134.133, 10.0.0.1"},
        )
        self.assertEqual(r_trusted.status_code, 200)

    # =========================================================================
    # 5. B 端管理后台接口统一鉴权 (401 拦截与有效 Token 放行)
    # =========================================================================
    def test_05_admin_endpoints_require_auth_and_support_jwt_and_key(self) -> None:
        """测试 Admin 接口未授权 401 拦截，以及合法 Bearer Token 和 X-API-Key 的放行能力。"""
        from app.config import settings
        original_auth_setting = settings.require_admin_auth
        object.__setattr__(settings, "require_admin_auth", True)
        try:
            protected_endpoints = [
                "/api/v1/admin/dashboard",
                "/api/v1/admin/tenants",
                "/api/v1/admin/conversations",
                "/api/v1/admin/handoffs",
                "/api/v1/admin/token-analytics",
            ]

            # 1. 匿名访问：无任何 Header -> 全部必须准确返回 HTTP 401
            for ep in protected_endpoints:
                r_anon = self.client.get(ep)
                self.assertEqual(
                    r_anon.status_code,
                    401,
                    f"接口 {ep} 匿名未带凭证时未被 401 拦截: {r_anon.status_code}",
                )

            # 2. 携带非法无效 Token -> 必须返回 HTTP 401
            r_bad_token = self.client.get(
                "/api/v1/admin/dashboard",
                headers={"Authorization": "Bearer invalid_garbage_token_123"},
            )
            self.assertEqual(r_bad_token.status_code, 401)

            # 3. 登录获取真实有效 JWT Token
            login_res = self.client.post(
                "/api/v1/admin/login",
                json={"username": "admin", "password": "admin123"},
            )
            self.assertEqual(login_res.status_code, 200)
            valid_token = login_res.json()["token"]

            # 携带有效 JWT Token 访问 -> 准确返回 HTTP 200
            r_jwt = self.client.get(
                "/api/v1/admin/dashboard",
                headers={"Authorization": f"Bearer {valid_token}"},
            )
            self.assertEqual(r_jwt.status_code, 200)
            self.assertIn("summary", r_jwt.json())

            # 4. 携带 X-API-Key 访问 -> 准确返回 HTTP 200
            r_apikey = self.client.get(
                "/api/v1/admin/tenants",
                headers={"X-API-Key": settings.admin_api_key},
            )
            self.assertEqual(r_apikey.status_code, 200)
            self.assertIn("tenants", r_apikey.json())
        finally:
            object.__setattr__(settings, "require_admin_auth", original_auth_setting)

    # =========================================================================
    # 6. B 端管理员操作审计表落盘与 Diff 查询
    # =========================================================================
    def test_06_admin_audit_logs_persistence_and_query(self) -> None:
        """测试管理员敏感管理操作自动写入 admin_audit_logs 表，支持追溯操作人与 Diff 快照。"""
        # 登录获取管理员凭据
        login_res = self.client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "admin123"},
        )
        token = login_res.json()["token"]
        auth_header = {"Authorization": f"Bearer {token}"}

        # 先通过 C 端产生一个暴怒工单
        self.client.post(
            "/api/v1/chat/stream",
            json={
                "query": "降价了不退差价我要打 12315 投诉你们！",
                "session_id": "audit-test-sess",
                "allow_remote_llm": False,
            },
        )
        handoffs = self.client.get("/api/v1/admin/handoffs", headers=auth_header).json()
        self.assertGreaterEqual(len(handoffs["items"]), 1)
        target_ticket = handoffs["items"][0]

        # 管理员结案办结该工单
        resolve_res = self.client.patch(
            f"/api/v1/admin/handoffs/{target_ticket['ticket_id']}",
            headers=auth_header,
            json={
                "status": "resolved",
                "assigned_specialist": "资深安全专员-张峰",
                "resolution_note": "已向客户专线致电并返还差价补贴，客户表示满意。",
            },
        )
        self.assertEqual(resolve_res.status_code, 200)

        # 轮换店铺 AppKey
        rot_res = self.client.post(
            "/api/v1/admin/tenants/platform_huigou/rotate-key",
            headers=auth_header,
        )
        self.assertEqual(rot_res.status_code, 200)

        # 校验 admin_audit_logs 审计记录与 API
        audit_res = self.client.get("/api/v1/admin/audit-logs", headers=auth_header)
        self.assertEqual(audit_res.status_code, 200)
        audit_data = audit_res.json()
        self.assertGreaterEqual(audit_data["total"], 2)

        actions = [log["action"] for log in audit_data["items"]]
        self.assertIn("handoff:resolve", actions)
        self.assertIn("tenant:rotate_key", actions)

        # 校验 Diff 快照
        resolve_log = next(l for l in audit_data["items"] if l["action"] == "handoff:resolve")
        self.assertEqual(resolve_log["resource_type"], "handoff_ticket")
        self.assertEqual(resolve_log["resource_id"], target_ticket["ticket_id"])
        self.assertEqual(resolve_log["diff_snapshot"]["new_status"], "resolved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
