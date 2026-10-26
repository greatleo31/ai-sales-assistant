# -*- coding: utf-8 -*-
"""
端到端流式双轨推理仲裁验证测试 (scripts/test_reasoning_e2e_stream.py)
验证完整链路：
  用户输入 -> ChatOrchestrator -> 确定性硬锁门禁 -> D-S/模糊双轨仲裁
  -> SSE 多事件输出 (decision_meta -> ui_cards[arbitration_card] -> text_delta -> done)
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.core.chat_orchestrator import ShoppingAgentOrchestrator

orchestrator = ShoppingAgentOrchestrator()


async def run_e2e_test():
    print("\n" + "=" * 70)
    print("【端到端实测】双轨智能仲裁 SSE 流式全链路与 GenUI 卡片下发测试")
    print("=" * 70)

    # 测试典型案例：微超期耳机订单 ORD-20261001-006
    query = "订单 ORD-20261001-006 刚过 168 小时（超期了不到一天）没拆封，能给通融退换或者补偿吗？"
    session_id = "test-reasoning-session-001"

    print(f"  用户自然语言诉求: '{query}'")
    received_events = []
    text_buffer = ""
    cards_payload = None

    async for chunk in orchestrator.stream_chat(
        query=query,
        session_id=session_id,
        history=[],
    ):
        lines = chunk.strip().split("\n")
        ev_name = None
        ev_data = None
        for line in lines:
            if line.startswith("event: "):
                ev_name = line.replace("event: ", "").strip()
            elif line.startswith("data: "):
                try:
                    ev_data = json.loads(line.replace("data: ", "").strip())
                except Exception:
                    ev_data = line.replace("data: ", "").strip()

        if ev_name:
            received_events.append(ev_name)
            if ev_name == "ui_cards":
                cards_payload = ev_data
            elif ev_name == "text_delta" and isinstance(ev_data, dict):
                text_buffer += ev_data.get("delta", "")

    print(f"\n  [SSE 事件序列]: {' -> '.join(received_events)}")
    assert "decision_meta" in received_events, "缺少 decision_meta 事件"
    assert "ui_cards" in received_events, "缺少 ui_cards 事件"
    assert "done" in received_events, "缺少 done 事件"

    # 验证 ui_cards 中的仲裁卡片
    assert cards_payload is not None, "未收到 ui_cards 数据"
    aftersales = cards_payload.get("aftersales_card")
    arb = cards_payload.get("arbitration_card") or (aftersales and aftersales.get("arbitration"))

    print("\n  [仲裁卡片下发核验]:")
    assert arb is not None, "ui_cards 中必须包含 arbitration_card 结构"
    print(f"    - 裁决代码 (decision_code): {arb.get('decision_code')}")
    print(f"    - 推理模式 (reasoning_mode): {arb.get('reasoning_mode')}")
    print(f"    - 最终是否准予处理 (is_eligible): {arb.get('is_eligible')}")
    print(f"    - 柔性关怀券金额: ¥{arb.get('compensation_amount')}")
    print(f"    - D-S 多源责任比例: {arb.get('responsibility_distribution')}")
    print(f"    - 证明树包含步骤数: {len(arb.get('proof_tree', {}).get('steps', []))} 步")

    assert arb.get("decision_code") == "SPECIAL_APPROVED", "微超期高信誉耳机订单应被判定为 SPECIAL_APPROVED"
    assert arb.get("compensation_amount", 0) > 0, "应下发柔性关怀券"
    assert len(arb.get("proof_tree", {}).get("steps", [])) >= 2, "白盒证明树必须包含正向推导步"

    print("\n  [大模型/直出回复核验]:")
    print(f"    文本长度: {len(text_buffer)} 字符")
    print(f"    回复摘录: {text_buffer[:150]}...")
    assert "双轨智能仲裁" in text_buffer or "特批" in text_buffer or "关怀" in text_buffer, "回复文本中必须包含双轨仲裁决议说明"

    print("\n" + "=" * 70)
    print("  [PASS] 端到端双轨推理全链路实测 100% 通过！")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
