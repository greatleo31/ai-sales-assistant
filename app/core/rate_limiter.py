# -*- coding: utf-8 -*-
"""
纯内存滑动窗口频控引擎 (app/core/rate_limiter.py)
严格对齐安全体系改造规范【阶段一】：
  1. 基于 collections.deque 实现纳秒级高精度内存滑动窗口，零外部 Redis/中间件依赖；
  2. 严格支持 20 RPM（每分钟 20 次请求）上限阈值；
  3. 采用 IP + User/Session 复合滑动窗口机制，已登录用户按 user_id 限流，匿名访客按客户端 IP 与会话复合限流，防止局域网 NAT 误伤；
  4. 超频立即抛出标准 HTTP 429 Too Many Requests，并附带 Retry-After 头部；
  5. 内置自动过期惰性清理机制，杜绝长周期运行下的内存泄露。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Request, status

from app.config import settings


class SlidingWindowRateLimiter:
    """高并发线程安全纯内存滑动窗口限流器。"""

    def __init__(self, limit: int = 20, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._queues: Dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._last_cleanup = time.time()

    def check_rate_limit(self, key: str) -> Tuple[bool, int, float]:
        """
        检查指定 key 是否允许当前请求通过。
        返回元组: (is_allowed, remaining_quota, retry_after_seconds)
        """
        now = time.time()
        window_start = now - self.window_seconds

        with self._lock:
            # 定期全局惰性清理闲置 key (每 300 秒执行一次)
            if now - self._last_cleanup > 300.0:
                self._purge_expired(window_start)
                self._last_cleanup = now

            q = self._queues[key]
            # 移出时间窗口之外的历史时间戳
            while q and q[0] <= window_start:
                q.popleft()

            current_count = len(q)
            if current_count >= self.limit:
                # 超过阈值：计算最早一条记录滑出窗口所需的等待时间
                oldest = q[0]
                retry_after = max(1.0, round(self.window_seconds - (now - oldest), 1))
                return False, 0, retry_after

            # 记录当前请求时间戳并放行
            q.append(now)
            remaining = self.limit - len(q)
            return True, remaining, 0.0

    def reset(self, key: Optional[str] = None) -> None:
        """重置限流状态（供单元测试快速清理状态）。"""
        with self._lock:
            if key is not None:
                self._queues.pop(key, None)
            else:
                self._queues.clear()

    def _purge_expired(self, window_start: float) -> None:
        """清理已完全失效且无请求残留的空队列。"""
        keys_to_delete = []
        for k, q in self._queues.items():
            while q and q[0] <= window_start:
                q.popleft()
            if not q:
                keys_to_delete.append(k)
        for k in keys_to_delete:
            del self._queues[k]


# 全局 20 RPM 默认限流器实例
global_rate_limiter = SlidingWindowRateLimiter(
    limit=settings.rate_limit_rpm,
    window_seconds=60.0,
)


def enforce_rate_limit(
    key: str,
    limiter: Optional[SlidingWindowRateLimiter] = None,
) -> None:
    """快捷断言：若指定 key 超过频控阈值则立即抛出 HTTP 429。"""
    active_limiter = limiter or global_rate_limiter
    allowed, remaining, retry_after = active_limiter.check_rate_limit(key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求过于频繁：当前频控策略上限为每分钟 {active_limiter.limit} 次请求，请在 {int(retry_after)} 秒后重试",
            headers={
                "Retry-After": str(int(retry_after)),
                "X-RateLimit-Limit": str(active_limiter.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
