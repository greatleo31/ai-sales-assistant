# -*- coding: utf-8 -*-
"""
人工智能导论推理技术创新核心模块 (app/reasoning)
涵盖三大核心能力：
  1. 确定性推理：产生式规则正反向链推导引擎与支持集启发式剪枝 (deterministic_engine.py)
  2. 不确定性推理 A：D-S 证据理论多源冲突融合与 Murphy 修正 (ds_evidence.py)
  3. 不确定性推理 B：Mamdani 模糊推理系统与重心法解模糊 (fuzzy_engine.py)
  4. 创新拔高：双轨神经-符号混合推理仲裁器 (hybrid_arbitrator.py)
"""

from app.reasoning.deterministic_engine import (
    DeterministicInferenceEngine,
    Rule,
    Fact,
    ProofTree,
    ProofStep,
)
from app.reasoning.ds_evidence import (
    DSEvidenceEngine,
    MassFunction,
    MurphyEvidenceCombiner,
)
from app.reasoning.fuzzy_engine import (
    FuzzyInferenceEngine,
    TriangularMF,
    TrapezoidalMF,
)
from app.reasoning.hybrid_arbitrator import (
    HybridArbitrator,
    ArbitrationDecision,
)

__all__ = [
    "DeterministicInferenceEngine",
    "Rule",
    "Fact",
    "ProofTree",
    "ProofStep",
    "DSEvidenceEngine",
    "MassFunction",
    "MurphyEvidenceCombiner",
    "FuzzyInferenceEngine",
    "TriangularMF",
    "TrapezoidalMF",
    "HybridArbitrator",
    "ArbitrationDecision",
]
