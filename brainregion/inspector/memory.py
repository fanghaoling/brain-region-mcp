"""inspect_memory：Experience Memory 盘点（read-only）。

按 region 计数 + 总量 + 最近 N 条（带年龄 days）+ 预览。复用 memory_store.list_experiences（新→旧，
降级规范：DB 错 → []）。治理标记（expired/low-confidence/corrected）等 v6 字段落了再加。
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..memory import store as memory_store


def inspect_memory(*, region: str | None = None, preview_k: int = 3) -> dict:
    events = memory_store.list_experiences(region=region)  # 新→旧；DB 错 → []
    by_region: dict[str, int] = {}
    for e in events:
        r = e.region or "(global)"
        by_region[r] = by_region.get(r, 0) + 1
    preview = [_event_summary(e) for e in events[: max(0, int(preview_k))]]
    return {
        "total": len(events),
        "region_filter": region,
        "by_region": by_region,
        "preview": preview,
    }


def _event_summary(e) -> dict:
    return {
        "id": e.id,
        "region": e.region,
        "summary": e.summary,
        "triggers": list(e.triggers or []),
        "created_at": e.created_at,
        "age_days": _age_days(e.created_at),
        "source": e.source,
    }


def _age_days(created_at: str | None) -> float | None:
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 86400.0, 1)
    except Exception:  # noqa: BLE001
        return None
