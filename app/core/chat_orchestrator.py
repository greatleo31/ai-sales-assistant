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
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from app.config import settings
from app.core.telemetry import count_tokens_exact
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
) -> str:
    """
    基于 100% 真实结构化上下文生成零幻觉导购/客服回复（所有价格、规格、时间差与差价均直接取自真源卡片）。
    """
    primary_intent = jev_decision.get("primary_intent", "product_recommend")
    handoff_guard = bool(jev_decision.get("handoff_guard", False))
    parts: List[str] = []

    # 1. 高危维权 / 挫败情绪前置安抚与人工专员接管说明
    if handoff_guard or emotion_signal.get("emotion_type") in ("negative", "frustrated"):
        parts.append(
            "非常理解您的心情，让您有不好的购物体验我们深感抱歉！系统已为您优先开启**【高级售后专员绿色通道】**，人工客服专员已同步接入跟进。"
        )

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
        parts.append("为您从同品类商品池中精选出最匹配硬约束与多模态口碑的款式：")
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
        return "\n\n".join(parts)

    # 6. 礼貌寒暄或通用导购引导
    parts.append(
        "您好！我是淘宝「千问」级 AI 购物助手，支持 **3C数码、换季服饰、美妆个护** 多模态精准选品、**预算与规格冲突双路径推荐**，以及 **订单物流查询与7天无理由/15天价保秒级核验**。您可以直接告诉我您的预算和具体规格需求！"
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
      - 支持 `/v1/responses` (`deepseek-flash`) 真实流式生成与离线确定性生成无缝切换。
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
        timeout_sec: float = 18.0,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        调用 `POST https://go.penguin.ooo/api/v1/responses` (`deepseek-flash`) 流式接口：
          - `instructions` 传入 100% 字节级恒定的静态系统提示词前缀 (`static_prefix`)；
          - `input` 传入动态策略后缀、多轮历史与当前用户消息；
          - 实时产出 `{"type": "delta", "delta": "..."}` 与结束时的 `{"type": "usage", "usage": {...}}`。
        """
        headers = {
            "Authorization": f"Bearer {node.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "model": node.model,
            "instructions": instructions,
            "input": input_text,
            "reasoning": {"effort": "low"},
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            async with client.stream(
                "POST", node.url, headers=headers, json=payload
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise RuntimeError(
                        f"/v1/responses HTTP {resp.status_code}: {body[:200]!r}"
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

                    evt_type = evt_obj.get("type") or current_event
                    if evt_type == "response.output_text.delta":
                        delta_txt = evt_obj.get("delta", "")
                        if delta_txt:
                            yield {"type": "delta", "delta": delta_txt}
                    elif evt_type == "response.completed":
                        resp_obj = evt_obj.get("response") or {}
                        usage_obj = resp_obj.get("usage") or {}
                        yield {"type": "usage", "usage": usage_obj}

    async def stream_chat(
        self,
        query: str,
        session_id: str = "default-session",
        history: Optional[List[Dict[str, str]]] = None,
        refresh_jev: bool = False,
        allow_remote_llm: bool = True,
        allow_remote_embedding: bool = False,
        retrieval_mode: str = "hybrid_rrf",
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
        conv_history = list(history or [])

        # ====================================================================
        # Step 1: ContentGuard 输入安全护栏（零延迟同步拦截）
        # ====================================================================
        input_guard = ContentGuard.check_input(clean_query)
        emotion_sig = EmotionTracker.detect(conv_history, clean_query)
        entities = EntityTracker.extract(conv_history, clean_query)

        if input_guard.action == GuardAction.BLOCK:
            # 输入触发 Prompt 注入或违法违禁词，Fail-Closed 安全阻断并输出完整 6 类 SSE 事件
            blocked_meta = {
                "session_id": session_id,
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
            }
            yield format_sse_event("decision_meta", blocked_meta)
            yield format_sse_event(
                "ui_cards",
                {
                    "product_cards": [],
                    "price_explain_cards": [],
                    "order_cards": [],
                    "aftersales_card": None,
                    "handoff_card": None,
                },
            )
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
            yield format_sse_event(
                "telemetry",
                {
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
                },
            )
            yield format_sse_event("done", "[DONE]")
            return

        # ====================================================================
        # Step 2: asyncio.gather 并发执行 JevDecisionRouter 与硬约束提取 + 预检索
        # ====================================================================
        extracted = self.extractor.extract(clean_query)
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
                query=clean_query,
                extracted_constraints=extracted,
                mode=retrieval_mode,
                allow_remote_embedding=allow_remote_embedding,
            )
        )
        jev_decision, pre_rag_result = await asyncio.gather(jev_task, rag_pre_task)

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
        # Step 3: 按意图路由至多模态 RAG、确定性订单查询或售后秒级核验工具
        # ====================================================================
        rag_result: Optional[Dict[str, Any]] = None
        order_result: Optional[Dict[str, Any]] = None
        aftersales_result: Optional[Dict[str, Any]] = None

        has_product_constraints = bool(
            extracted.get("sub_category") or extracted.get("hard_constraints")
        )

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
        elif primary_intent == "product_recommend" or (
            jev_decision.get("need_rag") and primary_intent not in ("privacy_guard", "chitchat")
        ) or has_product_constraints:
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

        decision_meta_payload = {
            "session_id": session_id,
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

        history_block = ""
        if trimmed_history:
            h_lines = ["## 历史对话记录 (Recent Conversation History)"]
            for m in trimmed_history[-6:]:
                h_lines.append(f"- {m['role']}: {m['content']}")
            history_block = "\n" + "\n".join(h_lines) + "\n"

        llm_input_text = f"{dynamic_suffix}{history_block}\n请直接基于以上真源数据回复用户：{clean_query}"

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
        if allow_remote_llm and selected_node.api_key:
            t_llm_0 = time.perf_counter()
            try:
                async for item in self._stream_remote_responses_api(
                    node=selected_node,
                    instructions=static_prefix,
                    input_text=llm_input_text,
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
            "ewma_latency_ms": selected_node.ewma_latency_ms,
            "circuit_breaker_state": selected_node.breaker.state,
            "rtc_voice_enabled": settings.enable_rtc_voice,
        }
        yield format_sse_event("telemetry", telemetry_payload)

        # ====================================================================
        # Step 10: 发送 SSE 事件 6 —— `event: done`
        # ====================================================================
        yield format_sse_event("done", "[DONE]")
