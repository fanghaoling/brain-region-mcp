"""Experience Memory store 单测：CRUD + 关键词召回 + 纯函数一致性 + 迁移 + 降级。"""
from __future__ import annotations

import sqlite3

from brainregion.memory import ExperienceEvent, store


def test_record_and_list_roundtrip():
    res = store.record_experience(
        summary="FlowField 死锁根因是 JobHandle 依赖未合并",
        details="CompleteDependency() 附近偶发卡住；CombineDependencies 后稳定。",
        triggers=["FlowField", "deadlock", "JobHandle"],
        region="debugging",
        source="consult-abc",
    )
    assert res["ok"] is True and res["id"].startswith("exp-")
    items = store.list_experiences()
    assert len(items) == 1
    e = items[0]
    assert e.summary.startswith("FlowField")
    assert "JobHandle" in e.triggers and "deadlock" in e.triggers
    assert e.region == "debugging"
    assert e.source == "consult-abc"
    assert e.created_at  # 自动填


def test_record_upsert_same_id_not_duplicate():
    res = store.record_experience(summary="x", region="r", experience_id="exp-fixed")
    assert res == {"ok": True, "id": "exp-fixed"}
    res2 = store.record_experience(summary="x-updated", region="r", experience_id="exp-fixed")
    assert res2 == {"ok": True, "id": "exp-fixed"}
    items = store.list_experiences(region="r")
    assert len(items) == 1
    assert items[0].summary == "x-updated"


def test_record_empty_summary_raises():
    import pytest

    with pytest.raises(ValueError):
        store.record_experience(summary="   ")


def test_search_keyword_ranking_and_topk():
    store.record_experience(summary="A", triggers=["foo", "bar"], region="r")
    store.record_experience(summary="B", triggers=["foo"], region="r")
    store.record_experience(summary="C", triggers=["unrelated"], region="r")
    hits = store.search("foo bar problem", top_k=5, region="r")
    # A 命中 2 词 > B 命中 1 词；C 无命中排除
    assert [h.summary for h in hits] == ["A", "B"]


def test_search_topk_limits():
    for i in range(5):
        store.record_experience(summary=f"s{i}", triggers=["foo"])
    assert len(store.search("foo", top_k=2)) == 2


def test_search_from_records_matches_db_search():
    store.record_experience(summary="A", triggers=["alpha"])
    store.record_experience(summary="B", triggers=["beta"])
    via_db = store.search("alpha beta", top_k=5)
    via_pure = store.search_from_records(store.list_experiences(), "alpha beta", 5)
    # 同算法（命中数降序 + 原序 tie-break）→ 同 id 序
    assert [e.id for e in via_db] == [e.id for e in via_pure]


def test_search_from_records_empty_text_returns_empty():
    recs = [ExperienceEvent(id="x", summary="s", triggers=["foo"])]
    assert store.search_from_records(recs, "", 5) == []


def test_schema_version_alter_migration(tmp_path):
    # 模拟"前一版"experiences 表：有所有列，唯独缺 schema_version（升级前形态）。
    # _connect 的 CREATE TABLE IF NOT EXISTS 对已存在表是 no-op，靠 ALTER 补列。
    db = store.reviews_db._db_path()
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE experiences ("
        "id TEXT PRIMARY KEY, region TEXT DEFAULT '', summary TEXT DEFAULT '', "
        "details TEXT DEFAULT '', triggers_json TEXT DEFAULT '[]', "
        "created_at TEXT DEFAULT '', source TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO experiences(id,region,summary,created_at) VALUES('exp-old','r','old','2026-01-01')"
    )
    conn.commit()
    conn.close()
    conn2 = store._connect()  # 触发 ALTER ADD COLUMN schema_version
    cols = {r[1] for r in conn2.execute("PRAGMA table_info(experiences)").fetchall()}
    assert "schema_version" in cols
    assert any(e.id == "exp-old" for e in store.list_experiences())  # 旧行保留、可读


def test_db_error_degrades_not_raise(monkeypatch):
    def _boom():
        raise sqlite3.OperationalError("boom")

    monkeypatch.setattr(store, "_connect", _boom)
    # 写/读/召回全部降级不抛
    assert store.record_experience(summary="x")["ok"] is False
    assert store.list_experiences() == []
    assert store.search("x") == []
    # 纯函数不受 DB 故障影响
    recs = [ExperienceEvent(id="x", summary="s", triggers=["foo"])]
    assert len(store.search_from_records(recs, "foo", 5)) == 1
