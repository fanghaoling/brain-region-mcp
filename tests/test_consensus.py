"""ConsensusStage：三档分类（consensus/majority/individual）。"""
from __future__ import annotations

import asyncio

from brain_region.core import ReviewDocument
from brain_region.core.pipeline import PipelineContext
from brain_region.core.report import CanonicalFinding, Finding
from brain_region.core.stages.consensus import ConsensusStage
from brain_region.providers import ModelResponse


def _cf(title: str, models: list[str]) -> CanonicalFinding:
    src = [
        Finding(
            model=m, dimension="ecs_perf", severity="high", title=title,
            evidence_quote="q", location="l", suggestion="s", confidence=0.8,
        )
        for m in models
    ]
    return CanonicalFinding(
        canonical_title=title, dimension="ecs_perf", severity="high",
        evidence_quote="q", location="l", suggestion="s", case_ref=None,
        flagged_by=list(models), source_findings=src,
    )


def _ctx():
    ctx = PipelineContext(
        document=ReviewDocument.markdown("x"), adapter=None, backend=None, knowledge=None
    )
    ctx.panel = ["gpt-5", "claude", "doubao"]
    ctx.responses = [
        {"model": m, "dimension": "ecs_perf", "response": ModelResponse(model=m, content="{}")}
        for m in ("gpt-5", "claude", "doubao")
    ]
    return ctx


def test_three_buckets():
    ctx = _ctx()
    ctx.canonical_findings = [
        _cf("A", ["gpt-5", "claude", "doubao"]),  # 全 → consensus
        _cf("B", ["gpt-5", "claude"]),  # 2/3 → majority
        _cf("C", ["gpt-5"]),  # 1 → individual
    ]
    asyncio.run(ConsensusStage(threshold=2).process(ctx))
    assert [c.canonical_title for c in ctx.consensus] == ["A"]
    assert [c.canonical_title for c in ctx.majority] == ["B"]
    assert "gpt-5" in ctx.individual
    assert [f.title for f in ctx.individual["gpt-5"]] == ["C"]


def test_threshold_clamps_to_num_models():
    """threshold=5 但只有 3 模型：clamp 到 3，全同意仍 consensus。"""
    ctx = _ctx()
    ctx.canonical_findings = [_cf("A", ["gpt-5", "claude", "doubao"])]
    asyncio.run(ConsensusStage(threshold=5).process(ctx))
    assert len(ctx.consensus) == 1
