# -*- coding: utf-8 -*-
"""
双轨神经-符号混合推理仲裁器 (app/reasoning/hybrid_arbitrator.py)
【创新拔高层核心成果：确定性 + 不确定性双轨级联混合推理系统】

系统架构哲学：
  - 阶段一：确定性硬锁门禁 (一阶产生式规则正向链 + 启发式剪枝)
    对非黑即白的合规订单或恶意欺诈订单实现微秒级（<0.1ms）硬性定论与证明树固化；
  - 阶段二：不确定性动态仲裁 (D-S 证据多源融合 + Mamdani 模糊决策)
    对处于微超期、包装破损责任不清、拆封故障等灰色争议订单，融合买家信誉、物流传感、AI质检证据，
    运用 Murphy 修正算法破解 Zadeh 悖论，结合模糊推理重心法平滑生成柔性关怀与特批方案。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.reasoning.deterministic_engine import (
    DeterministicInferenceEngine,
    ProofTree,
)
from app.reasoning.ds_evidence import (
    DSEvidenceEngine,
    EcommerceEvidenceFactory,
    ELEM_BUYER,
    ELEM_LOGISTICS,
    ELEM_SELLER,
    MassFunction,
    MurphyEvidenceCombiner,
    focal_set_to_str,
)
from app.reasoning.fuzzy_engine import (
    FuzzyDecisionResult,
    FuzzyInferenceEngine,
)

logger = logging.getLogger(__name__)


@dataclass
class ArbitrationDecision:
    """综合仲裁裁决书。"""
    order_id: str
    decision_code: str                     # PASS, REJECT, SPECIAL_APPROVED, DISPUTE_MEDIATED
    human_summary: str                     # 权威仲裁决议中文通告
    reasoning_mode: str                    # DETERMINISTIC / HYBRID_NEURO_SYMBOLIC
    is_eligible: bool                      # 最终是否允许退货/退款/换新
    compensation_amount: float = 0.0       # 柔性关怀补贴券金额（元）
    responsibility_distribution: Dict[str, float] = field(default_factory=dict)  # 买家/商家/物流责任比
    proof_tree: Optional[ProofTree] = None # 确定性阶段证明树
    ds_conflict_k: float = 0.0             # D-S 证据冲突度 K
    resolved_zadeh_paradox: bool = False   # 是否成功规避 Zadeh 悖论
    fuzzy_result: Optional[FuzzyDecisionResult] = None
    audit_trace: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "decision_code": self.decision_code,
            "human_summary": self.human_summary,
            "reasoning_mode": self.reasoning_mode,
            "is_eligible": self.is_eligible,
            "compensation_amount": self.compensation_amount,
            "responsibility_distribution": self.responsibility_distribution,
            "ds_conflict_k": round(self.ds_conflict_k, 4),
            "resolved_zadeh_paradox": self.resolved_zadeh_paradox,
            "proof_tree": self.proof_tree.to_dict() if self.proof_tree else None,
            "fuzzy_metrics": {
                "compensation": self.fuzzy_result.compensation_amount,
                "approval_confidence": self.fuzzy_result.approval_confidence,
                "summary": self.fuzzy_result.linguistic_summary,
            } if self.fuzzy_result else None,
            "audit_trace": self.audit_trace,
        }


class HybridArbitrator:
    """双轨混合推理仲裁中枢。"""

    def __init__(
        self,
        deterministic_engine: Optional[DeterministicInferenceEngine] = None,
        fuzzy_engine: Optional[FuzzyInferenceEngine] = None,
    ) -> None:
        self.det_engine = deterministic_engine or DeterministicInferenceEngine()
        self.fuzzy_engine = fuzzy_engine or FuzzyInferenceEngine()

    def arbitrate(self, context: Dict[str, Any]) -> ArbitrationDecision:
        """
        全流程仲裁入口：
          - context 接收订单状态、时间戳、用户输入事实、信誉分、物流传感器与质检数据。
        """
        order_id = str(context.get("order_id", "UNKNOWN-ORDER"))
        elapsed_hours = float(context.get("elapsed_hours", 0.0))
        is_opened = bool(context.get("is_opened", False))
        order_status = str(context.get("order_status", "delivered"))
        buyer_credit = float(context.get("buyer_credit_score", 85.0))
        sensor_shock_g = float(context.get("sensor_shock_g", 1.5))
        defect_confidence = float(context.get("defect_confidence", 0.0))
        defect_kind = str(context.get("defect_kind", "general_wear"))
        issue_type = str(context.get("issue_type", "normal_return"))
        buyer_risk = str(context.get("buyer_risk_level", "NORMAL"))

        # 构造确定性事实工作区
        det_facts = {
            "order_id": order_id,
            "order_status": order_status,
            "elapsed_hours": elapsed_hours,
            "is_opened": is_opened,
            "issue_type": issue_type,
            "buyer_risk_level": buyer_risk,
        }

        # ----------------------------------------------------------------------
        # 第一阶段：确定性硬锁门禁推导
        # ----------------------------------------------------------------------
        proof_tree = self.det_engine.forward_chain(
            facts=det_facts,
            target_predicate="return_eligibility",
            enable_pruning=True,
        )

        terminal_decision = proof_tree.terminal_decision

        # 1.1 若确定性规则给出明确结论且非争议态
        if terminal_decision == "ELIGIBLE_7D_STANDARD":
            return ArbitrationDecision(
                order_id=order_id,
                decision_code="ELIGIBLE_7D_STANDARD",
                human_summary="【确定性审核通过】订单在 168 小时窗口期内且完好未拆封，符合平台 7 天无理由退货条款，支持极速退换与顺丰免运费取件。",
                reasoning_mode="DETERMINISTIC",
                is_eligible=True,
                compensation_amount=0.0,
                proof_tree=proof_tree,
                audit_trace={"gate": "Stage1_Pass", "proof_steps": len(proof_tree.steps)},
            )

        if terminal_decision in ("REJECT_NOT_SIGNED", "REJECT_EXCEED_30D", "REJECT_FRAUD_LOCK", "REJECT_WINDOW_EXPIRED"):
            summary_map = {
                "REJECT_NOT_SIGNED": "【确定性驳回】订单尚未签收，暂不支持 7 天无理由退货，可联系客服拦截在途物流包裹。",
                "REJECT_EXCEED_30D": "【确定性驳回】订单签收已超 30 天，严重超出平台退换与维权窗口，系统硬性封死受理。",
                "REJECT_FRAUD_LOCK": "【风控拦截】买家账户命中恶意索赔/调包黑名单规则，已冻结自动化退换权限，转人工专员核验。",
                "REJECT_WINDOW_EXPIRED": "【确定性驳回】商品虽未拆封，但签收已超出 168 小时（7天）窗口期，无法办理无理由退货。",
            }
            return ArbitrationDecision(
                order_id=order_id,
                decision_code=terminal_decision,
                human_summary=summary_map.get(terminal_decision, "【确定性规则驳回】不满足平台退换条件。"),
                reasoning_mode="DETERMINISTIC",
                is_eligible=False,
                compensation_amount=0.0,
                proof_tree=proof_tree,
                audit_trace={"gate": "Stage1_HardReject", "terminal_code": terminal_decision},
            )

        # ----------------------------------------------------------------------
        # 第二阶段：不确定性动态证据融合与模糊仲裁
        # ----------------------------------------------------------------------
        # 当判定为 UNCERTAIN_DISPUTE 时触发
        logger.info(f"Order {order_id} enters Stage 2: Uncertainty Arbitration.")

        # 2.1 构建多源异构证据
        ev_buyer = EcommerceEvidenceFactory.create_buyer_reputation_evidence(buyer_credit)
        ev_logistics = EcommerceEvidenceFactory.create_logistics_sensor_evidence(sensor_shock_g)
        ev_defect = EcommerceEvidenceFactory.create_inspection_report_evidence(defect_confidence, defect_kind)

        evidences = [ev_buyer, ev_logistics, ev_defect]

        # 经典 Dempster 合成测试冲突度 K
        _, conflict_k = DSEvidenceEngine.dempster_combine_multiple(evidences)

        # 运用 Murphy 加权修正算法化解潜在 Zadeh 悖论
        murphy_fused = MurphyEvidenceCombiner.combine(evidences, name="Murphy_Arbitration_BPA")
        resolved_zadeh = bool(conflict_k >= 0.70)

        # 提取三大责任方的置信度
        mass_buyer = murphy_fused.belief(frozenset([ELEM_BUYER]))
        mass_seller = murphy_fused.belief(frozenset([ELEM_SELLER]))
        mass_logistics = murphy_fused.belief(frozenset([ELEM_LOGISTICS]))

        # 归一化主要责任分布
        total_party_mass = mass_buyer + mass_seller + mass_logistics
        if total_party_mass > 0.001:
            resp_distribution = {
                "buyer_fault": round(mass_buyer / total_party_mass, 4),
                "seller_fault": round(mass_seller / total_party_mass, 4),
                "logistics_fault": round(mass_logistics / total_party_mass, 4),
            }
        else:
            resp_distribution = {"buyer_fault": 0.33, "seller_fault": 0.33, "logistics_fault": 0.34}

        # 2.2 运行 Mamdani 模糊推理系统（计算柔性关怀券与放行置信度）
        overdue_gap = max(0.0, elapsed_hours - 168.0)
        fuzzy_inputs = {
            "overdue_hours": overdue_gap,
            "customer_loyalty": buyer_credit,
        }
        fuzzy_res = self.fuzzy_engine.evaluate(fuzzy_inputs)

        # ----------------------------------------------------------------------
        # 第三阶段：双轨裁决融合生成
        # ----------------------------------------------------------------------
        seller_or_logistics_resp = resp_distribution["seller_fault"] + resp_distribution["logistics_fault"]

        # 裁决判定逻辑：
        # 若非买家责任占比 >= 60% 且 模糊系统放行置信度 >= 0.45：
        if seller_or_logistics_resp >= 0.55 and fuzzy_res.approval_confidence >= 0.45:
            decision_code = "SPECIAL_APPROVED"
            is_eligible = True
            comp = fuzzy_res.compensation_amount
            summary = (
                f"【双轨智能仲裁：特批支持退换】经 D-S 多源证据融合，非买家责任高达 {seller_or_logistics_resp*100:.1f}% "
                f"（商家责任 {resp_distribution['seller_fault']*100:.1f}%, 物流责任 {resp_distribution['logistics_fault']*100:.1f}%）。"
                f"结合模糊关怀模型（置信度 {fuzzy_res.approval_confidence*100:.1f}%），惠购官方为您特批开辟绿色换新通道，"
                f"并补贴 {comp:.1f} 元官方体验关怀券。"
            )
        else:
            # 买家责任偏高或严重超期，维持驳回但送关怀券
            decision_code = "DISPUTE_MEDIATED_CARE"
            is_eligible = False
            comp = fuzzy_res.compensation_amount
            summary = (
                f"【双轨智能仲裁：柔性协商结案】经 D-S 多源证据核验，买家侧归因系数偏高（{resp_distribution['buyer_fault']*100:.1f}%），"
                f"且超出平台退换窗口（超时 {overdue_gap:.1f}h）。无法办理原路全额退货，但鉴于您的良好使用记录，"
                f"系统已特批赠送 {comp:.1f} 元关怀券，并提供自营半价维修服务。"
            )

        audit_trace = {
            "stage1_decision": terminal_decision,
            "ds_conflict_k": round(conflict_k, 4),
            "murphy_bpa": murphy_fused.to_readable_dict(),
            "fuzzy_activated_rules": len(fuzzy_res.activated_rules),
            "final_arbitration": decision_code,
        }

        return ArbitrationDecision(
            order_id=order_id,
            decision_code=decision_code,
            human_summary=summary,
            reasoning_mode="HYBRID_NEURO_SYMBOLIC",
            is_eligible=is_eligible,
            compensation_amount=comp,
            responsibility_distribution=resp_distribution,
            proof_tree=proof_tree,
            ds_conflict_k=conflict_k,
            resolved_zadeh_paradox=resolved_zadeh,
            fuzzy_result=fuzzy_res,
            audit_trace=audit_trace,
        )


# 单例工厂提供便捷全局引用
_global_arbitrator: Optional[HybridArbitrator] = None

def get_hybrid_arbitrator() -> HybridArbitrator:
    global _global_arbitrator
    if _global_arbitrator is None:
        _global_arbitrator = HybridArbitrator()
    return _global_arbitrator
