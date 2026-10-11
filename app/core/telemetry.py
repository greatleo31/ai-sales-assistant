# -*- coding: utf-8 -*-
"""
精确 Token 计量与静态前缀缓存对齐监控器 (app/core/telemetry.py)
严格对齐 HANDOFF.md §1.4, §2, §3 与落地级技术设计规范 §5.2：
  1. 废除 `len(text) // 2` 粗略估算，统一使用 `tiktoken` (`cl100k_base`) 进行确定性 Token 计量；
  2. 基于 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 二元切分系统提示词，确保边界之上的静态宪法在所有会话与轮次间 100% 字节级恒定；
  3. 输出 100% 本地可测、可断言的静态对齐指标：
     - `static_prefix_md5`：静态前缀的 MD5 哈希值
     - `prefix_cache_aligned`：当前轮次 `static_prefix_md5` 是否与基准哈希严格相等
     - `static_prefix_tokens` & `dynamic_suffix_tokens`：基于 `tiktoken` 实测的前后缀 Token 数量
     - `static_ratio_pct`：静态前缀占总系统提示词的百分比
     - `tokenizer`：固定标识 `"tiktoken_cl100k_base"`
"""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

from app.config import DATA_DIR

BOUNDARY_MARKER = "==================== SYSTEM_PROMPT_DYNAMIC_BOUNDARY ===================="

# 确保离线缓存目录优先生效，杜绝外网下载阻塞
if (DATA_DIR / "tiktoken_cache").exists():
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(DATA_DIR / "tiktoken_cache"))

import tiktoken  # noqa: E402

_ENCODER = tiktoken.get_encoding("cl100k_base")
_BASELINE_PREFIX_MD5: Optional[str] = None


def count_tokens_exact(text: str) -> int:
    """使用 tiktoken (cl100k_base) 精确计算文本 Token 数量。"""
    if not text:
        return 0
    return len(_ENCODER.encode(text))


def reset_baseline_prefix_md5(new_baseline: Optional[str] = None) -> None:
    """重置或指定静态前缀基准 MD5（供单测与 A/B 对比实验使用）。"""
    global _BASELINE_PREFIX_MD5
    _BASELINE_PREFIX_MD5 = new_baseline


def get_baseline_prefix_md5() -> Optional[str]:
    """读取当前已锁定的静态前缀基准 MD5。"""
    return _BASELINE_PREFIX_MD5


def split_system_prompt(full_system_prompt: str) -> tuple[str, str]:
    """按 BOUNDARY_MARKER 将完整系统提示词切分为 (static_prefix, dynamic_suffix)。"""
    if BOUNDARY_MARKER in full_system_prompt:
        parts = full_system_prompt.split(BOUNDARY_MARKER, maxsplit=1)
        return parts[0], parts[1]
    return full_system_prompt, ""


def evaluate_prompt_alignment(
    full_system_prompt: str,
    expected_baseline_md5: Optional[str] = None,
) -> Dict[str, Any]:
    """
    评估系统提示词的前缀缓存对齐状态与精确 Token 占比：
      - 若包含 `BOUNDARY_MARKER`，切分出 `static_prefix` 与 `dynamic_suffix`；
      - 若未包含 `BOUNDARY_MARKER`（如传统 Monolithic 拼接模式），则将首段或全文视作前缀计算 MD5，用于 A/B 对比。
    """
    global _BASELINE_PREFIX_MD5

    has_boundary = BOUNDARY_MARKER in full_system_prompt
    if has_boundary:
        static_prefix, dynamic_suffix = split_system_prompt(full_system_prompt)
    else:
        # 传统 Monolithic 模式：无边界切分，前 50% 或全文直接受头部动态变量污染
        static_prefix = full_system_prompt
        dynamic_suffix = ""

    current_md5 = hashlib.md5(static_prefix.encode("utf-8")).hexdigest()
    baseline = expected_baseline_md5 if expected_baseline_md5 is not None else _BASELINE_PREFIX_MD5
    if baseline is None:
        _BASELINE_PREFIX_MD5 = current_md5
        baseline = current_md5

    prefix_tokens = count_tokens_exact(static_prefix) if has_boundary else 0
    suffix_tokens = (
        count_tokens_exact(dynamic_suffix)
        if has_boundary
        else count_tokens_exact(full_system_prompt)
    )
    total_tokens = max(prefix_tokens + suffix_tokens, 1)

    return {
        "prefix_cache_aligned": bool(has_boundary and (current_md5 == baseline)),
        "static_prefix_md5": current_md5,
        "baseline_prefix_md5": baseline,
        "static_prefix_tokens": prefix_tokens,
        "dynamic_suffix_tokens": suffix_tokens,
        "total_system_tokens": prefix_tokens + suffix_tokens,
        "static_ratio_pct": round((prefix_tokens / total_tokens) * 100.0, 2),
        "tokenizer": "tiktoken_cl100k_base",
    }


@contextmanager
def trace_span(metrics_dict: Dict[str, float], key: str) -> Generator[None, None, None]:
    """轻量高精度计时上下文管理器（毫秒级，保留 2 位小数）。"""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        metrics_dict[key] = round((time.perf_counter() - t0) * 1000.0, 2)
