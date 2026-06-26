"""ReviewStage：ModelBackend 并发 fan-out（panel × dimensions，独立采样，失败隔离）。

Pipeline 第 4 步。对 PromptStage 产出的每个 job（一个模型 × 一个维度）并发调用 backend，
单模型失败由 backend 内部隔离（返回 error），gather 不被打断。
"""
from __future__ import annotations

import asyncio

from ..pipeline import PipelineContext, Stage


class ReviewStage:
    name = "review"

    async def process(self, ctx: PipelineContext) -> PipelineContext:

        async def _one(job: dict) -> dict:
            resp = await ctx.backend.complete(
                model=job["model"],
                system=job["system"],
                user=job["user"],
                temperature=job["temperature"],
                top_p=job["top_p"],
                max_tokens=job["max_tokens"],
            )
            return {"model": job["model"], "dimension": job["dimension"], "response": resp}

        ctx.responses = await asyncio.gather(*(_one(j) for j in ctx.prompts))
        return ctx
