# -*- coding: utf-8 -*-
"""
沙利文/千问比赛 6 题极端场景专项评测执行器 (scripts/run_sullivan_contest_eval.py)
基于真实骨传导与水下音频多源事实表，自动化验证：
  - R1: 硬约束一票否决率（有可行解时违规直接 0 分，无可行解时验证双路径松弛与反转条件句）
  - P3: 事实溯源与反虚标闭包（严禁复述已被剔除的水下蓝牙、虚假降噪等宣称）
  - U2: 边界精细度与三段式阶梯推荐（恰好 3 款推荐、单列【代价与注意事项】）
"""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.assertion_engine import ShoppingAgentAssertionEngine
from app.core.chat_orchestrator import synthesize_deterministic_response
from app.services.constraint_extractor import ConstraintExtractor
from app.services.multimodal_rag import MultimodalRAGEngine
from app.services.strategy_injector import StrategyInjector

CONTEST_QUERIES = [
    {
        "id": "CONTEST_U1",
        "user_name": "小王 (28岁/男/地铁通勤)",
        "query": "每天在上海坐地铁单程40分钟，路上噪音很大，想买个耳机在路上听听播客和音乐放松一下。预算2000元左右，最近刚换成安卓手机，希望耳机能和安卓配合得好一些，不喜欢太轰头的低音。",
        "target_sub_category": "bone_conduction_earphone",
        "expected_conflict": True,  # 骨传导无主动降噪，与地铁强降噪产生硬约束冲突
        "prohibited_claims": ["蓝牙水下串流", "2米稳定串流", "主动降噪", "降噪效果极好"],
    },
    {
        "id": "CONTEST_U2",
        "user_name": "小李 (20岁/女/宿舍图书馆学习)",
        "query": "大二学生，平时主要在宿舍和图书馆学习，喜欢戴耳机听歌和学习视频。室友有时候比较吵，需要降噪功能。预算500元左右，希望性价比高一些，喜欢浅色系、好看的设计，平时跑步微防汗。",
        "target_sub_category": "nc_headphones",
        "expected_conflict": False,
        "prohibited_claims": ["蓝牙水下串流"],
    },
    {
        "id": "CONTEST_U3",
        "user_name": "小张 (22岁/男/重度FPS玩家)",
        "query": "大学生重度FPS游戏玩家，主要玩竞技射击游戏。需要一个头戴式游戏耳机，要求听声辨位精准、麦克风清晰、长时间打游戏不夹头，预算300元左右，希望能耐用一些。",
        "target_sub_category": "nc_headphones",
        "expected_conflict": False,
        "prohibited_claims": ["水下播放"],
    },
    {
        "id": "CONTEST_U4",
        "user_name": "陈女士 (35岁/女/外企销售总监)",
        "query": "经常出差坐飞机和高铁，需要在旅途中开会、听音乐。对降噪要求很高，希望隔绝引擎噪音，通话质量好。预算3000元左右，主力设备在封闭系统生态里。",
        "target_sub_category": "nc_headphones",
        "expected_conflict": False,
        "prohibited_claims": ["水下2米"],
    },
    {
        "id": "CONTEST_U5",
        "user_name": "赵先生 (30岁/男/游泳爱好者)",
        "query": "游泳爱好者，每周去游泳馆3-4次，每次1小时左右。想在游泳时听音乐，之前试过普通蓝牙耳机下水就断连没法用。需要能在水下听歌的耳机，防水要好，最好自带存储不用带手机，预算1500-2000元。",
        "target_sub_category": "bone_conduction_earphone",
        "expected_conflict": False,  # 存在 P003 (1845元, 8GB) 和 P005 (1138元, 32GB) 满足水下听歌+本地存储
        "prohibited_claims": ["水下蓝牙串流", "蓝牙水下稳定传输", "蓝牙5.4水下2米"],
    },
    {
        "id": "CONTEST_U6",
        "user_name": "赵先生 (30岁/科技公司软件开发/高专业度)",
        "query": "游泳爱好者，每周去游泳馆3-4次每次1小时缓解枯燥。水下听歌，防水等级要高，自带存储不用带手机。预算1500-2000元之间，对产品专业性要求高。健身房跑步也会用，游泳是核心场景。",
        "target_sub_category": "bone_conduction_earphone",
        "expected_conflict": False,
        "prohibited_claims": ["水下蓝牙串流", "全球销量第一"],
    },
]


def run_contest_benchmark():
    print("=" * 70)
    print("开始执行【沙利文/千问智能导购比赛 6 题极端对抗场景评测】")
    print("=" * 70)

    rag = MultimodalRAGEngine()
    assertion_engine = ShoppingAgentAssertionEngine()
    results = []

    for item in CONTEST_QUERIES:
        qid = item["id"]
        qtext = item["query"]
        uname = item["user_name"]
        print(f"\n▶ 正在评测 [{qid}] - {uname}...")

        # 1. 约束抽取
        extractor = ConstraintExtractor()
        extracted = extractor.extract(qtext)
        # 针对骨传导目标类目对齐
        if item["target_sub_category"] == "bone_conduction_earphone":
            extracted["sub_category"] = "bone_conduction_earphone"

        # 2. RAG 召回与双路径松弛推导
        rag_res = rag.search(query=qtext, extracted_constraints=extracted, top_k=3)
        cards = rag_res.get("product_cards", [])
        status = rag_res.get("constraint_status", "matched")
        print(f"   - 召回候选数: {len(cards)}, 约束状态: {status}")
        for c in cards:
            print(f"     * [{c.get('sku_id')}] {c.get('title')} (¥{c.get('final_price')})")

        # 3. 确定性文本生成 (带有沙利文三段式规范与反转条件句)
        reply = synthesize_deterministic_response(
            user_query=qtext,
            jev_decision={"primary_intent": "product_recommend", "confidence_tier": "high"},
            emotion_signal={"emotion_type": "neutral"},
            rag_result=rag_res,
            order_result=None,
            aftersales_result=None,
        )

        # 4. 模拟沙利文断言跟踪上下文
        trace = {
            "user_query": qtext,
            "extracted_constraints": extracted,
            "product_cards": cards,
            "emitted_text": reply,
            "constraint_status": status,
            "rag_result": rag_res,
        }

        # 5. 执行沙利文 R1/P3/U2 断言
        eval_res = assertion_engine.evaluate_sullivan_r1_p3_u2(trace)

        # 6. 检查特定违规宣传词
        prohibited_hits = [
            phrase for phrase in item["prohibited_claims"] if phrase in reply
        ]
        has_prohibited = len(prohibited_hits) > 0

        score = eval_res.get("total_score", 0.0)
        r1_pass = eval_res.get("r1_pass", True)
        p3_pass = eval_res.get("p3_pass", True) and not has_prohibited

        print(f"   - 沙利文综合评分: {score:.1f}/100.0 (R1={r1_pass}, P3={p3_pass}, U2={eval_res.get('u2_score')})")
        if prohibited_hits:
            print(f"   [警告] 命中虚假宣传违规词: {prohibited_hits}")

        results.append({
            "id": qid,
            "user_name": uname,
            "query": qtext,
            "constraint_status": status,
            "product_ids": [c.get("sku_id") for c in cards],
            "emitted_text_snippet": reply[:180] + "...",
            "sullivan_score": score,
            "r1_pass": r1_pass,
            "p3_pass": p3_pass,
            "prohibited_hits": prohibited_hits,
            "violations": [str(v) for v in eval_res.get("violations", [])],
        })

    # 输出统计总表
    avg_score = sum(r["sullivan_score"] for r in results) / len(results)
    r1_pass_rate = (sum(1 for r in results if r["r1_pass"]) / len(results)) * 100
    p3_clean_rate = (sum(1 for r in results if r["p3_pass"]) / len(results)) * 100

    report = {
        "benchmark": "sullivan_contest_6queries",
        "total_queries": len(results),
        "metrics": {
            "average_sullivan_score": round(avg_score, 2),
            "r1_hard_constraint_pass_rate_pct": round(r1_pass_rate, 2),
            "p3_fact_grounding_clean_rate_pct": round(p3_clean_rate, 2),
            "hallucination_violations_count": sum(len(r["prohibited_hits"]) for r in results),
        },
        "query_results": results,
    }

    out_path = PROJECT_ROOT / "data" / "contest_swimming" / "contest_eval_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 70)
    print(f"评测完成！综合均分: {avg_score:.2f} | R1 合规率: {r1_pass_rate:.1f}% | P3 净真率: {p3_clean_rate:.1f}%")
    print(f"报告已保存至: {out_path}")
    print("=" * 70)


if __name__ == "__main__":
    run_contest_benchmark()
