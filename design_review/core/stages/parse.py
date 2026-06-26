"""ParseStage：解析 LLM JSON 输出 → Finding，强制 evidence_quote。

Pipeline 第 5 步。提 ```json 块 / 整段 json.loads → 校验 finding schema → 丢弃无
evidence_quote 的（防幻觉）。schema 不符 best-effort 丢弃并记日志。失败模型跳过。
"""
from __future__ import annotations

import json
import logging
import re

from ..pipeline import PipelineContext, Stage
from ..report import Finding
from ..schema import get_schema

logger = logging.getLogger("design_review.stage.parse")

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_schema_cache: dict | None = None


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = get_schema("finding")
    return _schema_cache


def extract_json_object(text: str) -> dict | None:
    """提 ```json 块；失败再整段 json.loads；都失败返回 None。"""
    m = _JSON_BLOCK_RE.search(text or "")
    candidate = m.group(1) if m else (text or "").strip()
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _validate_finding(f: dict) -> bool:
    """jsonschema 校验 + evidence_quote 非空。任一不满足返回 False（调用方丢弃）。"""
    try:
        import jsonschema

        jsonschema.validate(f, _schema())
    except Exception:  # noqa: BLE001
        return False
    return bool(f.get("evidence_quote"))


class ParseStage:
    name = "parse"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        for item in ctx.responses:
            r = item["response"]
            model = item["model"]
            dim = item["dimension"]
            if not r.ok:
                continue
            obj = extract_json_object(r.content)
            if obj is None:
                logger.warning("模型 %s(%s) 输出无法解析为 JSON", model, dim)
                continue
            issues = obj.get("issues") if isinstance(obj.get("issues"), list) else []
            for f in issues:
                if not isinstance(f, dict) or not _validate_finding(f):
                    logger.info(
                        "丢弃无 evidence/schema 不符的 finding: %s/%s",
                        model,
                        str(f.get("title", ""))[:40] if isinstance(f, dict) else "?",
                    )
                    continue
                ctx.findings.append(
                    Finding(
                        model=model,
                        dimension=f.get("dimension", dim),
                        severity=f.get("severity", "medium"),
                        title=f.get("title", ""),
                        evidence_quote=f.get("evidence_quote", ""),
                        location=f.get("location", ""),
                        suggestion=f.get("suggestion", ""),
                        confidence=float(f.get("confidence", 0.5)),
                        case_ref=f.get("case_ref"),
                    )
                )
        return ctx
