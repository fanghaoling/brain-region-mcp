"""MemoryProvider：第一个 ContextProvider 实现（experience 召回 → ContextBlock）。

- ``from_store()`` = 生产（读 SQLite brain_region_reviews.db）。
- ``from_records()`` = eval（纯内存，防伪记忆，不读 DB；roadmap §15.3 🔍）。

retrieve 不调 LLM（§6）；ContextBlock.framing 恒为 "data"（存储型 prompt-injection 防御）。
"""
from __future__ import annotations

from ..core.context import ContextBlock, ContextQuery, RetrieveResult
from . import store
from .base import ExperienceEvent


class MemoryProvider:
    """Experience memory ContextProvider（结构化实现 ContextProvider 协议）。"""

    def __init__(
        self, *, records: list[ExperienceEvent] | None = None, region: str | None = None
    ) -> None:
        # records=None → 生产读 DB；records 非空 → eval 纯内存（防伪记忆）。
        self._records = records
        self._region = region

    @classmethod
    def from_store(cls, region: str | None = None) -> "MemoryProvider":
        return cls(region=region)

    @classmethod
    def from_records(
        cls, records: list[ExperienceEvent] | None, region: str | None = None
    ) -> "MemoryProvider":
        return cls(records=list(records or []), region=region)

    def retrieve(self, query: ContextQuery) -> RetrieveResult:
        top_k = max(0, int(query.top_k or 5))
        region = query.region or self._region
        if self._records is not None:
            # eval 纯内存路径：不读 DB。
            hits = store.search_from_records(self._records, query.text, top_k)
            candidates = len(self._records)
        else:
            # 生产 DB 路径。
            hits = store.search(query.text, top_k=top_k, region=region)
            candidates = len(store.list_experiences(region=region))
        blocks = [
            ContextBlock(
                source="memory",
                title=e.summary or e.id,
                content=(e.details or e.summary),
                framing="data",
                metadata={"id": e.id, "region": e.region},
            )
            for e in hits
        ]
        return RetrieveResult(
            provider="memory",
            blocks=blocks,
            meta={"candidates_before_top_k": candidates, "returned": len(blocks)},
        )
