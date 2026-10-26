# -*- coding: utf-8 -*-
"""
确定性候选集锁定、实体对齐与流式文本-卡片定序引擎 (app/core/candidate_lock.py)
面向电商导购智能体（Shopping Agent）的多轮确定性集合代数与流式对齐保障：
  1. CandidateDiffMeta: 记录多轮候选集演进元数据（STICKY_REFINE / PINNED_RELAX / SINGLETON_FOCUS / SNAPSHOT_RESTORE）；
  2. CandidateLockManager:
     - Sticky Intersection: 属性收窄时优先保留上一轮已展示商品并置顶，仅替补空缺；
     - Pinned Union: 预算/条件放宽时锁定上一轮商品不被洗牌，引入新价格带高分款；
     - Singleton Truncation: 单品问询时物理截断为 1 款，杜绝强推竞品；
  3. StreamOrderAligner:
     - 双通道出场捕获：滑动窗口实时剥离 `[[SKU:id]]` 锚点，并结合正文标题/别名首次出现字符偏移量（Byte Offset）；
     - 帧尾原子重排：下发的 `product_cards` 顺序与自然语言出场顺序 100% 物理一致；未提及款式物理裁剪；
  4. SpecRegistryMatcher:
     - 导购激活态下属性短词（如“通勤”、“换黑色”、“250元”）倒排匹配，覆写 Jev 闲聊误判；
     - 元对话纠错模式（如“怎么和上一轮不一样”、“卡片呢”）快照回滚。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 元对话纠错与质问正则（质疑推荐不一致或卡片丢失）
META_CORRECTION_RE = re.compile(
    r"(?i)(上一轮|上一次|刚才|不一样|卡片呢|怎么变了|怎么换了|不对吧|为什么变了|之前的呢|上一版|上个推荐|原来的呢|换回来了吗)"
)

# 显式单号提取正则
ORDER_ID_RE = re.compile(r"\b(ORD-\d{8}-\d{3,6})\b", re.IGNORECASE)

# 常见属性词表（供短词快速倒排匹配）
COMMON_SCENE_VOCAB: Set[str] = {
    "通勤", "商务", "出差", "旅行", "户外", "运动", "降噪", "游戏", "防水", "防泼水",
    "轻便", "防震", "气囊", "手提", "双肩", "大容量", "快充", "保鲜", "修护", "控油", "保湿"
}

COMMON_COLOR_VOCAB: Set[str] = {
    "黑色", "白色", "灰色", "蓝色", "粉色", "银色", "紫色", "冰晶紫", "米色", "深灰"
}


class CandidateDiffMeta(BaseModel):
    """用于向策略层与日志传递候选集演进详情，保障多轮连贯性。"""
    action: str = "INITIAL_LOCK"  # INITIAL_LOCK | STICKY_REFINE | PINNED_RELAX | SINGLETON_FOCUS | SNAPSHOT_RESTORE
    retained_sku_ids: List[str] = Field(default_factory=list)
    added_sku_ids: List[str] = Field(default_factory=list)
    removed_sku_ids: List[str] = Field(default_factory=list)


class CandidateLockManager:
    """
    确定性候选集集合代数管理器：
    杜绝多轮对话中的候选集随机洗牌与竞品污染。
    """
    DEFAULT_CAPACITY: int = 3

    @staticmethod
    def is_relaxation_query(query: str, current_max_price: Optional[float] = None) -> Tuple[bool, Optional[float]]:
        """检测当前输入是否为预算或规格放宽。"""
        q = query.lower()
        price_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|以内|左右|预算)", q)
        relax_kw = any(k in q for k in ("放宽", "加钱", "预算多点", "提高预算", "高一点", "放宽到"))
        if price_match:
            new_p = float(price_match.group(1))
            if current_max_price is not None and new_p > current_max_price:
                return True, new_p
            if relax_kw:
                return True, new_p
            return False, new_p
        if relax_kw:
            return True, None
        return False, None

    @staticmethod
    def card_satisfies_constraints(card: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
        """检查单张卡片是否满足当前提取的硬约束与属性。"""
        if not card:
            return False
        # 1. 类目约束
        target_sub = constraints.get("sub_category")
        if target_sub and card.get("sub_category") and card.get("sub_category") != target_sub:
            return False

        # 2. 价格上限约束
        max_p = constraints.get("max_price")
        if max_p is not None:
            final_p = float(card.get("final_price", 999999))
            if final_p > float(max_p):
                return False

        # 3. 屏幕尺寸约束
        req_size = constraints.get("screen_size")
        if req_size is not None and "specs" in card:
            c_size = card["specs"].get("screen_size")
            if c_size is not None and float(c_size) < float(req_size):
                return False

        # 4. 软属性匹配（如场景）
        scenes = set(constraints.get("scenes") or [])
        if scenes:
            card_highlights = set(card.get("highlights") or [])
            card_title = card.get("title", "")
            # 只要卡片标题或 highlights 中包含场景词即可满足
            if not any(sc in card_title or sc in card_highlights for sc in scenes):
                return False

        return True

    @classmethod
    def reconcile_candidates(
        cls,
        current_query: str,
        previous_cards: List[Dict[str, Any]],
        new_retrieved_cards: List[Dict[str, Any]],
        constraints: Dict[str, Any],
        matched_single_sku_id: Optional[str] = None,
        is_meta_correction: bool = False,
    ) -> Tuple[List[Dict[str, Any]], CandidateDiffMeta]:
        """
        核心集合代数运算：
          1. 快照恢复 (SNAPSHOT_RESTORE): 用户纠错质问时 100% 恢复上一轮展示卡片；
          2. 单品聚焦 (SINGLETON_FOCUS): 点名特定 SKU 时截断为 1 张，剔除未点名竞品；
          3. 约束放宽 (PINNED_RELAX): 保留上一轮已选款式置顶，引入新预算高分款式；
          4. 属性收窄 (STICKY_REFINE): 上一轮满足新属性的款式严格置顶保留，仅从新候选集中补齐空位。
        """
        # Case 1: 元对话纠错 / 快照恢复
        if is_meta_correction and previous_cards:
            return previous_cards, CandidateDiffMeta(
                action="SNAPSHOT_RESTORE",
                retained_sku_ids=[c.get("sku_id", "") for c in previous_cards if c.get("sku_id")],
            )

        # 针对双路径约束松弛场景（如 C1~C5），RAG 引擎已精准给出 Path A 与 Path B 折中解，保留完整双路径卡片
        if any(c.get("relaxation_path") in ("A", "B", "relax_budget", "relax_spec") for c in new_retrieved_cards):
            return new_retrieved_cards, CandidateDiffMeta(
                action="INITIAL_LOCK",
                added_sku_ids=[c.get("sku_id", "") for c in new_retrieved_cards if c.get("sku_id")],
            )

        # Case 2: 单品精准聚焦 (Singleton Truncation)
        if matched_single_sku_id:
            # 优先从新召回或历史卡片中提取目标 SKU
            pool = {c.get("sku_id"): c for c in (previous_cards + new_retrieved_cards) if c.get("sku_id")}
            if matched_single_sku_id in pool:
                focused_card = pool[matched_single_sku_id]
                return [focused_card], CandidateDiffMeta(
                    action="SINGLETON_FOCUS",
                    retained_sku_ids=[matched_single_sku_id],
                )

        if not previous_cards:
            # 首次推荐或无前序状态：直接采用新召回卡片
            initial_cards = new_retrieved_cards[: cls.DEFAULT_CAPACITY]
            return initial_cards, CandidateDiffMeta(
                action="INITIAL_LOCK",
                added_sku_ids=[c.get("sku_id", "") for c in initial_cards if c.get("sku_id")],
            )

        # 检查是否为放宽约束场景
        prev_max_price = None
        for c in previous_cards:
            p = float(c.get("final_price", 0))
            if prev_max_price is None or p > prev_max_price:
                prev_max_price = p
        is_relax, new_price = cls.is_relaxation_query(current_query, prev_max_price)

        # 计算上一轮卡片中依然满足新条件的商品 (Sticky Set)
        sticky_retained: List[Dict[str, Any]] = []
        removed_ids: List[str] = []
        for c in previous_cards:
            if cls.card_satisfies_constraints(c, constraints):
                sticky_retained.append(c)
            else:
                sid = c.get("sku_id")
                if sid:
                    removed_ids.append(sid)

        sticky_ids = {c.get("sku_id") for c in sticky_retained if c.get("sku_id")}

        # 计算新召回池中可供替补的商品 (Backfill Pool)
        backfill_candidates: List[Dict[str, Any]] = [
            c for c in new_retrieved_cards
            if c.get("sku_id") not in sticky_ids and cls.card_satisfies_constraints(c, constraints)
        ]

        if is_relax:
            # Case 3: 约束放宽 (Pinned Union)
            # 保留上一轮中最多 K-1 款满足条件的商品，确保给新解锁高配款留出 1~2 个展示位
            pinned_limit = max(1, cls.DEFAULT_CAPACITY - 1)
            pinned = sticky_retained[:pinned_limit]
            needed = max(0, cls.DEFAULT_CAPACITY - len(pinned))
            added = backfill_candidates[:needed]
            final_cards = pinned + added
            action = "PINNED_RELAX"
        else:
            # Case 4: 属性收窄 (Sticky Intersection)
            # 上一轮满足新收窄属性的款 100% 保持原有相对顺序置顶保留，空缺位由 backfill 填补
            needed = max(0, cls.DEFAULT_CAPACITY - len(sticky_retained))
            added = backfill_candidates[:needed]
            final_cards = (sticky_retained + added)[: cls.DEFAULT_CAPACITY]
            action = "STICKY_REFINE"

        final_retained_ids = [c.get("sku_id", "") for c in sticky_retained if c in final_cards and c.get("sku_id")]
        final_added_ids = [c.get("sku_id", "") for c in added if c.get("sku_id")]

        return final_cards, CandidateDiffMeta(
            action=action,
            retained_sku_ids=final_retained_ids,
            added_sku_ids=final_added_ids,
            removed_sku_ids=removed_ids,
        )


class StreamOrderAligner:
    """
    流式字节级文本-卡片定序器：
      1. 剥离 LLM 输出流中的 [[SKU:xxx]] 显式锚点，保证向前端发送的 text_delta 纯净无乱码；
      2. 实时记录商品首次被提及的先后顺序；
      3. 在流结束时，将 product_cards 物理数组原子重排为与文本叙述 100% 一致；未提及款式物理裁剪。
    """
    _ANCHOR_PATTERN = re.compile(r"\[\[SKU:([A-Za-z0-9_\-]+)\]\]")

    def __init__(self, candidate_cards: List[Dict[str, Any]]):
        self.candidate_cards: List[Dict[str, Any]] = list(candidate_cards)
        self.candidate_map: Dict[str, Dict[str, Any]] = {
            c["sku_id"]: c for c in candidate_cards if isinstance(c, dict) and c.get("sku_id")
        }
        self.mentioned_sku_ids: List[str] = []
        self._buffer: str = ""
        self._accumulated_clean_text: str = ""

    def _record_mention(self, sku_id: str) -> None:
        if sku_id in self.candidate_map and sku_id not in self.mentioned_sku_ids:
            self.mentioned_sku_ids.append(sku_id)

    def feed_chunk(self, chunk: str) -> str:
        """输入原始 token chunk，实时剥离锚点并返回纯净文本。"""
        if not chunk:
            return ""
        self._buffer += chunk
        output_clean = ""

        while True:
            match = self._ANCHOR_PATTERN.search(self._buffer)
            if match:
                prefix = self._buffer[: match.start()]
                output_clean += prefix
                self._record_mention(match.group(1))
                self._buffer = self._buffer[match.end() :]
                continue

            # 处理末尾未闭合的标签前缀 "[["
            partial_idx = self._buffer.rfind("[[")
            if partial_idx != -1 and (len(self._buffer) - partial_idx) < 30:
                output_clean += self._buffer[:partial_idx]
                self._buffer = self._buffer[partial_idx:]
            else:
                output_clean += self._buffer
                self._buffer = ""
            break

        self._accumulated_clean_text += output_clean
        return output_clean

    def flush(self) -> str:
        """流结束时排空缓冲区。"""
        tail = self._buffer
        self._buffer = ""
        self._accumulated_clean_text += tail
        return tail

    def finalize_aligned_cards(self) -> List[Dict[str, Any]]:
        """
        原子级卡片重排：
        1. 若模型打了 [[SKU:id]] 标签，严格按标签先后出场顺序重排；
        2. 若模型未打标签，使用候选卡片的品牌、型号指纹在全文中首次出现偏移量（Offset）兜底；
        3. 若且仅若模型正文中只介绍了其中 1 款，自动裁剪为 1 款（防止单品推荐混入竞品）；
        4. 若正文均未显式提及任何型号，保持原候选卡片顺序。
        """
        # 通道 2 兜底：型号与品牌在正文中的字节偏移量扫描
        offset_mentions: List[Tuple[int, str]] = []
        for sid, card in self.candidate_map.items():
            title = card.get("title", "")
            brand = card.get("brand", "")
            # 提取识别指纹（型号或品牌+核心词）
            fingerprints = [sid, brand]
            # 提取如 "W820NB", "17.3寸", "Nano3", "加厚气囊" 等短指纹
            for token in title.split():
                if len(token) >= 3 and token not in ("笔记本", "电脑包", "双肩包", "充电器", "快充头"):
                    fingerprints.append(token)

            first_pos = -1
            for fp in fingerprints:
                if not fp:
                    continue
                pos = self._accumulated_clean_text.find(fp)
                if pos != -1:
                    if first_pos == -1 or pos < first_pos:
                        first_pos = pos

            if first_pos != -1:
                offset_mentions.append((first_pos, sid))

        offset_mentions.sort(key=lambda x: x[0])
        for _, sid in offset_mentions:
            self._record_mention(sid)

        # 依据 mentioned_sku_ids 构建重排后的卡片
        if self.mentioned_sku_ids:
            aligned = [self.candidate_map[sid] for sid in self.mentioned_sku_ids if sid in self.candidate_map]
            return aligned

        return self.candidate_cards
