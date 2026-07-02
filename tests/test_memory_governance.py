"""Memory governance(v6 stage 1)测试:谓词 + schema 迁移 + ops + retrieve 漏斗 + 可恢复 + Health。

UNITY_PROJECT_ROOT=tmp 隔离 brain_region_reviews.db。
"""
from __future__ import annotations

import time

import pytest

from brainregion.core.context import ContextQuery
from brainregion.memory import MemoryProvider, governance, store as memory_store
from brainregion.memory.base import ExperienceEvent
from brainregion.memory.scope import MemoryScope


@pytest.fixture
def mem_root(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def _ev(eid, summary="s", triggers=None, region="r", status=governance.ACTIVE, valid_until_ts=0):
    return ExperienceEvent(
        id=eid, summary=summary, triggers=triggers or ["k"], region=region,
        status=status, valid_until_ts=valid_until_ts,
    )


NOW = 1_700_000_000
PAST = NOW - 1000
FUTURE = NOW + 1_000_000


# ── governance 谓词(纯函数)──────────────────────────────────────────────────

def test_is_expired_boundaries():
    assert governance.is_expired(0, now_ts=NOW) is False       # 0 = 永不过期
    assert governance.is_expired(PAST, now_ts=NOW) is True     # 过去 = 过期
    assert governance.is_expired(FUTURE, now_ts=NOW) is False  # 未来 = 未过期


def test_is_recallable_status_and_expiry():
    assert governance.is_recallable(_ev("a", status=governance.ACTIVE), now_ts=NOW)
    assert governance.is_recallable(_ev("p", status=governance.PENDING), now_ts=NOW)  # pending 照召回
    assert not governance.is_recallable(_ev("s", status=governance.SUPERSEDED), now_ts=NOW)
    assert not governance.is_recallable(_ev("w", status=governance.WRONG), now_ts=NOW)
    # active 但过期 → 不召回
    assert not governance.is_recallable(_ev("e", status=governance.ACTIVE, valid_until_ts=PAST), now_ts=NOW)
    # 缺 status 属性(老对象)→ 视 active
    class _Bare:
        valid_until_ts = 0
    assert governance.is_recallable(_Bare(), now_ts=NOW)


def test_filter_events_returns_list_and_stats():
    events = [
        _ev("a", status=governance.ACTIVE),
        _ev("p", status=governance.PENDING),
        _ev("s", status=governance.SUPERSEDED),
        _ev("w", status=governance.WRONG),
        _ev("x", status=governance.ACTIVE, valid_until_ts=PAST),  # expired
    ]
    out, stats = governance.filter_events(events, now_ts=NOW)
    ids = {e.id for e in out}
    assert ids == {"a", "p"}  # 只剩 active + pending(过期 active 被剔)
    assert stats["candidates_after_governance"] == 2
    assert stats["removed_superseded"] == 1
    assert stats["removed_wrong"] == 1
    assert stats["removed_expired"] == 1


# ── schema 迁移 + store ops(DB 路径)──────────────────────────────────────────

def test_old_rows_default_active_after_alter(mem_root):
    """旧行(直写,无 status 列)ALTER 后 → status=active / valid_until_ts=0。"""
    conn = memory_store._connect()  # 建表 + ALTER
    conn.execute(
        "INSERT INTO experiences(id, region, summary, triggers_json, created_at, source, schema_version) "
        "VALUES(?,?,?,?,?,?,?)",
        ("old-row", "r", "old", "[]", "2026-01-01", "", 1),
    )
    conn.commit()
    events = memory_store.list_experiences()
    e = next(x for x in events if x.id == "old-row")
    assert e.status == governance.ACTIVE
    assert e.valid_until_ts == 0
    assert e.superseded_by == ""
    assert e.last_reviewed == ""
    assert governance.is_recallable(e)  # 旧行 → active → 召回


def test_record_with_status_and_valid_until(mem_root):
    memory_store.record_experience(summary="temp fact", triggers=["k"], region="r",
                                   status=governance.PENDING, valid_until_ts=FUTURE)
    e = memory_store.list_experiences()[0]
    assert e.status == governance.PENDING
    assert e.valid_until_ts == FUTURE


def test_record_supersedes_marks_old(mem_root):
    old = memory_store.record_experience(summary="old way", triggers=["k"], region="r")
    new = memory_store.record_experience(summary="new way", triggers=["k"], region="r",
                                         supersedes=old["id"])
    by_id = {e.id: e for e in memory_store.list_experiences()}
    assert by_id[old["id"]].status == governance.SUPERSEDED
    assert by_id[old["id"]].superseded_by == new["id"]
    assert by_id[new["id"]].status == governance.ACTIVE


def test_set_experience_status_auto_last_reviewed_on_active(mem_root):
    rec = memory_store.record_experience(summary="x", triggers=["k"], region="r")
    old_lv = memory_store.list_experiences()[0].last_reviewed
    # record(active) 已 stamp;标 wrong 不刷新 last_reviewed
    memory_store.set_experience_status(rec["id"], governance.WRONG)
    e = next(x for x in memory_store.list_experiences() if x.id == rec["id"])
    assert e.status == governance.WRONG
    assert e.last_reviewed == old_lv  # wrong 不刷新
    # 标回 active → last_reviewed 刷新
    time.sleep(1.1)
    memory_store.set_experience_status(rec["id"], governance.ACTIVE)
    e = next(x for x in memory_store.list_experiences() if x.id == rec["id"])
    assert e.status == governance.ACTIVE
    assert e.last_reviewed > old_lv  # 重新 stamp


# ── retrieve 过滤 + 4 级漏斗(records 路径)─────────────────────────────────────

def test_retrieve_filters_inactive_and_funnel():
    recs = [
        _ev("a", status=governance.ACTIVE, region="unity"),
        _ev("p", status=governance.PENDING, region="unity"),
        _ev("s", status=governance.SUPERSEDED, region="unity"),
        _ev("w", status=governance.WRONG, region="unity"),
        _ev("b", status=governance.ACTIVE, region="blender"),  # 跨 region(scope 剔)
    ]
    p = MemoryProvider.from_records(recs, scope=MemoryScope(frozenset({"unity"})))
    r = p.retrieve(ContextQuery(text="k", top_k=5))
    ids = {b.metadata["id"] for b in r.blocks}
    assert ids == {"a", "p"}  # scope=unity + governance 留 active/pending
    m = r.meta
    assert m["candidates_before_top_k"] == 5
    assert m["candidates_after_scope"] == 4        # unity 的 4 条
    assert m["candidates_after_governance"] == 2   # active+pending
    assert m["removed_superseded"] == 1
    assert m["removed_wrong"] == 1
    assert m["returned"] == 2
    # 漏斗单调
    assert m["candidates_before_top_k"] >= m["candidates_after_scope"] >= m["candidates_after_governance"] >= m["returned"]


def test_retrieve_expired_filtered_unexpired_kept():
    past = int(time.time()) - 100
    future = int(time.time()) + 10000
    recs = [
        _ev("gone", status=governance.ACTIVE, valid_until_ts=past),   # 过期 → 剔
        _ev("soon", status=governance.ACTIVE, valid_until_ts=future), # 未到 → 留
        _ev("forever", status=governance.ACTIVE, valid_until_ts=0),   # 0 → 永不滤
    ]
    p = MemoryProvider.from_records(recs)
    r = p.retrieve(ContextQuery(text="k", top_k=5))
    ids = {b.metadata["id"] for b in r.blocks}
    assert ids == {"soon", "forever"}
    assert r.meta["removed_expired"] == 1


# ── 可恢复(误标回滚)────────────────────────────────────────────────────────

def _recallable_ids(text: str = "k") -> set[str]:
    """生产 retrieve 路径(superseded/wrong/expired 被滤)召回的 id 集。"""
    r = MemoryProvider.from_store().retrieve(ContextQuery(text=text, top_k=20))
    return {b.metadata["id"] for b in r.blocks}


def test_recoverable_superseded_then_active(mem_root):
    rec = memory_store.record_experience(summary="x", triggers=["k"], region="r")
    rid = rec["id"]
    assert rid in _recallable_ids()                       # active → 召回
    memory_store.set_experience_status(rid, governance.SUPERSEDED)
    assert rid not in _recallable_ids()                   # superseded → 不召回
    memory_store.set_experience_status(rid, governance.ACTIVE)
    assert rid in _recallable_ids()                       # 改回 active → 召回恢复


def test_recoverable_wrong_then_active_refreshes_last_reviewed(mem_root):
    rec = memory_store.record_experience(summary="y", triggers=["k"], region="r")
    rid = rec["id"]
    memory_store.set_experience_status(rid, governance.WRONG)
    assert rid not in _recallable_ids()                   # wrong → 不召回
    time.sleep(1.1)
    memory_store.set_experience_status(rid, governance.ACTIVE)
    e = next(x for x in memory_store.list_experiences() if x.id == rid)
    assert e.status == governance.ACTIVE
    assert e.last_reviewed                                 # 刷新了
    assert rid in _recallable_ids()                        # 召回恢复


# ── recall_experiences include_inactive ──────────────────────────────────────

def test_recall_filters_inactive_unless_include_inactive(mem_root, monkeypatch):
    good = memory_store.record_experience(summary="good", triggers=["k"], region="r")
    bad = memory_store.record_experience(summary="bad", triggers=["k"], region="r")
    memory_store.set_experience_status(bad["id"], governance.WRONG)

    from brainregion import server
    default = server.recall_experiences(text="k")
    assert {e["id"] for e in default["experiences"]} == {good["id"]}  # 默认滤 wrong

    full = server.recall_experiences(text="k", include_inactive=True)
    assert {e["id"] for e in full["experiences"]} == {good["id"], bad["id"]}  # 含全部


# ── Inspector Health ─────────────────────────────────────────────────────────

def test_inspector_memory_health(mem_root):
    from brainregion.inspector import memory as mem_view
    memory_store.record_experience(summary="a1", triggers=["k"], region="unity", status=governance.ACTIVE)
    memory_store.record_experience(summary="p1", triggers=["k"], region="unity", status=governance.PENDING)
    memory_store.record_experience(summary="s1", triggers=["k"], region="unity", status=governance.SUPERSEDED)
    memory_store.record_experience(summary="w1", triggers=["k"], region="unity", status=governance.WRONG)
    res = mem_view.inspect_memory()
    h = res["health"]
    assert h["by_status"][governance.ACTIVE] == 1
    assert h["by_status"][governance.PENDING] == 1
    assert h["by_status"][governance.SUPERSEDED] == 1
    assert h["by_status"][governance.WRONG] == 1
    assert h["recallable"] == 2  # active + pending
    assert h["non_recallable"] == 2  # superseded + wrong
    # preview 含治理字段
    assert "status" in res["preview"][0] and "valid_until_ts" in res["preview"][0]


# ── 向后兼容:既有 memory 行为不破 ────────────────────────────────────────────

def test_backward_compat_default_record_is_active_and_recallable(mem_root):
    memory_store.record_experience(summary="plain", triggers=["k"], region="r")  # 不传 status
    e = memory_store.list_experiences()[0]
    assert e.status == governance.ACTIVE
    assert governance.is_recallable(e)
