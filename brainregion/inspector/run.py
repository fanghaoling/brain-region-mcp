"""inspect_run：读历史 eval run（read-only）。

- run_id 省略 → 最近 N run 历史表（status/cost/tasks/judges/date，免 copy run_id）。
- run_id 给定 → 读**已存 summary**（authoritative，不重算 per_variant/gate/sanity/memory_diagnostics，
  避免双 aggregation 漂移——GPT 二-一）+ per-task **阶段状态时间线**（5 态，StageDescriptor 驱动）。
顶部 provenance（git_sha/version/summary_schema——GPT 三②）。

timeline 的 per-task 5 态从 raw cases + judgements 派生（net-new debug 投影，非 summary 重算）。
"""
from __future__ import annotations

from .render import FAILED, NOT_INSTRUMENTED, SKIPPED, SUCCESS, UNKNOWN, StageDescriptor, status_symbol
from ..eval import store as eval_store


# ── per-task stage status_fn（每个返回 5 态之一；case 已解析，jt=该 task×variant 的盲评行）──────────────
def _wake_status(case: dict, _jt: list[dict]) -> str:
    wake = (case.get("report_summary") or {}).get("wake")
    if wake is None:
        return NOT_INSTRUMENTED
    if isinstance(wake, dict):
        woken = wake.get("woken") or []
    elif isinstance(wake, (list, tuple)):
        woken = list(wake)
    else:
        woken = []
    return SUCCESS if woken else SKIPPED  # 没 wake 任何 region = SKIPPED（非失败）


def _retrieve_status(case: dict, _jt: list[dict]) -> str:
    ids = case.get("retrieved_case_ids")
    v = (case.get("variant") or "").lower()
    if ids is None:
        return NOT_INSTRUMENTED
    if not v.startswith("retrieve_"):
        # consult/outcome 路径不检索知识库 → 该阶段未执行
        return SKIPPED
    n = len(ids)
    if "off" in v:  # retrieve_off 期望 0
        return SUCCESS if n == 0 else FAILED
    if n > 0:
        return SUCCESS
    return FAILED if case.get("error") else UNKNOWN


def _memory_status(case: dict, _jt: list[dict]) -> str:
    mem = (case.get("report_summary") or {}).get("memory")
    if mem is None:
        # 现状：per-task memory 追踪未落（run-level 在 summary.memory_instrumentation）
        return NOT_INSTRUMENTED
    retrieved = int((mem or {}).get("retrieved") or 0)
    injected = int((mem or {}).get("injected") or 0)
    if injected > 0:
        return SUCCESS
    if retrieved == 0:
        return SKIPPED  # wake 没召回 → 没执行注入（非失败）
    return FAILED  # retrieved>0 但 injected=0 → 被 budget 截光


def _consult_status(case: dict, _jt: list[dict]) -> str:
    if case.get("error"):
        return FAILED
    return SUCCESS if case.get("outputs_json") else FAILED


def _judge_status(_case: dict, jt: list[dict]) -> str:
    if not jt:
        return SKIPPED  # 该 task×variant 没被评
    fails = [j for j in jt if "parse" in (j.get("reason") or "").lower() or not j.get("scores")]
    if len(fails) == len(jt):
        return FAILED
    if fails:
        return UNKNOWN
    return SUCCESS


# 列表驱动：未来加 ProjectState/Git/Knowledge = 追加一个 StageDescriptor，不改本表消费者（GPT 二）。
STAGES = [
    StageDescriptor("wake", _wake_status),
    StageDescriptor("retrieve", _retrieve_status),
    StageDescriptor("memory", _memory_status),
    StageDescriptor("consult", _consult_status),
    StageDescriptor("judge", _judge_status),
]


def inspect_run(*, run_id: str | None = None, history_limit: int = 20) -> dict:
    if not run_id:
        return _run_history(history_limit)
    run = eval_store.fetch_run(run_id)
    if not run:
        return {"error": f"run not found: {run_id}"}
    summary = run.get("summary") or {}
    cases = eval_store.fetch_cases(run_id)
    judgements = eval_store.fetch_judgements(run_id)
    return {
        "run": {
            "run_id": run_id,
            "date": run.get("date"),
            "git_sha": run.get("git_sha"),
            "n_tasks": run.get("n_tasks"),
            "variants": run.get("variants"),
            "judge_models": run.get("judge_models"),
        },
        "provenance": summary.get("__provenance__") or {
            "brainregion_version": "unknown", "summary_schema": "unknown",
        },
        "gate": summary.get("gate"),
        "summary": summary,  # 权威 aggregate 原样（per_variant/sanity/memory_diagnostics/memory_instrumentation）
        "timeline": _build_timeline(cases, judgements),
    }


def _run_history(limit: int) -> dict:
    runs = eval_store.list_runs(limit)
    rows = []
    for r in runs:
        summary = r.get("summary") or {}
        rows.append({
            "run_id": r.get("run_id"),
            "date": r.get("date"),
            "git_sha": r.get("git_sha"),
            "n_tasks": r.get("n_tasks"),
            "variants": r.get("variants"),
            "judge_models": r.get("judge_models"),
            "status": _run_status(summary),
            "cost_usd": _run_cost(summary),
        })
    return {"history": rows, "n": len(rows)}


def _run_status(summary: dict) -> str:
    gate = summary.get("gate") or {}
    if gate.get("decision"):
        return str(gate["decision"])
    sanity = summary.get("sanity") or {}
    if sanity.get("errors"):
        return "FAIL"
    return "OK"


def _run_cost(summary: dict) -> float | None:
    pv = summary.get("per_variant") or {}
    try:
        return round(sum(float((v or {}).get("inference_cost_usd") or 0) for v in pv.values()), 6)
    except Exception:  # noqa: BLE001
        return None


def _build_timeline(cases: list[dict], judgements: list[dict]) -> list[dict]:
    """逐 (task × variant) 一行 5 态阶段表。judgements 按 (task_id, variant) 聚合喂 _judge_status。"""
    jmap: dict[tuple, list[dict]] = {}
    for j in judgements:
        jmap.setdefault((j.get("task_id"), j.get("variant")), []).append(j)

    rows = []
    for c in cases:
        jt = jmap.get((c.get("task_id"), c.get("variant")), [])
        statuses: dict[str, str] = {}
        symbols: dict[str, str] = {}
        for stage in STAGES:
            s = stage.status_fn(c, jt)
            statuses[stage.name] = s
            symbols[stage.name] = status_symbol(s)
        rows.append({
            "task_id": c.get("task_id"),
            "variant": c.get("variant"),
            "stages": statuses,
            "symbols": symbols,
            "error": c.get("error") or "",
        })
    return rows
