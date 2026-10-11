# -*- coding: utf-8 -*-
"""
FastAPI 应用主入口 (app/main.py)
严格对齐 HANDOFF.md §2, §3, §4 与 Claude Sonnet 5.5 模块四实施规范：
  1. 挂载本地静态商品图服务 `/static/images/{sku_id}.jpg` (`data/images`)，100% 返回 HTTP 200 与 `image/jpeg`；
  2. 挂载单页静态演示看板 `/static/showcase.html` (`static/showcase.html`) 及根路径 `/`；
  3. 暴露淘宝「千问」级 Typed Multi-Event SSE 流式接口 `POST /api/v1/chat/stream`；
  4. 暴露只读订单查询 `/api/v1/orders/{order_id}`、售后核验 `/api/v1/aftersales/evaluate` 与健康检查 `/health`；
  5. 契约内保留 `voice_input_enabled` / `rtc_session_id` 可选语音入口字段（默认关闭，零告警启动）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import DATA_DIR, ROOT_DIR, settings
from app.core.chat_orchestrator import ShoppingAgentOrchestrator
from app.services.order_tools import (
    evaluate_aftersales_eligibility,
    query_order_status,
)

STATIC_DIR: Path = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR: Path = DATA_DIR / "images"

app = FastAPI(
    title="AI Sales Assistant (淘宝「千问」级多模态电商导购 Agent)",
    description="System One (Jev) 门控 + 确定性硬约束预过滤 + BM25/halfvec(1024)/RRF 混合检索 + 双路径约束松弛 + 272K 状态机",
    version="2026.10.m4",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. 优先挂载 /static/images -> data/images (确保 /static/images/{sku_id}.jpg 精确映射)
if IMAGES_DIR.exists():
    app.mount(
        "/static/images",
        StaticFiles(directory=str(IMAGES_DIR)),
        name="static_images",
    )

# 2. 挂载 /static -> static/ (提供 /static/showcase.html)
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static_files",
)

# 全局单例状态机编排器
orchestrator = ShoppingAgentOrchestrator()


class ChatStreamRequest(BaseModel):
    query: Optional[str] = Field(default=None, description="用户输入的主查询文本")
    message: Optional[str] = Field(default=None, description="兼容字段：用户消息文本")
    session_id: str = Field(default="default-session", description="会话 ID")
    history: List[Dict[str, str]] = Field(default_factory=list, description="多轮对话历史")
    refresh_jev: bool = Field(default=False, description="是否穿透 jev_cache.json 强制刷新远端 Jev")
    allow_remote_llm: bool = Field(default=True, description="是否调用远端 /v1/responses 生成模型")
    allow_remote_embedding: bool = Field(default=False, description="是否调用远端 Embedding 接口")
    retrieval_mode: str = Field(
        default="hybrid_rrf",
        description="检索模式：hybrid_rrf / bm25_only / dense_only",
    )
    # 预留 RTC 语音可选字段 (HANDOFF.md §2：预留语音入口字段，默认关闭)
    voice_input_enabled: bool = Field(default=False, description="可选 RTC 语音输入开关（默认关闭）")
    rtc_room_id: Optional[str] = Field(default=None, description="可选火山 RTC 房间号预留字段")


class AftersalesEvalRequest(BaseModel):
    order_id: Optional[str] = Field(default=None, description="订单号，如 ORD-20261001-002")
    scenario: str = Field(default="auto", description="核验场景：auto / return_7d / price_guarantee")
    reference_time: Optional[str] = Field(default=None, description="带 UTC 时区偏移的 ISO-8601 基准时间")
    query_text: Optional[str] = Field(default=None, description="自然语言查询文本")


@app.get("/", include_in_schema=False)
async def root_showcase() -> FileResponse:
    """根路径直接返回单页演示看板 static/showcase.html。"""
    showcase_file = STATIC_DIR / "showcase.html"
    if not showcase_file.exists():
        raise HTTPException(status_code=404, detail="static/showcase.html not found")
    return FileResponse(str(showcase_file), media_type="text/html; charset=utf-8")


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """系统健康检查与核心架构配置快照。"""
    return {
        "status": "ok",
        "version": "2026.10.m4",
        "effective_context_window": settings.effective_context_window,
        "llm_protocol": settings.llm_protocol,
        "llm_primary_model": settings.llm_primary_model,
        "jev_model": settings.jev_model,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "db_backend": orchestrator.rag_engine.db_backend,
        "sku_count": len(orchestrator.rag_engine.catalog),
        "rtc_voice_enabled": settings.enable_rtc_voice,
    }


@app.get("/api/v1/catalog/summary")
async def catalog_summary() -> Dict[str, Any]:
    """返回 60 SKU 目录与 5 组冲突对概览供前端看板展示。"""
    return {
        "total_skus": len(orchestrator.rag_engine.catalog),
        "sub_categories": list(orchestrator.rag_engine.spec_registry.get("sub_categories", {}).keys()),
        "conflict_pairs_count": len(orchestrator.order_tools.orders),
    }


@app.get("/api/v1/orders/{order_id}")
async def get_order_status_endpoint(order_id: str) -> Dict[str, Any]:
    """只读订单物流状态查询接口。"""
    res = query_order_status(order_id=order_id)
    if not res.get("found"):
        raise HTTPException(status_code=404, detail=res.get("error", "Order not found"))
    return res


@app.post("/api/v1/aftersales/evaluate")
async def evaluate_aftersales_endpoint(req: AftersalesEvalRequest) -> Dict[str, Any]:
    """只读确定性售后/价保资格核验接口。"""
    try:
        res = evaluate_aftersales_eligibility(
            order_id=req.order_id,
            scenario=req.scenario,
            reference_time=req.reference_time,
            query_text=req.query_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not res.get("found"):
        raise HTTPException(status_code=404, detail=res.get("error", "Order not found"))
    return res


@app.post("/api/v1/chat/stream")
async def chat_stream_endpoint(req: ChatStreamRequest) -> StreamingResponse:
    """
    淘宝「千问」级 Typed Multi-Event SSE 流式对话接口：
    依次推送：`decision_meta` -> `ui_cards` -> `text_delta` -> `followup_pills` -> `telemetry` -> `done`。
    """
    user_query = (req.query or req.message or "").strip()
    if not user_query:
        raise HTTPException(status_code=422, detail="Field 'query' (or 'message') must not be empty.")

    sse_generator = orchestrator.stream_chat(
        query=user_query,
        session_id=req.session_id,
        history=req.history,
        refresh_jev=req.refresh_jev,
        allow_remote_llm=req.allow_remote_llm,
        allow_remote_embedding=req.allow_remote_embedding,
        retrieval_mode=req.retrieval_mode,
    )
    return StreamingResponse(
        sse_generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
