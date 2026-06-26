"""pytest 公共配置。

所有测试用临时 UNITY_PROJECT_ROOT（不写真项目的 db/config）。MCP 工具/网络层不依赖。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _unity_root_env(tmp_path, monkeypatch):
    """把 UNITY_PROJECT_ROOT 指向临时目录，避免 reviews_db/defaults 写真项目。"""
    monkeypatch.setenv("UNITY_PROJECT_ROOT", str(tmp_path))
