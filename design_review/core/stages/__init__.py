"""内置 Pipeline Stage + 默认 pipeline 构造。

8 个 Stage 顺序：retrieve → context → prompt → review → parse → normalize → consensus → score。
v2 可 pipeline.insert(DebateStage(), before="normalize")。
"""
from __future__ import annotations

from pathlib import Path

from ..pipeline import Pipeline
from .consensus import ConsensusStage
from .context import ContextStage
from .normalize import NormalizeStage
from .parse import ParseStage
from .prompt import PromptStage
from .retrieve import RetrieveStage
from .review import ReviewStage
from .score import ScoreStage

__all__ = [
    "RetrieveStage",
    "ContextStage",
    "PromptStage",
    "ReviewStage",
    "ParseStage",
    "NormalizeStage",
    "ConsensusStage",
    "ScoreStage",
    "CORE_REVIEWERS_DIR",
    "build_default_pipeline",
]

CORE_REVIEWERS_DIR = Path(__file__).resolve().parent.parent / "reviewers"


def build_default_pipeline(
    *,
    normalizer_model: str = "claude-opus-4-8",
    threshold: int = 2,
    core_reviewers_dir: str | Path | None = None,
    default_dimensions: list[str] | None = None,
) -> Pipeline:
    """构造默认 8-Stage pipeline。"""
    crd = Path(core_reviewers_dir) if core_reviewers_dir else CORE_REVIEWERS_DIR
    return Pipeline(
        [
            RetrieveStage(),
            ContextStage(),
            PromptStage(crd, default_dimensions=default_dimensions),
            ReviewStage(),
            ParseStage(),
            NormalizeStage(normalizer_model),
            ConsensusStage(threshold),
            ScoreStage(),
        ]
    )
