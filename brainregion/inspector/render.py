"""Inspector 共享渲染原语：5 态 status、StageDescriptor、符号映射。

5 态（GPT 三①，review_plan dogfood 采纳）：区分 SKIPPED（未执行，非失败）与 FAILED，
区分 NOT_INSTRUMENTED（该 run 无此追踪点，历史数据）与 UNKNOWN（执行了但结果不明）。
timeline 由 StageDescriptor 列表驱动（`for stage in STAGES`）——未来加 ProjectState/Git/Knowledge
只追加一个 descriptor，不改 render/run（GPT 二）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# ── 5 态 status ──────────────────────────────────────────────────────────────
SUCCESS = "SUCCESS"                          # 执行且成功
FAILED = "FAILED"                            # 执行但失败 / 违反预期
SKIPPED = "SKIPPED"                          # 未执行（上游没路由到，非失败）
UNKNOWN = "UNKNOWN"                          # 执行了但结果不明
NOT_INSTRUMENTED = "NOT_INSTRUMENTED"        # 该 run 无此追踪点（字段缺失，历史数据）

_STATUS_SYMBOL = {
    SUCCESS: "✓",
    FAILED: "✗",
    SKIPPED: "⏭",
    UNKNOWN: "?",
    NOT_INSTRUMENTED: "N/A",
}


def status_symbol(status: str) -> str:
    """status → 单符号（未知 status 归 ?,防御）。"""
    return _STATUS_SYMBOL.get(status, "?")


@dataclass
class StageDescriptor:
    """一个 timeline 阶段：name + 从 (case_dict, judgements_for_task) 推 5 态。

    - case_dict：一行 eval_case_record（report_summary/retrieved_case_ids/cost/error/outputs_json 已解析）。
    - judgements_for_task：该 (task, variant) 的全部盲评行（list[dict]，scores 已解析）。

    返回 5 态之一。未来新增 stage = 追加一个 StageDescriptor 到 STAGES，不改 render/run。
    """

    name: str
    status_fn: Callable[[dict, list[dict]], str]
