"""calibrate_advice 单测：mock judge_task_advice（绕开盲打乱），测方向/tie/wilson/方向感知。"""
from __future__ import annotations

import pytest

from brainregion.eval import calibrate as calibrate_mod
from brainregion.eval.calibrate import calibrate_advice, load_gold_advice, summarize_advice
from brainregion.eval.schema import BlindJudgement

_GOLD = "brainregion/eval/gold/advice_calibration.yaml"
_JE = [{"label": "j", "model": "m", "endpoint_id": None}]


class _FakeJudge:
    """替身 judge_task_advice：直接返回 good/bad 的固定判分（绕开盲打乱）。"""

    def __init__(self, good_u=3, bad_u=1, good_mc=0, bad_mc=1, good_harmful=0, bad_harmful=1):
        self.good_u = good_u
        self.bad_u = bad_u
        self.good_mc = good_mc
        self.bad_mc = bad_mc
        self.good_harmful = good_harmful
        self.bad_harmful = bad_harmful

    async def __call__(self, backend, je, rubric_text, rubric_hash, run_id, task_id, variant_outputs, task_context=""):
        return [
            BlindJudgement(run_id=run_id, task_id=task_id, judge_id=je["label"], judge_model=je["model"],
                           rubric_hash=rubric_hash, variant="good", blind=True,
                           scores={"useful": self.good_u, "overall": 4, "missed_critical": self.good_mc,
                                   "harmful": self.good_harmful}),
            BlindJudgement(run_id=run_id, task_id=task_id, judge_id=je["label"], judge_model=je["model"],
                           rubric_hash=rubric_hash, variant="bad", blind=True,
                           scores={"useful": self.bad_u, "overall": 2, "missed_critical": self.bad_mc,
                                   "harmful": self.bad_harmful}),
        ]


def test_load_gold_advice():
    gold = load_gold_advice(_GOLD)
    assert len(gold) >= 8
    assert "summary" in gold[0]["good_advice"]
    assert "likely_causes" in gold[0]["bad_advice"]


@pytest.mark.asyncio
async def test_calibrate_advice_good_gt_bad(monkeypatch):
    monkeypatch.setattr(calibrate_mod, "judge_task_advice", _FakeJudge())
    gold = load_gold_advice(_GOLD)
    rows = await calibrate_advice(gold[:4], backend=None, judge_entries=_JE,
                                  rubric_text="", rubric_hash="h", run_id="r")
    s = summarize_advice(rows)
    # useful/overall：good>bad 全部 → agreement 1.0（higher_better）
    assert s["agreement_rate"] == 1.0
    # missed_critical/harmful：good(0)<bad(1) → correct_direction_rate 1.0（lower_better diagnostic）
    assert s["penalty_metrics"]["missed_critical"]["correct_direction_rate"] == 1.0
    assert s["penalty_metrics"]["harmful"]["correct_direction_rate"] == 1.0
    # 方向标签正确
    assert all(r["direction"] == "higher_better" for r in rows if r["metric"] in ("useful", "overall"))
    assert all(r["direction"] == "lower_better" for r in rows if r["metric"] in ("missed_critical", "harmful"))


@pytest.mark.asyncio
async def test_calibrate_advice_tie_excluded_from_agreement(monkeypatch):
    # good.useful == bad.useful → useful 全 tie；overall 仍 good(4)>bad(2)。tie 仅计入 useful 行。
    monkeypatch.setattr(calibrate_mod, "judge_task_advice", _FakeJudge(good_u=2, bad_u=2))
    gold = load_gold_advice(_GOLD)
    rows = await calibrate_advice(gold[:4], None, _JE, "", "h", "r")
    s = summarize_advice(rows)
    # agree_rows = useful(4) + overall(4) = 8；useful 全 tie(4)、overall 全 agreed(4)
    assert s["tie_rate"] == 0.5
    assert s["total"] == 4               # 只有 overall 4 对非-tie
    assert s["agreement_rate"] == 1.0    # overall 全 agreed；useful tie 不计入分母


@pytest.mark.asyncio
async def test_calibrate_advice_wilson_small_n_not_calibrated(monkeypatch):
    # n=4 对、全对 → agreement=1.0，但 Wilson 下界（小 n）< 0.7 门槛 → 不硬放行（吸收 I4）
    monkeypatch.setattr(calibrate_mod, "judge_task_advice", _FakeJudge())
    gold = load_gold_advice(_GOLD)
    rows = await calibrate_advice(gold[:4], None, _JE, "", "h", "r")
    s = summarize_advice(rows)
    assert s["agreement_rate"] == 1.0
    assert s["wilson_lower"] < 0.7
    assert s["calibrated"] is False  # 下界没过门槛 → smoke 级，不硬放行
