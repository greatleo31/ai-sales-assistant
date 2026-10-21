# -*- coding: utf-8 -*-
"""
统一配置中心 (app/config.py)
严格对齐 HANDOFF.md §1, §2, §5：
  1. System One: TypeSafe Jev API (POST https://api.typesafe.ai/v1/systemone, model=jev-latest)
  2. System Two: DeepSeek-V4.1-Flash via /v1/responses 协议 (窗口默认 272K)
  3. Embedding: Google Vertex AI gemini-embedding-2 (1024 维, halfvec(1024))
  4. Database: PostgreSQL 16 (pgvector) + 本地 SQLite 镜像库自动降级
  5. Optional RTC Voice: 默认关闭 (ENABLE_RTC_VOICE=false)，零告警启动
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DEPLOY_DIR = ROOT_DIR / "deploy"
ENV_FILE = ROOT_DIR / ".env"


def load_dotenv_file(env_path: Path = ENV_FILE, override: bool = False) -> Dict[str, str]:
    """零外部依赖解析 .env 文件并注入环境变量（若环境变量已存在且 override=False 则保留现值）。"""
    loaded: Dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        loaded[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return loaded


# 模块导入时自动加载根目录 .env，并固定 tiktoken 本地离线缓存目录
load_dotenv_file()
if (DATA_DIR / "tiktoken_cache").exists():
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(DATA_DIR / "tiktoken_cache"))


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None or not val.strip():
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val is None or not val.strip():
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class AppConfig:
    # 1. System One (TypeSafe Jev)
    typesafe_api_key: str = os.environ.get("TYPESAFE_API_KEY", "")
    jev_base_url: str = os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai/v1/systemone")
    jev_model: str = os.environ.get("JEV_MODEL", "jev-latest")
    # 在线推理默认超时时间（支持调用方按场景传入 0.8s 严格 SLA 超时或 3.5s 跨境实时超时）
    jev_timeout_sec: float = _env_float("JEV_TIMEOUT_SEC", 3.5)
    jev_sla_timeout_sec: float = _env_float("JEV_SLA_TIMEOUT_SEC", 0.8)
    jev_high_confidence: float = _env_float("JEV_HIGH_CONFIDENCE", 0.75)
    jev_low_confidence: float = _env_float("JEV_LOW_CONFIDENCE", 0.45)
    need_rag_threshold: float = _env_float("NEED_RAG_THRESHOLD", 0.50)
    context_dependent_threshold: float = _env_float("CONTEXT_DEPENDENT_THRESHOLD", 0.50)
    handoff_guard_threshold: float = _env_float("HANDOFF_GUARD_THRESHOLD", 0.80)
    jev_cache_path: Path = DATA_DIR / "jev_cache.json"

    # 2. System Two (DeepSeek-V4.1-Flash via /v1/chat/completions)
    llm_protocol: str = os.environ.get("LLM_PROTOCOL", "chat")
    llm_primary_base_url: str = os.environ.get("LLM_PRIMARY_BASE_URL", "https://go.penguin.ooo/api/v1")
    llm_primary_responses_url: str = os.environ.get(
        "LLM_PRIMARY_RESPONSES_URL", "https://go.penguin.ooo/api/v1/responses"
    )
    llm_primary_api_key: str = os.environ.get("LLM_PRIMARY_API_KEY", "")
    llm_primary_model: str = os.environ.get("LLM_PRIMARY_MODEL", "deepseek-flash")
    effective_context_window: int = _env_int("EFFECTIVE_CONTEXT_WINDOW", 272000)

    # 备用节点槽位 (保留 EWMA + 三态熔断架构，默认关闭 vLLM 备用节点)
    llm_fallback_enabled: bool = _env_bool("LLM_FALLBACK_ENABLED", False)
    llm_fallback_base_url: str = os.environ.get("LLM_FALLBACK_BASE_URL", "http://127.0.0.1:8000/v1")
    llm_fallback_api_key: str = os.environ.get("LLM_FALLBACK_API_KEY", "EMPTY")
    llm_fallback_model: str = os.environ.get("LLM_FALLBACK_MODEL", "vllm/deepseek-ai/DeepSeek-V4-Flash")

    # 3. Embedding (Google Vertex AI gemini-embedding-2, 1024维)
    embedding_provider: str = os.environ.get("EMBEDDING_PROVIDER", "gemini_vertex_v1beta")
    embedding_url: str = os.environ.get(
        "EMBEDDING_URL",
        "https://go.penguin.ooo/api/v1beta/models/gemini-embedding-2:batchEmbedContents",
    )
    embedding_api_key: str = os.environ.get("EMBEDDING_API_KEY", "")
    embedding_model: str = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-2")
    embedding_dim: int = _env_int("EMBEDDING_DIM", 1024)
    embeddings_cache_path: Path = DATA_DIR / "embeddings_cache.json"

    # 4. 腾讯云 COS
    cos_host: str = os.environ.get("COS_HOST", "")
    cos_region: str = os.environ.get("COS_REGION", "ap-guangzhou")
    cos_bucket: str = os.environ.get("COS_BUCKET", "")
    cos_secret_id: str = os.environ.get("COS_SECRET_ID", "")
    cos_secret_key: str = os.environ.get("COS_SECRET_KEY", "")

    # 5. PostgreSQL + pgvector & 本地 SQLite 镜像库
    postgres_user: str = os.environ.get("POSTGRES_USER", "postgres")
    postgres_password: str = os.environ.get("POSTGRES_PASSWORD", "")
    postgres_db: str = os.environ.get("POSTGRES_DB", "shopping_agent")
    postgres_host: str = os.environ.get("POSTGRES_HOST", "127.0.0.1")
    postgres_port: int = _env_int("POSTGRES_PORT", 5432)
    sqlite_db_path: Path = DATA_DIR / "local_vector_store.db"

    # 6. 数据资产路径
    spec_registry_path: Path = DATA_DIR / "spec_registry.json"
    conflict_pairs_path: Path = DATA_DIR / "conflict_pairs.json"
    catalog_path: Path = DATA_DIR / "ecommerce_catalog_60skus.json"
    platform_rules_path: Path = DATA_DIR / "platform_rules.json"
    mock_orders_path: Path = DATA_DIR / "mock_orders.json"
    image_manifest_path: Path = DATA_DIR / "image_manifest.json"

    # 7. RTC 语音预留开关 (默认关闭，零告警)
    enable_rtc_voice: bool = _env_bool("ENABLE_RTC_VOICE", False)


settings = AppConfig()
