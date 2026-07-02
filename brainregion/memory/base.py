"""ExperienceEvent：结构化经验记忆模型（Phase2A 起，v6 stage 1 加治理字段）。

triggers 用于关键词召回（search 不调模型，roadmap §6）。v6 stage 1 加 4 个**手动生命周期**字段
（status/valid_until_ts/superseded_by/last_reviewed，见 memory/governance.py）。**auto-confidence**
仍 defer——它应来自 outcome eval（reliability 飞轮），非人工写；届时与 last_reviewed（人工）语义不撞。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class ExperienceEvent:
    """一条经验记忆。Identity(id/region/...) + Lifecycle(治理状态,v6 stage 1)。"""

    id: str
    region: str = ""  # scope：debugging/security/...；空=全局
    summary: str = ""
    details: str = ""
    triggers: list[str] = field(default_factory=list)  # 关键词召回用
    created_at: str = ""  # ISO datetime；空则 store 写入时填
    source: str = ""  # 追溯（如 consult-xxx / 手工 record）
    # ── v6 stage 1 governance（手动生命周期 + 时间过期）──
    status: str = "active"      # active|pending|superseded|wrong（见 governance.py 常量）
    valid_until_ts: int = 0     # Unix 秒；0=永不过期；过期退召回
    superseded_by: str = ""     # 替代者 id（status=superseded 时的链接）
    last_reviewed: str = ""     # ISO；status→active 时 store 自动 stamp

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
