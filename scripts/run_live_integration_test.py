# -*- coding: utf-8 -*-
"""
双端与全链路真实联调测试脚本 (scripts/run_live_integration_test.py)
对标淘宝「千问」平台级自营导购客服架构：
  1. 验证 C 端 (H5) 与管理后台 (Admin) 在真实 FastAPI 运行时下的全链路接口协同；
  2. 验证纯净零数据基线下，全部指标与数据均由买家真实流式交互动态驱动生成；
  3. 覆盖四大核心业务流转：
     - 场景 A: 真实流式导购与商品卡片透传 -> 后台实时 Trace 与 IP 归属地审计；
     - 场景 B: 买家真实好差评反馈 -> 后台好评率动态核算与管理员工单批注；
     - 场景 C: 买家暴怒维权触发 handoff_guard -> 自动升档 P0 工单 -> 客服专员办结；
     - 场景 D: 缺货商品登记心愿单 -> 采购流转与上下文快照；
  4. 验证看板与算力大屏 100% 由真实会话驱动，绝无任何硬编码假数据。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi.testclient import TestClient
from app.main import app
from app.services.admin_analytics_service import analytics_service, get_static_constitution_metrics


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


def run_integration_test():
    print("=" * 80)
    print("🚀 开始执行：C 端 H5 + Admin 后台管理系统 本地全链路真实联调测试")
    print("=" * 80)

    client = TestClient(app)

    # 步骤 0：初始化重置，确保纯净零数据起点
    print("\n[步骤 0] 环境基线准备：重置 SQLite 至 100% 纯净零数据状态...")
    analytics_service.reset_to_baseline()
    dash_zero = client.get("/api/v1/admin/dashboard").json()
    summary_zero = dash_zero["summary"]
    assert summary_zero["total_messages"] == 0, "会话计数必须为0"
    assert summary_zero["feedback_total"] == 0, "反馈计数必须为0"
    assert summary_zero["handoff_turns"] == 0, "待办P0工单必须为0"
    print("  ✓ 纯净零基线验证通过：会话数=0，反馈数=0，待办工单数=0")

    # 步骤 1：双端静态资源挂载与图片服务联调
    print("\n[步骤 1] 挂载与静态资源联调：测试 /admin/、/h5/ 与本地商品图片接口...")
    r_admin = client.get("/admin/")
    assert r_admin.status_code == 200, f"/admin/ 响应异常: {r_admin.status_code}"
    assert "/admin/assets/" in r_admin.text, "admin-frontend 静态资源映射正确"

    r_h5 = client.get("/h5/")
    assert r_h5.status_code == 200, f"/h5/ 响应异常: {r_h5.status_code}"
    assert "/h5/assets/" in r_h5.text, "h5-frontend 静态资源映射正确"

    # 随机测试一个 SKU 图片
    r_img = client.get("/static/images/SKU-3C-001.jpg")
    print(f"  ✓ /admin/ 静态路由正常 (HTTP 200)")
    print(f"  ✓ /h5/ 静态路由正常 (HTTP 200)")
    print(f"  ✓ 商品图片接口 /static/images/SKU-3C-001.jpg 状态: {r_img.status_code}")
    assert r_img.status_code == 200, "SKU-3C-001.jpg 图片必须返回 200"

    # 步骤 2：场景 A - 真实买家流式导购选品（深圳电信客户端）
    print("\n[步骤 2] 场景 A 联调：买家通过 C 端发起流式导购咨询 (深圳电信 IP)...")
    sz_ip = "202.96.134.133"
    session_id_a = "session-e2e-buyer-sz"
    chat_req_a = {
        "query": "推荐一个百元以内防泼水户外双肩包，平时通勤用",
        "session_id": session_id_a,
        "tenant_id": "platform_huigou",
        "allow_remote_llm": True,
        "allow_remote_embedding": False,
    }
    r_chat_a = client.post("/api/v1/chat/stream", json=chat_req_a, headers={"X-Forwarded-For": sz_ip})
    assert r_chat_a.status_code == 200, f"Chat 请求失败: {r_chat_a.status_code}"

    events_a = _parse_sse(r_chat_a.text)
    meta_a = next((e["data"] for e in events_a if e["event"] == "decision_meta"), None)
    assert meta_a is not None, "必须返回 decision_meta 事件"
    msg_id_a = meta_a["message_id"]

    stream_text_a = "".join(e["data"]["delta"] for e in events_a if e["event"] == "text_delta")
    print(f"  ✓ C 端收到 SSE 流式响应，消息 ID: {msg_id_a}")
    print(f"  ✓ 决策元数据: 意图={meta_a.get('intent')}, 推荐SKU={meta_a.get('recommended_skus')}")
    print(f"  ✓ 导购回复文本片段: {stream_text_a[:60]}...")

    # 验证 Admin 后台真实 Trace 审计
    convs_a = client.get("/api/v1/admin/conversations", params={"ip_keyword": sz_ip}).json()
    assert convs_a["total"] >= 1, "Admin 后台必须实时捕获该条会话"
    logged_item_a = convs_a["items"][0]
    assert logged_item_a["client_ip"] == sz_ip, f"客户端 IP 记录不符: {logged_item_a['client_ip']}"
    assert "广东省深圳市" in logged_item_a["ip_region"], f"IP 归属地解析不符: {logged_item_a['ip_region']}"
    print(f"  ✓ Admin 后台实时审计验证通过：IP={logged_item_a['client_ip']}, 归属地={logged_item_a['ip_region']}, 耗时={logged_item_a['total_rtt_ms']}ms")

    # 步骤 3：场景 B - 买家好差评交互联调
    print("\n[步骤 3] 场景 B 联调：买家对上一条推荐结果提交真实好评 (👍)...")
    fb_req_like = {
        "message_id": msg_id_a,
        "session_id": session_id_a,
        "tenant_id": "platform_huigou",
        "rating": "like",
        "reason_tags": ["推荐方案合理", "性价比高"],
        "comment": "推荐的防泼水双肩包很合心意，价格也在预算内！",
    }
    r_fb_a = client.post("/api/v1/feedback", json=fb_req_like, headers={"X-Forwarded-For": sz_ip})
    assert r_fb_a.status_code == 200

    # 另外一位北京买家发起会话并提交差评
    bj_ip = "124.64.16.88"
    session_id_b = "session-e2e-buyer-bj"
    r_chat_b = client.post(
        "/api/v1/chat/stream",
        json={"query": "买个50块钱的降噪耳机", "session_id": session_id_b, "allow_remote_llm": False},
        headers={"X-Forwarded-For": bj_ip},
    )
    events_b = _parse_sse(r_chat_b.text)
    meta_b = next((e["data"] for e in events_b if e["event"] == "decision_meta"), None)
    msg_id_b = meta_b["message_id"]

    fb_req_dislike = {
        "message_id": msg_id_b,
        "session_id": session_id_b,
        "tenant_id": "platform_huigou",
        "rating": "dislike",
        "reason_tags": ["预算不符"],
        "comment": "预算严重超标了",
    }
    client.post("/api/v1/feedback", json=fb_req_dislike, headers={"X-Forwarded-For": bj_ip})

    # Admin 后台实时验证好差评中心统计
    fb_analytics = client.get("/api/v1/admin/feedbacks").json()
    assert fb_analytics["total"] == 2, f"总反馈数应为2，实际为{fb_analytics['total']}"
    assert fb_analytics["likes"] == 1, f"好评数应为1，实际为{fb_analytics['likes']}"
    assert fb_analytics["dislikes"] == 1, f"差评数应为1，实际为{fb_analytics['dislikes']}"
    assert fb_analytics["satisfaction_rate_pct"] == 50.0, f"好评率应为50.0%，实际为{fb_analytics['satisfaction_rate_pct']}"
    print(f"  ✓ 反馈中心统计实时计算：总评价=2, 好评=1, 差评=1, 好评率={fb_analytics['satisfaction_rate_pct']}%")

    # 管理员在后台对差评进行批注流转
    dislike_fb_id = next(item["feedback_id"] for item in fb_analytics["items"] if item["rating"] == "dislike")
    r_patch_fb = client.patch(
        f"/api/v1/admin/feedbacks/{dislike_fb_id}",
        json={"status": "reviewed", "admin_note": "已记录该价位段降噪耳机痛点，建议采购供应链补充入门级入门款"},
    )
    assert r_patch_fb.status_code == 200
    print("  ✓ 管理员后台批注流转成功")

    # 步骤 4：场景 C - 12315 维权情绪 -> P0 工单生成 -> 专员办结
    print("\n[步骤 4] 场景 C 联调：买家发起暴怒维权 -> 触发 handoff_guard -> P0 工单流转...")
    sh_ip = "101.80.12.34"
    session_id_c = "session-e2e-buyer-sh-angry"
    r_chat_c = client.post(
        "/api/v1/chat/stream",
        json={
            "query": "充电器刚用就发烫冒烟了！怀疑是假货！立刻找你们店长出来赔钱，不然我马上打12315消协举报！",
            "session_id": session_id_c,
            "allow_remote_llm": False,
        },
        headers={"X-Forwarded-For": sh_ip},
    )
    events_c = _parse_sse(r_chat_c.text)
    cards_event = next((e["data"] for e in events_c if e["event"] == "ui_cards"), None)
    assert cards_event is not None and cards_event.get("handoff_card"), "必须触发 handoff_card 绿色通道"
    print(f"  ✓ 实时触发转人工门控卡片: 标题={cards_event['handoff_card'].get('title')}")

    # Admin 验证 P0 工单生成
    handoffs_res = client.get("/api/v1/admin/handoffs").json()
    assert handoffs_res["summary"]["total_tickets"] >= 1
    p0_ticket = handoffs_res["items"][0]
    assert p0_ticket["priority"] == "P0_URGENT", f"工单优先级应为 P0_URGENT，实际为 {p0_ticket['priority']}"
    ticket_id = p0_ticket["ticket_id"]
    print(f"  ✓ Admin 后台实时捕捉到 P0 工单: ID={ticket_id}, 状态={p0_ticket['status']}, 摘要={p0_ticket['user_query'][:25]}...")

    # 客服专员林夏在后台一键办结
    r_resolve = client.patch(
        f"/api/v1/admin/handoffs/{ticket_id}",
        json={
            "status": "resolved",
            "assigned_specialist": "VIP售后专家-林夏",
            "resolution_note": "已主动为客户极速补退差价15元，电话回访达成和解。",
        },
    )
    assert r_resolve.status_code == 200
    resolved_ticket = r_resolve.json()["ticket"]
    assert resolved_ticket["status"] == "resolved"
    agent_name = resolved_ticket.get("assigned_agent") or resolved_ticket.get("assigned_specialist")
    print(f"  ✓ 客服专员成功办结工单: 处理人={agent_name}, 状态={resolved_ticket['status']}")

    # 步骤 5：场景 D - 缺货心愿单登记与流转
    print("\n[步骤 5] 场景 D 联调：登记缺货品类心愿单 -> 后台采买流转...")
    wish_req = {
        "missing_item": "钛合金轻量化露营折叠椅",
        "user_query": "有没有轻量化钛合金露营折叠椅？",
        "expected_budget": "¥150 - ¥200",
        "user_subscribed": True,
    }
    r_wish = client.post("/api/v1/wishlist", json=wish_req, headers={"X-Forwarded-For": sz_ip})
    assert r_wish.status_code == 200, f"心愿单提交失败: {r_wish.text}"
    hook_id = r_wish.json()["wishlist"]["hook_id"]
    print(f"  ✓ 缺货心愿单登记成功: ID={hook_id}, 品名={wish_req['missing_item']}")

    # 后台采购人员更新采买进度
    r_wish_patch = client.patch(
        f"/api/v1/admin/wishlists/{hook_id}",
        json={"status": "sourcing", "admin_note": "已对接户外供应链，预计下周打样入库"},
    )
    assert r_wish_patch.status_code == 200
    assert r_wish_patch.json()["wishlist"]["status"] == "sourcing"
    print(f"  ✓ 后台心愿单状态流转成功: 新状态={r_wish_patch.json()["wishlist"]["status"]}")

    # 步骤 6：真实数据看板大屏与算力账单核验
    print("\n[步骤 6] 数据大屏真实性核验：验证 Dashboard 指标完全由上述真实操作计算驱动...")
    dash = client.get("/api/v1/admin/dashboard").json()
    summary_dash = dash["summary"]
    assert summary_dash["total_messages"] == 3, f"总会话数应为3，实际为{summary_dash['total_messages']}"
    assert summary_dash["feedback_total"] == 2, f"总反馈数应为2，实际为{summary_dash['feedback_total']}"
    assert summary_dash["satisfaction_rate_pct"] == 50.0, f"好评率应为50.0%，实际为{summary_dash['satisfaction_rate_pct']}"
    print(f"  ✓ Dashboard 数据完全吻合：总请求数={summary_dash['total_messages']}, 总评价={summary_dash['feedback_total']}, 满意率={summary_dash['satisfaction_rate_pct']}%")

    # 验证 Token 账单与真实硬件前缀缓存
    token_stats = client.get("/api/v1/admin/token-analytics").json()
    summary_tok = token_stats["summary"]
    assert summary_tok["total_turns"] == 3
    assert summary_tok["total_tokens"] > 0
    print(f"  ✓ Token 算力账单验证通过：总请求轮次={summary_tok['total_turns']}, 总真实消耗Token={summary_tok['total_tokens']}")

    runtime_res = client.get("/api/v1/admin/runtime-status").json()
    real_md5, real_tokens = get_static_constitution_metrics()
    assert runtime_res["static_prefix_md5"] == real_md5
    assert runtime_res["static_prefix_tokens"] == real_tokens
    print(f"  ✓ 底层硬件 Prefix 缓存验证通过：MD5={real_md5}, Token数={real_tokens}")

    # 重置环境回纯净基线
    analytics_service.reset_to_baseline()
    print("  ✓ 联调完成，数据库已再次安全重置为 100% 纯净初始基线")

    print("\n" + "=" * 80)
    print("🎉 恭喜！双端全链路真实联调测试 100% 全部通过 (无任何 mock 数据，全真实接口闭环)")
    print("=" * 80)


if __name__ == "__main__":
    run_integration_test()
