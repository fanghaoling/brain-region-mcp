"""judge 校准：用 gold 对（已知好/坏 review）测盲评 judge 能否稳定把好的排在前面。

复用 judge.judge_task（盲/打乱/脱敏/解析），只加一层"对 gold 排序 → 算 agreement"。
gold 权威性 = 标注者的标准：seed 是 Claude 标注的 MVP，**要改成符合你真实 review 标准**（改 YAML 即可）。

agreement < threshold → judge/rubric 没校准好，先修尺子再谈 A/B。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from .judge import judge_task, judge_task_advice

logger = logging.getLogger("brainregion.eval.calibrate")


def _to_report(findings: list) -> dict:
    """compact finding 列表 → judge.desensitize 能读的 report dict（放 consensus）。"""
    return {
        "consensus": [
            {
                "canonical_title": f.get("title", ""),
                "severity": f.get("severity", ""),
                "evidence_quote": f.get("evidence", ""),
                "suggestion": f.get("suggestion", ""),
                "case_ref": None,
            }
            for f in (findings or [])
        ],
        "majority": [],
        "individual": {},
    }


def load_gold(path: str) -> list[dict]:
    """gold YAML → [{id, failure_mode, task, good_report, bad_report, note}]。

    每条：id / failure_mode / task(待审查内容摘要) / good[finding] / bad[finding] / note。
    """
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.glob("*.yaml"))
    pairs: list[dict] = []
    for fp in files:
        data = yaml.safe_load(fp.read_text(encoding="utf-8"))
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            pairs.append({
                "id": item.get("id", fp.stem),
                "failure_mode": item.get("failure_mode", ""),
                "task": item.get("task", ""),
                "good_report": _to_report(item.get("good") or []),
                "bad_report": _to_report(item.get("bad") or []),
                "note": item.get("note", ""),
            })
    return pairs


async def calibrate(
    gold_pairs: list[dict], backend, judge_entries: list[dict],
    rubric_text: str, rubric_hash: str, run_id: str,
    metrics: tuple = ("overall", "useful"),
) -> list[dict]:
    """对每对 gold 跑盲评 judge，返回每 (pair×judge×metric) 一行的结果。"""
    rows: list[dict] = []
    for pair in gold_pairs:
        variant_outputs = {
            "good": json.dumps(pair["good_report"], ensure_ascii=False),
            "bad": json.dumps(pair["bad_report"], ensure_ascii=False),
        }
        for je in judge_entries:
            try:
                jds = await judge_task(
                    backend, je, rubric_text, rubric_hash, run_id, pair["id"],
                    variant_outputs, task_context=pair["task"],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("calibrate judge 失败 pair=%s: %s", pair["id"], e)
                continue
            by_v = {j.variant: j for j in jds}
            gj, bj = by_v.get("good"), by_v.get("bad")
            for m in metrics:
                gv = float((gj.scores or {}).get(m, 0) or 0) if gj else 0.0
                bv = float((bj.scores or {}).get(m, 0) or 0) if bj else 0.0
                rows.append({
                    "pair": pair["id"], "failure_mode": pair["failure_mode"],
                    "judge": je["label"], "metric": m, "good": gv, "bad": bv,
                    "agreed": gv > bv, "tied": gv == bv, "note": pair["note"],
                })
    return rows


def summarize(rows: list[dict], threshold: float = 0.7) -> dict:
    """agreement 汇总：总体 + 按 failure_mode + 按 metric + 错判清单。"""
    total = len(rows)
    agreed = sum(1 for r in rows if r["agreed"])
    rate = agreed / total if total else 0.0
    by_fm: dict[str, dict] = {}
    by_metric: dict[str, dict] = {}
    for r in rows:
        for bucket, key in ((by_fm, r["failure_mode"]), (by_metric, r["metric"])):
            b = bucket.setdefault(key, {"n": 0, "agreed": 0})
            b["n"] += 1
            b["agreed"] += 1 if r["agreed"] else 0
    wrong = [f"{r['pair']}({r['metric']}, good={r['good']} bad={r['bad']})" for r in rows if not r["agreed"]]
    return {
        "agreement_rate": round(rate, 3),
        "agreed": agreed,
        "total": total,
        "calibrated": rate >= threshold,
        "threshold": threshold,
        "per_failure_mode": {k: round(v["agreed"] / v["n"], 3) for k, v in by_fm.items() if v["n"]},
        "per_metric": {k: round(v["agreed"] / v["n"], 3) for k, v in by_metric.items() if v["n"]},
        "wrong_pairs": wrong,
    }


# ===== advice judge 校准（outcome eval 用）=====


def load_gold_advice(path: str) -> list[dict]:
    """advice gold YAML → [{id, failure_mode, task, good_advice(dict), bad_advice(dict), note}]。

    good/bad 是 ConsultReport 聚合层 advice dict（summary/likely_causes/...），不是 finding 列表。
    """
    p = Path(path)
    files = [p] if p.is_file() else sorted(p.glob("*.yaml"))
    pairs: list[dict] = []
    for fp in files:
        data = yaml.safe_load(fp.read_text(encoding="utf-8"))
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            pairs.append({
                "id": item.get("id", fp.stem),
                "failure_mode": item.get("failure_mode", ""),
                "task": item.get("task", ""),
                "good_advice": item.get("good") or {},
                "bad_advice": item.get("bad") or {},
                "note": item.get("note", ""),
            })
    return pairs


async def calibrate_advice(
    gold_pairs: list[dict], backend, judge_entries: list[dict],
    rubric_text: str, rubric_hash: str, run_id: str,
    metrics: tuple = ("useful", "overall"),
    penalty_metrics: tuple = ("missed_critical", "harmful"),
) -> list[dict]:
    """对每对 advice gold 跑 judge_task_advice，返回每 (pair×judge×metric) 一行。

    - metrics (useful/overall)：higher_better，agreement = good>bad。
    - penalty_metrics (missed_critical/harmful)：lower_better，good<bad 算 correct（diagnostic，不入主 agreement）。
    tie (good==bad) 单列标记（吸收 C3）。
    """
    rows: list[dict] = []
    for pair in gold_pairs:
        variant_outputs = {
            "good": json.dumps(pair["good_advice"], ensure_ascii=False),
            "bad": json.dumps(pair["bad_advice"], ensure_ascii=False),
        }
        for je in judge_entries:
            try:
                jds = await judge_task_advice(
                    backend, je, rubric_text, rubric_hash, run_id, pair["id"],
                    variant_outputs, task_context=pair["task"],
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("calibrate_advice judge 失败 pair=%s: %s", pair["id"], e)
                continue
            by_v = {j.variant: j for j in jds}
            gj, bj = by_v.get("good"), by_v.get("bad")
            for m in metrics:  # higher_better
                gv = float((gj.scores or {}).get(m, 0) or 0) if gj else 0.0
                bv = float((bj.scores or {}).get(m, 0) or 0) if bj else 0.0
                rows.append({"pair": pair["id"], "failure_mode": pair["failure_mode"],
                             "judge": je["label"], "metric": m, "good": gv, "bad": bv,
                             "direction": "higher_better", "agreed": gv > bv, "tied": gv == bv, "note": pair["note"]})
            for m in penalty_metrics:  # lower_better
                gv = float((gj.scores or {}).get(m, 0) or 0) if gj else 0.0
                bv = float((bj.scores or {}).get(m, 0) or 0) if bj else 0.0
                rows.append({"pair": pair["id"], "failure_mode": pair["failure_mode"],
                             "judge": je["label"], "metric": m, "good": gv, "bad": bv,
                             "direction": "lower_better", "agreed": gv < bv, "tied": gv == bv, "note": pair["note"]})
    return rows


def summarize_advice(rows: list[dict], threshold: float = 0.7, confidence: float = 0.95) -> dict:
    """advice 校准汇总：agreement 只算 higher_better metrics（useful/overall），tie 单列；Wilson 下界过门槛
    才 calibrated（吸收 I4：n=10 是 smoke，下界<门槛→不硬放行）。penalty metrics 作 diagnostic。"""
    from .stats import wilson_lower

    agree_rows = [r for r in rows if r["direction"] == "higher_better"]
    penalty_rows = [r for r in rows if r["direction"] == "lower_better"]
    non_tie = [r for r in agree_rows if not r["tied"]]
    agreed = sum(1 for r in non_tie if r["agreed"])
    total = len(non_tie)
    rate = agreed / total if total else 0.0
    ties = sum(1 for r in agree_rows if r["tied"])

    by_metric: dict[str, dict] = {}
    for r in agree_rows:
        b = by_metric.setdefault(r["metric"], {"n": 0, "agreed": 0, "tied": 0})
        b["n"] += 1
        if r["tied"]:
            b["tied"] += 1
        elif r["agreed"]:
            b["agreed"] += 1
    penalty_by_metric: dict[str, dict] = {}
    for r in penalty_rows:
        b = penalty_by_metric.setdefault(r["metric"], {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += 1 if r["agreed"] else 0  # good<bad = correct direction

    wilson = wilson_lower(agreed, total, confidence)
    wrong = [f"{r['pair']}({r['metric']}, good={r['good']} bad={r['bad']})"
             for r in non_tie if not r["agreed"]]
    return {
        "agreement_rate": round(rate, 3),
        "agreed": agreed,
        "total": total,
        "tie_rate": round(ties / len(agree_rows), 3) if agree_rows else 0.0,
        "wilson_lower": round(wilson, 3),
        "calibrated": wilson >= threshold,  # 下界过门槛（吸收 I4：小样本不硬放行）
        "threshold": threshold,
        "confidence": confidence,
        "per_metric": {
            k: {"agreement": round(v["agreed"] / max(1, v["n"] - v["tied"]), 3) if (v["n"] - v["tied"]) else 0.0,
                "tie_rate": round(v["tied"] / v["n"], 3) if v["n"] else 0.0}
            for k, v in by_metric.items()
        },
        "penalty_metrics": {
            k: {"correct_direction_rate": round(v["correct"] / v["n"], 3) if v["n"] else 0.0}
            for k, v in penalty_by_metric.items()
        },
        "wrong_pairs": wrong,
        "note": "n=10 是 smoke；Wilson 下界过门槛才初步可用，硬放行需扩 gold（吸收 I4）",
    }
