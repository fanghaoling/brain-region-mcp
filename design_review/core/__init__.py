"""框架核心：项目无关的 Pipeline/Stage/Engine/Document/Report 抽象。"""
from __future__ import annotations

from .document import DocumentType, ReviewDocument
from .engine import ReviewEngine
from .pipeline import Pipeline, PipelineContext, Stage
from .report import CanonicalFinding, Finding, ReviewReport

__all__ = [
    "DocumentType",
    "ReviewDocument",
    "ReviewEngine",
    "Pipeline",
    "PipelineContext",
    "Stage",
    "Finding",
    "CanonicalFinding",
    "ReviewReport",
]
