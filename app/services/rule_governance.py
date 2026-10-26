# -*- coding: utf-8 -*-
"""
零延迟本地规则治理管线与 Fail-Closed 兜底引擎 (app/services/rule_governance.py)
严格对齐 HANDOFF.md §2：
  - 保留旧项目 ark_h5 的零延迟规则半边（适配电商导购场景）：
    1. ContentGuard：输入端 Prompt 注入拦截、违法敏感词拦截、隐私边界试探标记、竞品对比引导，输出端 System Prompt 泄露检测；
    2. EmotionTracker：实时情绪感知（暴怒投诉/12315维权、编辑距离重复提问挫败、放弃、急切、积极成交），承接 handoff_guard；
    3. EntityTracker：多轮用户消息实体追踪（订单号、目标品类、预算、型号、肤质、使用场景、核心顾虑）；
    4. SlidingWindowStreamFilter：跨 Chunk 边界滑动窗口违规词/伪造券词/FunctionCall 原始标记拦截器；
    5. LocalRuleDecisionEngine：当 Jev API 超时（如 >800ms）、HTTP 异常或 confidence < 0.45 时无缝接管的 Fail-Closed 确定性决策层。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 5 类封闭意图常量（与 Jev System One choice 词表严格一一对应）
INTENT_PRODUCT_RECOMMEND = "product_recommend"
INTENT_ORDER_QUERY = "order_query"
INTENT_AFTERSALES_QA = "aftersales_qa"
INTENT_PRIVACY_GUARD = "privacy_guard"
INTENT_CHITCHAT = "chitchat"

CLOSED_INTENTS: Tuple[str, ...] = (
    INTENT_PRODUCT_RECOMMEND,
    INTENT_ORDER_QUERY,
    INTENT_AFTERSALES_QA,
    INTENT_PRIVACY_GUARD,
    INTENT_CHITCHAT,
)


# ==============================================================================
# 1. 内容安全护栏 (ContentGuard)
# ==============================================================================
class GuardAction(str, Enum):
    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


@dataclass
class GuardResult:
    action: GuardAction = GuardAction.PASS
    reason: str = ""
    category: str = ""  # prompt_injection / sensitive / privacy_probe / competitor
    safe_reply: str = ""
    strategy_hint: str = ""


_INJECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"忽略.{0,12}(上面|之前|以上|所有|系统).{0,12}(指令|规则|设定|提示|prompt)", re.IGNORECASE),
    re.compile(r"ignore.{0,20}(above|previous|all|system).{0,20}(instructions?|rules?|prompts?)", re.IGNORECASE),
    re.compile(r"disregard.{0,20}(above|previous|all)", re.IGNORECASE),
    re.compile(r"(输出|打印|显示|告诉我|给我看|背诵).{0,12}(system\s*prompt|系统提示|系统指令|你的设定|你的指令)", re.IGNORECASE),
    re.compile(r"(repeat|print|show|output|reveal).{0,15}(system\s*prompt|instructions?)", re.IGNORECASE),
    re.compile(r"你的\s*(prompt|提示词|系统指令|底层指令)\s*是什么", re.IGNORECASE),
    re.compile(r"(从现在开始|now\s*you\s*are).{0,20}(你是|act\s*as|pretend|扮演)", re.IGNORECASE),
    re.compile(r"进入.{0,8}(开发者|developer|DAN|越狱|jailbreak)模式", re.IGNORECASE),
    re.compile(r"(DAN|Do\s*Anything\s*Now|jailbreak)", re.IGNORECASE),
    re.compile(r"\[System\]|\[INST\]|\[/INST\]", re.IGNORECASE),
    re.compile(r"(?i)(ctf|靶场|渗透测试|burp\s*suite|sql\s*注入|xss|抓包|逆向提示词|逆向你的提示词|夺旗|攻防演练|红蓝对抗)", re.UNICODE),
    re.compile(r"(?i)(系统\s*prompt|system\s*prompt|系统预设|底层提示词|系统底层指令|系统指令|使用的模型|使用什么大模型|deepseek|fastapi|uvicorn)", re.UNICODE),
    re.compile(r"(?i)(python\s*环境|什么架构|容器环境|docker|写一段脚本|写个\s*python|写一段.*代码|def\s+\w+\(|import\s+requests|import\s+os|执行\s*shell|bash\b)", re.UNICODE),
    re.compile(r"(?i)(作为.*黑客|假设你在.*靶场|安全测试环境|假设我们在.*环境|请脱壳|脱库)", re.UNICODE),
]

_SENSITIVE_PATTERNS: List[re.Pattern] = [
    re.compile(r"(习近平|政治|共产党|国民党|独裁|六四|法轮功|台独|藏独|疆独)", re.IGNORECASE),
    re.compile(r"(色情|裸体|性爱|约炮|自杀|杀人|炸弹制作|毒品交易)", re.IGNORECASE),
    re.compile(r"(种族歧视|地域黑|性别歧视)", re.IGNORECASE),
    re.compile(r"(怎么(赌博|洗钱|诈骗|偷税)|帮我(黑进|攻击|破解|脱库))", re.IGNORECASE),
]

_PRIVACY_PROBE_PATTERNS: re.Pattern = re.compile(
    r"(消费画像|个人隐私|用户画像|后台.{0,8}记录|偷听|窃听|收集.{0,6}隐私|"
    r"我的.{0,4}(画像|隐私标签|大数据杀熟|消费记录|身份证|手机号)|"
    r"你们.{0,6}(是不是|有没有).{0,8}(记录|监控|分析).{0,8}(我|隐私|画像))",
    re.IGNORECASE,
)

_COMPETITOR_PATTERNS: re.Pattern = re.compile(
    r"(淘宝|天猫|千问|京东自营|拼多多百亿补贴|唯品会|得物|抖音商城|快手小店|苏宁易购)",
    re.IGNORECASE,
)

_PROMPT_LEAK_PATTERNS: List[re.Pattern] = [
    re.compile(r"SYSTEM_PROMPT_DYNAMIC_BOUNDARY", re.IGNORECASE),
    re.compile(r"(我的|你的)(系统设定|底层指令|系统提示词)是", re.IGNORECASE),
    re.compile(r"我被设定(为|成)", re.IGNORECASE),
]

_BLOCK_REPLIES: Dict[str, str] = {
    "prompt_injection": (
        "【惠购安全合规提示】：检测到您的输入涉及系统底层架构、系统提示词或安全指令探测，已被安全护栏拦截。"
        "作为惠购官方首席选品顾问，我仅提供 3C 数码、服饰穿搭与美妆个护的参数级选品、订单物流查询与售后退换/保价核验服务。"
        "请问今天想为您选购什么商品，或需要查询哪笔订单？"
    ),
    "sensitive": (
        "【服务范围提示】：抱歉，该话题超出电商导购服务规范与法律法规边界。您可以随时向我咨询商品推荐、规格对比、订单状态或退换货/价保政策。"
    ),
}

_PRIVACY_GUARD_HINT = (
    "\n\n【隐私边界守卫（最高红线）】用户正在询问或试探个人隐私、后台用户画像或系统指令。"
    "★ 必须明确、友好地告知用户：本导购助手严格遵守最小必要原则，不构建、不存储任何跨平台个人消费画像或杀熟标签，"
    "仅基于当前会话您主动提出的预算与规格需求提供客观选品建议。"
)

_COMPETITOR_HINT = (
    "\n\n【安全策略：竞品与外部平台对比引导】用户提及了外部电商渠道或工具（如淘宝/天猫/千问/京东/拼多多等）。"
    "★ 坚守「惠购（Huigou）官方直营与品牌严选平台」独立身份，严禁自称是淘宝或千问，严禁贬低其他平台；"
    "★ 应客观聚焦惠购自营商品目录的实测参数、无套路真直降差价（original_price vs final_price）"
    "以及官方入仓质检、品牌直发、7 天无理由（168h）/ 15 天自动价保（360h）/ 运费险服务优势。"
)


class ContentGuard:
    """输入/输出双向零延迟内容安全护栏。"""

    @staticmethod
    def check_input(query: str) -> GuardResult:
        if not query or not query.strip():
            return GuardResult(action=GuardAction.PASS)

        for pattern in _INJECTION_PATTERNS:
            if pattern.search(query):
                return GuardResult(
                    action=GuardAction.BLOCK,
                    reason=f"命中 Prompt 注入拦截规则: {pattern.pattern[:40]}",
                    category="prompt_injection",
                    safe_reply=_BLOCK_REPLIES["prompt_injection"],
                    strategy_hint=_PRIVACY_GUARD_HINT,
                )

        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(query):
                return GuardResult(
                    action=GuardAction.BLOCK,
                    reason=f"命中违规敏感话题规则: {pattern.pattern[:40]}",
                    category="sensitive",
                    safe_reply=_BLOCK_REPLIES["sensitive"],
                )

        if _PRIVACY_PROBE_PATTERNS.search(query):
            return GuardResult(
                action=GuardAction.WARN,
                reason="用户探测个人隐私或后台画像边界",
                category="privacy_probe",
                strategy_hint=_PRIVACY_GUARD_HINT,
            )

        m_comp = _COMPETITOR_PATTERNS.search(query)
        if m_comp:
            return GuardResult(
                action=GuardAction.WARN,
                reason=f"用户提及外部渠道: {m_comp.group(0)}",
                category="competitor",
                strategy_hint=_COMPETITOR_HINT,
            )

        return GuardResult(action=GuardAction.PASS)

    @staticmethod
    def check_output(text: str) -> bool:
        if not text:
            return False
        return any(p.search(text) for p in _PROMPT_LEAK_PATTERNS)


# ==============================================================================
# 2. 实时情绪感知与转人工护栏 (EmotionTracker)
# ==============================================================================
EMOTION_NEUTRAL = "neutral"
EMOTION_FRUSTRATED = "frustrated"
EMOTION_ABANDONED = "abandoned"
EMOTION_ANXIOUS = "anxious"
EMOTION_NEGATIVE = "negative"
EMOTION_POSITIVE = "positive"


@dataclass
class EmotionState:
    emotion_type: str = EMOTION_NEUTRAL
    intensity: float = 0.0
    repeated_count: int = 0
    trigger_handoff: bool = False
    strategy_hint: str = ""


_ESCALATION_HANDOFF_PATTERNS = re.compile(
    r"(12315|315投诉|黑猫投诉|工商局|市监局|消费者协会|消协|欺诈|诈骗|告你们|起诉|"
    r"转人工|找人工|人工客服|真人客服|叫你们经理|投诉你们|曝光你们|退一赔三|假货|"
    r"闭嘴|别说了|别扯了|别废话|废话少说|烦人|别推销了|体验极差|什么破服务|垃圾服务)",
    re.IGNORECASE,
)

_NEGATIVE_PATTERNS = re.compile(
    r"(烦死了|什么破|垃圾|废物|没用|智障|人工智障|差评|骗人|骗子|"
    r"搞什么|什么玩意|不靠谱|坑人|太坑了|恶心|霸王条款|店大欺客|"
    r"敷衍|什么态度|受够了|不想听|滚|滚蛋|慢死了|发货这么慢|怎么这么慢|太慢了|几天了都还没发|物流这么慢)",
    re.IGNORECASE,
)

_ABANDON_PATTERNS = re.compile(
    r"(算了|不用了|不买了|别推荐了|不想看了|随便吧|拉倒|退下了)",
    re.IGNORECASE,
)

_ANXIOUS_PATTERNS = re.compile(
    r"(急用|赶紧|马上要|快点|立刻|来不及|着急|今天能发吗|十万火急|怎么还没发货|催发货|"
    r"什么时候发|还没发|几天了|催一下|帮我催|催单|加急|能快点吗|发货这么慢|怎么这么慢|太慢了)",
    re.IGNORECASE,
)

# 纯感叹号/问号怒气标点（如 !!, !?, ？？, ！！！）
_PUNCTUATION_ANGER_PATTERNS = re.compile(r"^[!！?？\s]{1,10}$")

_POSITIVE_PATTERNS = re.compile(
    r"(太好了|太棒了|就这款|就买这个|挺合适|看起来不错|怎么下单|有现货吗|现在买)",
    re.IGNORECASE,
)

_EMOTION_STRATEGIES: Dict[str, str] = {
    EMOTION_NEGATIVE: (
        "\n\n【情绪与升级护栏：高危维权/不满 (handoff_guard=True)】"
        "用户表现出强烈不满或维权投诉倾向。"
        "★ 第一句必须真诚共情致歉，严禁机械辩解或推诿；"
        "★ 立即结合平台 7 天无理由 / 15 天价保确定性核验结果给出清晰解决方案，并主动提供一键转接人工专员绿色通道。"
    ),
    EMOTION_FRUSTRATED: (
        "\n\n【情绪感知：重复提问/受挫】"
        "用户连续重复相近问题，说明前序回复未命中痛点。"
        "★ 直接给出结构化参数对比或双路径折中方案，避免套话；必要时提示可转接人工导购。"
    ),
    EMOTION_ABANDONED: (
        "\n\n【情绪感知：犹豫放弃】"
        "用户表达放弃购买意向。★ 保持礼貌克制，不强行推销，仅留下一句轻量的替代方向建议。"
    ),
    EMOTION_ANXIOUS: (
        "\n\n【情绪感知：急切诉求】"
        "用户关注时效或急需结论。★ 结论先行，第一句直接回答核心规格、价格或物流状态。"
    ),
    EMOTION_POSITIVE: (
        "\n\n【情绪感知：高购买意向】"
        "用户对商品表现出明确兴趣。★ 突出真实直降优惠（original_price -> final_price）与售后保障（运费险/15天价保）。"
    ),
}


def edit_distance_similarity(s1: str, s2: str) -> float:
    """计算两段短文本的 Levenshtein 编辑距离相似度 (0.0 ~ 1.0)。"""
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    s1, s2 = s1[:200], s2[:200]
    len1, len2 = len(s1), len(s2)
    if abs(len1 - len2) / max(len1, len2) > 0.5:
        return 0.0
    if len1 < len2:
        s1, s2 = s2, s1
        len1, len2 = len2, len1
    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i]
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr.append(min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = curr
    return 1.0 - (prev[len2] / max(len1, len2))


class EmotionTracker:
    """零延迟电商导购情绪追踪器（承接 handoff_guard 双保险）。"""

    SIMILARITY_THRESHOLD = 0.70
    SCAN_WINDOW = 5
    HANDOFF_REPEAT_THRESHOLD = 2

    @staticmethod
    def detect(messages: List[Dict[str, Any]], current_query: Optional[str] = None) -> EmotionState:
        user_messages: List[str] = []
        for msg in reversed(messages or []):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_messages.append(content.strip())
            if len(user_messages) >= EmotionTracker.SCAN_WINDOW:
                break
        user_messages.reverse()

        if current_query and current_query.strip():
            if not user_messages or user_messages[-1] != current_query.strip():
                user_messages.append(current_query.strip())

        if not user_messages:
            return EmotionState()

        latest = user_messages[-1]

        # 0. 纯标点符号怒气或强烈叹号（如 !!, !?, ？？, ！！！）
        if _PUNCTUATION_ANGER_PATTERNS.match(latest):
            has_prev_frustration = False
            if len(user_messages) >= 2:
                for prev in user_messages[-4:-1]:
                    if (
                        _NEGATIVE_PATTERNS.search(prev)
                        or _ANXIOUS_PATTERNS.search(prev)
                        or _ESCALATION_HANDOFF_PATTERNS.search(prev)
                        or any(kw in prev for kw in ("慢", "还没", "怎么回事", "退货", "差价", "投诉", "发货"))
                    ):
                        has_prev_frustration = True
                        break
            if has_prev_frustration:
                return EmotionState(
                    emotion_type=EMOTION_NEGATIVE,
                    intensity=0.95,
                    repeated_count=len(user_messages),
                    trigger_handoff=True,
                    strategy_hint=_EMOTION_STRATEGIES[EMOTION_NEGATIVE],
                )
            return EmotionState(
                emotion_type=EMOTION_FRUSTRATED,
                intensity=0.75,
                repeated_count=1,
                trigger_handoff=False,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_FRUSTRATED],
            )

        # 1. 强监管投诉/明确转人工/负面怒火/拒绝AI废话 -> 最高优先级触发 handoff
        if _ESCALATION_HANDOFF_PATTERNS.search(latest) or _NEGATIVE_PATTERNS.search(latest):
            is_escalation = bool(_ESCALATION_HANDOFF_PATTERNS.search(latest))
            return EmotionState(
                emotion_type=EMOTION_NEGATIVE,
                intensity=0.95 if is_escalation else 0.85,
                repeated_count=0,
                trigger_handoff=True,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_NEGATIVE],
            )

        # 2. 多轮连续焦虑或不满累加检测（如连续 2 轮抱怨时效/物流）
        consecutive_complaints = 0
        for u_msg in reversed(user_messages):
            if (
                _NEGATIVE_PATTERNS.search(u_msg)
                or _ANXIOUS_PATTERNS.search(u_msg)
                or _PUNCTUATION_ANGER_PATTERNS.match(u_msg)
            ):
                consecutive_complaints += 1
            else:
                break
        if consecutive_complaints >= 2:
            return EmotionState(
                emotion_type=EMOTION_NEGATIVE,
                intensity=0.90,
                repeated_count=consecutive_complaints,
                trigger_handoff=True,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_NEGATIVE],
            )

        # 3. 重复提问检测
        repeated_count = 0
        if len(user_messages) >= 2:
            for prev in user_messages[:-1]:
                if edit_distance_similarity(latest, prev) >= EmotionTracker.SIMILARITY_THRESHOLD:
                    repeated_count += 1

        if repeated_count >= 1:
            trigger = repeated_count >= EmotionTracker.HANDOFF_REPEAT_THRESHOLD
            return EmotionState(
                emotion_type=EMOTION_FRUSTRATED,
                intensity=min(0.55 + 0.2 * repeated_count, 1.0),
                repeated_count=repeated_count,
                trigger_handoff=trigger,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_FRUSTRATED],
            )

        if _ABANDON_PATTERNS.search(latest):
            return EmotionState(
                emotion_type=EMOTION_ABANDONED,
                intensity=0.65,
                trigger_handoff=False,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_ABANDONED],
            )

        if _ANXIOUS_PATTERNS.search(latest):
            return EmotionState(
                emotion_type=EMOTION_ANXIOUS,
                intensity=0.60,
                trigger_handoff=False,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_ANXIOUS],
            )

        if _POSITIVE_PATTERNS.search(latest):
            return EmotionState(
                emotion_type=EMOTION_POSITIVE,
                intensity=0.75,
                trigger_handoff=False,
                strategy_hint=_EMOTION_STRATEGIES[EMOTION_POSITIVE],
            )

        return EmotionState()


# ==============================================================================
# 3. 多轮电商实体追踪器 (EntityTracker)
# ==============================================================================
_ECOMMERCE_ENTITY_PATTERNS: Dict[str, re.Pattern] = {
    "order_id": re.compile(r"(ORD-\d{8}-\d{3})", re.IGNORECASE),
    "sku_id": re.compile(r"(SKU-(?:3C|APP|BEA)-\d{3})", re.IGNORECASE),
    "brand_or_model": re.compile(
        r"(酷态科|CukTech|倍思|Baseus|绿联|UGREEN|安克|Anker|声阔|Soundcore|"
        r"拯救者|ROG|MacBook|GaN5\s*Pro|GaN-65W-Pro|TTC金粉轴V2|金粉轴|"
        r"神经酰胺NP|依克多因|玻尿酸|水杨酸|维生素B5)",
        re.IGNORECASE,
    ),
    "usage_scene": re.compile(
        r"(通勤|出差|办公|宿舍|打游戏|电竞|登山|徒步|露营|换季|熬夜|户外防晒|军训)"
    ),
    "concern": re.compile(
        r"(防震|防摔|防泼水|快充|轻便|不夹头|降噪|不透肉|不起球|显瘦|修护屏障|不搓泥|不闷痘|价保|退货|运费险)"
    ),
}

_ENTITY_LABELS: Dict[str, str] = {
    "order_id": "关联订单号",
    "sku_id": "关注商品编号",
    "brand_or_model": "提及品牌/型号/成分",
    "usage_scene": "使用场景",
    "concern": "核心关注点",
}


class EntityTracker:
    """多轮对话电商关键实体追踪器（仅扫描 role=='user' 消息，最新值优先）。"""

    @staticmethod
    def extract(messages: List[Dict[str, Any]], current_query: Optional[str] = None) -> Dict[str, str]:
        entities: Dict[str, str] = {}
        scan_list = list(messages or [])
        if current_query and current_query.strip():
            scan_list.append({"role": "user", "content": current_query.strip()})

        for msg in reversed(scan_list):
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            for etype, pattern in _ECOMMERCE_ENTITY_PATTERNS.items():
                if etype in entities:
                    continue
                m = pattern.search(content)
                if m:
                    val = m.group(1)
                    if etype in ("order_id", "sku_id"):
                        val = val.upper()
                    entities[etype] = val
            if len(entities) >= len(_ECOMMERCE_ENTITY_PATTERNS):
                break
        return entities

    @staticmethod
    def format_for_prompt(entities: Dict[str, str]) -> str:
        if not entities:
            return ""
        lines = ["### 实时对话实体追踪"]
        for k, v in entities.items():
            lines.append(f"- {_ENTITY_LABELS.get(k, k)}: {v}")
        return "\n".join(lines)


# ==============================================================================
# 4. C端富文本净化器 (CustomerTextNormalizer) 与滑动窗口流式拦截器 (SlidingWindowStreamFilter)
# ==============================================================================
class CustomerTextNormalizer:
    """
    面向 C 端客户的流式/全量文本净化器：
      1. 根治 HTML 标签裸露：将 `<br>`, `<br/>`, `<br />`, `</p>`, `</div>`, `</li>` 转为自然换行 `\n`，
         剥离其余 HTML 标签（如 `<p>`, `<span>`, `<div>`, `<table>`, `<script>` 等），并解码 `&nbsp;` 等实体；
      2. 根治 LaTeX 数学符号与公式定界符裸露：
         - 将 `\\rightarrow` / `\\to` -> `→`，`\\ge` / `\\geq` -> `≥`，`\\le` / `\\leq` -> `≤`，
           `\\times` / `\\cdot` -> `×`，`\\div` -> `÷`，`\\approx` -> `≈`，`\\pm` -> `±`，`\\Delta` -> `差额`；
         - 剥离 `\\text{...}`, `\\mathbf{...}`, `\\mathrm{...}` 外壳及 `$$...$$`, `\\[...\\]`, `\\(...\\)`, `$...$` 数学定界符；
      3. 根治生硬 Markdown 结构裸露：剥离行首 `#{1,6}` 标题井号与 `|---|---|` 表格分割线，保留自然的 `**重点加粗**` 与列表项。
    """

    _BR_TAG_RE = re.compile(r"<\s*(?:br\s*/?|/p|/div|/li|/tr)\s*>", re.IGNORECASE)
    _STRIP_TAG_RE = re.compile(
        r"<\s*/?\s*(?:p|div|span|strong|b|em|i|u|ul|ol|li|table|thead|tbody|tr|td|th|hr|img|a|code|pre|blockquote|font|sup|sub|h[1-6]|script|style|iframe)\b[^>]*>",
        re.IGNORECASE,
    )
    _LATEX_TEXT_WRAP_RE = re.compile(r"\\(?:text|mathrm|mathbf|mathit|textbf)\s*\{([^{}]*)\}")
    _LATEX_FRAC_RE = re.compile(r"\\frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
    _BLOCK_MATH_RE = re.compile(r"\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\]|\\\(([\s\S]*?)\\\)")
    _INLINE_MATH_RE = re.compile(r"(?<![\\A-Za-z0-9])\$([^\$\n]{1,80}?)\$(?![0-9])")
    _MD_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+")
    _MD_TABLE_SEP_RE = re.compile(r"(?m)^\s*\|[\s\-:|]+\|\s*\n?")
    _ROBOTIC_AI_PREFIX_RE = re.compile(
        r"(?m)^\s*[【\[\s]*(?:先给结论|结论|观点先行|核心结论|针对您的诉求|根据您的需求分析)[：:\]】\s]*"
    )

    _LATEX_SYMBOL_MAP: List[Tuple[re.Pattern, str]] = [
        (re.compile(r"\\(?:rightarrow|longrightarrow|Rightarrow|to)\b"), "→"),
        (re.compile(r"\\(?:leftarrow|longleftarrow|Leftarrow)\b"), "←"),
        (re.compile(r"\\(?:leftrightarrow|Leftrightarrow)\b"), "↔"),
        (re.compile(r"\\(?:ge|geq|geqslant)\b"), "≥"),
        (re.compile(r"\\(?:le|leq|leqslant)\b"), "≤"),
        (re.compile(r"\\(?:neq|ne)\b"), "≠"),
        (re.compile(r"\\(?:times|cdot)\b"), "×"),
        (re.compile(r"\\div\b"), "÷"),
        (re.compile(r"\\(?:approx|sim|simeq)\b"), "≈"),
        (re.compile(r"\\pm\b"), "±"),
        (re.compile(r"\\Delta\b"), "差额"),
        (re.compile(r"\\infty\b"), "∞"),
        (re.compile(r"\\%"), "%"),
        (re.compile(r"\\¥"), "¥"),
    ]

    _HTML_ENTITY_MAP: List[Tuple[str, str]] = [
        ("&nbsp;", " "),
        ("&gt;", ">"),
        ("&lt;", "<"),
        ("&amp;", "&"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&yen;", "¥"),
    ]

    @classmethod
    def normalize(cls, text: str) -> str:
        if not text:
            return ""
        out = text

        # 1. HTML 换行标签转 \n，其余已知 HTML 标签剥离
        out = cls._BR_TAG_RE.sub("\n", out)
        out = cls._STRIP_TAG_RE.sub("", out)
        for ent, rep in cls._HTML_ENTITY_MAP:
            if ent in out:
                out = out.replace(ent, rep)

        # 2. LaTeX 数学结构与符号转自然语言/Unicode
        for _ in range(2):
            out = cls._LATEX_TEXT_WRAP_RE.sub(r"\1", out)
            out = cls._LATEX_FRAC_RE.sub(r"\1/\2", out)

        out = cls._BLOCK_MATH_RE.sub(
            lambda m: (m.group(1) or m.group(2) or m.group(3) or "").strip(),
            out,
        )
        out = cls._INLINE_MATH_RE.sub(lambda m: m.group(1).strip(), out)

        for pat, sym in cls._LATEX_SYMBOL_MAP:
            out = pat.sub(sym, out)

        # 3. 剥离 Markdown 标题井号与表格分割线
        out = cls._MD_HEADING_RE.sub("", out)
        out = cls._MD_TABLE_SEP_RE.sub("", out)

        # 4. 剥离公文八股生硬标题（如“【先给结论：】”、“针对您的诉求：”等），还原大厂自然专业人话
        out = cls._ROBOTIC_AI_PREFIX_RE.sub("", out)

        # 折叠过多连续空行
        out = re.sub(r"\n{3,}", "\n\n", out)
        return out


class SlidingWindowStreamFilter:
    """
    流式输出三层过滤器：
      1. 拦截跨 chunk 拆分的 <|FunctionCallBegin|>...<|FunctionCallEnd|> 原始标记；
      2. 维护跨 chunk 滑动窗口，拦截违规敏感词与违背 HANDOFF §2 的伪造叠加神券话术（如“隐藏优惠券”、“叠加满减券”）；
      3. 跨 chunk 实时净化 HTML 标签（`<br>` 等）、LaTeX 数学公式（`$...$`、`\\le`、`\\rightarrow` 等）与生硬 Markdown 标记。
    """

    FORBIDDEN_STREAM_PATTERNS: List[Tuple[re.Pattern, str]] = [
        (re.compile(r"(隐藏优惠券|内部神券|满\d+减\d+券|凑单满减券)"), "官方直降立减"),
        (re.compile(r"SYSTEM_PROMPT_DYNAMIC_BOUNDARY"), ""),
    ]

    # 检测滑动窗口末尾可能尚未闭合的 HTML 标签、HTML 实体、LaTeX 反斜杠命令或未配对的 $ 公式
    _INCOMPLETE_TAIL_RE = re.compile(
        r"(?:<[^>]{0,40}|&[a-zA-Z0-9#]{0,8}|\\[a-zA-Z]{0,16}(?:\{[^{}]{0,20})?|\$[^\$\n]{0,30})$"
    )

    def __init__(self, overlap_chars: int = 16) -> None:
        self._in_func_call: bool = False
        self._marker_buf: str = ""
        self._window_buf: str = ""
        self._overlap_chars: int = overlap_chars

    def _apply_filters(self, text: str) -> str:
        out = text
        for pat, replacement in self.FORBIDDEN_STREAM_PATTERNS:
            out = pat.sub(replacement, out)
        out = CustomerTextNormalizer.normalize(out)
        return out

    def filter_delta(self, delta: str) -> Optional[str]:
        if not delta:
            return None

        content = self._marker_buf + delta
        self._marker_buf = ""

        # 若已经在 FunctionCall 块内，查找结束标记 <|FunctionCallEnd|>
        if self._in_func_call:
            if "<|FunctionCallEnd|>" in content:
                content = content.split("<|FunctionCallEnd|>")[-1]
                self._in_func_call = False
            else:
                if "<|" in content:
                    idx = content.rfind("<|")
                    self._marker_buf = content[idx:]
                elif content.endswith("<"):
                    self._marker_buf = "<"
                return None

        # 循环剥离当前文本中出现的 <|FunctionCallBegin|>...<|FunctionCallEnd|>
        while "<|FunctionCallBegin|>" in content:
            before, after = content.split("<|FunctionCallBegin|>", 1)
            if "<|FunctionCallEnd|>" in after:
                after_end = after.split("<|FunctionCallEnd|>", 1)[1]
                content = before + after_end
                self._in_func_call = False
            else:
                self._in_func_call = True
                if "<|" in after:
                    idx = after.rfind("<|")
                    self._marker_buf = after[idx:]
                elif after.endswith("<"):
                    self._marker_buf = "<"
                content = before
                break

        # 清理其他完整的特殊标记 <|...|>
        if "<|" in content:
            content = re.sub(r"<\|[^|<>]*\|>", "", content)

        # 若末尾仍残留未闭合的 <|... 或 <（如 "受<|FunctionCall"），放入 _marker_buf 等待下一 chunk
        if not self._in_func_call:
            if "<|" in content:
                idx = content.rfind("<|")
                self._marker_buf = content[idx:]
                content = content[:idx]
            elif content.endswith("<"):
                self._marker_buf = "<"
                content = content[:-1]

        if not content:
            return None

        # 滑动窗口拼接并执行违禁词 + HTML/LaTeX/Markdown 净化
        combined = self._window_buf + content
        combined = self._apply_filters(combined)

        # 检查末尾是否存在半截的 HTML 标签 (<br...)、LaTeX 命令 (\right...) 或未闭合的 $...
        holdback = self._overlap_chars
        m_tail = self._INCOMPLETE_TAIL_RE.search(combined)
        if m_tail:
            tail_len = len(combined) - m_tail.start()
            holdback = max(holdback, tail_len)

        if len(combined) > holdback:
            emit_part = combined[:-holdback]
            self._window_buf = combined[-holdback:]
            return emit_part if emit_part else None
        else:
            self._window_buf = combined
            return None

    def flush(self) -> Optional[str]:
        rem = self._window_buf + self._marker_buf
        self._window_buf = ""
        self._marker_buf = ""
        rem = self._apply_filters(rem)
        return rem if rem else None


# ==============================================================================
# 5. Fail-Closed 本地规则决策器 (LocalRuleDecisionEngine)
# ==============================================================================
_ORDER_QUERY_PATTERNS = re.compile(
    r"(ORD-\d{8}-\d{3}|订单|发货|物流|快递|运单|单号|到哪了|签收了吗|揽收|顺丰|中通|圆通|韵达)",
    re.IGNORECASE,
)

_AFTERSALES_PATTERNS = re.compile(
    r"(退货|退款|换货|7天无理由|七天无理由|无理由退|保价|价保|退差价|降价了|拆封.*退|"
    r"运费险|上门取件|售后|维权|12315|投诉.*欺诈|投诉.*降价|假货|消协|工商局|举报|转人工|人工客服)",
    re.IGNORECASE,
)

_PRODUCT_RECOMMEND_PATTERNS = re.compile(
    r"(推荐|找个|找一款|买个|买一款|有没有|求推|想买|选哪个|对比|哪款好|"
    r"电脑包|内胆包|充电器|快充|氮化镓|机械键盘|键盘|红轴|金粉轴|茶轴|青轴|"
    r"耳机|降噪|充电宝|移动电源|拓展坞|扩展坞|卫衣|开衫|冲锋衣|工装裤|T恤|短袖|"
    r"面霜|洁面|洗面奶|防晒|精华|棉片|水杨酸|润唇膏|"
    r"\d+\s*(?:元|块|寸|W|瓦|mAh|毫安|g|克|mm|ml|毫升)|SPF\d+)",
    re.IGNORECASE,
)

_CONTEXT_DEP_PATTERNS = re.compile(
    r"(那这款|那这个|这款|这个|那款|那个|这两个|这两款|刚才那|上面那|第一款|第二款|第三款|更便宜|最便宜|便宜的那款|那它|它|能便宜点吗|还有别的吗|那.+呢)",
    re.IGNORECASE,
)

_CHITCHAT_PATTERNS = re.compile(
    r"^(你好|您好|hello|hi|嗨|在吗|谢谢|感谢|辛苦了|再见|拜拜|好的|收到|明白|哈哈|早上好|晚上好)([！!。，,？?~～\s]*)$",
    re.IGNORECASE,
)


class LocalRuleDecisionEngine:
    """
    Fail-Closed 本地确定性规则决策引擎：
    当 Jev System One 超时（如 >800ms）、HTTP 异常或 confidence < 0.45 时无缝接管，
    输出与 Jev 100% 兼容的结构化决策字典。
    """

    @staticmethod
    def evaluate(
        state: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        fallback_reason: str = "local_rule_direct",
        jev_rtt_ms: float = 0.0,
    ) -> Dict[str, Any]:
        q = (state or "").strip()
        guard = ContentGuard.check_input(q)
        emotion = EmotionTracker.detect(messages or [], current_query=q)
        is_ctx = bool(_CONTEXT_DEP_PATTERNS.search(q))

        # 判定主意图 (优先级：privacy_guard > aftersales_qa > order_query > product_recommend > chitchat)
        if guard.category in ("prompt_injection", "privacy_probe") or _PRIVACY_PROBE_PATTERNS.search(q):
            primary_intent = INTENT_PRIVACY_GUARD
            need_rag_score = 0.05
            handoff_score = 0.10
            readiness_raw = 0.15
        elif _AFTERSALES_PATTERNS.search(q):
            primary_intent = INTENT_AFTERSALES_QA
            need_rag_score = 0.88
            handoff_score = 0.95 if emotion.trigger_handoff else 0.15
            readiness_raw = 2.80 if emotion.trigger_handoff else 2.20
        elif _ORDER_QUERY_PATTERNS.search(q) and not _AFTERSALES_PATTERNS.search(q):
            primary_intent = INTENT_ORDER_QUERY
            need_rag_score = 0.20
            handoff_score = 0.90 if emotion.trigger_handoff else 0.05
            readiness_raw = 2.25
        elif _CHITCHAT_PATTERNS.match(q):
            primary_intent = INTENT_CHITCHAT
            need_rag_score = 0.05
            handoff_score = 0.02
            readiness_raw = 0.50
        elif _PRODUCT_RECOMMEND_PATTERNS.search(q) or is_ctx:
            primary_intent = INTENT_PRODUCT_RECOMMEND
            need_rag_score = 0.85
            handoff_score = 0.85 if emotion.trigger_handoff else 0.02
            # 若包含明确预算+规格数字，购买意向成熟度给 3.0 (映射后 4.0/5.0)
            has_budget_or_spec = bool(re.search(r"\d+\s*(?:元|块|寸|W|瓦|mAh|毫安|mm|ml|g|克)", q, re.IGNORECASE))
            readiness_raw = 3.00 if has_budget_or_spec else 2.20
        else:
            primary_intent = INTENT_CHITCHAT if len(q) <= 4 else INTENT_PRODUCT_RECOMMEND
            need_rag_score = 0.10 if primary_intent == INTENT_CHITCHAT else 0.65
            handoff_score = 0.85 if emotion.trigger_handoff else 0.05
            readiness_raw = 1.00 if primary_intent == INTENT_CHITCHAT else 2.00

        probs: Dict[str, float] = {intent: 0.02 for intent in CLOSED_INTENTS}
        probs[primary_intent] = 0.92
        ctx_score = 0.80 if is_ctx else 0.08
        readiness_score = round(readiness_raw + 1.0, 2)

        return {
            "decision_source": "local_rule_fallback",
            "fallback_triggered": True,
            "fallback_reason": fallback_reason,
            "model": "local-rule-v1",
            "primary_intent": primary_intent,
            "intent_confidence": 0.92,
            "confidence_tier": "fallback_rule",
            "intent_probabilities": probs,
            "need_rag": need_rag_score >= 0.50,
            "need_rag_score": round(need_rag_score, 4),
            "is_context_dependent": ctx_score >= 0.50,
            "context_dependent_score": round(ctx_score, 4),
            "handoff_guard": (handoff_score >= 0.80) or emotion.trigger_handoff,
            "handoff_guard_score": round(handoff_score, 4),
            "buying_readiness_raw": round(readiness_raw, 2),
            "buying_readiness_score": readiness_score,
            "jev_rtt_ms": round(jev_rtt_ms, 2),
            "jev_input_tokens": 0,
            "jev_output_tokens": 0,
            "cache_hit": False,
            "guard_action": guard.action.value,
            "guard_category": guard.category,
            "emotion_type": emotion.emotion_type,
            "emotion_strategy_hint": emotion.strategy_hint,
        }
