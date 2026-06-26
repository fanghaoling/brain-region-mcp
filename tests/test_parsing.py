"""ParseStage：JSON 提取 + schema 校验 + evidence 强制。"""
from __future__ import annotations

from design_review.core.stages.parse import _validate_finding, extract_json_object

_GOOD = {
    "dimension": "ecs_perf",
    "severity": "high",
    "title": "t",
    "evidence_quote": "q",
    "location": "l",
    "suggestion": "s",
    "confidence": 0.9,
}


def test_extract_json_block():
    assert extract_json_object('prefix ```json\n{"issues":[]}\n``` suffix') == {"issues": []}


def test_extract_raw():
    assert extract_json_object('{"issues":[]}') == {"issues": []}


def test_extract_invalid():
    assert extract_json_object("not json") is None


def test_validate_ok():
    assert _validate_finding(dict(_GOOD))


def test_validate_rejects_empty_evidence():
    f = dict(_GOOD)
    f["evidence_quote"] = ""
    assert not _validate_finding(f)


def test_validate_rejects_bad_severity():
    f = dict(_GOOD)
    f["severity"] = "critical"
    assert not _validate_finding(f)


def test_validate_rejects_missing_field():
    f = dict(_GOOD)
    del f["location"]
    assert not _validate_finding(f)
