"""Memory region scoping(Phase A)测试:MemoryScope + provider scope 过滤 + 漏斗 meta + consult 集成。

核心断言:跨 region 记忆 bleed 被 scope 防住(Unity 任务不注入 Blender 记忆)。
UNITY_PROJECT_ROOT=tmp 隔离 brain_region_reviews.db。
"""
from __future__ import annotations

import pytest

from brainregion.core.context import ContextQuery
from brainregion.memory import MemoryProvider, MemoryScope, store as memory_store
from brainregion.memory.base import ExperienceEvent


@pytest.fixture
def mem_root(monkeypatch, tmp_path):
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def _ev(eid: str, summary: str, triggers: list[str], region: str) -> ExperienceEvent:
    return ExperienceEvent(id=eid, summary=summary, triggers=triggers, region=region)


# ── MemoryScope.matches(单一过滤真相源)──────────────────────────────────────

def test_scope_matches_region_and_global():
    s = MemoryScope(frozenset({"unity_ecs"}))
    assert s.matches("unity_ecs")
    assert s.matches("")          # 全局默认通过(软过滤)
    assert not s.matches("blender")


def test_scope_include_global_false_excludes_global():
    s = MemoryScope(frozenset({"unity_ecs"}), include_global=False)
    assert s.matches("unity_ecs")
    assert not s.matches("")      # 全局也被滤


def test_scope_empty_regions_only_global():
    s = MemoryScope()             # 空 regions + include_global
    assert s.matches("")
    assert not s.matches("unity_ecs")


# ── MemoryProvider records 路径 scope ────────────────────────────────────────

def test_provider_records_scope_filters_cross_region():
    recs = [
        _ev("e1", "Unity FlowField", ["path"], "unity_ecs"),
        _ev("e2", "Blender bake", ["path"], "blender"),
        _ev("e3", "global fact", ["path"], ""),
    ]
    p = MemoryProvider.from_records(recs, scope=MemoryScope(frozenset({"unity_ecs"})))
    r = p.retrieve(ContextQuery(text="path", top_k=5))
    titles = [b.title for b in r.blocks]
    assert "Unity FlowField" in titles
    assert "global fact" in titles        # 全局通过
    assert "Blender bake" not in titles   # 跨 region bleed 被防住
    # 漏斗:total 3 → after_scope 2(unity+global)→ returned 2
    assert r.meta["candidates_before_top_k"] == 3
    assert r.meta["candidates_after_scope"] == 2
    assert r.meta["returned"] == 2
    assert r.meta["scope"] == ["unity_ecs"]


def test_provider_records_scope_none_returns_all():
    recs = [_ev("e1", "Unity", ["x"], "unity_ecs"), _ev("e2", "Blender", ["x"], "blender")]
    p = MemoryProvider.from_records(recs)  # scope=None(unscoped,向后兼容)
    r = p.retrieve(ContextQuery(text="x", top_k=5))
    assert len(r.blocks) == 2
    assert r.meta["candidates_after_scope"] == 2  # 无 scope → 不过滤
    assert r.meta["scope"] is None


def test_provider_records_include_global_false():
    recs = [_ev("e1", "Unity", ["x"], "unity_ecs"), _ev("e2", "global", ["x"], "")]
    p = MemoryProvider.from_records(recs, scope=MemoryScope(frozenset({"unity_ecs"}), include_global=False))
    r = p.retrieve(ContextQuery(text="x", top_k=5))
    titles = [b.title for b in r.blocks]
    assert "Unity" in titles
    assert "global" not in titles


def test_provider_funnel_monotone():
    recs = [_ev("e1", "a", ["k"], "unity_ecs"), _ev("e2", "b", ["k"], "blender"),
            _ev("e3", "c", ["k"], "smt"), _ev("e4", "g", ["k"], "")]
    p = MemoryProvider.from_records(recs, scope=MemoryScope(frozenset({"unity_ecs"})))
    r = p.retrieve(ContextQuery(text="k", top_k=5))
    assert r.meta["candidates_before_top_k"] >= r.meta["candidates_after_scope"] >= r.meta["returned"]


# ── 向后兼容:旧 region 单值参数(→ 不含全局的单 region scope)─────────────────

def test_provider_legacy_region_param_excludes_global_and_others():
    recs = [_ev("e1", "Unity", ["x"], "unity_ecs"), _ev("e2", "Blender", ["x"], "blender"),
            _ev("e3", "global", ["x"], "")]
    p = MemoryProvider.from_records(recs, region="unity_ecs")  # 旧 region 参数
    r = p.retrieve(ContextQuery(text="x", top_k=5))
    titles = [b.title for b in r.blocks]
    assert "Unity" in titles
    assert "Blender" not in titles
    assert "global" not in titles  # 旧 region=WHERE region=? 不含全局


def test_provider_legacy_query_region_adhoc():
    recs = [_ev("e1", "Unity", ["x"], "unity_ecs"), _ev("e2", "Blender", ["x"], "blender")]
    p = MemoryProvider.from_records(recs)  # 无 provider scope
    r = p.retrieve(ContextQuery(text="x", region="unity_ecs", top_k=5))  # query.region ad-hoc
    titles = [b.title for b in r.blocks]
    assert "Unity" in titles and "Blender" not in titles


# ── DB 路径 scope(生产 from_store)────────────────────────────────────────────

def test_provider_db_path_scope(mem_root):
    memory_store.record_experience(summary="Unity FlowField", details="用 FlowField",
                                   triggers=["path"], region="unity_ecs")
    memory_store.record_experience(summary="Blender bake", details="BVH baker",
                                   triggers=["path"], region="blender")
    memory_store.record_experience(summary="global fact", triggers=["path"], region="")
    p = MemoryProvider.from_store(scope=MemoryScope(frozenset({"unity_ecs"})))
    r = p.retrieve(ContextQuery(text="path", top_k=5))
    titles = [b.title for b in r.blocks]
    assert "Unity FlowField" in titles
    assert "global fact" in titles
    assert "Blender bake" not in titles
    assert r.meta["candidates_before_top_k"] == 3
    assert r.meta["candidates_after_scope"] == 2


def test_provider_db_path_scope_none_all(mem_root):
    memory_store.record_experience(summary="Unity", triggers=["x"], region="unity_ecs")
    memory_store.record_experience(summary="Blender", triggers=["x"], region="blender")
    p = MemoryProvider.from_store()  # scope=None
    r = p.retrieve(ContextQuery(text="x", top_k=5))
    assert len(r.blocks) == 2


# ── consult_problem scoping 集成 ──────────────────────────────────────────────

def _seed_two_regions():
    memory_store.record_experience(summary="Unity FlowField寻路", details="用 FlowField 不用 NavMesh",
                                   triggers=["path", "unity", "寻路"], region="unity_ecs")
    memory_store.record_experience(summary="Blender 烘焙法线", details="BVH raycast baker",
                                   triggers=["path"], region="blender")


@pytest.mark.asyncio
async def test_consult_problem_scopes_memory_no_bleed(mem_root, monkeypatch):
    from brainregion import server
    from brainregion.core.consult.report import ConsultAdvice, ConsultReport

    _seed_two_regions()
    monkeypatch.setenv("BRAIN_REGION_DEFAULT_MEMORY_INJECT", "true")  # memory_scope 默认 woken

    captured: dict = {}

    class _FakeEngine:
        async def consult(self, *a, **kw):
            captured["context_blocks"] = list(kw.get("context_blocks") or [])
            return ConsultReport(
                consultation_id="c", summary="ok",
                individual=[ConsultAdvice(id="c0", model="m", consultant="debugger", summary="ok")],
                usage={"cost_usd": 0.0},
            )

    monkeypatch.setattr(server, "_build_consult_engine", lambda dd: _FakeEngine())
    monkeypatch.setattr(server, "_route_regions", lambda **kw: {"selected": [{"id": "unity_ecs"}]})

    result = await server.consult_problem(problem="path 寻路 unity ecs", mode="debugging")
    titles = [b.title for b in captured["context_blocks"]]
    assert any("FlowField" in t for t in titles)
    assert not any("Blender" in t for t in titles)   # 无跨 region bleed
    assert result["memory"]["scope"] == ["unity_ecs"]
    assert result["memory"]["candidates_after_scope"] < result["memory"]["candidates_before_top_k"]


@pytest.mark.asyncio
async def test_consult_problem_unscoped_when_memory_scope_none(mem_root, monkeypatch):
    from brainregion import server
    from brainregion.core.consult.report import ConsultAdvice, ConsultReport

    _seed_two_regions()
    monkeypatch.setenv("BRAIN_REGION_DEFAULT_MEMORY_INJECT", "true")
    monkeypatch.setenv("BRAIN_REGION_DEFAULT_MEMORY_SCOPE", "none")  # unscoped 消融

    captured: dict = {}

    class _FakeEngine:
        async def consult(self, *a, **kw):
            captured["context_blocks"] = list(kw.get("context_blocks") or [])
            return ConsultReport(
                consultation_id="c", summary="ok",
                individual=[ConsultAdvice(id="c0", model="m", consultant="debugger", summary="ok")],
                usage={"cost_usd": 0.0},
            )

    monkeypatch.setattr(server, "_build_consult_engine", lambda dd: _FakeEngine())
    monkeypatch.setattr(server, "_route_regions", lambda **kw: {"selected": [{"id": "unity_ecs"}]})

    await server.consult_problem(problem="path 寻路 unity ecs", mode="debugging")
    titles = [b.title for b in captured["context_blocks"]]
    assert any("FlowField" in t for t in titles)
    assert any("Blender" in t for t in titles)  # unscoped → 两条都注入


# ── config ───────────────────────────────────────────────────────────────────

def test_defaults_memory_scope_default_and_coerce(monkeypatch):
    from brainregion import defaults

    dd = defaults.apply()
    assert dd["memory_scope"] == "woken"  # 默认开

    monkeypatch.setenv("BRAIN_REGION_DEFAULT_MEMORY_SCOPE", "none")
    assert defaults.apply()["memory_scope"] == "none"

    monkeypatch.setenv("BRAIN_REGION_DEFAULT_MEMORY_SCOPE", "bogus")
    assert defaults.apply()["memory_scope"] == "woken"  # 非法回退 woken
