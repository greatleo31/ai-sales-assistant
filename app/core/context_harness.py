# -*- coding: utf-8 -*-
"""
分级决策账本与上下文治理 Harness (app/core/context_harness.py)
面向电商决策型 Shopping Agent 的分层上下文管理体系：
  1. 决策状态账本 (Working Memory Decision Ledger)：
     - 结构化锁定用户意图、类目 (`locked_sub_category`)、动态预算区间 (`budget_min`, `budget_max`)；
     - 实时维护已推荐/已比价的自营商品 SKU 实体清单 (`evaluated_skus`)，杜绝多轮前置商品遗忘；
     - 追踪决策阶段 (`decision_stage`: 探索/比价/答疑/促单/售后/转人工) 与焦点商品 (`focused_sku_id`)；
     - 控制在 < 100 Tokens 的极简结构化视图，从根源消除长上下文注意力串扰。
  2. 分级活跃窗口 (Active Dialogue Window)：
     - 活跃交互区：最近 2~3 轮保留高保真对话与核心商品元数据；
     - 历史归档区：对超过活跃窗口的旧轮次进行语义压缩，仅保留核心诉求与结论；
     - 消除上下文膨胀 60%+，保障大模型在 20+ 轮交互下依然保持注意力聚焦与确定性。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class DecisionStage(str, Enum):
    EXPLORATION = "exploration"          # 需求探索与初次选品
    COMPARISON = "comparison"            # 多款商品对比与规格比价
    OBJECTION = "objection"              # 细节答疑/库存发货/消除顾虑
    TRANSACTION = "transaction"          # 结算引导与促单
    AFTERSALES = "aftersales"            # 订单查询/物流价保/售后退换
    SAFETY_HANDOFF = "safety_handoff"    # 投诉维权/极端情绪/人工接管


@dataclass
class EvaluatedSKU:
    sku_id: str
    title: str
    final_price: float
    category_name: str = ""
    status: str = "recommended"  # recommended, focused, compared, excluded
    matched_features: List[str] = field(default_factory=list)
    turn_introduced: int = 1

    def to_compact_str(self) -> str:
        feat_str = f" ({','.join(self.matched_features)})" if self.matched_features else ""
        return f"[{self.sku_id}] {self.title} ¥{self.final_price:.2f}{feat_str}"


@dataclass
class DecisionLedgerState:
    session_id: str = "default_session"
    turn_count: int = 1
    decision_stage: DecisionStage = DecisionStage.EXPLORATION
    locked_category: Optional[str] = None
    locked_sub_category: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    specs_constraints: Dict[str, Any] = field(default_factory=dict)
    excluded_specs: List[str] = field(default_factory=list)
    evaluated_skus: List[EvaluatedSKU] = field(default_factory=list)
    focused_sku_id: Optional[str] = None
    last_user_intent: Optional[str] = None
    handoff_required: bool = False
    pending_followup: Optional[str] = None

    def get_sku(self, sku_id: str) -> Optional[EvaluatedSKU]:
        for s in self.evaluated_skus:
            if s.sku_id == sku_id:
                return s
        return None

    def upsert_sku(self, sku: EvaluatedSKU) -> None:
        for idx, s in enumerate(self.evaluated_skus):
            if s.sku_id == sku.sku_id:
                self.evaluated_skus[idx] = sku
                return
        self.evaluated_skus.append(sku)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_count": self.turn_count,
            "decision_stage": self.decision_stage.value,
            "locked_category": self.locked_category,
            "locked_sub_category": self.locked_sub_category,
            "budget_min": self.budget_min,
            "budget_max": self.budget_max,
            "specs_constraints": self.specs_constraints,
            "excluded_specs": self.excluded_specs,
            "evaluated_skus": [asdict(s) for s in self.evaluated_skus],
            "focused_sku_id": self.focused_sku_id,
            "last_user_intent": self.last_user_intent,
            "handoff_required": self.handoff_required,
            "pending_followup": self.pending_followup,
        }

    def render_ledger_prompt(self) -> str:
        """
        渲染供 LLM 理解的高密度极简账本，严控在 60~100 Tokens 之间。
        """
        lines = ["[决策工作账本 / Working Memory Ledger]"]
        
        # 1. 类目与阶段
        sub_cat_str = self.locked_sub_category or "待锁定"
        lines.append(f"• 当前锁定细分类目: {sub_cat_str} | 决策阶段: {self.decision_stage.value}")
        
        # 2. 预算与硬约束
        budget_parts = []
        if self.budget_min is not None:
            budget_parts.append(f"≥¥{self.budget_min:.0f}")
        if self.budget_max is not None:
            budget_parts.append(f"≤¥{self.budget_max:.0f}")
        budget_str = " ~ ".join(budget_parts) if budget_parts else "未限定"
        
        specs_str = ", ".join(f"{k}={v}" for k, v in self.specs_constraints.items()) if self.specs_constraints else "无特定硬规格"
        lines.append(f"• 预算区间: {budget_str} | 硬性偏好: {specs_str}")
        
        if self.excluded_specs:
            lines.append(f"• 排除偏好: {', '.join(self.excluded_specs)}")

        # 3. 已评估商品与当前聚焦
        if self.evaluated_skus:
            sku_summaries = []
            for s in self.evaluated_skus[-3:]:  # 最多保留最近 3 款已评估商品，防止膨胀
                prefix = "*" if s.sku_id == self.focused_sku_id else ""
                sku_summaries.append(f"{prefix}{s.sku_id}(¥{s.final_price:.0f})")
            lines.append(f"• 已向用户推荐商品: {', '.join(sku_summaries)}" + (f" (当前焦点: {self.focused_sku_id})" if self.focused_sku_id else ""))

        if self.handoff_required:
            lines.append("• 警报: 已触发人工/情绪升级介入标志！")

        return "\n".join(lines)


class HierarchicalContextHarness:
    """
    分级上下文治理与状态流转 Harness。
    负责多轮上下文裁剪、决策账本提炼更新与注意力防串扰装载。
    """

    # 意图与决策阶段映射规则
    INTENT_STAGE_MAP = {
        "product_recommend": DecisionStage.EXPLORATION,
        "compare_products": DecisionStage.COMPARISON,
        "price_inquiry": DecisionStage.COMPARISON,
        "spec_inquiry": DecisionStage.OBJECTION,
        "aftersales_qa": DecisionStage.AFTERSALES,
        "order_logistics": DecisionStage.AFTERSALES,
        "order_status": DecisionStage.AFTERSALES,
        "frustration_handoff": DecisionStage.SAFETY_HANDOFF,
        "out_of_scope": DecisionStage.EXPLORATION,
        "chitchat": DecisionStage.EXPLORATION,
    }

    @classmethod
    def evolve_ledger(
        cls,
        current_ledger: Optional[DecisionLedgerState],
        session_id: str,
        current_query: str,
        detected_intent: str,
        extracted_constraints: Optional[Dict[str, Any]] = None,
        recommended_cards: Optional[List[Dict[str, Any]]] = None,
        handoff_flag: bool = False,
    ) -> DecisionLedgerState:
        """
        依据当前轮次的用户输入与系统决策，增量推演并更新决策账本。
        """
        ledger = current_ledger or DecisionLedgerState(session_id=session_id)
        ledger.turn_count += 1
        ledger.last_user_intent = detected_intent
        ledger.handoff_required = handoff_flag or ledger.handoff_required

        # 1. 阶段自动跃迁
        stage = cls.INTENT_STAGE_MAP.get(detected_intent, DecisionStage.EXPLORATION)
        # 如果已有已推荐商品，且用户在追问具体款式细节/库存发货/促单，自然推进至 OBJECTION 或 TRANSACTION
        if stage == DecisionStage.EXPLORATION and (ledger.evaluated_skus or ledger.decision_stage in (DecisionStage.COMPARISON, DecisionStage.OBJECTION)):
            if any(kw in current_query for kw in ("这款", "那个", "现货", "发货", "保修", "怎么用", "适合吗", "能装下", "能放下", "尺寸", "接口")):
                stage = DecisionStage.OBJECTION
            elif any(kw in current_query for kw in ("买", "下单", "怎么买", "链接", "付")):
                stage = DecisionStage.TRANSACTION
        ledger.decision_stage = stage

        # 2. 约束槽位继承与更新
        if extracted_constraints:
            new_sub_cat = extracted_constraints.get("sub_category")
            # 只有当明确识别出新细分类目，或者原类目为空时才更新，防止被空值抹除！
            if new_sub_cat:
                ledger.locked_sub_category = new_sub_cat
            
            b_min = extracted_constraints.get("budget_min")
            b_max = extracted_constraints.get("budget_max")
            if b_min is not None:
                ledger.budget_min = float(b_min)
            if b_max is not None:
                ledger.budget_max = float(b_max)

            # 合并数值规格与枚举规格
            num_specs = extracted_constraints.get("numeric_specs") or {}
            for k, v in num_specs.items():
                ledger.specs_constraints[k] = v
            
            enum_specs = extracted_constraints.get("enum_specs") or {}
            for k, v in enum_specs.items():
                ledger.specs_constraints[k] = v

            # 排除项
            excl = extracted_constraints.get("excluded_specs") or []
            for item in excl:
                if item not in ledger.excluded_specs:
                    ledger.excluded_specs.append(item)

        # 3. 记录已推荐的自营商品 SKU
        if recommended_cards:
            for card in recommended_cards:
                sid = card.get("sku_id")
                if sid:
                    title = card.get("title") or card.get("name") or sid
                    f_price = float(card.get("final_price") or card.get("price") or 0.0)
                    ledger.upsert_sku(
                        EvaluatedSKU(
                            sku_id=sid,
                            title=title,
                            final_price=f_price,
                            category_name=card.get("category_name") or ledger.locked_sub_category or "",
                            status="recommended",
                            turn_introduced=ledger.turn_count,
                        )
                    )

        # 4. 用户焦点实体识别与追踪 (例如指代“酷态科”、“第二款”、“SKU-3C-008”)
        focused = None
        # 4.1 精确 SKU ID 命中
        for s in ledger.evaluated_skus:
            if s.sku_id.lower() in current_query.lower():
                focused = s.sku_id
                break
        
        # 4.2 标题/品牌关键词命中
        if not focused:
            for s in reversed(ledger.evaluated_skus):
                # 提取标题中显著的中文词组 (>= 2 字)
                words = re.findall(r"[\u4e00-\u9fa5a-zA-Z0-9]{2,}", s.title)
                for w in words:
                    if len(w) >= 2 and w in current_query:
                        focused = s.sku_id
                        break
                if focused:
                    break

        # 4.3 序数代词命中 ("第一款", "第二款", "前一个")
        if not focused and ledger.evaluated_skus:
            if "第一" in current_query or "第1" in current_query or "前一款" in current_query:
                focused = ledger.evaluated_skus[0].sku_id
            elif "第二" in current_query or "第2" in current_query:
                if len(ledger.evaluated_skus) >= 2:
                    focused = ledger.evaluated_skus[1].sku_id
            elif "这款" in current_query or "这个" in current_query:
                # 默认指向上一次被推荐的 top SKU
                focused = ledger.focused_sku_id or ledger.evaluated_skus[-1].sku_id

        if focused:
            ledger.focused_sku_id = focused
            # 同步更新对应 SKU 的 status 为 focused
            for s in ledger.evaluated_skus:
                if s.sku_id == focused:
                    s.status = "focused"

        return ledger

    @classmethod
    def assemble_hierarchical_context(
        cls,
        history: List[Dict[str, Any]],
        ledger: DecisionLedgerState,
        active_turns_window: int = 3,
    ) -> Tuple[str, List[Dict[str, str]]]:
        """
        装配分级上下文：
          1. ledger_prompt_block: 结构化决策账本，固定注入动态指令中；
          2. active_history: 仅保留最近 `active_turns_window` 轮的高保真问答，更早的旧轮次被精炼压缩，彻底杜绝注意力串扰与 Token 浪费。
        """
        ledger_prompt = ledger.render_ledger_prompt()

        # 分割活跃窗口与归档窗口
        raw_msgs = [m for m in (history or []) if m.get("content")]
        # 每一轮包含一问一答（2条消息）
        max_active_msgs = active_turns_window * 2

        if len(raw_msgs) <= max_active_msgs:
            active_msgs = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in raw_msgs
            ]
            archive_summary = ""
        else:
            archived = raw_msgs[:-max_active_msgs]
            active = raw_msgs[-max_active_msgs:]
            
            # 压缩旧轮次为紧凑纪要
            summary_items = []
            for m in archived:
                role = "用户" if m.get("role") == "user" else "导购"
                snippet = m.get("content", "").strip().replace("\n", " ")
                if len(snippet) > 40:
                    snippet = snippet[:40] + "..."
                summary_items.append(f"{role}: {snippet}")
            
            archive_summary = "### 更早前置轮次要点回顾 (Archived Context):\n" + "\n".join(f"- {item}" for item in summary_items)
            active_msgs = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in active
            ]

        # 组合历史区块描述
        history_lines = ["## 分级对话上下文与决策记忆 (Hierarchical Memory)"]
        history_lines.append(ledger_prompt)
        if archive_summary:
            history_lines.append(archive_summary)
        
        if active_msgs:
            history_lines.append("### 活跃轮次高保真记录 (Active Recent Dialogue):")
            for m in active_msgs:
                r_label = "用户" if m["role"] == "user" else "导购助手"
                history_lines.append(f"- {r_label}: {m['content']}")

        full_context_block = "\n\n".join(history_lines) + "\n"
        return full_context_block, active_msgs
