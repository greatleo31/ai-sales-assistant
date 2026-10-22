# -*- coding: utf-8 -*-
"""
272K 全量对话状态机与流式编排器 (app/core/chat_orchestrator.py)
严格对齐 HANDOFF.md §1, §2, §3, §4 与 Claude Sonnet 5.5 模块四实施规范：
  1. 有效上下文窗口锁定为配置值 `272K` (`settings.effective_context_window = 272000`)；
  2. 保留 EWMA 延迟感知路由器 (`EWMALatencyRouter`) + 无锁三态熔断器 (`CircuitBreaker`: CLOSED/OPEN/HALF_OPEN)，
     主节点为 `deepseek-flash` (`POST https://go.penguin.ooo/api/v1/responses`)，预留 `vllm` 备用节点槽位 (`LLM_FALLBACK_ENABLED=false`)；
  3. 完整状态机主链路：
     用户消息输入 -> ContentGuard 输入护栏 -> EmotionTracker & EntityTracker
     -> asyncio.gather 并发执行 JevDecisionRouter 与 MultimodalRAGEngine / OrderTools
     -> StrategyInjector (SYSTEM_PROMPT_DYNAMIC_BOUNDARY 静态前缀冻结 + 动态策略后缀组装)
     -> 主生成模型 (/v1/responses) 流式生成 + 事实一致性校验兜底
     -> SlidingWindowStreamFilter 跨 Chunk 剥离协议标记与伪造神券词
     -> 依次推送 6 类标准 SSE 事件：decision_meta -> ui_cards -> text_delta -> followup_pills -> telemetry -> done。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.core.context_harness import DecisionLedgerState, HierarchicalContextHarness
from app.core.telemetry import count_tokens_exact
from app.services.admin_analytics_service import analytics_service
from app.services.constraint_extractor import ConstraintExtractor
from app.services.jev_router import JevDecisionRouter
from app.services.multimodal_rag import MultimodalRAGEngine
from app.services.order_tools import DeterministicOrderTools, get_order_tools
from app.services.rule_governance import (
    ContentGuard,
    EmotionTracker,
    EntityTracker,
    GuardAction,
    SlidingWindowStreamFilter,
)
from app.services.strategy_injector import StrategyInjector

logger = logging.getLogger(__name__)

VALID_IN_CATALOG_SUB_CATEGORIES = {
    "laptop_bag",
    "gan_charger",
    "mechanical_keyboard",
    "nc_headphones",
    "power_bank",
    "usb_hub_cable",
    "hoodie",
    "knit_cardigan",
    "windbreaker",
    "cargo_pants",
    "tshirt",
    "facial_cream",
    "amino_cleanser",
    "sunscreen",
    "b5_serum",
    "salicylic_pads",
    "lip_balm",
}


# ============================================================================
# 1. 无锁三态熔断器 (CLOSED / OPEN / HALF_OPEN) 与 EWMA 延迟感知路由器
# ============================================================================
@dataclass
class CircuitBreaker:
    """无锁三态熔断器：CLOSED -> OPEN -> HALF_OPEN -> CLOSED。"""

    node_name: str
    failure_threshold: int = 3
    recovery_timeout_sec: float = 15.0
    state: str = "CLOSED"
    failure_count: int = 0
    last_failure_ts: float = 0.0

    def allow_request(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if (time.monotonic() - self.last_failure_ts) >= self.recovery_timeout_sec:
                self.state = "HALF_OPEN"
                return True
            return False
        # HALF_OPEN 允许探测请求通过
        return True

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_ts = time.monotonic()
        if self.state == "HALF_OPEN" or self.failure_count >= self.failure_threshold:
            self.state = "OPEN"


@dataclass
class LLMGatewayNode:
    name: str
    protocol: str
    url: str
    api_key: str
    model: str
    enabled: bool = True
    ewma_latency_ms: float = 450.0
    alpha: float = 0.3
    breaker: CircuitBreaker = field(init=False)

    def __post_init__(self) -> None:
        self.breaker = CircuitBreaker(node_name=self.name)

    def update_latency(self, rtt_ms: float) -> None:
        self.ewma_latency_ms = round(
            self.alpha * rtt_ms + (1.0 - self.alpha) * self.ewma_latency_ms, 2
        )


class EWMALatencyRouter:
    """
    EWMA 延迟感知路由器 + 三态熔断器：
      - 主节点：`deepseek-flash-primary` (`POST https://go.penguin.ooo/api/v1/responses`)
      - 备用槽位：`vllm-fallback` (`settings.llm_fallback_enabled` 默认为 `False`，预留扩展)
    """

    def __init__(self) -> None:
        self.primary_node = LLMGatewayNode(
            name="deepseek-flash-primary",
            protocol=settings.llm_protocol,
            url=settings.llm_primary_responses_url,
            api_key=settings.llm_primary_api_key,
            model=settings.llm_primary_model,
            enabled=True,
            ewma_latency_ms=420.0,
        )
        self.fallback_node = LLMGatewayNode(
            name="vllm-fallback",
            protocol="chat_completions",
            url=settings.llm_fallback_base_url,
            api_key=settings.llm_fallback_api_key,
            model=settings.llm_fallback_model,
            enabled=settings.llm_fallback_enabled,
            ewma_latency_ms=250.0,
        )
        self.nodes: List[LLMGatewayNode] = [self.primary_node, self.fallback_node]

    def select_node(self) -> LLMGatewayNode:
        """按启用状态、熔断器放行状态与 EWMA 延迟最低原则选择生成节点。"""
        candidates = [
            n for n in self.nodes if n.enabled and n.breaker.allow_request()
        ]
        if not candidates:
            return self.primary_node
        candidates.sort(key=lambda n: n.ewma_latency_ms)
        return candidates[0]


# ============================================================================
# 2. 确定性零幻觉导购话术合成器（用于离线模式、熔断降级与事实保底）
# ============================================================================
def synthesize_deterministic_response(
    user_query: str,
    jev_decision: Dict[str, Any],
    emotion_signal: Dict[str, Any],
    rag_result: Optional[Dict[str, Any]],
    order_result: Optional[Dict[str, Any]],
    aftersales_result: Optional[Dict[str, Any]],
    guard_warning: Optional[str] = None,
    vision_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    基于 100% 真实结构化上下文生成零幻觉导购/客服回复（所有价格、规格、时间差与差价均直接取自真源卡片）。
    """
    primary_intent = jev_decision.get("primary_intent", "product_recommend")
    handoff_guard = bool(jev_decision.get("handoff_guard", False))
    parts: List[str] = []

    # 0. 多模态视觉识别特殊场景（非商品/模糊图、未上架商品三步应对法、在售商品看图前缀）
    if vision_info:
        v_scene = vision_info.get("scene_type", "")
        v_item = vision_info.get("recognized_item") or "图片中的物品"
        v_desc = vision_info.get("visual_description") or ""
        v_tip = (
            vision_info.get("related_category_tip")
            or "如果您还需要搭配 3C 数码配件（如 65W~100W 氮化镓快充头、磁吸充电宝、笔记本内胆包）、换季机能服饰或温和护肤品，随时告诉我预算，我为您同场严选。"
        )
        if v_scene == "non_product_or_blurry":
            desc_clause = f"（图中主要内容：{v_desc}）" if v_desc else ""
            parts.append(
                f"我已经仔细查看了您上传的图片{desc_clause}，但目前暂未识别出清晰具体的商品主体、包装规格铭牌或订单凭证信息。\n\n"
                "为了帮您精准匹配同款/平替或核验售后，建议您：\n"
                "1. **对准商品正面全貌或包装参数标签重新拍一张清晰照片**（点击右下角 `+` 号选择「拍照」或「照片」）；\n"
                "2. 或者直接用文字告诉我您的**目标品类、预算上限与核心规格要求**，我立即为您检索！"
            )
            return "\n\n".join(parts)

        if v_scene == "out_of_catalog_product":
            desc_sentence = f"（外观特征：{v_desc}）" if v_desc else ""
            parts.append(
                f"通过视觉识别，我已经看清您上传的图片是一台/件**「{v_item}」**{desc_sentence}。\n\n"
                f"- **库存核对说明**：非常抱歉，惠购官方自营库目前暂未上架**「{v_item}」**品类，已为您在上方弹出**【缺货商品心愿单】**卡片，您可以一键点选目标预算区间并登记到货提醒，采购团队将优先评估补货；\n"
                f"- **跨品类选品建议**：{v_tip}"
            )
            return "\n\n".join(parts)

        if v_scene == "in_catalog_product":
            desc_sentence = f"（{v_desc}）" if v_desc else ""
            parts.append(
                f"已通过 V4.1 原生多模态视觉识别出您上传的图片为**「{v_item}」**{desc_sentence}。"
            )

    # 1. 高危维权 / 挫败情绪前置安抚与人工专员接管说明
    if handoff_guard or emotion_signal.get("emotion_type") in ("negative", "frustrated"):
        parts.append(
            "非常理解您的心情，让您有不好的购物体验我们深感抱歉！系统已为您优先开启**【高级售后专员绿色通道】**，人工客服专员已同步接入跟进。"
        )
        if handoff_guard and not (aftersales_result or order_result or rag_result):
            return "\n\n".join(parts)

    # 2. 隐私红线守卫回复
    if primary_intent == "privacy_guard" or guard_warning == "privacy_probe":
        parts.append(
            "请您完全放心，我们严格遵循个人信息与隐私保护规范：**导购助手绝不在后台记录、构建或跨会话追踪您的个人消费画像与敏感隐私标签**，仅基于您在当前对话窗口中主动提出的需求为您提供客观选品与售后咨询服务。"
        )
        return "\n\n".join(parts)

    # 3. 售后资格与价保核验回复
    if primary_intent == "aftersales_qa" and aftersales_result and aftersales_result.get("aftersales_card"):
        ac = aftersales_result["aftersales_card"]
        parts.append(
            f"已为您调取订单 **`{ac['order_id']}`**（商品：**{ac['sku_title']}**，实付 **¥{ac['pay_amount']}**，当前到手价 **¥{ac['current_sku_price']}**）的秒级核验结果："
        )
        if "保价" in user_query or "价保" in user_query or "降价" in user_query or "差价" in user_query:
            if ac["price_guarantee_15d_eligible"]:
                parts.append(
                    f"- **15天价格保护核验通过**：距离签收仅过 **{ac['elapsed_signed_hours']} 小时**（在 **360 小时** 价保窗口期内），当前商品降价差额为 **¥{ac['price_diff_amount']}**，系统支持立即为您退还差价 **¥{ac['price_diff_amount']}**。"
                )
            else:
                parts.append(
                    f"- **15天价格保护核验说明**：当前状态为 `{ac['price_guarantee_15d_reason_code']}`——{ac['price_guarantee_15d_explanation']}。"
                )
        else:
            if ac["return_7d_eligible"]:
                parts.append(
                    f"- **7天无理由退货核验通过**：该订单已签收 **{ac['elapsed_signed_hours']} 小时**（未超 **168 小时** 上限），且商品状态为**完好未拆封**，符合平台7天无理由退货条款，且享运费险补贴。"
                )
            else:
                parts.append(
                    f"- **7天无理由退货核验说明**：当前状态为 `{ac['return_7d_reason_code']}`——{ac['return_7d_explanation']}。"
                )
                if ac["price_guarantee_15d_eligible"]:
                    parts.append(
                        f"- **补充权益提醒**：虽然已拆封不支持无理由退货，但该订单符合 **15天价格保护** 条件，可退还差价 **¥{ac['price_diff_amount']}**。"
                    )
        return "\n\n".join(parts)

    # 4. 订单物流状态查询回复
    if primary_intent == "order_query" and order_result and order_result.get("order_card"):
        oc = order_result["order_card"]
        parts.append(
            f"为您查到订单 **`{oc['order_id']}`**（**{oc['sku_title']}**，实付 **¥{oc['pay_amount']}**）的最新物流进展：\n"
            f"- **当前状态**：{oc['order_status_zh']}\n"
            f"- **承运物流**：{oc['logistics_company']}（运单号：`{oc['tracking_number']}`）\n"
            f"- **最新轨迹**：{oc['latest_logistics_event']}"
        )
        return "\n\n".join(parts)

    # 5. 商品推荐与双路径约束松弛回复
    if rag_result and rag_result.get("product_cards"):
        c_status = rag_result.get("constraint_status", "satisfied")
        cards = rag_result["product_cards"]
        p_map = {
            pc["sku_id"]: pc
            for pc in (rag_result.get("price_explain_cards") or [])
            if isinstance(pc, dict) and pc.get("sku_id")
        }

        if c_status == "relaxed":
            reason = rag_result.get("relaxation_reason") or "当前预算与目标规格存在冲突，已为您生成双路径折中方案："
            parts.append(f"理解您的核心诉求！经检索核对，{reason}")
            for card in cards:
                sid = card["sku_id"]
                pc = p_map.get(sid, {})
                disc = pc.get(
                    "discount_amount",
                    round(float(card["original_price"]) - float(card["final_price"]), 2),
                )
                path_label = (
                    "路径 A【保规格·微调预算】"
                    if card.get("relaxation_path") == "relax_budget"
                    else "路径 B【严守预算·微调规格】"
                )
                hl = "、".join(card.get("highlights", [])[:3])
                parts.append(
                    f"- **{path_label}**：推荐 **{card['title']}**（`{sid}`），当前到手价 **¥{card['final_price']}**（日常原价 **¥{card['original_price']}**，官方直降立减 **¥{disc}**）。标签：{card.get('match_badge')}；核心亮点：{hl}。"
                )
            parts.append("您可以根据更看重**核心规格达标**还是**严守预算上限**点击下方卡片或追问胶囊查看详情！")
            return "\n\n".join(parts)

        # 满足硬约束的正常混合检索推荐
        parts.append("为您从惠购官方自营商品池中精选出最匹配硬约束与实测口碑的款式：")
        for idx, card in enumerate(cards[:3], 1):
            sid = card["sku_id"]
            pc = p_map.get(sid, {})
            disc = pc.get(
                "discount_amount",
                round(float(card["original_price"]) - float(card["final_price"]), 2),
            )
            hl = "、".join(card.get("highlights", [])[:3])
            parts.append(
                f"{idx}. **{card['title']}**（`{sid}`）：当前到手价 **¥{card['final_price']}**（日常原价 **¥{card['original_price']}**，官方直降立减 **¥{disc}**）。核心亮点：{hl}。"
            )
        parts.append("您可以点击下方商品卡片查看完整参数与立减明细，或在下方追问胶囊中微调预算与规格！")
        return "\n\n".join(parts)

    # 6. 开放式提问、日常知识、外部平台对比或通用导购引导（离线确定性兜底）
    q_lower = (user_query or "").lower()
    if (
        guard_warning == "competitor"
        or any(k in q_lower for k in ("淘宝", "天猫", "千问", "京东", "拼多多", "唯品会", "得物", "什么平台", "哪家平台", "正品", "货源", "靠谱吗"))
    ):
        parts.append(
            "我是**惠购官方首席选品顾问**。**惠购（Huigou）**是官方直营与品牌严选平台，与传统综合电商平台相比，我们坚持三大核心标准：\n"
            "1. **官方直营与全检溯源**：在售 **3C数码、换季服饰、美妆个护** 全量商品均由品牌直发并通过入仓质检；\n"
            "2. **无套路真直降**：废除跨店满减与复杂凑单，直接透明呈现**日常原价**与**当前到手价**；\n"
            "3. **确定性售后兜底**：全品类享签收次日零点起 **7 天无理由退货（168小时）**、**15 天自动价保退差（360小时）** 及退货运费险补贴。\n\n"
            "您可以直接告诉我您的目标品类、预算上限或具体规格参数，我为您做客观选品对比！"
        )
        return "\n\n".join(parts)

    if any(k in q_lower for k in ("天气", "下雨", "降温", "穿什么", "穿搭", "换季", "露营", "徒步", "防晒", "紫外线")):
        parts.append(
            "作为**惠购官方首席选品顾问**，针对换季出行与天气变化，给您一套实用的穿搭建议：\n"
            "- **降温/阴雨/户外徒步**：建议采用「三层穿衣法」，外层优选具备高防风防水指数的**三合一冲锋衣**（带可拆卸抓绒内胆，冷热皆宜），下装搭配耐磨防泼水工装裤或高弹垂感运动卫裤；\n"
            "- **晴热/强紫外线通勤**：优选具备 **UPF50+ 原纱防晒认证**、冰丝凉感透气的防晒衣，内搭高克重重磅纯棉 T 恤，兼顾透气与版型。\n\n"
            "您可以告诉我您的**预算范围（如 200 元以内）**或具体场景，我立即从惠购服饰严选库中为您匹配实测好款！"
        )
        return "\n\n".join(parts)

    if any(k in q_lower for k in ("送礼", "礼物", "男朋友", "女朋友", "生日", "纪念日", "父母", "长辈", "买什么好", "好物")):
        parts.append(
            "作为**惠购官方首席选品顾问**，为您梳理三个高口碑、不踩雷的严选送礼方向：\n"
            "1. **实用 3C 数码（送男生/通勤党首选）**：如 **65W~100W 氮化镓多口快充头**、**3C 认证超薄磁吸充电宝** 或 **防震电脑内胆包**，高频刚需且参数扎实；\n"
            "2. **品质换季穿搭（男女皆宜）**：如 **三合一防风防水冲锋衣** 或 **UPF50+ 冰丝防晒衣**，实穿度极高；\n"
            "3. **温和美妆个护（送女生/贴心关怀）**：如 **氨基酸温和洁面慕斯**、**玻尿酸修护精华/面霜**，敏感肌也能安心用。\n\n"
            "您可以告诉我您的**预算上限（如 100 元内 / 300 元内）**和收礼对象，我马上为您精准锁定款式！"
        )
        return "\n\n".join(parts)

    if any(k in q_lower for k in ("氮化镓", "gan", "pd快充", "毫安", "额定容量", "氨基酸", "敏感肌", "烟酰胺", "玻尿酸", "防水指数", "upf")):
        parts.append(
            "作为**惠购官方首席选品顾问**，为您一针见血拆解核心参数门道：\n"
            "- **数码充电看真实输出与认证**：氮化镓（GaN）体积小发热低，笔记本建议选 **65W 及以上多口 PD 快充**；充电宝需认准 **3C 认证（可上飞机）** 及额定容量转化率；\n"
            "- **美妆护肤看核心成分与温和度**：敏感肌优先选 **纯氨基酸表活、无酒精无香精** 配方；修护屏障看神经酰胺与泛醇（B5），提亮看烟酰胺浓度；\n"
            "- **功能服饰看硬核指标**：防晒衣认准 **UPF50+ 原纱防晒**（水洗不衰减），冲锋衣重点看防水透湿指数与压胶工艺。\n\n"
            "如果您有心仪的品类和预算，直接发给我，我帮您按硬核参数筛出最高性价比的严选款！"
        )
        return "\n\n".join(parts)

    parts.append(
        "您好！我是**惠购官方首席选品顾问**。惠购坚持「官方直营·品牌严选·无套路真直降」，为您提供 **3C数码、换季服饰、美妆个护** 的参数级精准选品、**预算与规格冲突双路径推荐**，以及 **订单物流追踪与 7 天无理由（168h）/ 15 天自动价保（360h）秒级核验**。您可以直接告诉我目标品类、预算上限或具体规格要求！"
    )
    return "\n\n".join(parts)


def format_sse_event(event: str, data: Any) -> str:
    """将事件名与载荷序列化为标准 SSE 帧 (`event: <name>\\ndata: <payload>\\n\\n`)。"""
    if isinstance(data, str):
        payload_str = data
    else:
        payload_str = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload_str}\n\n"


# ============================================================================
# 3. 272K 全量对话状态机与多事件 SSE 编排器
# ============================================================================
class ShoppingAgentOrchestrator:
    """
    电商导购 Agent 核心状态机 (`chat_orchestrator.py`)：
      - 锁死有效上下文窗口 `context_window_limit = 272000`；
      - Jev System One 与预检索通过 `asyncio.gather` 异步并发执行；
      - 支持 `/v1/responses` & `/v1/chat/completions` (`deepseek-flash`) 原生多模态视觉识别与流式生成。
    """

    def __init__(
        self,
        jev_router: Optional[JevDecisionRouter] = None,
        rag_engine: Optional[MultimodalRAGEngine] = None,
        order_tools: Optional[DeterministicOrderTools] = None,
        strategy_injector: Optional[StrategyInjector] = None,
        llm_router: Optional[EWMALatencyRouter] = None,
        context_window_limit: Optional[int] = None,
    ) -> None:
        self.context_window_limit = (
            context_window_limit or settings.effective_context_window
        )
        self.jev_router = jev_router or JevDecisionRouter()
        self.rag_engine = rag_engine or MultimodalRAGEngine()
        self.order_tools = order_tools or get_order_tools()
        self.extractor = ConstraintExtractor()
        self.strategy_injector = strategy_injector or StrategyInjector(
            context_window_limit=self.context_window_limit
        )
        self.llm_router = llm_router or EWMALatencyRouter()

        # 加载 60-SKU 图像指纹库 (image_manifest.json + ecommerce_catalog_60skus.json)，用于 0ms 精确识别在售商品原图及离线视觉兜底
        self._image_sha256_map: Dict[str, Dict[str, Any]] = {}
        try:
            catalog_sku_map: Dict[str, Dict[str, Any]] = {}
            if settings.catalog_path.exists():
                cat_obj = json.loads(settings.catalog_path.read_text(encoding="utf-8"))
                if isinstance(cat_obj, list):
                    skus_list = cat_obj
                elif isinstance(cat_obj, dict):
                    skus_list = cat_obj.get("products") or cat_obj.get("skus") or []
                else:
                    skus_list = []
                for p in skus_list:
                    if isinstance(p, dict) and p.get("sku_id"):
                        catalog_sku_map[str(p["sku_id"])] = p

            if settings.image_manifest_path.exists():
                manifest_obj = json.loads(
                    settings.image_manifest_path.read_text(encoding="utf-8")
                )
                entries = (
                    manifest_obj.get("images")
                    if isinstance(manifest_obj.get("images"), dict)
                    else manifest_obj
                )
                for sku_id, item in (entries or {}).items():
                    if isinstance(item, dict) and item.get("sha256"):
                        cat_p = catalog_sku_map.get(str(sku_id), {})
                        enriched = dict(item)
                        enriched.setdefault("sku_id", str(sku_id))
                        enriched.setdefault("title", cat_p.get("title", str(sku_id)))
                        enriched.setdefault("sub_category", cat_p.get("sub_category"))
                        self._image_sha256_map[str(item["sha256"])] = enriched
        except Exception as exc:
            logger.warning(f"[ShoppingAgentOrchestrator] 加载 image_manifest.json 失败: {exc}")

    async def _analyze_uploaded_image(
        self,
        image_base64: str,
        user_query: str,
        node: LLMGatewayNode,
        allow_remote_llm: bool = True,
        missing_item_keywords: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        V4.1 (deepseek-flash) 原生多模态视觉预解析：
          1. 若上传图片 SHA-256 命中 60-SKU 图像 Manifest，0ms 精准返回对应在售 SKU 与细分类目；
          2. 否则调用 V4.1 原生多模态接口提取结构化 JSON (scene_type, recognized_item, sub_category, visual_description, visual_keywords, related_category_tip)；
          3. 在离线模式或网络抖动时提供 100% 确定性降级识别。
        """
        raw_str = (image_base64 or "").strip()
        mime_type = "image/jpeg"
        raw_b64 = raw_str
        if raw_str.startswith("data:"):
            header, _, b64_part = raw_str.partition(",")
            raw_b64 = b64_part.strip()
            m = re.match(r"data:(image/[a-zA-Z0-9.+-]+);base64", header)
            if m:
                mime_type = m.group(1)
        data_url = f"data:{mime_type};base64,{raw_b64}"

        # Step A: 0ms 校验是否命中 60-SKU 官方商品图 SHA-256 指纹
        try:
            img_bytes = base64.b64decode(raw_b64)
            sha256_hex = hashlib.sha256(img_bytes).hexdigest()
            if sha256_hex in self._image_sha256_map:
                entry = self._image_sha256_map[sha256_hex]
                return {
                    "scene_type": "in_catalog_product",
                    "recognized_item": entry.get("title") or entry.get("sub_category") or "在售商品",
                    "matched_sku_id": entry.get("sku_id"),
                    "sub_category": entry.get("sub_category"),
                    "visual_description": entry.get("calibrated_visual_description") or "",
                    "visual_keywords": f"{entry.get('title', '')} {entry.get('sub_category', '')}".strip(),
                    "related_category_tip": "",
                    "data_url": data_url,
                    "vision_source": "manifest_sha256_exact",
                }
        except Exception:
            pass

        # Step B: 调用 V4.1 (deepseek-flash) 原生多模态接口进行结构化视觉理解
        if allow_remote_llm and node.api_key:
            target_url = node.url.rstrip("/")
            if not target_url.endswith("/chat/completions"):
                if target_url.endswith("/responses"):
                    target_url = target_url[: -len("/responses")] + "/chat/completions"
                else:
                    target_url = f"{target_url}/chat/completions"
            effective_model = node.model
            if effective_model in ("api-v4.1-flash", "deepseek-v4.1-flash"):
                effective_model = "deepseek-flash"

            vision_prompt = (
                "你是惠购官方首席选品顾问的原生视觉识别模块。请仔细观察用户上传的图片，并结合用户的文字提问："
                f"「{user_query}」\n"
                "惠购当前在售的 17 个细分类目英文 key 列表如下：\n"
                "- 3C数码：laptop_bag(笔记本电脑包/内胆包), gan_charger(氮化镓快充头/充电器), mechanical_keyboard(机械键盘), "
                "nc_headphones(降噪耳机/头戴式耳机), power_bank(充电宝/移动电源), usb_hub_cable(扩展坞/Type-C数据线)\n"
                "- 换季服饰：hoodie(连帽卫衣), knit_cardigan(针织开衫), windbreaker(三合一冲锋衣/防风外套), cargo_pants(工装裤), tshirt(纯棉T恤)\n"
                "- 美妆个护：facial_cream(保湿修护面霜), amino_cleanser(氨基酸洁面乳), sunscreen(防晒霜), b5_serum(B5修护精华液), "
                "salicylic_pads(水杨酸棉片), lip_balm(润唇膏)\n\n"
                "请判断图片属于以下哪一种场景（scene_type）：\n"
                "1. in_catalog_product：图片中的核心商品属于上述 17 个在售细分类目之一；\n"
                "2. out_of_catalog_product：图片中是明确的具体商品（如空气炸锅、咖啡机、跑鞋、运动鞋、鼠标、手机、手表、马克杯、猫粮、吹风机、口红、香水等），但不属于上述 17 个在售子类目；\n"
                "3. aftersales_evidence：图片是订单截图、物流破损照片、瑕疵凭证或用户明确要求退换货/价保核验；\n"
                "4. non_product_or_blurry：图片是纯风景、表情包、人物自拍、纯色无意义图案或过于模糊无法辨认具体商品。\n\n"
                "请严格只输出一个合法的 JSON 对象（禁止输出 Markdown 代码块标记），字段如下：\n"
                "{\n"
                '  "scene_type": "in_catalog_product" | "out_of_catalog_product" | "aftersales_evidence" | "non_product_or_blurry",\n'
                '  "recognized_item": "识别出的具体物品中文名称（如：复古98键机械键盘、不锈钢空气炸锅、黑色越野跑鞋、纯色测试图案等）",\n'
                '  "sub_category": "若 scene_type 为 in_catalog_product 则填写上述 17 个英文 key 之一，否则填 null",\n'
                '  "visual_description": "35~65字客观描述图中物品的颜色、材质、外形结构或细节特征",\n'
                '  "visual_keywords": "2~4个核心中文检索词（空格分隔）",\n'
                '  "related_category_tip": "若为 out_of_catalog_product，给出一句与惠购在售3C/服饰/美妆品类自然联动的贴心搭配建议"\n'
                "}"
            )
            headers = {
                "Authorization": f"Bearer {node.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": effective_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "thinking": {"type": "disabled"},
                "stream": False,
                "max_tokens": 320,
            }
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(timeout=12.0) as client:
                        resp = await client.post(target_url, headers=headers, json=payload)
                    if resp.status_code == 200:
                        resp_json = resp.json()
                        raw_txt = (
                            ((resp_json.get("choices") or [{}])[0].get("message") or {}).get("content")
                            or ""
                        ).strip()
                        cleaned_txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_txt, flags=re.IGNORECASE).strip()
                        m_json = re.search(r"\{.*\}", cleaned_txt, flags=re.DOTALL)
                        if m_json:
                            parsed = json.loads(m_json.group(0))
                            scene = str(parsed.get("scene_type") or "").strip()
                            rec_item = str(parsed.get("recognized_item") or "图中物品").strip()
                            sub_cat = parsed.get("sub_category")
                            if isinstance(sub_cat, str):
                                sub_cat = sub_cat.strip()
                            if sub_cat not in VALID_IN_CATALOG_SUB_CATEGORIES:
                                # 尝试用 ConstraintExtractor 从识别名称与关键词中反推子类目
                                ext_probe = self.extractor.extract(
                                    f"{rec_item} {parsed.get('visual_keywords', '')}"
                                )
                                sub_cat = ext_probe.get("sub_category")

                            # 若识别出的物品命中缺货品类词表，强制归类为 out_of_catalog_product
                            if missing_item_keywords and any(
                                kw in rec_item for kw in missing_item_keywords
                            ):
                                scene = "out_of_catalog_product"
                                sub_cat = None
                            elif scene == "in_catalog_product" and not sub_cat:
                                scene = "out_of_catalog_product"
                            elif sub_cat in VALID_IN_CATALOG_SUB_CATEGORIES and scene not in (
                                "aftersales_evidence",
                                "non_product_or_blurry",
                            ):
                                scene = "in_catalog_product"

                            if scene not in (
                                "in_catalog_product",
                                "out_of_catalog_product",
                                "aftersales_evidence",
                                "non_product_or_blurry",
                            ):
                                scene = "in_catalog_product" if sub_cat else "out_of_catalog_product"

                            return {
                                "scene_type": scene,
                                "recognized_item": rec_item,
                                "matched_sku_id": None,
                                "sub_category": sub_cat if scene == "in_catalog_product" else None,
                                "visual_description": str(parsed.get("visual_description") or "").strip(),
                                "visual_keywords": str(parsed.get("visual_keywords") or rec_item).strip(),
                                "related_category_tip": str(parsed.get("related_category_tip") or "").strip(),
                                "data_url": data_url,
                                "vision_source": "v4_1_native_multimodal",
                            }
                except Exception as exc:
                    if attempt == 0:
                        await asyncio.sleep(0.25)
                        continue
                    logger.warning(f"[ShoppingAgentOrchestrator] V4.1 视觉预解析降级: {exc}")

        # Step C: 离线确定性视觉降级（结合用户附加文本与特征词推断）
        q_text = (user_query or "").strip()
        if any(k in q_text for k in ("退货", "换货", "破损", "瑕疵", "保价", "价保", "差价", "ORD-")):
            return {
                "scene_type": "aftersales_evidence",
                "recognized_item": "订单与售后凭证图片",
                "matched_sku_id": None,
                "sub_category": None,
                "visual_description": "用户上传的售后或订单核验凭证图片",
                "visual_keywords": q_text,
                "related_category_tip": "",
                "data_url": data_url,
                "vision_source": "deterministic_offline_fallback",
            }

        if missing_item_keywords:
            for kw in missing_item_keywords:
                if kw in q_text:
                    return {
                        "scene_type": "out_of_catalog_product",
                        "recognized_item": kw,
                        "matched_sku_id": None,
                        "sub_category": None,
                        "visual_description": f"图中展示的「{kw}」实物外观细节清晰",
                        "visual_keywords": kw,
                        "related_category_tip": "如果您日常出行或居家还需要搭配 65W~100W 氮化镓快充头、磁吸充电宝、防风冲锋衣或温和护肤品，可以随时告诉我预算，我为您同场严选。",
                        "data_url": data_url,
                        "vision_source": "deterministic_offline_fallback",
                    }

        ext_q = self.extractor.extract(q_text)
        if ext_q.get("sub_category"):
            return {
                "scene_type": "in_catalog_product",
                "recognized_item": q_text or ext_q["sub_category"],
                "matched_sku_id": None,
                "sub_category": ext_q["sub_category"],
                "visual_description": "根据您上传的商品图片与规格偏好匹配到的同品类款式",
                "visual_keywords": q_text,
                "related_category_tip": "",
                "data_url": data_url,
                "vision_source": "deterministic_offline_fallback",
            }

        return {
            "scene_type": "non_product_or_blurry",
            "recognized_item": "未识别出明确在售商品主体",
            "matched_sku_id": None,
            "sub_category": None,
            "visual_description": "图片画面未包含清晰的商品主体或规格参数标签",
            "visual_keywords": "",
            "related_category_tip": "",
            "data_url": data_url,
            "vision_source": "deterministic_offline_fallback",
        }

    def _trim_history_to_window(
        self,
        history: List[Dict[str, str]],
        reserved_tokens: int,
    ) -> tuple[List[Dict[str, str]], int]:
        """
        基于有效上下文窗口 `272K` 对多轮历史消息执行微压缩与窗口预算裁剪：
          返回裁剪后的历史列表与精确历史 Token 计数。
        """
        budget = max(self.context_window_limit - reserved_tokens - 4096, 2048)
        kept: List[Dict[str, str]] = []
        used_tokens = 0
        for msg in reversed(history or []):
            role = str(msg.get("role", "user"))
            content = str(msg.get("content", ""))
            t_cnt = count_tokens_exact(content) + 4
            if used_tokens + t_cnt > budget:
                break
            kept.append({"role": role, "content": content})
            used_tokens += t_cnt
        kept.reverse()
        return kept, used_tokens

    async def _stream_remote_responses_api(
        self,
        node: LLMGatewayNode,
        instructions: str,
        input_text: str,
        image_data_url: Optional[str] = None,
        timeout_sec: float = 18.0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        调用主生成模型流式接口，同时兼容：
          1. OpenAI `chat` 协议 (`POST /v1/chat/completions`)：
             - `messages[0]` (`role="system"`) 传入 100% 字节级恒定的静态系统提示词前缀 (`static_prefix`)，触发服务端硬件级 KV Prefix Cache；
             - `messages[1]` (`role="user"`) 传入动态策略后缀、多轮历史、当前用户消息与可选的多模态 `image_url`；
          2. OpenAI `responses` 协议 (`POST /v1/responses`)：
             - `instructions` 传入 `static_prefix`，`input` 传入 `input_text`。
        """
        headers = {
            "Authorization": f"Bearer {node.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        is_chat_protocol = (
            node.protocol.lower() in ("chat", "chat_completions")
            or node.url.rstrip("/").endswith("/chat/completions")
            or node.url.rstrip("/").endswith("/v1")
        )
        effective_model = node.model
        if effective_model in ("api-v4.1-flash", "deepseek-v4.1-flash"):
            effective_model = "deepseek-flash"

        if is_chat_protocol:
            target_url = node.url.rstrip("/")
            if not target_url.endswith("/chat/completions"):
                target_url = f"{target_url}/chat/completions"
            user_content: Any = (
                [
                    {"type": "text", "text": input_text},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ]
                if image_data_url
                else input_text
            )
            payload: Dict[str, Any] = {
                "model": effective_model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": user_content},
                ],
                "thinking": {"type": "disabled"},
                "stream": True,
            }
        else:
            target_url = node.url
            payload = {
                "model": effective_model,
                "instructions": instructions,
                "input": input_text,
                "reasoning": {"effort": "low"},
                "stream": True,
            }

        emitted_any = False
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout_sec) as client:
                    async with client.stream(
                        "POST", target_url, headers=headers, json=payload
                    ) as resp:
                        if resp.status_code != 200:
                            body = await resp.aread()
                            raise RuntimeError(
                                f"LLM stream HTTP {resp.status_code} ({target_url}): {body[:200]!r}"
                            )

                        current_event = ""
                        async for raw_line in resp.aiter_lines():
                            line = raw_line.strip()
                            if not line:
                                continue
                            if line.startswith("event:"):
                                current_event = line[6:].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                evt_obj = json.loads(data_str)
                            except Exception:
                                continue

                            if current_event == "error" or evt_obj.get("type") == "error":
                                err_msg = (evt_obj.get("error") or {}).get("message") or str(evt_obj)
                                raise RuntimeError(f"LLM stream error event: {err_msg}")

                            if is_chat_protocol or evt_obj.get("object") == "chat.completion.chunk":
                                choices = evt_obj.get("choices") or []
                                if choices:
                                    delta_obj = choices[0].get("delta") or {}
                                    delta_txt = delta_obj.get("content")
                                    if delta_txt:
                                        emitted_any = True
                                        yield {"type": "delta", "delta": delta_txt}
                                usage_raw = evt_obj.get("usage")
                                if isinstance(usage_raw, dict) and usage_raw:
                                    cached_tok = int(
                                        (usage_raw.get("prompt_tokens_details") or {}).get("cached_tokens")
                                        or usage_raw.get("prompt_cache_hit_tokens")
                                        or 0
                                    )
                                    yield {
                                        "type": "usage",
                                        "usage": {
                                            "input_tokens": int(usage_raw.get("prompt_tokens") or 0),
                                            "output_tokens": int(usage_raw.get("completion_tokens") or 0),
                                            "input_tokens_details": {"cached_tokens": cached_tok},
                                        },
                                    }
                            else:
                                evt_type = evt_obj.get("type") or current_event
                                if evt_type == "response.output_text.delta":
                                    delta_txt = evt_obj.get("delta", "")
                                    if delta_txt:
                                        emitted_any = True
                                        yield {"type": "delta", "delta": delta_txt}
                                elif evt_type == "response.completed":
                                    resp_obj = evt_obj.get("response") or {}
                                    usage_obj = resp_obj.get("usage") or {}
                                    yield {"type": "usage", "usage": usage_obj}
                break
            except Exception as exc:
                if attempt == 0 and not emitted_any:
                    await asyncio.sleep(0.25)
                    continue
                raise exc

    async def stream_chat(
        self,
        query: str,
        session_id: str = "default-session",
        history: Optional[List[Dict[str, str]]] = None,
        refresh_jev: bool = False,
        allow_remote_llm: bool = True,
        allow_remote_embedding: bool = False,
        retrieval_mode: str = "hybrid_rrf",
        tenant_id: str = "tenant_all",
        client_ip: str = "127.0.0.1",
        user_agent: str = "Mozilla/5.0",
        image_base64: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """
        执行全链路状态机并按序 yield 标准 SSE 帧：
          1. `event: decision_meta`
          2. `event: ui_cards`
          3. `event: text_delta`
          4. `event: followup_pills`
          5. `event: telemetry`
          6. `event: done`
        """
        t_start = time.perf_counter()
        ttft_ms: Optional[float] = None
        clean_query = (query or "").strip()
        has_image = bool(image_base64 and image_base64.strip())
        if not clean_query and has_image:
            clean_query = "请帮我识别这张图片中的商品并推荐合适的款式"
        conv_history = list(history or [])
        message_id = f"msg-{uuid.uuid4().hex[:12]}"

        # ====================================================================
        # Step 1: ContentGuard 输入安全护栏（零延迟同步拦截）
        # ====================================================================
        input_guard = ContentGuard.check_input(clean_query)
        emotion_sig = EmotionTracker.detect(conv_history, clean_query)
        entities = EntityTracker.extract(conv_history, clean_query)

        if input_guard.action == GuardAction.BLOCK:
            # 输入触发 Prompt 注入或违法违禁词，Fail-Closed 安全阻断并输出完整 6 类 SSE 事件
            blocked_meta = {
                "message_id": message_id,
                "session_id": session_id,
                "tenant_id": tenant_id,
                "client_ip": client_ip,
                "primary_intent": "privacy_guard",
                "intent_confidence": 1.0,
                "confidence_tier": "high",
                "intent_probabilities": {
                    "product_recommend": 0.0,
                    "order_query": 0.0,
                    "aftersales_qa": 0.0,
                    "privacy_guard": 1.0,
                    "chitchat": 0.0,
                },
                "need_rag": False,
                "need_rag_score": 0.0,
                "is_context_dependent": False,
                "handoff_guard": False,
                "handoff_guard_score": 0.0,
                "buying_readiness_raw": 0.0,
                "buying_readiness_score": 1.0,
                "decision_source": "content_guard_block",
                "fallback_triggered": False,
                "guard_category": input_guard.category,
                "extracted_constraints": {},
                "constraint_status": "none",
                "relaxation_reason": None,
                "sub_category": None,
                "jev_rtt_ms": 0.0,
                "has_image": has_image,
                "vision_recognized_item": None,
                "vision_scene_type": None,
            }
            empty_cards = {
                "product_cards": [],
                "price_explain_cards": [],
                "order_cards": [],
                "aftersales_card": None,
                "handoff_card": None,
            }
            yield format_sse_event("decision_meta", blocked_meta)
            yield format_sse_event("ui_cards", empty_cards)
            safe_msg = input_guard.safe_reply or "抱歉，您的请求触发了平台安全护栏，我是电商导购助手，很高兴为您提供选品与订单售后咨询。"
            ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
            yield format_sse_event("text_delta", {"delta": safe_msg})
            yield format_sse_event(
                "followup_pills",
                {"pills": ["帮我推荐一款 20 元以内的 17 寸电脑包", "查下我的订单发货状态", "了解 7 天无理由退货规则"]},
            )
            prompt_pkg = self.strategy_injector.compile_prompt(
                user_query=clean_query,
                jev_decision=blocked_meta,
                guard_warning=input_guard.category,
            )
            align = prompt_pkg["alignment_metrics"]
            total_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
            blocked_telemetry = {
                "ttft_ms": ttft_ms,
                "total_rtt_ms": total_ms,
                "rag_retrieve_ms": 0.0,
                "jev_rtt_ms": 0.0,
                "index_mode": "none",
                "db_backend": self.rag_engine.db_backend,
                "recall_channels": {
                    "sub_category_pool": 0,
                    "hard_filtered": 0,
                    "bm25_hits": 0,
                    "pgvector_halfvec_hits": 0,
                    "relaxed_candidates": 0,
                },
                "prefix_cache_aligned": align["prefix_cache_aligned"],
                "static_prefix_md5": align["static_prefix_md5"],
                "static_prefix_tokens": align["static_prefix_tokens"],
                "dynamic_suffix_tokens": align["dynamic_suffix_tokens"],
                "static_ratio_pct": align["static_ratio_pct"],
                "tokenizer": align["tokenizer"],
                "context_window_limit": self.context_window_limit,
                "history_tokens": 0,
                "jev_input_tokens": 0,
                "jev_output_tokens": 0,
                "jev_cache_hit": False,
                "llm_input_tokens": 0,
                "llm_output_tokens": count_tokens_exact(safe_msg),
                "llm_cached_tokens": 0,
                "gateway_node": "content_guard_shortcircuit",
                "circuit_breaker_state": self.llm_router.primary_node.breaker.state,
                "rtc_voice_enabled": settings.enable_rtc_voice,
                "has_image": has_image,
                "vision_recognized_item": None,
                "vision_scene_type": None,
            }
            yield format_sse_event("telemetry", blocked_telemetry)
            analytics_service.record_chat_turn(
                message_id=message_id,
                session_id=session_id,
                tenant_id=tenant_id,
                client_ip=client_ip,
                user_agent=user_agent,
                user_query=clean_query,
                ai_response=safe_msg,
                decision_meta=blocked_meta,
                ui_cards=empty_cards,
                telemetry=blocked_telemetry,
            )
            yield format_sse_event("done", "[DONE]")
            return

        # ====================================================================
        # Step 2: 多模态视觉预解析 + 多轮上下文类目锁继承 + asyncio.gather 并发执行
        # ====================================================================
        extracted = self.extractor.extract(clean_query)

        # 全平台未覆盖品类词表（优先检测当前句是否切换到未上架新品类）
        missing_item_keywords = [
            "空气炸锅", "电饭煲", "微波炉", "咖啡机", "破壁机", "榨汁机", "吹风机", "吸尘器",
            "扫地机器人", "洗地机", "洗衣机", "冰箱", "空调", "电视", "投影仪", "显卡",
            "主板", "内存条", "固态硬盘", "显示器", "无线鼠标", "电竞鼠标", "鼠标", "游戏手柄",
            "手机", "平板", "iPad", "智能手表", "手环", "相机", "无人机", "筋膜枪",
            "电动牙刷", "剃须刀", "越野跑鞋", "跑鞋", "运动鞋", "篮球鞋", "球鞋", "皮鞋",
            "马丁靴", "连衣裙", "半身裙", "牛仔裤", "羽绒服", "行李箱", "拉杆箱", "墨镜",
            "冻干猫粮", "猫粮", "狗粮", "猫砂", "咖啡", "零食", "奶粉", "香水", "口红",
            "粉底液", "眼影", "洗发水", "沐浴露", "护手霜", "马克杯", "水杯", "保温杯",
        ]
        has_missing_kw_in_current = any(kw in clean_query for kw in missing_item_keywords)

        vision_info: Optional[Dict[str, Any]] = None
        if has_image and image_base64:
            vision_info = await self._analyze_uploaded_image(
                image_base64=image_base64,
                user_query=clean_query,
                node=self.llm_router.select_node(),
                allow_remote_llm=allow_remote_llm,
                missing_item_keywords=missing_item_keywords,
            )
            if vision_info.get("scene_type") == "in_catalog_product":
                v_sub = vision_info.get("sub_category")
                if v_sub in VALID_IN_CATALOG_SUB_CATEGORIES:
                    extracted["sub_category"] = v_sub
                    extracted["matched_sub_categories"] = [v_sub]
                    extracted["sub_category_ambiguity"] = "none"
            elif vision_info.get("scene_type") == "out_of_catalog_product":
                has_missing_kw_in_current = True

        # 若当前追问（如“把预算放宽到 230 元以内，再帮我找一轮最合适的款式”）省略了商品名，
        # 自动从最近的历史对话轮次继承 sub_category 类目锁与未被覆盖的规格条件，防止跨类目漂移到 SKU-3C-001（笔记本内胆包）
        effective_rag_query = clean_query
        if vision_info and vision_info.get("scene_type") == "in_catalog_product":
            effective_rag_query = (
                f"{vision_info.get('recognized_item', '')} "
                f"{vision_info.get('visual_keywords', '')} "
                f"{clean_query}"
            ).strip()
        elif not extracted.get("sub_category") and not has_missing_kw_in_current and conv_history:
            user_hist = [m for m in reversed(conv_history) if m.get("role") == "user"]
            all_hist = user_hist + [m for m in reversed(conv_history) if m.get("role") != "user"]
            for h_msg in all_hist:
                h_text = str(h_msg.get("content") or "").strip()
                if not h_text:
                    continue
                h_ext = self.extractor.extract(h_text)
                if h_ext.get("sub_category"):
                    inherited_sub = h_ext["sub_category"]
                    extracted["sub_category"] = inherited_sub
                    extracted["matched_sub_categories"] = [inherited_sub]
                    extracted["sub_category_ambiguity"] = "none"
                    effective_rag_query = f"{inherited_sub} {clean_query}"

                    # 继承历史对话中的软约束偏好（不强锁上一轮已冲突的硬规格，使放宽预算后能正常筛出同品类候选）
                    if h_msg.get("role") == "user":
                        for sc in h_ext.get("soft_constraints") or []:
                            if sc not in (extracted.get("soft_constraints") or []):
                                extracted.setdefault("soft_constraints", []).append(sc)
                    break

        jev_task = asyncio.create_task(
            self.jev_router.evaluate_async(
                state=clean_query,
                messages=conv_history,
                refresh=refresh_jev,
            )
        )
        # 并发启动 RAG 预检索（若最终意图不需要 RAG 则丢弃结果，零串行等待损耗）
        rag_pre_task = asyncio.create_task(
            self.rag_engine.search_async(
                query=effective_rag_query,
                extracted_constraints=extracted,
                mode=retrieval_mode,
                allow_remote_embedding=allow_remote_embedding,
            )
        )
        jev_decision, pre_rag_result = await asyncio.gather(jev_task, rag_pre_task)

        # 若上传图片命中了 60-SKU 原图 SHA-256 指纹且 pre_rag_result 有商品卡，确保对应同款排在首位
        if (
            vision_info
            and vision_info.get("matched_sku_id")
            and pre_rag_result
            and pre_rag_result.get("product_cards")
        ):
            target_sku_id = vision_info["matched_sku_id"]
            cards = list(pre_rag_result["product_cards"])
            cards.sort(key=lambda c: 0 if c.get("sku_id") == target_sku_id else 1)
            pre_rag_result["product_cards"] = cards

        # 合并本地情绪护栏与隐私试探信号
        primary_intent = jev_decision["primary_intent"]
        if input_guard.category == "privacy_probe":
            primary_intent = "privacy_guard"
        handoff_guard = bool(
            jev_decision.get("handoff_guard", False) or emotion_sig.trigger_handoff
        )
        jev_decision["primary_intent"] = primary_intent
        jev_decision["handoff_guard"] = handoff_guard

        # ====================================================================
        # Step 3: 按意图与视觉场景路由至多模态 RAG、确定性订单查询、售后核验或「缺货品类心愿单钩子」
        # ====================================================================
        rag_result: Optional[Dict[str, Any]] = None
        order_result: Optional[Dict[str, Any]] = None
        aftersales_result: Optional[Dict[str, Any]] = None
        missing_stock_reply_msg: Optional[str] = None
        wishlist_card: Optional[Dict[str, Any]] = None

        has_product_constraints = bool(
            extracted.get("sub_category")
            or extracted.get("matched_sub_categories")
            or extracted.get("hard_constraints")
            or extracted.get("soft_constraints")
        )

        detected_missing_item: Optional[str] = None
        if vision_info:
            v_scene = vision_info.get("scene_type")
            if v_scene == "aftersales_evidence":
                primary_intent = "aftersales_qa"
                jev_decision["primary_intent"] = "aftersales_qa"
            elif v_scene == "out_of_catalog_product":
                detected_missing_item = vision_info.get("recognized_item") or "图中商品"
            elif v_scene == "in_catalog_product":
                primary_intent = "product_recommend"
                jev_decision["primary_intent"] = "product_recommend"
                jev_decision["need_rag"] = True
                has_product_constraints = True
            elif v_scene == "non_product_or_blurry":
                primary_intent = "chitchat"
                jev_decision["primary_intent"] = "chitchat"
                jev_decision["need_rag"] = False
                has_product_constraints = False

        if (
            not detected_missing_item
            and not extracted.get("sub_category")
            and primary_intent not in ("order_query", "aftersales_qa", "privacy_guard")
            and not handoff_guard
            and not (vision_info and vision_info.get("scene_type") == "non_product_or_blurry")
        ):
            for kw in missing_item_keywords:
                if kw in clean_query:
                    detected_missing_item = kw
                    break

        if primary_intent == "order_query":
            order_result = self.order_tools.query_order_status(
                order_id=entities.get("order_id"),
                query_text=clean_query,
            )
        elif primary_intent == "aftersales_qa":
            aftersales_result = self.order_tools.evaluate_aftersales_eligibility(
                order_id=entities.get("order_id"),
                query_text=clean_query,
            )
            order_result = self.order_tools.query_order_status(
                order_id=aftersales_result.get("order_id") or entities.get("order_id"),
                query_text=clean_query,
            )
        elif detected_missing_item:
            primary_intent = "product_recommend"
            jev_decision["primary_intent"] = "product_recommend"
            hook_rec = analytics_service.record_wishlist_hook(
                missing_item=detected_missing_item,
                user_query=clean_query,
                session_id=session_id,
                client_ip=client_ip,
                user_subscribed=False,
            )
            wishlist_card = {
                "card_type": "wishlist_card",
                "hook_id": hook_rec.get("hook_id", "wish-live"),
                "missing_item": detected_missing_item,
                "query": clean_query,
                "title": f"暂无「{detected_missing_item}」在售商品",
                "subtitle": (
                    "已通过 V4.1 视觉识别提取商品特征，点击下方按钮一键登记到货提醒"
                    if vision_info
                    else "我们会尽快安排采买上架，点击下方按钮登记到货提醒"
                ),
                "budget_options": ["¥100以内", "¥100-300", "¥300-800", "¥800以上"],
                "request_count": int(hook_rec.get("request_count") or 1),
                "status": hook_rec.get("status", "pending"),
            }
            rag_result = None
            # 纯文字缺货查询保持极速确定性提示；带图缺货查询交由 V4.1 多模态或确定性三步应对法输出看图细节+心愿单+跨品类搭配
            if not vision_info:
                missing_stock_reply_msg = (
                    f"抱歉，惠购目前暂无「{detected_missing_item}」相关商品，我们会尽快安排补货上架。"
                    f"您可以点击上方卡片一键登记心愿单，上架后第一时间通知您。"
                )
        elif has_product_constraints and (
            primary_intent == "product_recommend"
            or (jev_decision.get("need_rag") and primary_intent not in ("privacy_guard", "chitchat"))
            or has_product_constraints
        ):
            rag_result = pre_rag_result

        # ====================================================================
        # Step 4: 发送 SSE 事件 1 —— `event: decision_meta`
        # ====================================================================
        constraint_status = (
            rag_result["constraint_status"] if rag_result else "none"
        )
        relaxation_reason = (
            rag_result.get("relaxation_reason") if rag_result else None
        )
        sub_category = (
            rag_result.get("sub_category")
            if rag_result
            else extracted.get("sub_category")
        )

        product_cards_list = rag_result["product_cards"] if rag_result else []
        session_ledger = HierarchicalContextHarness.evolve_ledger(
            current_ledger=None,
            session_id=session_id,
            current_query=clean_query,
            detected_intent=primary_intent,
            extracted_constraints=extracted,
            recommended_cards=product_cards_list,
            handoff_flag=handoff_guard,
        )

        decision_meta_payload = {
            "message_id": message_id,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "client_ip": client_ip,
            "primary_intent": primary_intent,
            "intent_confidence": jev_decision.get("intent_confidence", 1.0),
            "confidence_tier": jev_decision.get("confidence_tier", "high"),
            "clarification_needed": bool(jev_decision.get("clarification_needed", False)),
            "intent_probabilities": jev_decision.get("intent_probabilities", {}),
            "need_rag": bool(jev_decision.get("need_rag", False)),
            "need_rag_score": jev_decision.get("need_rag_score", 0.0),
            "is_context_dependent": bool(jev_decision.get("is_context_dependent", False)),
            "handoff_guard": handoff_guard,
            "handoff_guard_score": jev_decision.get("handoff_guard_score", 0.0),
            "buying_readiness_raw": jev_decision.get("buying_readiness_raw", 2.0),
            "buying_readiness_score": jev_decision.get("buying_readiness_score", 3.0),
            "decision_source": jev_decision.get("decision_source", "local_rule_fallback"),
            "fallback_triggered": bool(jev_decision.get("fallback_triggered", False)),
            "fallback_reason": jev_decision.get("fallback_reason"),
            "emotion_type": emotion_sig.emotion_type,
            "extracted_constraints": {
                "sub_category": sub_category,
                "max_price": extracted.get("max_price"),
                "hard_constraints": extracted.get("hard_constraints", []),
                "excluded_specs": extracted.get("excluded_specs", {}),
            },
            "constraint_status": constraint_status,
            "relaxation_reason": relaxation_reason,
            "jev_rtt_ms": jev_decision.get("jev_rtt_ms", 0.0),
            "has_image": bool(vision_info),
            "vision_recognized_item": (
                vision_info.get("recognized_item") if vision_info else None
            ),
            "vision_scene_type": (
                vision_info.get("scene_type") if vision_info else None
            ),
            "decision_ledger": session_ledger.to_dict(),
        }
        yield format_sse_event("decision_meta", decision_meta_payload)

        # ====================================================================
        # Step 5: 发送 SSE 事件 2 —— `event: ui_cards`
        # ====================================================================
        handoff_card = None
        if handoff_guard:
            handoff_card = {
                "card_type": "handoff_card",
                "title": "高级售后维权专员绿色通道已开启",
                "priority": "P0_URGENT",
                "reason": emotion_sig.strategy_hint.strip() or "触发高优维权转人工阈值 (handoff_guard >= 0.8)",
                "specialist_queue": "VIP_AFTERSALES_DESK",
                "sla_promise": "人工专员将在 15 秒内优先接入处理您的诉求",
            }

        ui_cards_payload = {
            "product_cards": rag_result["product_cards"] if rag_result else [],
            "price_explain_cards": rag_result["price_explain_cards"] if rag_result else [],
            "order_cards": (
                [order_result["order_card"]]
                if (order_result and order_result.get("order_card"))
                else []
            ),
            "aftersales_card": (
                aftersales_result["aftersales_card"]
                if (aftersales_result and aftersales_result.get("aftersales_card"))
                else None
            ),
            "handoff_card": handoff_card,
            "wishlist_card": wishlist_card,
        }
        yield format_sse_event("ui_cards", ui_cards_payload)

        # ====================================================================
        # Step 6: 编译缓存对齐系统提示词 (SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        # ====================================================================
        prompt_pkg = self.strategy_injector.compile_prompt(
            user_query=clean_query,
            jev_decision=jev_decision,
            emotion_signal={
                "emotion_type": emotion_sig.emotion_type,
                "reason": emotion_sig.strategy_hint.strip(),
            },
            entities=entities,
            rag_result=rag_result,
            order_result=order_result,
            aftersales_result=aftersales_result,
            guard_warning=input_guard.category if input_guard.action == GuardAction.WARN else None,
        )
        static_prefix = prompt_pkg["static_prefix"]
        dynamic_suffix = prompt_pkg["dynamic_suffix"]
        align_metrics = prompt_pkg["alignment_metrics"]

        trimmed_history, history_tokens = self._trim_history_to_window(
            conv_history,
            reserved_tokens=align_metrics["total_system_tokens"],
        )

        history_block, active_msgs = HierarchicalContextHarness.assemble_hierarchical_context(
            history=trimmed_history,
            ledger=session_ledger,
            active_turns_window=3,
        )

        vision_context_block = ""
        if vision_info:
            v_scene = vision_info.get("scene_type", "")
            v_item = vision_info.get("recognized_item") or "图中物品"
            v_desc = vision_info.get("visual_description") or ""
            v_tip = vision_info.get("related_category_tip") or ""
            if v_scene == "out_of_catalog_product":
                vision_context_block = (
                    "\n## V4.1 原生多模态视觉识别结果（未上架商品三步应对指令）\n"
                    f"- 识别出的商品名称：「{v_item}」\n"
                    f"- 视觉外观特征：{v_desc}\n"
                    f"- 跨品类联动建议：{v_tip}\n"
                    "- **回答硬约束（必须严格按以下三步在 140~220 字内输出）**：\n"
                    f"  1. 先精准描述图片中的「{v_item}」外观细节，体现你已看清看懂图片；\n"
                    f"  2. 坦诚说明惠购官方自营库目前暂未上架「{v_item}」，严禁拿店内不相干商品硬凑同款，并引导客户在上方弹出的【缺货商品心愿单】卡片一键选择预算并登记到货提醒；\n"
                    "  3. 自然给出 1 句克制贴心的跨品类在售好物（如氮化镓快充、磁吸充电宝、电脑包、冲锋衣、护肤品）搭配建议。\n"
                )
            elif v_scene == "in_catalog_product":
                vision_context_block = (
                    "\n## V4.1 原生多模态视觉识别结果（在售品类精准匹配）\n"
                    f"- 识别出的商品/款式：「{v_item}」（细分类目：`{vision_info.get('sub_category')}`）\n"
                    f"- 视觉外观特征：{v_desc}\n"
                    "- **回答指引**：先用一句话点明已识别出图片中的商品款式与外观细节，再结合上方召回的在售商品卡片给出选品结论与亮点对比。\n"
                )
            elif v_scene == "non_product_or_blurry":
                vision_context_block = (
                    "\n## V4.1 原生多模态视觉识别结果（非商品或画面不清晰引导）\n"
                    f"- 画面识别摘要：{v_desc or v_item}\n"
                    "- **回答指引**：先温和说明已在图片中看到的内容，指出暂未识别出清晰具体的商品主体或规格标签，引导客户重新拍摄清晰的商品正面照/包装规格铭牌，或直接用文字告知目标品类与预算上限。\n"
                )
            elif v_scene == "aftersales_evidence":
                vision_context_block = (
                    "\n## V4.1 原生多模态视觉识别结果（售后/订单凭证图片）\n"
                    f"- 凭证识别摘要：{v_desc or v_item}\n"
                    "- **回答指引**：说明已收到客户上传的图片凭证，并结合上方确定性售后/订单核验卡片给出清晰结论。\n"
                )

        c_end_format_rule = (
            "\n【C端客户界面排版硬约束】：直接输出面向消费者的清爽纯中文与列表，"
            "严禁输出 `<br>`、`<div>` 等任何 HTML 标签，严禁输出 `$...$`、`\\rightarrow` 等 LaTeX 数学公式符号或 Markdown 表格线。\n"
        )
        llm_input_text = f"{dynamic_suffix}{vision_context_block}{history_block}{c_end_format_rule}\n请直接基于以上真源数据回复用户：{clean_query}"

        # ====================================================================
        # Step 7: 调用主生成模型 (/v1/responses) 或确定性合成器，经滑动窗口过滤推送 `text_delta`
        # ====================================================================
        stream_filter = SlidingWindowStreamFilter(overlap_chars=12)
        selected_node = self.llm_router.select_node()
        gateway_node_used = selected_node.name
        llm_input_tokens = count_tokens_exact(static_prefix) + count_tokens_exact(llm_input_text)
        llm_output_tokens = 0
        llm_cached_tokens = 0
        emitted_full_text_parts: List[str] = []

        remote_succeeded = False
        if missing_stock_reply_msg:
            gateway_node_used = "missing-stock-wishlist"
            chunk_size = 28
            for i in range(0, len(missing_stock_reply_msg), chunk_size):
                piece = missing_stock_reply_msg[i : i + chunk_size]
                safe_piece = stream_filter.filter_delta(piece)
                if safe_piece:
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
                    emitted_full_text_parts.append(safe_piece)
                    yield format_sse_event("text_delta", {"delta": safe_piece})
            tail_piece = stream_filter.flush()
            if tail_piece:
                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
                emitted_full_text_parts.append(tail_piece)
                yield format_sse_event("text_delta", {"delta": tail_piece})
            remote_succeeded = True
        elif allow_remote_llm and selected_node.api_key:
            t_llm_0 = time.perf_counter()
            try:
                async for item in self._stream_remote_responses_api(
                    node=selected_node,
                    instructions=static_prefix,
                    input_text=llm_input_text,
                    image_data_url=vision_info.get("data_url") if vision_info else None,
                ):
                    if item["type"] == "delta":
                        safe_chunk = stream_filter.filter_delta(item["delta"])
                        if safe_chunk:
                            if ttft_ms is None:
                                ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
                            emitted_full_text_parts.append(safe_chunk)
                            yield format_sse_event("text_delta", {"delta": safe_chunk})
                    elif item["type"] == "usage":
                        u = item["usage"]
                        if isinstance(u, dict):
                            llm_input_tokens = int(u.get("input_tokens") or llm_input_tokens)
                            llm_output_tokens = int(u.get("output_tokens") or 0)
                            details = u.get("input_tokens_details") or {}
                            if isinstance(details, dict):
                                llm_cached_tokens = int(details.get("cached_tokens") or 0)

                tail_chunk = stream_filter.flush()
                if tail_chunk:
                    is_leaked = ContentGuard.check_output(tail_chunk)
                    final_tail = (
                        "（系统安全拦截：已过滤内部配置文本）"
                        if is_leaked
                        else tail_chunk
                    )
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
                    emitted_full_text_parts.append(final_tail)
                    yield format_sse_event("text_delta", {"delta": final_tail})

                if emitted_full_text_parts:
                    remote_succeeded = True
                    selected_node.breaker.record_success()
                    selected_node.update_latency((time.perf_counter() - t_llm_0) * 1000.0)
            except Exception as exc:
                logger.warning(f"[ShoppingAgentOrchestrator] 远端 /v1/responses 异常，切换本地确定性合成兜底: {exc}")
                selected_node.breaker.record_failure()

        if not remote_succeeded:
            gateway_node_used = (
                "deterministic-local-synthesizer"
                if not allow_remote_llm
                else f"{selected_node.name}->deterministic-fallback"
            )
            det_text = synthesize_deterministic_response(
                user_query=clean_query,
                jev_decision=jev_decision,
                emotion_signal={
                    "emotion_type": emotion_sig.emotion_type,
                    "reason": emotion_sig.strategy_hint.strip(),
                },
                rag_result=rag_result,
                order_result=order_result,
                aftersales_result=aftersales_result,
                guard_warning=input_guard.category if input_guard.action == GuardAction.WARN else None,
                vision_info=vision_info,
            )
            # 模拟真实流式切片经过 SlidingWindowStreamFilter 输出
            chunk_size = 28
            for i in range(0, len(det_text), chunk_size):
                piece = det_text[i : i + chunk_size]
                safe_piece = stream_filter.filter_delta(piece)
                if safe_piece:
                    if ttft_ms is None:
                        ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
                    emitted_full_text_parts.append(safe_piece)
                    yield format_sse_event("text_delta", {"delta": safe_piece})
            tail_piece = stream_filter.flush()
            if tail_piece:
                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
                emitted_full_text_parts.append(tail_piece)
                yield format_sse_event("text_delta", {"delta": tail_piece})

        full_emitted_text = "".join(emitted_full_text_parts)
        if llm_output_tokens == 0:
            llm_output_tokens = count_tokens_exact(full_emitted_text)

        # ====================================================================
        # Step 8: 发送 SSE 事件 4 —— `event: followup_pills`
        # ====================================================================
        if handoff_guard:
            pills = [
                "立即接入人工售后专员",
                "查看订单价保与退差明细",
                "上传凭证加速处理",
            ]
        elif primary_intent == "privacy_guard":
            pills = [
                "帮我找个 20 元以内的 17 寸防震电脑包",
                "推荐一款百元内氮化镓快充头",
                "查看平台7天无理由退货规则",
            ]
        elif wishlist_card and detected_missing_item:
            pills = [
                "帮我推荐百元内 65W 氮化镓快充头",
                "看看 200 元以内的防风防水冲锋衣",
                "推荐敏感肌温和氨基酸洁面乳",
            ]
        elif vision_info and vision_info.get("scene_type") == "non_product_or_blurry":
            pills = [
                "帮我推荐百元内 65W 氮化镓快充头",
                "60 元以内、2 万毫安的轻便充电宝",
                "查下我的订单发货状态",
            ]
        elif primary_intent == "aftersales_qa" and aftersales_result and aftersales_result.get("aftersales_card"):
            ac = aftersales_result["aftersales_card"]
            if ac["price_guarantee_15d_eligible"]:
                pills = [
                    f"一键申请 ¥{ac['price_diff_amount']} 价保退差",
                    "退差金额多久到账？",
                    "查看该订单完整物流轨迹",
                ]
            elif ac["return_7d_eligible"]:
                pills = [
                    "一键申请 7 天无理由退货",
                    "运费险怎么自动抵扣首重？",
                    "换货流程怎么操作？",
                ]
            else:
                pills = [
                    "转接人工客服协商处理",
                    "查看15天价格保护规则详情",
                    "查询我的其他订单状态",
                ]
        elif primary_intent == "order_query" and order_result and order_result.get("order_card"):
            oc = order_result["order_card"]
            pills = [
                f"核验订单 {oc['order_id']} 能否申请7天无理由退货",
                f"核验订单 {oc['order_id']} 是否有15天价保差额",
                "联系物流客服催派件",
            ]
        elif rag_result and rag_result.get("followup_pills"):
            pills = list(rag_result["followup_pills"])
        else:
            pills = [
                "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
                "60 元以内、2 万毫安、200 克以内的充电宝",
                "查下我的订单发货状态",
            ]

        yield format_sse_event("followup_pills", {"pills": pills[:3]})

        # ====================================================================
        # Step 9: 发送 SSE 事件 5 —— `event: telemetry`
        # ====================================================================
        total_rtt_ms = round((time.perf_counter() - t_start) * 1000.0, 2)
        if ttft_ms is None:
            ttft_ms = total_rtt_ms

        telemetry_payload = {
            "ttft_ms": ttft_ms,
            "total_rtt_ms": total_rtt_ms,
            "rag_retrieve_ms": rag_result["rag_retrieve_ms"] if rag_result else 0.0,
            "jev_rtt_ms": jev_decision.get("jev_rtt_ms", 0.0),
            "index_mode": rag_result["index_mode"] if rag_result else "none",
            "db_backend": (
                rag_result["db_backend"] if rag_result else self.rag_engine.db_backend
            ),
            "recall_channels": (
                rag_result["recall_channels"]
                if rag_result
                else {
                    "sub_category_pool": 0,
                    "hard_filtered": 0,
                    "bm25_hits": 0,
                    "pgvector_halfvec_hits": 0,
                    "relaxed_candidates": 0,
                }
            ),
            "prefix_cache_aligned": align_metrics["prefix_cache_aligned"],
            "static_prefix_md5": align_metrics["static_prefix_md5"],
            "static_prefix_tokens": align_metrics["static_prefix_tokens"],
            "dynamic_suffix_tokens": align_metrics["dynamic_suffix_tokens"],
            "static_ratio_pct": align_metrics["static_ratio_pct"],
            "tokenizer": align_metrics["tokenizer"],
            "context_window_limit": self.context_window_limit,
            "history_tokens": history_tokens,
            "jev_input_tokens": jev_decision.get("jev_input_tokens", 0),
            "jev_output_tokens": jev_decision.get("jev_output_tokens", 0),
            "jev_cache_hit": bool(jev_decision.get("cache_hit", False)),
            "llm_input_tokens": llm_input_tokens,
            "llm_output_tokens": llm_output_tokens,
            "llm_cached_tokens": llm_cached_tokens,
            "gateway_node": gateway_node_used,
            "llm_fallback_active": not remote_succeeded,
            "ewma_latency_ms": selected_node.ewma_latency_ms,
            "circuit_breaker_state": selected_node.breaker.state,
            "rtc_voice_enabled": settings.enable_rtc_voice,
            "has_image": bool(vision_info),
            "vision_recognized_item": (
                vision_info.get("recognized_item") if vision_info else None
            ),
            "vision_scene_type": (
                vision_info.get("scene_type") if vision_info else None
            ),
        }
        yield format_sse_event("telemetry", telemetry_payload)

        # 实时审计入库 (AdminAnalyticsService)
        try:
            analytics_service.record_chat_turn(
                message_id=message_id,
                session_id=session_id,
                tenant_id=tenant_id,
                client_ip=client_ip,
                user_agent=user_agent,
                user_query=clean_query,
                response_text=full_emitted_text,
                decision_meta=decision_meta_payload,
                ui_cards=ui_cards_payload,
                telemetry=telemetry_payload,
            )
        except Exception as audit_err:
            logger.warning(f"[ShoppingAgentOrchestrator] 审计日志写入异常: {audit_err}")

        # ====================================================================
        # Step 10: 发送 SSE 事件 6 —— `event: done`
        # ====================================================================
        yield format_sse_event("done", "[DONE]")

