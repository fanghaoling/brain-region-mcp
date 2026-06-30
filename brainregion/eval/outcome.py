"""Outcome eval：wake_gate 的 woken 真正驱动 consult 选 consultants，盲评 judge 量建议质量，
A(default 静态默认面板) vs B(routed wake 派生) 对照 cost_per_useful_advice（roadmap §8 v5.5 闸门主指标）。

level-1（LM-judge）。level-2 沙盒程序验收（eval_harness §10）不做——这里只做"judge 觉得建议好不好"
的近似；客观终极裁判留 level-2。eval-only：harness 内部建 consult 引擎 + 应用 woken→consultants，
**不改生产 server.consult_problem**（隔离，与现有 eval 一致；GO 后再谈接生产）。

复用（不重造）：wake_gate（core/wake/gate.py，免费、不调模型）/ ConsultEngine.consult /
aggregate_variant_stats（runner.py 共享统计）/ judge 盲评骨架（judge.judge_task_advice）/ store ledger。
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ..core.consult.report import ConsultReport
from ..core.consult import ConsultRequest
from ..core.regions import REGIONS_DIR
from ..core.wake.gate import wake_gate
from ..server import (
    _build_consult_engine,
    _normalize_panel,
    _resolve_consult_panel,
    _resolve_endpoints,
)
from . import store
from .judge import judge_task_advice
from .metadata import defaults_hash, git_sha
from .runner import aggregate_variant_stats
from .schema import EvalCaseRecord, EvalLedgerEntry

logger = logging.getLogger("brainregion.eval.outcome")

# region → consultants 映射（扶正 workflow.py:_build_actions + server._CONSULT_MODE_CONSULTANTS
# 已有的设计意图——目前它们只进 suggested_args advisory、从没到引擎）。
# memory/research/review 无天然 consult specialist → 空，由 _resolve_variant_consultants 回退默认。
REGION_CONSULTANTS: dict[str, list[str]] = {
    "debugging": ["debugger"],
    "performance": ["performance", "critic"],
    "security": ["challenge", "critic"],  # security 无同名 consultant；对应质疑/边界挑战视角
    "unity_ecs": ["unity_ecs"],
    "planning": ["architect", "test_designer", "critic"],
    "memory": [],
    "research": [],
    "review": [],  # review 走 review 管线，不量 consult
}

# 对齐 defaults.py consult_consultants（fallback 用，也是 A 臂的 default 面板）
_DEFAULT_CONSULTANTS = ["debugger", "architect", "critic"]

MappingSource = Literal["routed", "fallback", "default"]
Strategy = Literal["default", "routed", "wake_all"]


def consultants_for_regions(woken: list[str]) -> list[str]:
    """woken region 并集 → consultants（纯映射，去重保序）。

    无 specialist 的 region 贡献空；并集为空时返回 []。**不掺 fallback**——映射职责与回退策略分离
    （回退随 variant strategy 变，放 _resolve_variant_consultants）。
    """
    out: list[str] = []
    for rid in woken or []:
        for c in REGION_CONSULTANTS.get(rid, []):
            if c not in out:
                out.append(c)
    return out


def _resolve_variant_consultants(
    variant: "OutcomeVariant", woken: list[str], dd: dict,
) -> tuple[list[str], MappingSource]:
    """变体 → (consultants, mapping_source)。default=A 静态默认；routed=B wake 派生，空并集回退。"""
    defaults = list(dd.get("consult_consultants") or _DEFAULT_CONSULTANTS)
    if variant.strategy == "default":
        return defaults, "default"
    if variant.strategy == "routed":
        mapped = consultants_for_regions(woken)
        if mapped:
            return mapped, "routed"
        return defaults, "fallback"
    raise NotImplementedError("wake_all strategy 预留（roadmap §2，missed-wake ground truth）；follow-up")


@dataclass
class OutcomeVariant:
    name: str
    strategy: Strategy = "default"


DEFAULT_OUTCOME_VARIANTS = [
    OutcomeVariant("default", "default"),
    OutcomeVariant("routed", "routed"),
]


@dataclass
class GateConfig:
    """闸门阈值（默认对齐 eval_harness §6）。集中于此——实验/CI/论文改阈值不动代码。"""

    cost_ratio: float = 0.85              # primary: cost_per_useful_advice_B/A ≤ 此值
    missed_wake_rate_max: float = 0.10    # hard: missed_wake_rate_B ≤ 此值
    latency_p95_floor_ms: float = 6000.0  # hard: latency_p95_B 的绝对下限
    latency_ratio_max: float = 1.5        # hard: latency_p95_B ≤ max(ratio×A, floor)
    min_tasks: int = 4                    # 低于此 → INCONCLUSIVE（样本不足）


@dataclass
class OutcomeRecord:
    """单任务 × 单变体 的 consult 产出。独立 dataclass（不 mimic EvalCaseRecord），
    字段按职责命名；喂 store 时走 to_case_record() 薄 adapter。"""

    run_id: str
    task_id: str
    variant: str
    report_summary: dict = field(default_factory=dict)  # consult 产出：advice_count/failed_count
    wake: dict = field(default_factory=dict)            # strategy/mapping_source/consultants/woken/wake_metrics/shadow_promoted
    cost: dict = field(default_factory=dict)            # {inference_usd, estimated_usd, total_tokens}
    latency_ms: float = 0.0
    outputs_json: str = ""
    error: str = ""

    def to_case_record(self) -> EvalCaseRecord:
        """映射到 store.record_case 期望的 EvalCaseRecord shape（wake 并入 report_summary 持久化）。"""
        return EvalCaseRecord(
            run_id=self.run_id, task_id=self.task_id, variant=self.variant,
            report_summary={**self.report_summary, "wake": self.wake},
            retrieved_case_ids=[],
            cost=self.cost, latency_ms=self.latency_ms,
            outputs_json=self.outputs_json, error=self.error,
        )


def build_outcome_engines(dd: dict):
    """从 server._build_consult_engine 拿真 ConsultEngine + backend（judge 复用 backend）。单引擎。"""
    engine = _build_consult_engine(dd)
    return engine, engine.backend


def _build_request(task) -> ConsultRequest:
    inp = task.input or {}
    return ConsultRequest(
        problem=inp.get("problem", ""),
        context=inp.get("context", ""),
        files=inp.get("files") or {},
        logs=inp.get("logs", ""),
        attempts=inp.get("attempts") or [],
        goal=inp.get("goal", ""),
        current_attempt=inp.get("current_attempt", ""),
        why_stuck=inp.get("why_stuck", ""),
        question=inp.get("question", ""),
        desired_output=inp.get("desired_output", ""),
        constraints=inp.get("constraints") or [],
    )


def _task_context(task) -> str:
    """给 judge 的会诊问题摘要（problem/why_stuck/question/goal）——判相关性/missing_critical 的锚。"""
    inp = task.input or {}
    parts = []
    for k in ("problem", "why_stuck", "question", "goal"):
        v = inp.get(k)
        if v:
            parts.append(f"{k}: {v}")
    return "\n".join(parts)


def _run_wake_once(task, regions_dir) -> dict:
    """每 task 跑一次 wake_gate（所有 variant 共用），返回 woken + wake_metrics + shadow_promoted。"""
    inp = task.input or {}
    out = wake_gate(
        goal=inp.get("goal", ""),
        problem=inp.get("problem", ""),
        context=inp.get("context", ""),
        files=inp.get("files"),
        gold_regions=list(task.gold_regions or []),
        regions_dir=regions_dir,
    )
    ar = out.get("activated_regions") or {}
    return {
        "woken": list(ar.get("woken") or []),
        "wake_metrics": dict(out.get("wake_metrics") or {}),
        "shadow_promoted": int((out.get("trace") or {}).get("shadow_promoted") or 0),
    }


async def run_outcome_variant(
    engine, request: ConsultRequest, panel: list, consultants: list[str],
    variant: OutcomeVariant, mapping_source: MappingSource, shared_wake: dict,
    effort, max_cost_usd, run_id: str, task_id: str,
) -> OutcomeRecord:
    t0 = time.perf_counter()
    wake_info = {
        "strategy": variant.strategy,
        "mapping_source": mapping_source,
        "consultants": list(consultants),
        "woken": shared_wake.get("woken", []),
        "wake_metrics": shared_wake.get("wake_metrics", {}),
        "shadow_promoted": shared_wake.get("shadow_promoted", 0),
    }
    try:
        report: ConsultReport = await engine.consult(
            request, panel=panel, consultants=consultants,
            max_cost_usd=max_cost_usd, effort=effort,
        )
        dt = (time.perf_counter() - t0) * 1000.0
        return OutcomeRecord(
            run_id=run_id, task_id=task_id, variant=variant.name,
            report_summary={
                "advice_count": len(report.individual),
                "failed_count": len(report.failed_models),
            },
            wake=wake_info,
            cost={
                "inference_usd": (report.usage or {}).get("cost_usd"),
                "estimated_usd": (report.budget or {}).get("estimated_usd"),
                "total_tokens": (report.usage or {}).get("total_tokens", 0),
            },
            latency_ms=round(dt, 1),
            outputs_json=json.dumps(report.to_dict(), ensure_ascii=False, default=str),
        )
    except Exception as e:  # noqa: BLE001 — 单变体失败不阻断整 run
        dt = (time.perf_counter() - t0) * 1000.0
        logger.warning("run_outcome_variant 失败 task=%s variant=%s: %s", task_id, variant.name, e)
        return OutcomeRecord(
            run_id=run_id, task_id=task_id, variant=variant.name,
            wake=wake_info, latency_ms=round(dt, 1),
            error=f"{type(e).__name__}: {e}",
        )


def _routed_default_overlap(records: list, variants: list[OutcomeVariant]) -> float | None:
    """routed vs default 的专家集重合率（Jaccard 均值）——高则 B 退化成 A、无信号。"""
    names = {v.strategy: v.name for v in variants}
    d_name = names.get("default")
    r_name = names.get("routed")
    if not d_name or not r_name:
        return None
    by_task: dict[str, dict[str, set]] = {}
    for r in records:
        by_task.setdefault(r.task_id, {})[r.variant] = set(((r.wake or {}).get("consultants") or []))
    rates = []
    for vm in by_task.values():
        d, r = vm.get(d_name, set()), vm.get(r_name, set())
        union = d | r
        if union:
            rates.append(len(d & r) / len(union))
    return round(statistics.mean(rates), 3) if rates else None


def compute_outcome_summary(records: list, judgements: list, variants: list[OutcomeVariant]) -> dict:
    per_variant: dict[str, dict] = {}
    for v in variants:
        recs = [r for r in records if r.variant == v.name]
        jdgs = [j for j in judgements if j.variant == v.name]
        stats = aggregate_variant_stats(recs, jdgs)
        # missed_critical 计数（judge 强项；不进 aggregate_variant_stats 以免改 review 输出）
        stats["missed_critical_total"] = sum(
            int((j.scores or {}).get("missed_critical", 0) or 0) for j in jdgs
        )
        # wake 诊断（采纳外部建议：主指标外的诊断量 + 监控 B 是否退化）
        woken_counts = [len((r.wake or {}).get("woken") or []) for r in recs]
        expert_counts = [len((r.wake or {}).get("consultants") or []) for r in recs]
        shadow_total = sum(int((r.wake or {}).get("shadow_promoted") or 0) for r in recs)
        sources = [(r.wake or {}).get("mapping_source") for r in recs]
        fallback_n = sum(1 for s in sources if s == "fallback")
        stats["wake_stats"] = {
            "avg_regions_woken": round(statistics.mean(woken_counts), 2) if woken_counts else 0.0,
            "avg_experts_selected": round(statistics.mean(expert_counts), 2) if expert_counts else 0.0,
            "shadow_promoted_total": shadow_total,
            "fallback_rate": round(fallback_n / len(sources), 3) if sources else 0.0,
            "mapping_source_breakdown": {str(s): sources.count(s) for s in sorted(set(sources))},
        }
        # missed_wake_rate / false_wake_rate：路由层指标，A/B 同一 wake_gate 输出（每 task 算一次）
        missed_rates, false_rates = [], []
        for r in recs:
            wm = (r.wake or {}).get("wake_metrics") or {}
            missed = wm.get("missed") or []
            hit = wm.get("hit") or []
            false_wake = wm.get("false_wake") or []
            woken = (r.wake or {}).get("woken") or []
            gold_total = len(set(missed) | set(hit))  # hit/missed 互斥且并集=gold（wake_gate 对 gold 的集合运算）
            if gold_total:
                missed_rates.append(len(missed) / gold_total)
            if woken:
                false_rates.append(len(false_wake) / len(woken))
        stats["missed_wake_rate"] = round(statistics.mean(missed_rates), 3) if missed_rates else 0.0
        stats["false_wake_rate"] = round(statistics.mean(false_rates), 3) if false_rates else 0.0
        per_variant[v.name] = stats
    return {
        "per_variant": per_variant,
        "routed_default_overlap_rate": _routed_default_overlap(records, variants),
    }


def outcome_sanity(records: list, judgements: list, variants: list[OutcomeVariant]) -> dict:
    """errors=结构性失败；warnings=观察（cost None / 盲评解析失败 / B≡A 无信号）。"""
    errors: list[str] = []
    warnings: list[str] = []
    for r in records:
        if r.cost and r.cost.get("inference_usd") is None and not r.error:
            warnings.append(
                f"task={r.task_id} variant={r.variant} inference_usd=None（litellm 无单价，ISS-003）"
            )
        if r.error:
            warnings.append(f"task={r.task_id} variant={r.variant} 运行失败: {r.error}")
    parse_fails = [j for j in judgements if "parse" in (j.reason or "").lower() or not j.scores]
    if parse_fails:
        warnings.append(f"{len(parse_fails)} 条盲评解析失败/空（judge 输出非 JSON）")
    return {"errors": errors, "warnings": warnings}


def evaluate_gate(summary: dict, cfg: GateConfig | None = None) -> dict:
    """A(default) vs B(routed) 对照，对齐 eval_harness §6。返回 decision/primary/hard_gates/reasons/diagnostics。

    GO = primary + hard_gates 全过；NO_GO = 任一 hard_gate 破 或 primary 两项都不过；
    INCONCLUSIVE = 其余（n<min_tasks / 单臂 useful=0 使 cost ratio 无定义 / primary 只过一项）。
    不凭空造置信度小数——真 CI 需 bootstrap（ISS-008 样本量线，follow-up）。
    """
    cfg = cfg or GateConfig()
    pv = summary.get("per_variant", {})
    d = pv.get("default") or {}
    r = pv.get("routed") or {}
    n = int(r.get("n", 0))

    cost_a = d.get("cost_per_useful_advice")
    cost_b = r.get("cost_per_useful_advice")
    cost_ratio = (cost_b / cost_a) if (cost_a and cost_b) else None
    useful_rate_a = float(d.get("useful_advice_rate") or 0.0)
    useful_rate_b = float(r.get("useful_advice_rate") or 0.0)
    cost_ok = cost_ratio is not None and cost_ratio <= cfg.cost_ratio
    useful_ok = useful_rate_b >= useful_rate_a - 1e-9
    primary = {
        "cost_ratio": round(cost_ratio, 4) if cost_ratio is not None else None,
        "cost_ok": cost_ok,
        "useful_rate_delta": round(useful_rate_b - useful_rate_a, 3),
        "useful_ok": useful_ok,
    }

    missed_wake_b = float(r.get("missed_wake_rate") or 0.0)
    lat_a = float(d.get("latency_p95_ms") or 0.0)
    lat_b = float(r.get("latency_p95_ms") or 0.0)
    lat_limit = max(cfg.latency_ratio_max * lat_a, cfg.latency_p95_floor_ms)
    missed_crit_delta = int(r.get("missed_critical_total") or 0) - int(d.get("missed_critical_total") or 0)
    hard = {
        "missed_wake_rate_B": missed_wake_b,
        "missed_wake_ok": missed_wake_b <= cfg.missed_wake_rate_max,
        "latency_p95_B": lat_b,
        "latency_limit": round(lat_limit, 1),
        "latency_ok": lat_b <= lat_limit,
        "missed_critical_delta": missed_crit_delta,
        "missed_critical_ok": missed_crit_delta <= 0,
    }

    hard_all_ok = all(v for k, v in hard.items() if k.endswith("_ok"))
    primary_ok = cost_ok and useful_ok
    primary_both_fail = (not cost_ok) and (not useful_ok)

    if n < cfg.min_tasks or cost_ratio is None:
        decision = "INCONCLUSIVE"
    elif (not hard_all_ok) or primary_both_fail:
        decision = "NO_GO"
    elif primary_ok:
        decision = "GO"
    else:
        decision = "INCONCLUSIVE"

    reasons: list[str] = []
    if n < cfg.min_tasks:
        reasons.append(f"样本不足 n={n} < min_tasks={cfg.min_tasks}")
    if cost_ratio is None:
        reasons.append("cost_per_useful_advice 无定义（某臂 useful=0）→ cost ratio 不可比")
    if cost_ratio is not None and not cost_ok:
        reasons.append(f"B 成本未降至 {cfg.cost_ratio}×A（cost_ratio={cost_ratio:.4f}）")
    if not useful_ok:
        reasons.append(f"B useful_rate 未非劣（Δ={useful_rate_b - useful_rate_a:+.3f}）")
    if not hard["missed_wake_ok"]:
        reasons.append(f"missed_wake_rate_B={missed_wake_b:.3f} > {cfg.missed_wake_rate_max}（路由漏唤醒）")
    if not hard["latency_ok"]:
        reasons.append(f"latency_p95_B={lat_b}ms > limit {lat_limit:.1f}ms")
    if not hard["missed_critical_ok"]:
        reasons.append(f"B 新增关键漏建议 missed_critical_delta={missed_crit_delta}")
    if decision == "GO":
        reasons.append("primary 满足 且 hard_gates 全过")

    diagnostics = {
        "n": n,
        "cost_ratio": primary["cost_ratio"],
        "useful_rate_delta": primary["useful_rate_delta"],
        "primary_margin": round(cfg.cost_ratio - (cost_ratio or 0), 4) if cost_ratio is not None else None,
        "routed_default_overlap_rate": summary.get("routed_default_overlap_rate"),
    }
    return {
        "decision": decision,
        "primary": primary,
        "hard_gates": hard,
        "reasons": reasons,
        "diagnostics": diagnostics,
    }


async def run_outcome_eval(
    tasks: list, variants: list[OutcomeVariant], judge_entries: list[dict],
    dd: dict, rubric_text: str, rubric_hash: str, run_id: str,
    effort=None, max_cost_usd: float = 1.0, panel_override: list | None = None,
    *, regions_dir=REGIONS_DIR,
) -> tuple[list, list, EvalLedgerEntry, dict]:
    """主编排（仿 runner.run_eval，但量 consult 而非 review）。

    每 task：wake_gate 跑一次（所有 variant 共用）→ 每 variant 解析 consultants（纯 dict）+
    run_outcome_variant + record_case → 每 judge judge_task_advice 盲评 + record_judgement →
    compute_outcome_summary → evaluate_gate → record_run。
    """
    engine, backend = build_outcome_engines(dd)
    endpoint_ids = set((_resolve_endpoints(dd.get("endpoints") or {}) or {}).keys())
    records: list[OutcomeRecord] = []
    judgements: list = []

    for task in tasks:
        request = _build_request(task)
        panel_src, _ = _resolve_consult_panel(panel_override, dd)
        panel = _normalize_panel(panel_src, endpoint_ids, dd.get("endpoints"))
        shared_wake = _run_wake_once(task, regions_dir)  # 每 task 一次，variant 共用
        variant_outputs: dict[str, str] = {}
        for v in variants:
            consultants, mapping_source = _resolve_variant_consultants(v, shared_wake["woken"], dd)
            rec = await run_outcome_variant(
                engine, request, panel, consultants, v, mapping_source, shared_wake,
                effort, max_cost_usd, run_id, task.id,
            )
            records.append(rec)
            store.record_case(rec.to_case_record())
            variant_outputs[v.name] = rec.outputs_json
        for je in judge_entries:
            try:
                js = await judge_task_advice(
                    backend, je, rubric_text, rubric_hash, run_id, task.id,
                    variant_outputs, _task_context(task),
                )
                for j in js:
                    store.record_judgement(j)
                    judgements.append(j)
            except Exception as e:  # noqa: BLE001
                logger.warning("judge_task_advice 失败 task=%s judge=%s: %s", task.id, je.get("label"), e)

    summary = compute_outcome_summary(records, judgements, variants)
    summary["sanity"] = outcome_sanity(records, judgements, variants)
    gate = evaluate_gate(summary)
    summary["gate"] = gate

    entry = EvalLedgerEntry(
        run_id=run_id,
        date=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        git_sha=git_sha(),
        variants=[v.name for v in variants],
        judge_models=[je["model"] for je in judge_entries],
        rubric_hash=rubric_hash,
        knowledge_hash="",  # consult 无知识库检索
        reviewer_hash="",   # consult 无 reviewer
        defaults_hash=defaults_hash(dd),
        n_tasks=len(tasks),
        summary=summary,
    )
    store.record_run(entry)
    return records, judgements, entry, gate
