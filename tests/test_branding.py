from __future__ import annotations

from pathlib import Path


def test_ping_reports_brain_region_name():
    from brain_region.server import ping

    got = ping()

    assert got["name"] == "brain_region"
    assert got["legacy_name"] == "design_review"


def test_pyproject_exposes_brain_region_commands_and_legacy_aliases():
    text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'name = "brain-region-mcp"' in text
    assert 'brain-region-mcp = "brain_region.server:main"' in text
    assert 'brain-region = "brain_region.cli:main"' in text
    assert 'design-review-mcp = "brain_region.server:main"' in text
    assert 'design-review = "brain_region.cli:main"' in text
