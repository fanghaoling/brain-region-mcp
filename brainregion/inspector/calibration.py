"""inspect_calibration：judge 校准状态（全量或单 judge）+ am-I-blocked。

outcome gate 出 GO/NO_GO 前强制每 judge 有 pass 校准 artifact（_calibration_ok）；本 view 一眼看出
哪些 judge 未达标（会被 gate 拒）。复用 eval.store.fetch_calibrations（read，参数化）。
"""
from __future__ import annotations

from ..eval import store as eval_store


def inspect_calibration(*, judge_id: str | None = None) -> dict:
    rows = eval_store.fetch_calibrations(judge_id=judge_id)
    not_passed = [r for r in rows if not r.get("passed")]
    return {
        "n": len(rows),
        "judge_filter": judge_id,
        "calibrations": [_row_summary(r) for r in rows],
        "passed_count": len(rows) - len(not_passed),
        "not_passed": [
            {
                "judge_id": r.get("judge_id"),
                "judge_model": r.get("judge_model"),
                "wilson_lower": r.get("wilson_lower"),
                "threshold": r.get("threshold"),
                "date": r.get("date"),
            }
            for r in not_passed
        ],
        "am_i_blocked": bool(not_passed),  # 任一 judge 未 pass → outcome gate 会卡
    }


def _row_summary(r: dict) -> dict:
    return {
        "judge_id": r.get("judge_id"),
        "judge_model": r.get("judge_model"),
        "agreement_rate": r.get("agreement_rate"),
        "wilson_lower": r.get("wilson_lower"),
        "threshold": r.get("threshold"),
        "passed": r.get("passed"),
        "date": r.get("date"),
        "rubric_hash": r.get("rubric_hash"),
        "summary": r.get("summary"),
    }
