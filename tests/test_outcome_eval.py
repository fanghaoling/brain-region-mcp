"""Outcome eval 单测（mock，不联网）。

覆盖：region→consultants 映射 + 回退、advice 脱敏、compute_outcome_summary 数学、
evaluate_gate GO/NO_GO/INCONCLUSIVE、run_outcome_eval 端到端（monkeypatch 引擎+judge backend）。
"""
from __future__ import annotations

import json

import pytest

from brainregion.eval import outcome
from brainregion.eval.outcome import (
    DEFAULT_OUTCOME_VARIANTS,
    GateConfig,
    OutcomeRecord,
    OutcomeVariant,
    _resolve_variant_consultants,
    compute_outcome_summary,
    consultants_for_regions,
    evaluate_gate,
    run_outcome_eval,
)
from brainregion.eval.schema import BlindJudgement, EvalTask
from brainregion.providers.base import ModelResponse


# ---------- region → consultants 映射 ----------

def test_consultants_for_regions_union_dedup():
    # debugging ∪ security ∪ unity_ecs，去重保序
    assert consultants_for_regions(["debugging", "security"]) == ["debugger", "challenge", "critic"]
    # performance 的 critic 与 security 的 critic 去重
    assert consultants_for_regions(["performance", "security"]) == ["performance", "critic", "challenge"]


def test_consultants_for_regions_empty_for_no_specialist():
    # memory/research/review 无 specialist → 空（不掺 fallback）
    assert consultants_for_regions(["memory"]) == []
    assert consultants_for_regions(["research", "review"]) == []
    assert consultants_for_regions([]) == []


def test_resolve_variant_consultants_default_vs_routed_vs_fallback():
    dd = {"consult_consultants": ["debugger", "architect", "critic"]}
    default = OutcomeVariant("default", "default")
    routed = OutcomeVariant("routed", "routed")

    # default → 静态默认面板，source=default
    cons, src = _resolve_variant_consultants(default, ["debugging"], dd)
    assert cons == ["debugger", "architect", "critic"] and src == "default"

    # routed + 有 specialist 映射 → routed
    cons, src = _resolve_variant_consultants(routed, ["debugging", "security"], dd)
    assert cons == ["debugger", "challenge", "critic"] and src == "routed"

    # routed + 空并集（memory）→ 回退默认，source=fallback
    cons, src = _resolve_variant_consultants(routed, ["memory"], dd)
    assert cons == ["debugger", "architect", "critic"] and src == "fallback"

    # dd 无 consult_consultants → 回退内置默认
    cons, src = _resolve_variant_consultants(routed, ["memory"], {})
    assert cons == ["debugger", "architect", "critic"]


def test_resolve_variant_consultants_wake_all_reserved():
    with pytest.raises(NotImplementedError):
        _resolve_variant_consultants(OutcomeVariant("wake_all", "wake_all"), [], {})


# ---------- advice 脱敏 ----------

def test_desensitize_advice_strips_identity(monkeypatch):
    from brainregion.eval.judge import desensitize_advice

    report = {
        "summary": "isolate the boundary",
        "likely_causes": ["config drift"],
        "next_experiments": ["smallest repro"],
        "solution_options": ["guard + fake backend"],
        "risks": ["secret leak"],
        "recommended_plan": ["start with MVP"],
        "individual": [
            {"id": "c0", "model": "claude-opus-4-8", "consultant": "debugger",
             "summary": "sub", "likely_causes": ["x"]},
        ],
        "routing": {"panel_source": "consult_panel", "consultants_source": "mode"},
        "panel": ["claude-opus-4-8"],
        "usage": {"cost_usd": 0.01},
        "budget": {"estimated_usd": 0.01},
        "guard": {"sent_chars": 100},
    }
    out = desensitize_advice(report)
    assert len(out) >= 1
    flat = json.dumps(out, ensure_ascii=False)
    # 保留建议实质
    assert "config drift" in flat and "smallest repro" in flat
    # 剥离身份线索（防盲破）
    assert "claude-opus-4-8" not in flat
    assert "debugger" not in flat
    assert "panel_source" not in flat and "consultants_source" not in flat


def test_desensitize_advice_falls_back_to_individual_when_no_aggregate():
    from brainregion.eval.judge import desensitize_advice

    report = {
        "summary": "", "likely_causes": [], "next_experiments": [],
        "solution_options": [], "risks": [], "recommended_plan": [],
        "individual": [
            {"model": "m", "consultant": "c", "summary": "only individual", "likely_causes": ["c1"]},
        ],
    }
    out = desensitize_advice(report)
    assert len(out) == 1
    assert out[0]["summary"] == "only individual"
    assert "model" not in out[0] and "consultant" not in out[0]


# ---------- compute_outcome_summary 数学 ----------

def _rec(variant, *, inference, useful_judges, wake_metrics=None, consultants=None, woken=None):
    jdgs = [BlindJudgement(run_id="r", task_id="t", judge_id="j", judge_model="m",
                           rubric_hash="h", variant=variant, scores=s) for s in useful_judges]
    rec = OutcomeRecord(
        run_id="r", task_id="t", variant=variant,
        report_summary={"advice_count": 1, "failed_count": 0},
        wake={
            "strategy": variant, "mapping_source": "routed" if variant == "routed" else "default",
            "consultants": consultants or [], "woken": woken or [],
            "wake_metrics": wake_metrics or {}, "shadow_promoted": 0,
        },
        cost={"inference_usd": inference, "estimated_usd": inference, "total_tokens": 10},
        latency_ms=100.0,
    )
    return rec, jdgs


def test_compute_outcome_summary_cost_per_useful_and_missed_wake():
    # 2 任务，每变体：default useful=2 cost=0.02；routed useful=4 cost=0.02
    records, judgements = [], []
    for _ in range(2):
        r1, j1 = _rec("default", inference=0.02, useful_judges=[{"useful": 2, "overall": 4, "missed_critical": 0}])
        r2, j2 = _rec("routed", inference=0.02, useful_judges=[{"useful": 4, "overall": 5, "missed_critical": 0}],
                      wake_metrics={"missed": [], "hit": ["debugging"], "false_wake": []},
                      consultants=["debugger"], woken=["debugging"])
        records += [r1, r2]
        judgements += j1 + j2

    s = compute_outcome_summary(records, judgements, DEFAULT_OUTCOME_VARIANTS)
    pv = s["per_variant"]
    # cost_per_useful = inference/useful：default 0.02/2=0.01；routed 0.02/4=0.005
    assert pv["default"]["cost_per_useful_advice"] == 0.01
    assert pv["routed"]["cost_per_useful_advice"] == 0.005
    assert pv["routed"]["useful_advice_rate"] >= pv["default"]["useful_advice_rate"]
    # missed_wake_rate：routed 的 gold={debugging}（hit），missed=0 → 0.0
    assert pv["routed"]["missed_wake_rate"] == 0.0
    assert pv["routed"]["missed_critical_total"] == 0


def test_compute_outcome_summary_useful_zero_yields_none():
    r1, j1 = _rec("default", inference=0.01, useful_judges=[{"useful": 0}])
    r2, j2 = _rec("routed", inference=0.01, useful_judges=[{"useful": 0}])
    s = compute_outcome_summary([r1, r2], j1 + j2, DEFAULT_OUTCOME_VARIANTS)
    assert s["per_variant"]["default"]["cost_per_useful_advice"] is None


# ---------- evaluate_gate ----------

def _gate_summary(*, cost_a, cost_b, ur_a, ur_b, n=5, missed_wake_b=0.0,
                  lat_a=1000.0, lat_b=1200.0, mc_a=0, mc_b=0):
    return {
        "per_variant": {
            "default": {"n": n, "cost_per_useful_advice": cost_a, "useful_advice_rate": ur_a,
                        "latency_p95_ms": lat_a, "missed_critical_total": mc_a, "missed_wake_rate": 0.0},
            "routed": {"n": n, "cost_per_useful_advice": cost_b, "useful_advice_rate": ur_b,
                       "latency_p95_ms": lat_b, "missed_critical_total": mc_b, "missed_wake_rate": missed_wake_b},
        },
        "routed_default_overlap_rate": 0.5,
    }


def test_evaluate_gate_go():
    # B cost 降至 0.5×A，useful 非劣，hard 全过
    s = _gate_summary(cost_a=0.01, cost_b=0.005, ur_a=0.5, ur_b=0.6, n=5)
    g = evaluate_gate(s)
    assert g["decision"] == "GO"
    assert g["primary"]["cost_ok"] and g["primary"]["useful_ok"]


def test_evaluate_gate_no_go_on_hard_gate():
    # missed_wake_rate_B 超阈值 → NO_GO（即使 primary 过）
    s = _gate_summary(cost_a=0.01, cost_b=0.005, ur_a=0.5, ur_b=0.6, missed_wake_b=0.2)
    g = evaluate_gate(s)
    assert g["decision"] == "NO_GO"
    assert any("missed_wake" in r for r in g["reasons"])


def test_evaluate_gate_no_go_on_primary_both_fail():
    # B cost 没降 且 useful 退化 → NO_GO
    s = _gate_summary(cost_a=0.01, cost_b=0.02, ur_a=0.6, ur_b=0.4)
    g = evaluate_gate(s)
    assert g["decision"] == "NO_GO"


def test_evaluate_gate_inconclusive_small_n():
    s = _gate_summary(cost_a=0.01, cost_b=0.005, ur_a=0.5, ur_b=0.6, n=2)
    assert evaluate_gate(s)["decision"] == "INCONCLUSIVE"


def test_evaluate_gate_inconclusive_when_cost_ratio_undefined():
    # 单臂 useful=0 → cost_per_useful None → ratio 无定义
    s = _gate_summary(cost_a=0.01, cost_b=None, ur_a=0.5, ur_b=0.0)
    assert evaluate_gate(s)["decision"] == "INCONCLUSIVE"


def test_evaluate_gate_respects_custom_config():
    s = _gate_summary(cost_a=0.01, cost_b=0.005, ur_a=0.5, ur_b=0.6)
    # 默认 cost_ratio=0.85：B/A=0.5 达标 → GO
    assert evaluate_gate(s)["decision"] == "GO"
    # 更严的 cost_ratio=0.3：B/A=0.5 不达标 → primary cost 失败（useful 仍过，只 fail 一项）
    # roadmap 语义：primary 两项都不过才 NO_GO；只 fail 一项 → INCONCLUSIVE。关键是 config 生效、不再 GO。
    g = evaluate_gate(s, GateConfig(cost_ratio=0.3))
    assert g["decision"] != "GO"
    assert g["primary"]["cost_ok"] is False


# ---------- run_outcome_eval 端到端（mock）----------


class _FakeJudgeBackend:
    """judge backend：返回固定 X/Y JSON 评分。"""

    async def complete(self, *, model, system, user, temperature=0.1, max_tokens=2048, effort=None, endpoint_id=None):
        content = json.dumps({
            "X": {"useful": 3, "correct": 3, "harmful": 0, "missed_critical": 0, "overall": 4},
            "Y": {"useful": 2, "correct": 2, "harmful": 0, "missed_critical": 1, "overall": 3},
        })
        return ModelResponse(model=model, content=content, cost_usd=0.001)


class _FakeConsultEngine:
    """consult 引擎：忽略 panel/consultants，返回固定 ConsultReport。"""

    def __init__(self):
        self.backend = _FakeJudgeBackend()

    async def consult(self, request, *, panel, consultants, max_cost_usd=None, effort=None, consultation_id=None):
        from brainregion.core.consult.report import ConsultAdvice, ConsultReport
        return ConsultReport(
            consultation_id="c-test",
            summary="isolate the failing boundary",
            likely_causes=["race on DB reconnect"],
            next_experiments=["smallest reproduction under load"],
            solution_options=["add guard + fake-backend test"],
            risks=["external calls may leak secrets"],
            recommended_plan=["start with the smallest repro"],
            individual=[ConsultAdvice(id="c0", model="fake-model",
                                      consultant=(consultants[0] if consultants else "debugger"),
                                      summary="isolate the boundary")],
            usage={"total_tokens": 42, "cost_usd": 0.002},
            budget={"estimated_usd": 0.002, "jobs_run": 1, "jobs_total": 1, "exhausted": False},
        )


@pytest.mark.asyncio
async def test_run_outcome_eval_mock_end_to_end(monkeypatch, tmp_path):
    # 隔离 eval DB 到 tmp（避免污染本地 .brain-region/eval/eval.db）
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    # mock consult 引擎（含 judge backend）
    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: _FakeConsultEngine())

    tasks = [
        EvalTask(id="oc-mock-1", task_type="consult",
                 input={"problem": "Flaky test: intermittent race condition in CI, never local. Bug around DB reconnect.",
                        "why_stuck": "can't reproduce locally", "question": "likely races?"},
                 gold_regions=["debugging"]),
        EvalTask(id="oc-mock-2", task_type="consult",
                 input={"problem": "SQL injection and 越权 risk in a REST endpoint before launch.",
                        "question": "where are the real risks?"},
                 gold_regions=["security"]),
    ]
    judge_entries = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]

    records, judgements, entry, gate = await run_outcome_eval(
        tasks, DEFAULT_OUTCOME_VARIANTS, judge_entries, dd={},
        rubric_text="", rubric_hash="h", run_id="run-mock",
        max_cost_usd=0.5,
    )

    # 每 task × 2 variant = 4 records；每 task × 1 judge × 2 variant = 4 judgements
    assert len(records) == 4
    assert len(judgements) == 4
    # routed 的 consultants 来自 wake_gate 派生（debugging→[debugger], security→[challenge,critic]）
    routed_recs = [r for r in records if r.variant == "routed"]
    consultants_by_task = {r.task_id: set(r.wake["consultants"]) for r in routed_recs}
    assert "debugger" in consultants_by_task["oc-mock-1"]
    assert "challenge" in consultants_by_task["oc-mock-2"]
    # default 恒为静态默认面板
    assert all(r.wake["mapping_source"] == "default" for r in records if r.variant == "default")
    # wake_gate 只调一次/任务：两变体共用同一 woken（routed 与 default 的 woken 相同）
    by_task = {}
    for r in records:
        by_task.setdefault(r.task_id, {})[r.variant] = set(r.wake["woken"])
    for tid in by_task:
        assert by_task[tid]["default"] == by_task[tid]["routed"]
    # gate 结构完整
    assert gate["decision"] in {"GO", "NO_GO", "INCONCLUSIVE"}
    assert "primary" in gate and "hard_gates" in gate and gate["reasons"]
    # entry 入 ledger
    assert entry.run_id == "run-mock" and entry.n_tasks == 2
    assert entry.knowledge_hash == "" and entry.reviewer_hash == ""  # consult 无知识库/reviewer


@pytest.mark.asyncio
async def test_run_outcome_eval_judge_shuffle_is_deterministic(monkeypatch, tmp_path):
    """同 task_id 两次跑，盲打乱映射应一致（_seed(task_id) 确定性）。"""
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
    calls: list[str] = []

    class _CapturingBackend(_FakeJudgeBackend):
        async def complete(self, **kw):
            calls.append(kw["user"])
            return await super().complete(**kw)

    class _Engine:
        def __init__(self):
            self.backend = _CapturingBackend()

        async def consult(self, request, *, panel, consultants, **kw):
            from brainregion.core.consult.report import ConsultAdvice, ConsultReport
            return ConsultReport(summary="s", individual=[ConsultAdvice(id="c0", model="m", consultant="x")],
                                 usage={"cost_usd": 0.001})

    monkeypatch.setattr(outcome, "_build_consult_engine", lambda dd: _Engine())
    tasks = [EvalTask(id="oc-det", task_type="consult", input={"problem": "flaky race condition bug"},
                      gold_regions=["debugging"])]
    je = [{"label": "j", "model": "fake-judge", "endpoint_id": None}]
    await run_outcome_eval(tasks, DEFAULT_OUTCOME_VARIANTS, je, {}, "", "h", "run-1", max_cost_usd=0.5)
    await run_outcome_eval(tasks, DEFAULT_OUTCOME_VARIANTS, je, {}, "", "h", "run-2", max_cost_usd=0.5)
    # 两次的 judge user prompt（含打乱后的标签顺序）应完全一致
    assert calls[0] == calls[1]
