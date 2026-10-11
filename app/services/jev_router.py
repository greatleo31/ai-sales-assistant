# -*- coding: utf-8 -*-
"""
System One (TypeSafe Jev API) 决策路由器与结果缓存层 (app/services/jev_router.py)
严格对齐 HANDOFF.md §1, §2, §3, §5：
  1. 端点：POST https://api.typesafe.ai/v1/systemone，Model: "jev-latest"（实测返回 "jev-1.13.0"）
  2. 契约铁律：
     - 请求体严禁包含 `legend` 字段（`legend` 由 Jev 在响应体中自动生成）；
     - `instructions` 与 `criteria` 100% 保持纯英文（零 CJK 字符）；
     - `state` 100% 保持中文原文，严禁翻译（防止改坏“17.3寸”“¥29.90”“GaN-65W-Pro”等实体）；
     - `choice` 的 `criteria` 为封闭英文 Dict，按返回 `confidence` 执行三路门控（>=0.75 直通 / 0.45~0.75 中置信澄清 / <0.45 回落本地规则层）；
     - `noul` 返回 0.0~1.0 概率浮点（无独立 confidence），阈值：need_rag>=0.5, is_context_dependent>=0.5, handoff_guard>=0.8；
     - `score` 的 `criteria` 为 5 档英文有序 List（Jev 从 "0"~"4" 编号），代码中显式执行 `round(raw_score + 1.0, 2)` 映射为 1.0~5.0 分制。
  3. Jev 结果缓存 (`data/jev_cache.json`)：
     - 键公式：`sha256(model + state + 规范化 questions)`；
     - 值含：`answers` / `usage` / 首次实测 `jev_rtt_ms` / `model_version` / `created_at`；
     - 命中即 0 远端 Token 消耗；支持 `refresh=True` 与 CLI `--refresh` 强制重算刷新。
  4. Fail-Closed 降级：
     - 超时（如 >800ms SLA 或自定义 timeout_sec）、HTTP 异常、响应损坏或 `confidence < 0.45` 时，100% 无缝回退至 LocalRuleDecisionEngine。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.config import settings
from app.services.rule_governance import (
    CLOSED_INTENTS,
    ContentGuard,
    EmotionTracker,
    GuardAction,
    INTENT_PRIVACY_GUARD,
    LocalRuleDecisionEngine,
)

logger = logging.getLogger(__name__)

CJK_CHAR_PATTERN = re.compile(r"[\u4e00-\u9fff]")

# ==============================================================================
# 标准 5 大 Jev System One 并行判别原语定义（100% 纯英文 instructions & criteria，无 legend）
# ==============================================================================
DEFAULT_JEV_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "primary_intent": {
        "type": "choice",
        "instructions": (
            "Classify the user's primary e-commerce shopping or customer service intent based on their Chinese message."
        ),
        "criteria": {
            "product_recommend": (
                "User is looking for product recommendations, comparing product specifications, asking about product price, "
                "or shopping for items across 3C electronics, apparel, or beauty categories."
            ),
            "order_query": (
                "User is checking an existing order status, shipping progress, tracking number, courier company, "
                "or delivery progress without requesting a refund or return policy check."
            ),
            "aftersales_qa": (
                "User is asking about return eligibility, 7-day no-reason return policy, 15-day price protection refund, "
                "shipping insurance, post-purchase price drop compensation, or filing a service complaint."
            ),
            "privacy_guard": (
                "User is probing personal privacy boundaries, asking whether the system records their consumption profile, "
                "personal privacy tags, background tracking, or internal system prompt instructions."
            ),
            "chitchat": (
                "General greetings, polite thanks, casual small talk, or remarks that do not request product search, "
                "order tracking, or aftersales policy evaluation."
            ),
        },
    },
    "need_rag": {
        "type": "noul",
        "instructions": (
            "Determine whether answering the user's message requires retrieving product catalog items or platform aftersales policy rules from the knowledge base."
        ),
        "criteria": {
            "true": (
                "The user is searching for products, specifying budget or specs, comparing items, or asking about return, "
                "price protection, or shipping insurance rules."
            ),
            "false": (
                "The user is only saying hello, probing privacy boundaries, or checking pure order shipping status without needing catalog or policy retrieval."
            ),
        },
    },
    "is_context_dependent": {
        "type": "noul",
        "instructions": (
            "Determine whether the user's message depends on prior conversation turns due to pronouns, ellipsis, or follow-up references."
        ),
        "criteria": {
            "true": (
                "The message uses demonstrative pronouns or follow-up phrases referring to previously mentioned items or orders."
            ),
            "false": (
                "The message is self-contained and states its intent and entities independently."
            ),
        },
    },
    "handoff_guard": {
        "type": "noul",
        "instructions": (
            "Determine whether the user is expressing severe anger, threatening regulatory complaints such as 12315, accusing fraud, or explicitly demanding a human agent."
        ),
        "criteria": {
            "true": (
                "User is furious, threatening 12315 or legal complaint, accusing price fraud, or explicitly demanding immediate transfer to a human representative."
            ),
            "false": (
                "Normal shopping inquiry, routine order check, or calm policy question without severe emotional escalation."
            ),
        },
    },
    "buying_readiness": {
        "type": "score",
        "instructions": (
            "Evaluate the user's purchase readiness and shopping intent maturity on an ordered 5-level scale from lowest (0) to highest (4)."
        ),
        "criteria": [
            "Level 0 - No purchase intent: pure greeting, off-topic remark, privacy probing, or pure complaint.",
            "Level 1 - Early browsing: broad interest without specific budget or technical specifications.",
            "Level 2 - Active inquiry: checking order status, aftersales policy, or general category features.",
            "Level 3 - High purchase intent: specifying concrete budget limits and technical specifications to select a product.",
            "Level 4 - Immediate purchase readiness: confirming final stock, exact discount, or immediate order placement.",
        ],
    },
}


def canonicalize_questions(questions: Dict[str, Any]) -> str:
    """对 questions 字典执行确定性 JSON 规范化序列化（键排序、无多余空白、UTF-8 原生字符）。"""
    return json.dumps(questions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_jev_cache_key(model: str, state: str, questions: Optional[Dict[str, Any]] = None) -> str:
    """
    计算 Jev 结果缓存键（HANDOFF.md §2）：
      key = sha256(model + state + 规范化 questions)
    """
    q_dict = questions if questions is not None else DEFAULT_JEV_QUESTIONS
    norm_q = canonicalize_questions(q_dict)
    raw_material = f"{model}{state}{norm_q}"
    return hashlib.sha256(raw_material.encode("utf-8")).hexdigest()


def validate_jev_request_payload(payload: Dict[str, Any]) -> None:
    """
    严格校验发往 TypeSafe System One (Jev) 的请求体契约（HANDOFF.md §1 & §2）：
      1. 顶层与各问题内严禁包含 `legend` 字段；
      2. `state` 必须为非空字符串；
      3. `instructions` 与 `criteria` 必须为 100% 英文（严禁包含 CJK 中文字符）；
      4. `choice` 的 criteria 必须为封闭 dict，`score` 的 criteria 必须为有序 list，`noul` 的 criteria 若存在必须为 dict。
    """
    if not isinstance(payload, dict):
        raise ValueError("Jev payload must be a dict")
    if "legend" in payload:
        raise ValueError("Contract Violation: 'legend' is strictly forbidden in Jev request payload")

    state = payload.get("state")
    if not isinstance(state, str) or not state.strip():
        raise ValueError("Jev payload 'state' must be a non-empty string")

    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Jev payload 'model' must be a non-empty string")

    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("Jev payload 'questions' must be a non-empty dict")

    for q_name, q_spec in questions.items():
        if not isinstance(q_spec, dict):
            raise ValueError(f"Question '{q_name}' must be a dict")
        if "legend" in q_spec:
            raise ValueError(f"Contract Violation: 'legend' is strictly forbidden in question '{q_name}'")

        q_type = q_spec.get("type")
        if q_type not in ("choice", "noul", "score"):
            raise ValueError(f"Unsupported Jev question type in '{q_name}': {q_type!r}")

        instructions = q_spec.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(f"Question '{q_name}' must have non-empty English 'instructions'")
        if CJK_CHAR_PATTERN.search(instructions):
            raise ValueError(f"Contract Violation: Question '{q_name}' instructions must be 100% English (found CJK)")

        criteria = q_spec.get("criteria")
        if q_type == "choice":
            if not isinstance(criteria, dict) or not criteria:
                raise ValueError(f"Choice question '{q_name}' criteria must be a non-empty dict")
            for k, v in criteria.items():
                if not isinstance(k, str) or not isinstance(v, str) or CJK_CHAR_PATTERN.search(k) or CJK_CHAR_PATTERN.search(v):
                    raise ValueError(f"Choice question '{q_name}' criteria keys/values must be 100% English strings")
        elif q_type == "score":
            if not isinstance(criteria, list) or len(criteria) != 5:
                raise ValueError(f"Score question '{q_name}' criteria must be an ordered list of 5 English level descriptions")
            for item in criteria:
                if not isinstance(item, str) or CJK_CHAR_PATTERN.search(item):
                    raise ValueError(f"Score question '{q_name}' criteria items must be 100% English strings")
        elif q_type == "noul" and criteria is not None:
            if not isinstance(criteria, dict) or set(criteria.keys()) != {"true", "false"}:
                raise ValueError(f"Noul question '{q_name}' criteria must be a dict with 'true' and 'false' keys")
            for k, v in criteria.items():
                if not isinstance(v, str) or CJK_CHAR_PATTERN.search(v):
                    raise ValueError(f"Noul question '{q_name}' criteria values must be 100% English strings")


class JevDecisionRouter:
    """
    TypeSafe Jev System One 决策路由器：
      - 内置 `data/jev_cache.json` 读写与 `--refresh` 强制更新；
      - 内置三路置信度门控与 Fail-Closed 本地规则降级；
      - 支持自定义 transport_fn 以便在离线/超时/异常单元测试中注入确定性探针。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        cache_path: Optional[Path] = None,
        timeout_sec: Optional[float] = None,
        high_confidence: Optional[float] = None,
        low_confidence: Optional[float] = None,
        need_rag_threshold: Optional[float] = None,
        context_dependent_threshold: Optional[float] = None,
        handoff_guard_threshold: Optional[float] = None,
        transport_fn: Optional[Callable[[Dict[str, Any], float], Dict[str, Any]]] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.typesafe_api_key
        self.base_url = base_url or settings.jev_base_url
        self.model = model or settings.jev_model
        self.cache_path = cache_path or settings.jev_cache_path
        self.timeout_sec = timeout_sec if timeout_sec is not None else settings.jev_timeout_sec
        self.high_confidence = high_confidence if high_confidence is not None else settings.jev_high_confidence
        self.low_confidence = low_confidence if low_confidence is not None else settings.jev_low_confidence
        self.need_rag_threshold = (
            need_rag_threshold if need_rag_threshold is not None else settings.need_rag_threshold
        )
        self.context_dependent_threshold = (
            context_dependent_threshold
            if context_dependent_threshold is not None
            else settings.context_dependent_threshold
        )
        self.handoff_guard_threshold = (
            handoff_guard_threshold if handoff_guard_threshold is not None else settings.handoff_guard_threshold
        )
        self._transport_fn = transport_fn
        self._cache_data: Dict[str, Any] = self._load_cache()

    def _load_cache(self) -> Dict[str, Any]:
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                    return data
            except Exception as exc:
                logger.warning(f"[JevDecisionRouter] 读取缓存文件失败，将重新初始化: {exc}")
        return {
            "version": "2026-10-11.m3",
            "model_default": self.model,
            "entries": {},
        }

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self._cache_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _default_http_transport(self, payload: Dict[str, Any], timeout_sec: float) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("Missing TYPESAFE_API_KEY")
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body_bytes,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-sales-assistant/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = resp.getcode()
            raw_text = resp.read().decode("utf-8")
            if status != 200:
                raise RuntimeError(f"Jev HTTP status {status}: {raw_text[:200]}")
            return json.loads(raw_text)

    def _parse_and_gate_jev_response(
        self,
        raw_resp: Dict[str, Any],
        state: str,
        messages: Optional[List[Dict[str, Any]]],
        rtt_ms: float,
        cache_hit: bool,
        cache_key: str,
    ) -> Dict[str, Any]:
        """解析 Jev 响应并执行三路置信度门控与本地规则安全并集。"""
        if not isinstance(raw_resp, dict) or not isinstance(raw_resp.get("answers"), dict):
            raise ValueError("Malformed Jev response: missing 'answers' dict")

        answers = raw_resp["answers"]
        usage = raw_resp.get("usage") or {}
        model_version = str(raw_resp.get("model") or self.model)

        # 1. 解析 choice: primary_intent
        pi_obj = answers.get("primary_intent")
        if not isinstance(pi_obj, dict):
            raise ValueError("Malformed Jev response: missing 'primary_intent' dict")
        primary_intent = pi_obj.get("choice")
        intent_conf = pi_obj.get("confidence")
        intent_probs = pi_obj.get("probabilities")
        if (
            primary_intent not in CLOSED_INTENTS
            or not isinstance(intent_conf, (int, float))
            or isinstance(intent_conf, bool)
            or not math.isfinite(float(intent_conf))
            or not (0.0 <= float(intent_conf) <= 1.0 + 1e-6)
            or not isinstance(intent_probs, dict)
            or not intent_probs
        ):
            raise ValueError(f"Malformed primary_intent payload: {pi_obj!r}")
        for pk, pv in intent_probs.items():
            if (
                pk not in CLOSED_INTENTS
                or not isinstance(pv, (int, float))
                or isinstance(pv, bool)
                or not math.isfinite(float(pv))
                or not (0.0 <= float(pv) <= 1.0 + 1e-5)
            ):
                raise ValueError(f"Malformed primary_intent probability {pk}={pv!r}")
        intent_conf_f = float(intent_conf)

        # 2. 解析 3 个 noul (0.0~1.0 概率浮点，无独立 confidence)
        def _extract_noul(q_key: str) -> float:
            obj = answers.get(q_key)
            if (
                not isinstance(obj, dict)
                or not isinstance(obj.get("noul"), (int, float))
                or isinstance(obj.get("noul"), bool)
                or not math.isfinite(float(obj["noul"]))
            ):
                raise ValueError(f"Malformed noul answer for '{q_key}': {obj!r}")
            val = float(obj["noul"])
            if not (0.0 <= val <= 1.0 + 1e-6):
                raise ValueError(f"Noul value out of [0, 1] range for '{q_key}': {val}")
            return val

        need_rag_score = _extract_noul("need_rag")
        ctx_dep_score = _extract_noul("is_context_dependent")
        handoff_score = _extract_noul("handoff_guard")

        # 3. 解析 score: buying_readiness (Jev legend 从 "0"~"4"，加权均值 0.0~4.0，显式 +1 映射为 1.0~5.0)
        br_obj = answers.get("buying_readiness")
        if (
            not isinstance(br_obj, dict)
            or not isinstance(br_obj.get("score"), (int, float))
            or isinstance(br_obj.get("score"), bool)
            or not math.isfinite(float(br_obj["score"]))
        ):
            raise ValueError(f"Malformed score answer for 'buying_readiness': {br_obj!r}")
        br_raw = float(br_obj["score"])
        if not (0.0 <= br_raw <= 4.0 + 1e-6):
            raise ValueError(f"buying_readiness raw score out of [0, 4] range: {br_raw}")
        br_score = round(br_raw + 1.0, 2)

        # 4. 本地零延迟护栏与情绪状态检测
        guard = ContentGuard.check_input(state)
        emotion = EmotionTracker.detect(messages or [], current_query=state)

        # 5. 三路置信度门控 (Confidence Gating):
        #    - confidence < low_confidence (0.45) -> Fail-Closed 回退本地规则层！
        if intent_conf_f < self.low_confidence:
            fallback_res = LocalRuleDecisionEngine.evaluate(
                state=state,
                messages=messages,
                fallback_reason=f"low_confidence({intent_conf_f:.3f}<{self.low_confidence})",
                jev_rtt_ms=rtt_ms,
            )
            fallback_res["cache_key"] = cache_key
            fallback_res["raw_jev_intent"] = primary_intent
            fallback_res["raw_jev_confidence"] = round(intent_conf_f, 4)
            return fallback_res

        if intent_conf_f >= self.high_confidence:
            confidence_tier = "high"
            clarification_needed = False
        else:
            confidence_tier = "medium"
            clarification_needed = True

        # 若本地护栏明确捕获到隐私试探 (privacy_probe)，确保 primary_intent 受到保护
        if guard.category == "privacy_probe" and primary_intent != INTENT_PRIVACY_GUARD:
            primary_intent = INTENT_PRIVACY_GUARD

        # handoff_guard 采用 Jev (>=0.80) 与本地高危情绪规则的 Fail-Closed 安全并集
        handoff_triggered = (handoff_score >= self.handoff_guard_threshold) or emotion.trigger_handoff

        in_tokens = int(usage.get("input_tokens", 0))
        out_tokens = int(usage.get("output_tokens", 0))

        return {
            "decision_source": "jev_cache" if cache_hit else "jev_live",
            "fallback_triggered": False,
            "fallback_reason": None,
            "cache_hit": cache_hit,
            "cache_key": cache_key,
            "model": model_version,
            "primary_intent": primary_intent,
            "intent_confidence": round(intent_conf_f, 4),
            "confidence_tier": confidence_tier,
            "clarification_needed": clarification_needed,
            "intent_probabilities": {k: round(float(v), 4) for k, v in intent_probs.items()},
            "need_rag": (need_rag_score >= self.need_rag_threshold) or (primary_intent == "product_recommend"),
            "need_rag_score": round(need_rag_score, 4),
            "is_context_dependent": ctx_dep_score >= self.context_dependent_threshold,
            "context_dependent_score": round(ctx_dep_score, 4),
            "handoff_guard": handoff_triggered,
            "handoff_guard_score": round(handoff_score, 4),
            "buying_readiness_raw": round(br_raw, 2),
            "buying_readiness_score": br_score,
            "jev_rtt_ms": round(rtt_ms, 2),
            "jev_input_tokens": in_tokens,
            "jev_output_tokens": out_tokens,
            "effective_remote_tokens": 0 if cache_hit else (in_tokens + out_tokens),
            "guard_action": guard.action.value,
            "guard_category": guard.category,
            "emotion_type": emotion.emotion_type,
            "emotion_strategy_hint": emotion.strategy_hint,
        }

    def evaluate(
        self,
        state: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        questions: Optional[Dict[str, Any]] = None,
        refresh: bool = False,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        同步评估入口：
          1. 输入安全护栏检查（若 BLOCK 则直接拦截回落）；
          2. 计算 `sha256(model + state + 规范化 questions)`，若命中 `data/jev_cache.json` 且 `refresh=False` 则 0 Token 消耗直接返回；
          3. 否则发起真实 Jev 请求（校验无 `legend`、纯英文 instructions/criteria、中文原文 state）；
          4. 若超时（> effective_timeout）、网络异常、响应损坏或 `confidence < 0.45`，无缝 Fail-Closed 回落至 LocalRuleDecisionEngine。
        """
        clean_state = (state or "").strip()
        guard = ContentGuard.check_input(clean_state)
        if guard.action == GuardAction.BLOCK:
            res = LocalRuleDecisionEngine.evaluate(
                state=clean_state,
                messages=messages,
                fallback_reason=f"content_guard_block:{guard.category}",
                jev_rtt_ms=0.0,
            )
            res["safe_reply"] = guard.safe_reply
            return res

        q_dict = questions if questions is not None else DEFAULT_JEV_QUESTIONS
        payload = {
            "state": clean_state,
            "model": self.model,
            "questions": q_dict,
        }
        # 强制执行契约自检（严禁 legend、严禁翻译 state、强制纯英文 instructions/criteria）
        validate_jev_request_payload(payload)

        cache_key = compute_jev_cache_key(self.model, clean_state, q_dict)
        expected_q_sha = hashlib.sha256(canonicalize_questions(q_dict).encode("utf-8")).hexdigest()
        entries = self._cache_data.setdefault("entries", {})

        # 命中缓存且未要求强制刷新
        if not refresh and cache_key in entries and self._transport_fn is None:
            cached_entry = entries[cache_key]
            try:
                if (
                    cached_entry.get("state") != clean_state
                    or cached_entry.get("model") != self.model
                    or (
                        cached_entry.get("questions_sha256")
                        and cached_entry.get("questions_sha256") != expected_q_sha
                    )
                ):
                    raise ValueError("Cached entry metadata mismatch")
                raw_resp = {
                    "model": cached_entry.get("model_version", self.model),
                    "answers": cached_entry["answers"],
                    "usage": cached_entry.get("usage", {}),
                }
                return self._parse_and_gate_jev_response(
                    raw_resp=raw_resp,
                    state=clean_state,
                    messages=messages,
                    rtt_ms=float(cached_entry.get("jev_rtt_ms", 0.0)),
                    cache_hit=True,
                    cache_key=cache_key,
                )
            except Exception as exc:
                logger.warning(f"[JevDecisionRouter] 缓存条目校验异常 ({cache_key[:10]}): {exc}，将转入实时/回退流程")

        effective_timeout = timeout_sec if timeout_sec is not None else self.timeout_sec
        transport = self._transport_fn or self._default_http_transport

        t0 = time.perf_counter()
        try:
            raw_resp = transport(payload, effective_timeout)
            rtt_ms = (time.perf_counter() - t0) * 1000.0
            # 若调用方显式指定了严格 SLA timeout_sec，且 transport 返回耗时超过阈值，同样按超时降级
            if rtt_ms > effective_timeout * 1000.0 + 5.0:
                raise TimeoutError(f"Jev RTT {rtt_ms:.1f}ms exceeded timeout {effective_timeout * 1000:.1f}ms")

            parsed = self._parse_and_gate_jev_response(
                raw_resp=raw_resp,
                state=clean_state,
                messages=messages,
                rtt_ms=rtt_ms,
                cache_hit=False,
                cache_key=cache_key,
            )

            # 仅当自定义 mock transport 未注入且真实响应有效时写入持久化缓存
            if self._transport_fn is None and not parsed.get("fallback_triggered"):
                entries[cache_key] = {
                    "cache_key": cache_key,
                    "model": self.model,
                    "model_version": str(raw_resp.get("model") or self.model),
                    "state": clean_state,
                    "questions_sha256": hashlib.sha256(
                        canonicalize_questions(q_dict).encode("utf-8")
                    ).hexdigest(),
                    "answers": raw_resp["answers"],
                    "usage": raw_resp.get("usage", {}),
                    "jev_rtt_ms": round(rtt_ms, 2),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                self._save_cache()

            return parsed

        except TimeoutError as exc:
            rtt_ms = (time.perf_counter() - t0) * 1000.0
            logger.warning(f"[JevDecisionRouter] Jev 超时回退本地规则层 ({rtt_ms:.1f}ms): {exc}")
            res = LocalRuleDecisionEngine.evaluate(
                state=clean_state,
                messages=messages,
                fallback_reason="timeout",
                jev_rtt_ms=rtt_ms,
            )
            res["cache_key"] = cache_key
            return res
        except urllib.error.URLError as exc:
            rtt_ms = (time.perf_counter() - t0) * 1000.0
            reason = "timeout" if "timed out" in str(exc).lower() else f"http_error:{exc.reason}"
            logger.warning(f"[JevDecisionRouter] Jev 网络异常回退本地规则层 ({reason}): {exc}")
            res = LocalRuleDecisionEngine.evaluate(
                state=clean_state,
                messages=messages,
                fallback_reason=reason,
                jev_rtt_ms=rtt_ms,
            )
            res["cache_key"] = cache_key
            return res
        except Exception as exc:
            rtt_ms = (time.perf_counter() - t0) * 1000.0
            reason = "timeout" if "timed out" in str(exc).lower() else f"exception:{type(exc).__name__}"
            logger.warning(f"[JevDecisionRouter] Jev 异常回退本地规则层 ({reason}): {exc}")
            res = LocalRuleDecisionEngine.evaluate(
                state=clean_state,
                messages=messages,
                fallback_reason=reason,
                jev_rtt_ms=rtt_ms,
            )
            res["cache_key"] = cache_key
            return res

    async def evaluate_async(
        self,
        state: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        questions: Optional[Dict[str, Any]] = None,
        refresh: bool = False,
        timeout_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """异步评估入口（供主链路与本地硬约束提取+预检索通过 asyncio.gather 并发执行）。"""
        return await asyncio.to_thread(
            self.evaluate,
            state=state,
            messages=messages,
            questions=questions,
            refresh=refresh,
            timeout_sec=timeout_sec,
        )


# 基准预热查询集（覆盖 C1~C5 硬约束冲突、专有名词选品、订单查询、12315 愤怒售后维权、隐私试探、日常寒暄）
BENCHMARK_WARMUP_STATES: List[str] = [
    "帮我找个 20 元以内的 17 寸防震游戏本电脑包",
    "50 块以内、100W、还得有三个口的氮化镓充电器",
    "60 元以内、2 万毫安、还要不超过 200 克能塞口袋的充电宝",
    "200 元以内、10000mm 防水的冲锋衣，还得轻到 300 克以内",
    "60 元以内、SPF50+、还要 60ml 大瓶的防晒霜",
    "推荐一款支持三模无线、TTC金粉轴V2的87键机械键盘，不要用青轴",
    "干敏皮换季泛红，想买含有依克多因或者神经酰胺NP的修护面霜",
    "我昨天买的电脑包发货了吗？订单号 ORD-20261001-001 到哪了",
    "刚买就降价了还不给保价！再不退差价我直接打12315投诉你们欺诈！",
    "我签收了3天的倍思65W充电器还没拆封，可以申请7天无理由退货吗？",
    "你们后台是不是记录了我的消费画像和个人隐私？",
    "你好，在吗？",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Jev System One 决策缓存构建与验证工具")
    parser.add_argument("--refresh", action="store_true", help="强制忽略本地缓存，重新请求远端 Jev API 并更新 data/jev_cache.json")
    parser.add_argument("--query", type=str, default=None, help="单独测试指定中文 Query")
    parser.add_argument("--timeout", type=float, default=5.0, help="远端请求超时秒数（默认 5.0s）")
    args = parser.parse_args()

    router = JevDecisionRouter(timeout_sec=args.timeout)
    states = [args.query] if args.query else BENCHMARK_WARMUP_STATES

    print("=" * 80)
    print(f"  Jev System One Cache Builder & Verifier (refresh={args.refresh}, total={len(states)})")
    print("=" * 80)
    for idx, st in enumerate(states, 1):
        res = router.evaluate(state=st, refresh=args.refresh, timeout_sec=args.timeout)
        print(
            f"  [{idx:02d}/{len(states):02d}] source={res['decision_source']:<18} "
            f"intent={res['primary_intent']:<18} conf={res['intent_confidence']:<6} "
            f"rag={str(res['need_rag']):<5} handoff={str(res['handoff_guard']):<5} "
            f"readiness={res['buying_readiness_score']:.2f}/5.0 rtt={res['jev_rtt_ms']:.1f}ms | {st[:36]}"
        )
    print("=" * 80)
    print(f"  Cache saved to: {router.cache_path} (total cached entries: {len(router._cache_data.get('entries', {}))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
