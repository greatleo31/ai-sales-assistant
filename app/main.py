# -*- coding: utf-8 -*-
"""
FastAPI 应用主入口 (app/main.py)
严格对齐 HANDOFF.md §2, §3, §4 与 C端 H5 + Admin 多租户统计管理后台双端架构：
  1. 挂载本地静态商品图服务 `/static/images/{sku_id}.jpg` (`data/images`)，100% 返回 HTTP 200 与 `image/jpeg`；
  2. 挂载单页静态演示看板 `/static/showcase.html` (`static/showcase.html`) 及根路径 `/`（向后兼容全部评测套件）；
  3. 当 `h5-frontend/dist` 与 `admin-frontend/dist` 存在时，自动挂载 `/h5` 与 `/admin` 独立前端构建产物；
  4. 暴露惠购 (Huigou) 原生 Typed Multi-Event SSE 流式接口 `POST /api/v1/chat/stream`（含真实 Client IP 采集与多租户隔离）；
  5. 暴露 C 端店铺列表 `/api/v1/tenants/public` 与回答好坏评价接口 `POST /api/v1/feedback`；
  6. 暴露 Admin 管理后台全套统计与治理接口 `/api/v1/admin/*`（全景看板、多租户 CRUD、会话与 IP 审计、回答好坏反馈中心、RAG 松弛分析、Token 账单、12315 人工工单台、60 SKU 目录只读洞察）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import DATA_DIR, ROOT_DIR, settings
from app.core.chat_orchestrator import ShoppingAgentOrchestrator
from app.services.admin_analytics_service import (
    get_admin_analytics_service,
    get_static_constitution_metrics,
)
from app.services.order_tools import (
    evaluate_aftersales_eligibility,
    query_order_status,
)

STATIC_DIR: Path = ROOT_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR: Path = DATA_DIR / "images"
H5_DIST_DIR: Path = ROOT_DIR / "h5-frontend" / "dist"
ADMIN_DIST_DIR: Path = ROOT_DIR / "admin-frontend" / "dist"

app = FastAPI(
    title="Huigou AI Sales Assistant (惠购原生多模态电商导购 Agent + 多租户管理后台)",
    description="System One (Jev) 门控 + 确定性硬约束预过滤 + BM25/halfvec(1024)/RRF 混合检索 + 双路径约束松弛 + 272K 状态机 + 多租户/IP审计/反馈分析",
    version="2026.10.m6",
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

# 全局单例状态机编排器与统计分析服务
orchestrator = ShoppingAgentOrchestrator()
analytics_service = get_admin_analytics_service()


def _extract_client_ip(request: Request, explicit_ip: Optional[str] = None) -> str:
    """优先从显式参数、X-Forwarded-For、X-Real-IP 或 socket client 提取真实客户端 IP。"""
    if explicit_ip and explicit_ip.strip():
        return explicit_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return first_ip
    xri = request.headers.get("X-Real-IP")
    if xri and xri.strip():
        return xri.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


# ============================================================================
# Pydantic 请求契约模型
# ============================================================================
class ChatStreamRequest(BaseModel):
    query: Optional[str] = Field(default=None, description="用户输入的主查询文本")
    message: Optional[str] = Field(default=None, description="兼容字段：用户消息文本")
    session_id: str = Field(default="default-session", description="会话 ID")
    tenant_id: str = Field(default="tenant_all", description="所属租户/店铺 ID")
    client_ip: Optional[str] = Field(default=None, description="可选显式传入的客户端 IP")
    history: List[Dict[str, str]] = Field(default_factory=list, description="多轮对话历史")
    refresh_jev: bool = Field(default=False, description="是否穿透 jev_cache.json 强制刷新远端 Jev")
    allow_remote_llm: bool = Field(default=True, description="是否调用远端 /v1/responses 生成模型")
    allow_remote_embedding: bool = Field(default=False, description="是否调用远端 Embedding 接口")
    retrieval_mode: str = Field(
        default="hybrid_rrf",
        description="检索模式：hybrid_rrf / bm25_only / dense_only",
    )
    image_base64: Optional[str] = Field(
        default=None,
        description="可选：用户拍照或手机相册上传的图片 DataURL (data:image/jpeg;base64,...) 或纯 Base64 字符串",
    )
    # 预留 RTC 语音可选字段 (HANDOFF.md §2：预留语音入口字段，默认关闭)
    voice_input_enabled: bool = Field(default=False, description="可选 RTC 语音输入开关（默认关闭）")
    rtc_room_id: Optional[str] = Field(default=None, description="可选火山 RTC 房间号预留字段")


class AftersalesEvalRequest(BaseModel):
    order_id: Optional[str] = Field(default=None, description="订单号，如 ORD-20261001-002")
    scenario: str = Field(default="auto", description="核验场景：auto / return_7d / price_guarantee")
    reference_time: Optional[str] = Field(default=None, description="带 UTC 时区偏移的 ISO-8601 基准时间")
    query_text: Optional[str] = Field(default=None, description="自然语言查询文本")


class CustomerFeedbackRequest(BaseModel):
    message_id: str = Field(..., description="绑定的助手消息唯一 ID")
    session_id: Optional[str] = Field(default=None, description="会话 ID")
    tenant_id: Optional[str] = Field(default="tenant_all", description="租户 ID")
    rating: str = Field(..., description="评价类型：like (点赞) / dislike (点踩)")
    reason_tags: List[str] = Field(default_factory=list, description="点踩/点赞快捷原因标签")
    comment: str = Field(default="", description="用户补充评价说明")
    user_query: Optional[str] = Field(default=None, description="可选传入的用户原始提问")
    assistant_response: Optional[str] = Field(default=None, description="可选传入的助手回答摘要")


class AdminLoginRequest(BaseModel):
    username: str = Field(default="admin", description="管理员账号")
    password: str = Field(default="admin123", description="管理员密码")


class TenantUpsertRequest(BaseModel):
    tenant_id: Optional[str] = Field(default=None, description="租户 ID（新建可不填自动生成）")
    tenant_name: str = Field(..., description="租户/店铺名称")
    brand_subtitle: str = Field(default="惠购官方首席选品顾问", description="C端顶部副标题")
    allowed_categories: List[str] = Field(
        default_factory=lambda: ["3c", "apparel", "beauty"],
        description="允许检索的大类列表：3c / apparel / beauty",
    )
    rpm_limit: int = Field(default=120, ge=1, le=10000, description="每分钟请求配额上限")
    daily_token_quota: int = Field(default=2000000, ge=1000, description="每日 Token 配额上限")
    status: str = Field(default="active", description="状态：active / suspended")
    welcome_message: str = Field(
        default="您好！我是惠购官方首席选品顾问，随时为您推荐严选好物或核验订单售后。",
        description="C端自定义欢迎语",
    )
    theme_accent: str = Field(default="#38bdf8", description="C端主题高亮色")


class FeedbackUpdateRequest(BaseModel):
    status: str = Field(default="resolved", description="处理状态：pending / resolved / ignored")
    admin_note: str = Field(default="", description="管理员批注说明")


class HandoffUpdateRequest(BaseModel):
    status: str = Field(default="resolved", description="工单状态：pending / processing / resolved")
    assigned_specialist: str = Field(default="VIP专员-林夏 (工号 #8012)", description="指派专员")
    resolution_note: str = Field(default="", description="处理结论")


class WishlistSubmitRequest(BaseModel):
    missing_item: str = Field(..., description="缺货品类或商品名称")
    user_query: str = Field(default="", description="用户原始提问")
    session_id: str = Field(default="huigou-session", description="会话 ID")
    expected_budget: str = Field(default="", description="用户期望价格区间")
    user_subscribed: bool = Field(default=True, description="是否订阅到货通知")


class WishlistUpdateRequest(BaseModel):
    status: str = Field(default="sourcing", description="状态：pending / sourcing / stocked")
    admin_note: str = Field(default="", description="采买进展备注")


class EvalTriggerRequest(BaseModel):
    mode: str = Field(default="sandbox", description="运行模式：sandbox (快速沙箱回归) / e2e_llm (模型实测)")
    scenario_filter: Optional[str] = Field(default=None, description="可选指定评测场景组过滤")


class EvalCaseUpdateRequest(BaseModel):
    standard_response: Optional[str] = Field(default=None, description="修润后的官方自营标杆标准回复")
    audit_status: Optional[str] = Field(default=None, description="复核状态：verified (已确认) / pending_review (待复核)")
    hard_constraints: Optional[str] = Field(default=None, description="规格与预算硬约束 JSON 字符串")


class EvalAdoptFeedbackRequest(BaseModel):
    source_type: str = Field(description="来源类型：feedback (C端差评) / handoff (转人工工单)")
    source_id: str = Field(description="反馈ID或工单ID")
    scenario_group: str = Field(default="aftersales_verify", description="所属业务场景组")
    standard_response: str = Field(description="管理员核定的标杆标准答复")
    query_text: Optional[str] = Field(default=None, description="可选覆盖的用户原始提问")


# ============================================================================
# 基础与 C 端公共接口
# ============================================================================
@app.get("/", include_in_schema=False)
async def root_showcase() -> Any:
    """根路径直接呈现或重定向至 C 端现代移动导购助手。"""
    h5_index = H5_DIST_DIR / "index.html"
    if h5_index.exists():
        return FileResponse(str(h5_index), media_type="text/html; charset=utf-8")
    return RedirectResponse(url="/h5/", status_code=302)


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """系统健康检查与核心架构配置快照。"""
    return {
        "status": "ok",
        "version": "2026.10.m6",
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


@app.get("/api/v1/tenants/public")
async def list_public_tenants_endpoint() -> Dict[str, Any]:
    """供向后兼容读取可用租户列表（隐藏敏感 AppKey）。"""
    all_tenants = analytics_service.list_tenants()
    public_list = [
        {
            "tenant_id": t["tenant_id"],
            "tenant_name": t["tenant_name"],
            "brand_subtitle": t["brand_subtitle"],
            "allowed_categories": t["allowed_categories"],
            "status": t["status"],
            "welcome_message": t["welcome_message"],
            "theme_accent": t["theme_accent"],
        }
        for t in all_tenants
    ]
    return {"tenants": public_list}


@app.post("/api/v1/wishlist")
async def submit_wishlist_hook_endpoint(
    req: WishlistSubmitRequest,
    request: Request,
) -> Dict[str, Any]:
    """C 端「惠购」缺货品类心愿单登记钩子：用户遇到暂无的商品时一键登记上架提醒。"""
    client_ip = _extract_client_ip(request)
    record = analytics_service.record_wishlist_hook(
        missing_item=req.missing_item,
        user_query=req.user_query,
        session_id=req.session_id,
        client_ip=client_ip,
        expected_budget=req.expected_budget,
        user_subscribed=req.user_subscribed,
    )
    return {"status": "ok", "wishlist": record}


@app.post("/api/v1/feedback")
async def submit_customer_feedback_endpoint(
    req: CustomerFeedbackRequest,
    request: Request,
) -> Dict[str, Any]:
    """C 端客户对单条助手回答进行 👍 (`like`) 或 👎 (`dislike`) 评价并实时同步至 Admin 反馈中心。"""
    client_ip = _extract_client_ip(request)
    try:
        record = analytics_service.submit_feedback(
            message_id=req.message_id,
            rating=req.rating,
            reason_tags=req.reason_tags,
            comment=req.comment,
            session_id=req.session_id,
            tenant_id=req.tenant_id,
            client_ip=client_ip,
            user_query=req.user_query,
            assistant_response=req.assistant_response,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "feedback": record}


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
async def chat_stream_endpoint(req: ChatStreamRequest, request: Request) -> StreamingResponse:
    """
    惠购 (Huigou) 原生 Typed Multi-Event SSE 流式对话接口：
    依次推送：`decision_meta` -> `ui_cards` -> `text_delta` -> `followup_pills` -> `telemetry` -> `done`。
    """
    user_query = (req.query or req.message or "").strip()
    has_image = bool(req.image_base64 and req.image_base64.strip())
    if not user_query and has_image:
        user_query = "请帮我识别这张图片中的商品并推荐合适的款式"
    if not user_query:
        raise HTTPException(status_code=422, detail="Field 'query' (or 'message') must not be empty.")

    real_client_ip = _extract_client_ip(request, explicit_ip=req.client_ip)
    user_agent = request.headers.get("User-Agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X)")

    sse_generator = orchestrator.stream_chat(
        query=user_query,
        session_id=req.session_id,
        history=req.history,
        refresh_jev=req.refresh_jev,
        allow_remote_llm=req.allow_remote_llm,
        allow_remote_embedding=req.allow_remote_embedding,
        retrieval_mode=req.retrieval_mode,
        tenant_id=req.tenant_id or "tenant_all",
        client_ip=real_client_ip,
        user_agent=user_agent,
        image_base64=req.image_base64,
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


# ============================================================================
# Admin 管理后台统计与治理 API (/api/v1/admin/*)
# ============================================================================
@app.post("/api/v1/admin/login")
async def admin_login_endpoint(req: AdminLoginRequest) -> Dict[str, Any]:
    """管理员登录接口（内置演示账号 admin / admin123）。"""
    auth_res = analytics_service.authenticate_admin(req.username, req.password)
    if not auth_res:
        raise HTTPException(status_code=401, detail="管理员账号或密码错误（默认演示账号：admin / admin123）")
    return {"status": "ok", **auth_res}


@app.get("/api/v1/admin/dashboard")
async def admin_dashboard_endpoint(
    tenant_id: Optional[str] = Query(default=None, description="按租户 ID 筛选"),
    days: int = Query(default=7, ge=1, le=30, description="统计天数范围"),
) -> Dict[str, Any]:
    """管理后台模块①：全景数据看板 (Dashboard Overview)。"""
    return analytics_service.get_dashboard_overview(tenant_id=tenant_id, days=days)


@app.get("/api/v1/admin/tenants")
async def admin_list_tenants_endpoint() -> Dict[str, Any]:
    """管理后台模块②：获取全部店铺租户配置与实时配额消耗。"""
    return {"tenants": analytics_service.list_tenants()}


@app.post("/api/v1/admin/tenants")
async def admin_upsert_tenant_endpoint(req: TenantUpsertRequest) -> Dict[str, Any]:
    """管理后台模块②：新增或更新店铺租户（支持动态修改品类权限、配额、状态与欢迎语）。"""
    tenant = analytics_service.create_or_update_tenant(req.model_dump())
    return {"status": "ok", "tenant": tenant}


@app.post("/api/v1/admin/tenants/{tenant_id}/rotate-key")
async def admin_rotate_tenant_key_endpoint(tenant_id: str) -> Dict[str, Any]:
    """管理后台模块②：一键轮换指定店铺租户的 AppKey。"""
    try:
        tenant = analytics_service.rotate_tenant_app_key(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "tenant": tenant}


@app.get("/api/v1/admin/conversations")
async def admin_conversations_endpoint(
    tenant_id: Optional[str] = Query(default=None, description="按租户筛选"),
    intent: Optional[str] = Query(default=None, description="按主意图筛选"),
    ip_keyword: Optional[str] = Query(default=None, description="按客户端 IP 或归属地搜索"),
    keyword: Optional[str] = Query(default=None, description="按提问或回答关键词搜索"),
    handoff_only: bool = Query(default=False),
    feedback_filter: Optional[str] = Query(default=None, description="like / dislike / none"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    """管理后台模块③：会话与客户端 IP 审计列表。"""
    return analytics_service.list_audit_logs(
        tenant_id=tenant_id,
        intent=intent,
        ip_keyword=ip_keyword,
        keyword=keyword,
        handoff_only=handoff_only,
        feedback_filter=feedback_filter,
        page=page,
        page_size=page_size,
    )


@app.get("/api/v1/admin/feedbacks")
async def admin_feedbacks_endpoint(
    tenant_id: Optional[str] = Query(default=None),
    rating: Optional[str] = Query(default=None, description="like / dislike"),
    status: Optional[str] = Query(default=None, description="pending / resolved / ignored"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    """管理后台模块④：回答好坏反馈中心（含正负评汇总与原因标签分布）。"""
    return analytics_service.list_feedbacks(
        tenant_id=tenant_id,
        rating=rating,
        status=status,
        page=page,
        page_size=page_size,
    )


@app.patch("/api/v1/admin/feedbacks/{feedback_id}")
async def admin_update_feedback_endpoint(
    feedback_id: str,
    req: FeedbackUpdateRequest,
) -> Dict[str, Any]:
    """管理后台模块④：标注单条客户反馈的处理状态与管理员批注。"""
    try:
        updated = analytics_service.update_feedback_status(
            feedback_id=feedback_id,
            status=req.status,
            admin_note=req.admin_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "feedback": updated}


@app.get("/api/v1/admin/rag-analytics")
async def admin_rag_analytics_endpoint(
    tenant_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """管理后台模块⑤：RAG 检索与 C1~C5 双路径约束松弛分析。"""
    return analytics_service.get_rag_analytics(tenant_id=tenant_id)


@app.get("/api/v1/admin/token-analytics")
async def admin_token_analytics_endpoint(
    tenant_id: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """管理后台模块⑥：272K 上下文与 Prefix Cache 命中率及 Token 账单统计。"""
    return analytics_service.get_token_analytics(tenant_id=tenant_id)


@app.get("/api/v1/admin/handoffs")
async def admin_handoffs_endpoint(
    tenant_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """管理后台模块⑦：12315 暴怒维权与人工转接 P0 工单台。"""
    return analytics_service.list_handoff_tickets(tenant_id=tenant_id, status=status)


@app.patch("/api/v1/admin/handoffs/{ticket_id}")
async def admin_update_handoff_endpoint(
    ticket_id: str,
    req: HandoffUpdateRequest,
) -> Dict[str, Any]:
    """管理后台模块⑦：更新 P0 人工转接工单状态与处理结论。"""
    try:
        ticket = analytics_service.update_handoff_ticket(
            ticket_id=ticket_id,
            status=req.status,
            assigned_specialist=req.assigned_specialist,
            resolution_note=req.resolution_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "ticket": ticket}


@app.get("/api/v1/admin/catalog")
async def admin_catalog_insights_endpoint(
    category: Optional[str] = Query(default=None, description="3c / apparel / beauty"),
    sub_category: Optional[str] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    """管理后台模块⑦：60 SKU 商品库、3 条平台规则与 10 条模拟订单只读洞察。"""
    skus = list(orchestrator.rag_engine.catalog)
    if category:
        skus = [s for s in skus if s.get("category") == category]
    if sub_category:
        skus = [s for s in skus if s.get("sub_category") == sub_category]
    if keyword and keyword.strip():
        kw = keyword.strip().lower()
        skus = [
            s
            for s in skus
            if kw in str(s.get("sku_id", "")).lower()
            or kw in str(s.get("title", "")).lower()
            or kw in str(s.get("sub_category", "")).lower()
        ]
    return {
        "total_skus": len(orchestrator.rag_engine.catalog),
        "filtered_count": len(skus),
        "skus": skus,
        "platform_rules": list(orchestrator.order_tools.rules),
        "orders": list(orchestrator.order_tools.orders),
        "sub_categories": orchestrator.rag_engine.spec_registry.get("sub_categories", {}),
    }


@app.get("/api/v1/admin/wishlists")
async def admin_list_wishlists_endpoint(
    status: Optional[str] = Query(default=None, description="pending / sourcing / stocked"),
) -> Dict[str, Any]:
    """OA 管理后台：查询全平台缺货品类心愿单钩子汇总与明细。"""
    return analytics_service.list_wishlist_hooks(status=status)


@app.patch("/api/v1/admin/wishlists/{hook_id}")
async def admin_update_wishlist_endpoint(
    hook_id: str,
    req: WishlistUpdateRequest,
) -> Dict[str, Any]:
    """OA 管理后台：更新缺货品类心愿单采买状态与备注。"""
    try:
        updated = analytics_service.update_wishlist_hook(
            hook_id=hook_id,
            status=req.status,
            admin_note=req.admin_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "wishlist": updated}


@app.get("/api/v1/admin/context-manifest")
async def admin_context_manifest_endpoint() -> Dict[str, Any]:
    """启发 2：上下文缝补接口（返回全平台生效的软硬双锁、静态前缀 Hash、商品池与缺货心愿单快照）。"""
    return analytics_service.get_context_manifest()


@app.get("/api/v1/admin/runtime-status")
async def admin_runtime_status_endpoint() -> Dict[str, Any]:
    """管理后台研发中台：获取底层生成节点、熔断器、Jev 缓存与 Prefix Cache 运行状态快照。"""
    primary_node = orchestrator.llm_router.primary_node
    jev_cache = json.loads(settings.jev_cache_path.read_text(encoding="utf-8")) if settings.jev_cache_path.exists() else {}
    real_md5, real_static_tokens = get_static_constitution_metrics()
    return {
        "status": "ok",
        "gateway_node": primary_node.name,
        "gateway_url": primary_node.url,
        "llm_protocol": primary_node.protocol,
        "llm_model": primary_node.model,
        "circuit_breaker_state": primary_node.breaker.state,
        "circuit_breaker_failures": primary_node.breaker.failure_count,
        "ewma_latency_ms": primary_node.ewma_latency_ms,
        "effective_context_window": settings.effective_context_window,
        "static_prefix_md5": real_md5,
        "static_prefix_tokens": real_static_tokens,
        "jev_model": settings.jev_model,
        "jev_cache_entries": len(jev_cache.get("entries", {})),
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "db_backend": orchestrator.rag_engine.db_backend,
        "sku_count": len(orchestrator.rag_engine.catalog),
    }


@app.get("/api/v1/admin/eval-summary")
async def admin_eval_summary_endpoint() -> Dict[str, Any]:
    """管理后台研发中台：读取 50 题评测基准与三大 A/B 对照实验汇总结果。"""
    summary_path = DATA_DIR / "eval_results_summary.json"
    if not summary_path.exists():
        raise HTTPException(status_code=404, detail="data/eval_results_summary.json not found")
    return json.loads(summary_path.read_text(encoding="utf-8"))


@app.get("/api/v1/admin/benchmark-queries")
async def admin_benchmark_queries_endpoint() -> Dict[str, Any]:
    """管理后台研发中台：读取 50 题黄金评测集全量题目与期望真值。"""
    bench_path = DATA_DIR / "eval_benchmark_50queries.json"
    if not bench_path.exists():
        raise HTTPException(status_code=404, detail="data/eval_benchmark_50queries.json not found")
    return json.loads(bench_path.read_text(encoding="utf-8"))


@app.get("/api/v1/admin/eval/attribution-audit")
async def admin_eval_attribution_audit_endpoint(
    layer: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """管理后台研发中台：查询 CNT 六层漏斗短路判决与四层根因归因审计流水。"""
    audits = analytics_service.query_attribution_audits(
        attribution_layer=layer,
        funnel_stage=stage,
        limit=limit,
        offset=offset,
    )
    return {"status": "ok", "total": len(audits), "audits": audits}


@app.get("/api/v1/admin/eval/attribution-stats")
async def admin_eval_attribution_stats_endpoint() -> Dict[str, Any]:
    """管理后台研发中台：获取评测四层归因分布、六层漏斗通过率与高频根因建议统计。"""
    stats = analytics_service.get_attribution_summary_stats()
    return {"status": "ok", "stats": stats}


@app.post("/api/v1/admin/eval/trigger")
async def admin_eval_trigger_endpoint(req: EvalTriggerRequest) -> Dict[str, Any]:
    """管理后台评测中心：一键异步触发全量自动化评测（支持快速沙箱与实机测试模式）。"""
    task_id = analytics_service.trigger_eval_run(
        mode=req.mode,
        scenario_filter=req.scenario_filter,
    )
    return {
        "status": "started",
        "task_id": task_id,
        "mode": req.mode,
        "message": "评测任务已在后台启动",
    }


@app.get("/api/v1/admin/eval/progress/{task_id}")
async def admin_eval_progress_endpoint(task_id: str) -> Dict[str, Any]:
    """管理后台评测中心：轮询当前正在执行的评测进度或查询最新任务状态。"""
    progress = analytics_service.get_eval_progress(task_id)
    if not progress:
        raise HTTPException(status_code=404, detail=f"Eval task {task_id} not found")
    return progress


@app.get("/api/v1/admin/eval/cases")
async def admin_eval_cases_endpoint(
    scenario_group: Optional[str] = Query(default=None, description="场景组过滤"),
    audit_status: Optional[str] = Query(default=None, description="复核状态过滤 verified / pending_review"),
    case_type: Optional[str] = Query(default=None, description="类型 single_turn / multi_turn"),
    keyword: Optional[str] = Query(default=None, description="关键字检索"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
) -> Dict[str, Any]:
    """管理后台评测中心：分页查询基准题库与质检复核列表。"""
    return analytics_service.list_eval_cases(
        scenario_group=scenario_group,
        audit_status=audit_status,
        case_type=case_type,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@app.patch("/api/v1/admin/eval/cases/{case_id}")
async def admin_update_eval_case_endpoint(case_id: str, req: EvalCaseUpdateRequest) -> Dict[str, Any]:
    """管理后台评测中心：在线修订单个用例的标杆标准回答与复核状态。"""
    try:
        updated = analytics_service.update_eval_case(
            case_id=case_id,
            standard_response=req.standard_response,
            audit_status=req.audit_status,
            hard_constraints=req.hard_constraints,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "case": updated}


@app.post("/api/v1/admin/eval/adopt-feedback")
async def admin_adopt_feedback_endpoint(req: EvalAdoptFeedbackRequest) -> Dict[str, Any]:
    """管理后台评测中心：将 C 端点踩反馈或转人工工单直接收录为官方基准用例。"""
    try:
        new_case = analytics_service.adopt_feedback_as_case(
            source_type=req.source_type,
            source_id=req.source_id,
            scenario_group=req.scenario_group,
            standard_response=req.standard_response,
            query_text=req.query_text,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "new_case_id": new_case.get("case_id"), "case": new_case}



# 若独立前端已完成构建，自动挂载 /h5 与 /admin 静态资源
if H5_DIST_DIR.exists():
    app.mount(
        "/h5",
        StaticFiles(directory=str(H5_DIST_DIR), html=True),
        name="h5_frontend",
    )

if ADMIN_DIST_DIR.exists():
    app.mount(
        "/admin",
        StaticFiles(directory=str(ADMIN_DIST_DIR), html=True),
        name="admin_frontend",
    )
