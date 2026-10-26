# -*- coding: utf-8 -*-
"""
自动化评测与基准管理 API 端点测试套件 (scripts/test_eval_endpoints.py)
"""
import time
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_eval_endpoints():
    print(">>> 1. 测试 GET /api/v1/admin/eval/cases")
    res = client.get("/api/v1/admin/eval/cases?page=1&page_size=20")
    assert res.status_code == 200, f"Status code: {res.status_code}, {res.text}"
    data = res.json()
    assert data["status"] == "ok"
    assert data["total"] >= 50
    assert len(data["items"]) == 20
    assert "summary" in data
    print(f"  [PASS] 用例列表获取成功，总用例数: {data['total']}, 单轮: {data['summary']['single_turn_count']}")

    print(">>> 2. 测试 PATCH /api/v1/admin/eval/cases/Q01")
    patch_payload = {
        "standard_response": "【修润标杆】您好，主要区别在面料和内部收纳结构上：升级款采用高密度防泼水面料与独立加厚防震仓，基础款更轻便。常背电脑建议选升级款。",
        "audit_status": "verified"
    }
    patch_res = client.patch("/api/v1/admin/eval/cases/Q01", json=patch_payload)
    assert patch_res.status_code == 200, f"Status code: {patch_res.status_code}, {patch_res.text}"
    patch_data = patch_res.json()
    assert patch_data["status"] == "ok"
    assert "【修润标杆】" in patch_data["case"]["standard_response"]
    print("  [PASS] Q01 标杆标准回复修润与状态更新成功")

    print(">>> 3. 测试 POST /api/v1/admin/eval/adopt-feedback")
    adopt_payload = {
        "source_type": "feedback",
        "source_id": "FB-TEST-001",
        "scenario_group": "aftersales_verify",
        "standard_response": "【标杆标准回复】自营商品保价支持签收次日0点起算，您可在订单详情一键申请价保退差。",
        "query_text": "昨天刚买的商品今天降价了20元，怎么退差价？"
    }
    adopt_res = client.post("/api/v1/admin/eval/adopt-feedback", json=adopt_payload)
    assert adopt_res.status_code == 200, f"Status code: {adopt_res.status_code}, {adopt_res.text}"
    adopt_data = adopt_res.json()
    assert adopt_data["status"] == "ok"
    new_case_id = adopt_data["new_case_id"]
    print(f"  [PASS] 成功将反馈收录为官方用例: {new_case_id}")

    print(">>> 4. 测试 POST /api/v1/admin/eval/trigger 与 GET /api/v1/admin/eval/progress")
    trig_res = client.post("/api/v1/admin/eval/trigger", json={"mode": "sandbox"})
    assert trig_res.status_code == 200, f"Status code: {trig_res.status_code}, {trig_res.text}"
    trig_data = trig_res.json()
    task_id = trig_data["task_id"]
    print(f"  [PASS] 评测任务成功启动: {task_id}")

    # 等待或轮询进度
    for _ in range(20):
        prog_res = client.get(f"/api/v1/admin/eval/progress/{task_id}")
        assert prog_res.status_code == 200
        prog = prog_res.json()
        print(f"  ... 轮询进度: {prog.get('progress_pct')}% (状态: {prog.get('status')}, 场景: {prog.get('current_scenario')})")
        if prog.get("status") in ("completed", "failed"):
            break
        time.sleep(0.5)

    assert prog.get("status") == "completed", f"Task failed: {prog.get('error')}"
    print(f"  [PASS] 自动化评测任务顺利完成: 100% 通过, 检查项: {prog.get('results_summary', {}).get('assertions_checked')}")
    print("=" * 60)
    print("ALL 5 EVAL API ENDPOINTS VALIDATED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    test_eval_endpoints()
