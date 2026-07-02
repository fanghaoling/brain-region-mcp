"""Experience Memory 的 SQLite 存储（append-only，Phase2A）。

复用 reviews_db 的 ``brain_region_reviews.db``（同包 ``reviews_db._db_path``），自己的
``_connect`` 建 experiences 表。镜像 reviews_db 的降级规范：所有 accessor try/except warn 不抛。
search 是关键词子串匹配（不调模型，§6）；``search_from_records`` 是纯函数，eval 用，
不读 DB = 防伪记忆（roadmap §15.3 🔍）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

from .. import reviews_db
from .base import ExperienceEvent
from .governance import ACTIVE, SUPERSEDED

logger = logging.getLogger("brainregion.memory.store")

_SCHEMA_VERSION = 1


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(reviews_db._db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:  # noqa: BLE001 — 不支持 WAL 的文件系统静默回退
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS experiences (
            id TEXT PRIMARY KEY,
            region TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            details TEXT DEFAULT '',
            triggers_json TEXT DEFAULT '[]',
            created_at TEXT DEFAULT '',
            source TEXT DEFAULT '',
            schema_version INTEGER DEFAULT 1
        )
        """
    )
    # 加性迁移：旧库无 schema_version 列时补上（镜像 reviews_db 的 ALTER 风格）。
    try:
        conn.execute("ALTER TABLE experiences ADD COLUMN schema_version INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass  # 列已存在
    # v6 stage 1 governance 字段（加性；旧行 → status=active / valid_until_ts=0 永不过期）。
    for col, decl in (
        ("status", "TEXT DEFAULT 'active'"),
        ("valid_until_ts", "INTEGER DEFAULT 0"),
        ("superseded_by", "TEXT DEFAULT ''"),
        ("last_reviewed", "TEXT DEFAULT ''"),
    ):
        try:
            conn.execute(f"ALTER TABLE experiences ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_region ON experiences(region)")
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(summary: str, region: str, created_at: str) -> str:
    h = hashlib.sha256(f"{region}|{summary}|{created_at}".encode("utf-8")).hexdigest()
    return f"exp-{h[:12]}"


def _row_to_event(row: sqlite3.Row) -> ExperienceEvent:
    try:
        triggers = json.loads(row["triggers_json"] or "[]")
    except Exception:  # noqa: BLE001
        triggers = []
    if not isinstance(triggers, list):
        triggers = []
    return ExperienceEvent(
        id=row["id"],
        region=row["region"] or "",
        summary=row["summary"] or "",
        details=row["details"] or "",
        triggers=[str(t) for t in triggers],
        created_at=row["created_at"] or "",
        source=row["source"] or "",
        status=row["status"] or ACTIVE,
        valid_until_ts=int(row["valid_until_ts"] or 0),
        superseded_by=row["superseded_by"] or "",
        last_reviewed=row["last_reviewed"] or "",
    )


def record_experience(
    *,
    summary: str,
    details: str = "",
    triggers: list[str] | None = None,
    region: str = "",
    source: str = "",
    experience_id: str | None = None,
    created_at: str | None = None,
    status: str = ACTIVE,
    valid_until_ts: int = 0,
    supersedes: str = "",
) -> dict:
    """记录一条经验（append-only，UPSERT by id）。返回 {ok, id}。失败 warn 不抛。

    v6 stage 1: ``status`` / ``valid_until_ts`` 治理字段;``supersedes=old_id`` 便利参数 →
    记录新后调 ``mark_superseded(old, new)``(单一 UPDATE 真相源,不自己 UPDATE old)。
    """
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("summary 不能为空")
    ts = created_at or _now_iso()
    eid = experience_id or _new_id(summary, region, ts)
    triggers_json = json.dumps([str(t) for t in (triggers or [])], ensure_ascii=False)
    last_reviewed = ts if status == ACTIVE else ""
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO experiences(id, region, summary, details, triggers_json, created_at, source, "
            "schema_version, status, valid_until_ts, superseded_by, last_reviewed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  region=excluded.region, summary=excluded.summary, details=excluded.details, "
            "  triggers_json=excluded.triggers_json, source=excluded.source, status=excluded.status, "
            "  valid_until_ts=excluded.valid_until_ts, superseded_by=excluded.superseded_by, "
            "  last_reviewed=excluded.last_reviewed",
            (eid, region, summary[:2000], details[:8000], triggers_json, ts, source[:500], _SCHEMA_VERSION,
             status, int(valid_until_ts or 0), "", last_reviewed),
        )
        conn.commit()
        if supersedes:
            mark_superseded(supersedes, eid)
        return {"ok": True, "id": eid}
    except Exception as e:  # noqa: BLE001
        logger.warning("record_experience 失败: %s", e)
        return {"ok": False, "id": eid, "error": str(e)}


def set_experience_status(
    id: str,
    status: str,
    *,
    superseded_by: str | None = None,
    valid_until_ts: int | None = None,
) -> dict:
    """更新一条经验的 status（人工治理）。status→active 时自动 stamp ``last_reviewed=now``（调用方不传）。

    状态自由可逆（见 governance.py 状态图）：任意状态可互转,无 transition guard。
    ``superseded_by`` / ``valid_until_ts`` 给了才更新,否则不动。返回 {ok, id}。失败 warn 不抛。
    """
    try:
        conn = _connect()
        sets = ["status=?"]
        vals: list = [status]
        if superseded_by is not None:
            sets.append("superseded_by=?")
            vals.append(superseded_by or "")
        if valid_until_ts is not None:
            sets.append("valid_until_ts=?")
            vals.append(int(valid_until_ts or 0))
        if status == ACTIVE:
            sets.append("last_reviewed=?")
            vals.append(_now_iso())
        vals.append(id)
        conn.execute(f"UPDATE experiences SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()
        return {"ok": True, "id": id}
    except Exception as e:  # noqa: BLE001
        logger.warning("set_experience_status 失败: %s", e)
        return {"ok": False, "id": id, "error": str(e)}


def mark_superseded(old_id: str, new_id: str) -> dict:
    """把 old 标 superseded（superseded_by=new）。单一 UPDATE 真相源：
    record_experience.supersedes / MCP mark_superseded 都调它,不重复 UPDATE 逻辑。"""
    return set_experience_status(old_id, SUPERSEDED, superseded_by=new_id)


def list_experiences(region: str | None = None) -> list[ExperienceEvent]:
    """列出经验（可按 region 过滤，新→旧）。失败 → []。"""
    try:
        conn = _connect()
        if region:
            rows = conn.execute(
                "SELECT * FROM experiences WHERE region=? ORDER BY created_at DESC", (region,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM experiences ORDER BY created_at DESC").fetchall()
        return [_row_to_event(r) for r in rows]
    except Exception as e:  # noqa: BLE001
        logger.warning("list_experiences 失败: %s", e)
        return []


def _score(event: ExperienceEvent, text_lower: str) -> int:
    """关键词命中计分（triggers 子串匹配，命中数；anti_triggers 已 defer）。"""
    return sum(1 for t in event.triggers if t and t.lower() in text_lower)


def search(text: str, top_k: int = 5, region: str | None = None) -> list[ExperienceEvent]:
    """关键词召回（triggers 子串匹配 + top_k，不调模型）。失败 → []。"""
    try:
        events = list_experiences(region=region)
        return search_from_records(events, text, top_k)
    except Exception as e:  # noqa: BLE001
        logger.warning("search 失败: %s", e)
        return []


def search_from_records(records: list[ExperienceEvent], text: str, top_k: int = 5) -> list[ExperienceEvent]:
    """纯函数召回（eval 用，不读 DB = 防伪记忆）。与 search 同算法，可独立测、跨进程一致。"""
    text_lower = (text or "").lower()
    if not text_lower:
        return []
    # (score, 原序, event)：仅保留有命中，按命中数降序、原序 tie-break（稳定）。
    hits = [
        (s, i, e)
        for i, e in enumerate(records)
        if (s := _score(e, text_lower)) > 0
    ]
    hits.sort(key=lambda x: (-x[0], x[1]))
    return [e for _, _, e in hits[: max(0, int(top_k))]]
