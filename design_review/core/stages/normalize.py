"""NormalizeStage：LLM 归一 findings → canonical findings（B2，防同义漏报）。

Pipeline 第 6 步。用主审模型做一次归一 pass：把所有模型的 findings（去 evidence 细节）
交给 LLM，让它把语义相同的（如"重复Spawn"/"重复实例化"/"双生成"）合并成一个 canonical 组。
比 embedding 稳定。归一失败则降级为每 finding 一组（不阻塞 pipeline）。
"""
from __future__ import annotations

import json
import logging
import re

from ..pipeline import PipelineContext, Stage
from ..report import CanonicalFinding, Finding

logger = logging.getLogger("design_review.stage.normalize")

_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _build_prompt(findings: list[Finding]) -> tuple[str, str]:
    items = [
        {"id": i, "title": f.title, "dimension": f.dimension, "severity": f.severity}
        for i, f in enumerate(findings)
    ]
    system = (
        "你是审查发现的归一化引擎。把语义相同的发现合并成一个 canonical 组。"
        "输出严格 JSON：{\"groups\":[{\"canonical_title\":str, \"dimension\":str, "
        "\"severity\":str, \"finding_ids\":[int,...]}]}。"
        "canonical_title 是一句话标准标题。同义不同措辞（如'重复Spawn'/'重复实例化'/'双生成'）"
        "必须合并。不要丢掉任何 finding_id。"
    )
    user = "findings:\n```json\n" + json.dumps(items, ensure_ascii=False, indent=2) + "\n```"
    return system, user


def _parse_groups(content: str) -> list[dict]:
    m = _BLOCK_RE.search(content or "")
    cand = m.group(1) if m else (content or "").strip()
    try:
        obj = json.loads(cand)
        gs = obj.get("groups") if isinstance(obj, dict) else None
        return [g for g in gs if isinstance(g, dict)] if isinstance(gs, list) else []
    except Exception:  # noqa: BLE001
        return []


class NormalizeStage:
    name = "normalize"

    def __init__(self, normalizer_model: str = "claude-opus-4-8") -> None:
        self.model = normalizer_model

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.findings:
            return ctx
        system, user = _build_prompt(ctx.findings)
        resp = await ctx.backend.complete(
            model=self.model,
            system=system,
            user=user,
            temperature=0.1,
            top_p=0.9,
            max_tokens=4096,
        )
        groups = _parse_groups(resp.content) if resp.ok else []
        if not groups:
            logger.warning("归一失败(model=%s err=%s)，降级为每 finding 一组", self.model, resp.error)
            groups = [
                {
                    "canonical_title": f.title,
                    "dimension": f.dimension,
                    "severity": f.severity,
                    "finding_ids": [i],
                }
                for i, f in enumerate(ctx.findings)
            ]
        canonical: list[CanonicalFinding] = []
        for g in groups:
            ids = [i for i in g.get("finding_ids", []) if isinstance(i, int)]
            src = [ctx.findings[i] for i in ids if 0 <= i < len(ctx.findings)]
            if not src:
                continue
            rep = src[0]
            canonical.append(
                CanonicalFinding(
                    canonical_title=g.get("canonical_title") or rep.title,
                    dimension=g.get("dimension") or rep.dimension,
                    severity=g.get("severity") or rep.severity,
                    evidence_quote=rep.evidence_quote,
                    location=rep.location,
                    suggestion=rep.suggestion,
                    case_ref=rep.case_ref,
                    flagged_by=sorted({f.model for f in src}),
                    source_findings=src,
                )
            )
        ctx.canonical_findings = canonical
        return ctx
