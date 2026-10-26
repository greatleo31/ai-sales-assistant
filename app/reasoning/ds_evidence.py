# -*- coding: utf-8 -*-
"""
不确定性推理引擎 A：D-S 证据理论多源冲突融合与 Murphy 修正 (app/reasoning/ds_evidence.py)

理论与数学公理：
  1. 识别框架 (Frame of Discernment, Θ):
     Θ = {B, S, L}
     - B: Buyer Fault (买家过错/人为损坏)
     - S: Seller Fault (商家过错/出厂瑕疵)
     - L: Logistics Fault (物流过错/运输破损)
  2. 基本概率分配 (Basic Probability Assignment, BPA / Mass 函数 m):
     - m(∅) = 0
     - Σ_{A ⊆ Θ} m(A) = 1
  3. 经典 Dempster 合成规则 (Dempster's Rule of Combination):
     - 冲突因子 K = Σ_{B ∩ C = ∅} m1(B) * m2(C)
     - m_12(A) = (1 / (1 - K)) * Σ_{B ∩ C = A} m1(B) * m2(C)
  4. 进阶挑战层学术突破（破解 Zadeh 悖论）：
     - 实现 Murphy 加权平均证据修正算法 (Murphy's Averaged Combination Rule)；
     - 对比高冲突证据下经典 Dempster 规则的荒谬退化与 Murphy 算法的鲁棒收敛。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple


# 定义原子命题常量
ELEM_BUYER = "B"        # 买家责任
ELEM_SELLER = "S"       # 商家责任
ELEM_LOGISTICS = "L"    # 物流责任
FULL_FRAME = frozenset([ELEM_BUYER, ELEM_SELLER, ELEM_LOGISTICS])


def focal_set_to_str(s: FrozenSet[str]) -> str:
    """焦元集合友好可读显示。"""
    if s == FULL_FRAME:
        return "Θ (未知不确定)"
    mapping = {
        ELEM_BUYER: "买家过错",
        ELEM_SELLER: "商家过错",
        ELEM_LOGISTICS: "物流过错",
    }
    items = [mapping.get(x, x) for x in sorted(list(s))]
    return "{" + ",".join(items) + "}"


@dataclass
class MassFunction:
    """
    基本概率分配 (Mass 函数 / BPA)。
    底色字典为：FrozenSet[str] -> float
    """
    mass_dict: Dict[FrozenSet[str], float] = field(default_factory=dict)
    name: str = "Evidence"

    def __post_init__(self) -> None:
        self.normalize()

    def normalize(self) -> None:
        """归一化并剔除零概率与空集。"""
        # 移除空集
        empty_key = frozenset()
        if empty_key in self.mass_dict:
            del self.mass_dict[empty_key]

        total = sum(self.mass_dict.values())
        if total > 0.0:
            self.mass_dict = {
                k: round(v / total, 6)
                for k, v in self.mass_dict.items()
                if v > 1e-6
            }

    def get(self, focal: FrozenSet[str]) -> float:
        return self.mass_dict.get(focal, 0.0)

    def belief(self, target_set: FrozenSet[str]) -> float:
        """计算信任函数 Bel(A) = Σ_{B ⊆ A} m(B)。"""
        return sum(
            mass for b, mass in self.mass_dict.items()
            if b.issubset(target_set)
        )

    def plausibility(self, target_set: FrozenSet[str]) -> float:
        """计算似然函数 Pl(A) = Σ_{B ∩ A ≠ ∅} m(B)。"""
        return sum(
            mass for b, mass in self.mass_dict.items()
            if bool(b.intersection(target_set))
        )

    def uncertainty_interval(self, target_set: FrozenSet[str]) -> Tuple[float, float]:
        """计算信任度区间 [Bel(A), Pl(A)]。"""
        return round(self.belief(target_set), 4), round(self.plausibility(target_set), 4)

    def to_readable_dict(self) -> Dict[str, float]:
        return {
            focal_set_to_str(k): round(v, 4)
            for k, v in self.mass_dict.items()
        }


class DSEvidenceEngine:
    """D-S 证据理论核心推理与融合中枢。"""

    @staticmethod
    def dempster_combine_two(
        m1: MassFunction,
        m2: MassFunction,
        name: str = "Combined",
    ) -> Tuple[MassFunction, float]:
        """
        经典 Dempster 两个独立证据合成规则。
        返回: (合成后的 MassFunction, 冲突因子 K)。
        """
        combined_raw: Dict[FrozenSet[str], float] = {}
        conflict_k = 0.0

        for f1, w1 in m1.mass_dict.items():
            for f2, w2 in m2.mass_dict.items():
                intersection = f1.intersection(f2)
                mass_prod = w1 * w2
                if not intersection:
                    # 交集为空，累加到冲突度 K
                    conflict_k += mass_prod
                else:
                    combined_raw[intersection] = combined_raw.get(intersection, 0.0) + mass_prod

        # 检查是否完全冲突
        if conflict_k >= 0.999999:
            # 极端冲突，返回原始冲突度，避免除以零
            return MassFunction({}, name=f"{name}_Conflict"), conflict_k

        denom = 1.0 - conflict_k
        normalized_combined = {
            focal: (w / denom) for focal, w in combined_raw.items()
        }
        return MassFunction(normalized_combined, name=name), conflict_k

    @classmethod
    def dempster_combine_multiple(
        cls,
        evidences: List[MassFunction],
        name: str = "MultiCombined",
    ) -> Tuple[MassFunction, float]:
        """经典 Dempster 多证据链式合成。"""
        if not evidences:
            return MassFunction({FULL_FRAME: 1.0}, name="Empty"), 0.0
        if len(evidences) == 1:
            return evidences[0], 0.0

        current = evidences[0]
        total_k = 0.0
        for i, next_ev in enumerate(evidences[1:], start=2):
            current, k = cls.dempster_combine_two(current, next_ev, name=f"{name}_step{i}")
            total_k = max(total_k, k)
        return current, total_k


class MurphyEvidenceCombiner:
    """
    进阶优化算法：Murphy 加权平均证据修正算法。
    专治高冲突证据下的 Zadeh 悖论（Zadeh Paradox）。
    """

    @staticmethod
    def combine(evidences: List[MassFunction], name: str = "MurphyCombined") -> MassFunction:
        """
        Murphy 算法核心三步：
          1. 算术平均分配：m_avg(A) = (1/n) * Σ m_i(A)
          2. 将 m_avg 自身利用经典 Dempster 规则自乘 (n - 1) 次
          3. 消除极端冲突与 Zadeh 悖论
        """
        if not evidences:
            return MassFunction({FULL_FRAME: 1.0}, name="Empty")
        n = len(evidences)
        if n == 1:
            return evidences[0]

        # 第一步：计算加权平均分配 (Averaged Mass)
        avg_mass_raw: Dict[FrozenSet[str], float] = {}
        for ev in evidences:
            for focal, val in ev.mass_dict.items():
                avg_mass_raw[focal] = avg_mass_raw.get(focal, 0.0) + (val / n)

        averaged_mass = MassFunction(avg_mass_raw, name="AveragedBPA")

        # 第二步：将平均 Mass 连续自合成 (n - 1) 次
        result = averaged_mass
        for step in range(n - 1):
            result, _ = DSEvidenceEngine.dempster_combine_two(
                result,
                averaged_mass,
                name=f"{name}_iter{step+1}",
            )

        result.name = name
        return result


class EcommerceEvidenceFactory:
    """将电商售后实际业务参数（买家信誉、物流震动、AI质检）映射为 D-S 证据 BPA。"""

    @staticmethod
    def create_buyer_reputation_evidence(credit_score: float) -> MassFunction:
        """
        证据 1：买家历史信誉度 (0~100 分)。
        - 信誉极高（如 95 分）：人为调包欺诈概率极低，支持 {S, L}（商家或物流责任）；
        - 信誉极低（如 40 分）：存在恶意索赔嫌疑，向 {B}（买家责任）倾斜。
        """
        score = max(0.0, min(100.0, float(credit_score)))
        if score >= 85:
            # 优质买家：极大概率非买家过错
            return MassFunction({
                frozenset([ELEM_SELLER, ELEM_LOGISTICS]): 0.70,
                frozenset([ELEM_BUYER]): 0.05,
                FULL_FRAME: 0.25,
            }, name="Evidence_BuyerReputation")
        elif score <= 50:
            # 风险买家
            return MassFunction({
                frozenset([ELEM_BUYER]): 0.65,
                frozenset([ELEM_SELLER]): 0.10,
                FULL_FRAME: 0.25,
            }, name="Evidence_BuyerReputation")
        else:
            # 普通中立买家，高度不确定
            return MassFunction({
                frozenset([ELEM_SELLER, ELEM_LOGISTICS]): 0.40,
                frozenset([ELEM_BUYER]): 0.20,
                FULL_FRAME: 0.40,
            }, name="Evidence_BuyerReputation")

    @staticmethod
    def create_logistics_sensor_evidence(shock_g: float) -> MassFunction:
        """
        证据 2：物流运输中转颠簸度 (以加速度峰值 g 为依据)。
        - shock_g > 8.0：途中遭遇严重跌落或暴力分拣，强烈支持 {L}（物流责任）；
        - shock_g < 2.0：平稳运输，无异常中转记录，支持 {B, S}；
        """
        g = float(shock_g)
        if g >= 8.0:
            # 严重颠簸破损
            return MassFunction({
                frozenset([ELEM_LOGISTICS]): 0.75,
                frozenset([ELEM_SELLER]): 0.10,
                FULL_FRAME: 0.15,
            }, name="Evidence_LogisticsSensor")
        elif g >= 4.0:
            return MassFunction({
                frozenset([ELEM_LOGISTICS]): 0.45,
                FULL_FRAME: 0.55,
            }, name="Evidence_LogisticsSensor")
        else:
            # 平稳运输
            return MassFunction({
                frozenset([ELEM_BUYER, ELEM_SELLER]): 0.60,
                frozenset([ELEM_LOGISTICS]): 0.05,
                FULL_FRAME: 0.35,
            }, name="Evidence_LogisticsSensor")

    @staticmethod
    def create_inspection_report_evidence(defect_confidence: float, defect_kind: str) -> MassFunction:
        """
        证据 3：AI 图像破损与质检初审置信度 (0.0 ~ 1.0)。
        - defect_kind == "internal_circuit": 内部元器件短路/虚焊，强烈指向 {S}（商家制造缺陷）；
        - defect_kind == "external_crack": 外壳压痕撞裂，指向 {L, B}；
        """
        conf = max(0.0, min(1.0, float(defect_confidence)))
        if defect_kind == "internal_circuit":
            return MassFunction({
                frozenset([ELEM_SELLER]): round(0.80 * conf, 4),
                frozenset([ELEM_BUYER]): 0.05,
                FULL_FRAME: round(1.0 - 0.80 * conf - 0.05, 4),
            }, name="Evidence_DefectInspection")
        elif defect_kind == "external_crack":
            return MassFunction({
                frozenset([ELEM_LOGISTICS]): round(0.55 * conf, 4),
                frozenset([ELEM_BUYER]): round(0.25 * conf, 4),
                FULL_FRAME: round(1.0 - 0.80 * conf, 4),
            }, name="Evidence_DefectInspection")
        else:
            # 一般性外观磨损
            return MassFunction({
                frozenset([ELEM_BUYER]): 0.30,
                frozenset([ELEM_SELLER]): 0.30,
                FULL_FRAME: 0.40,
            }, name="Evidence_DefectInspection")
