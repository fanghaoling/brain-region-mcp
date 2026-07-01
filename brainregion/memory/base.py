"""ExperienceEvent：结构化经验记忆的最小 append-only 模型（Phase2A）。

故意最小（7 字段）：confidence / valid_until / wrong_mark / superseded / anti_triggers
全部 defer——confidence 应来自 outcome eval（像 reliability 飞轮），非人工写；治理等
有真实数据后作独立增量。triggers 用于关键词召回（search 不调模型，roadmap §6）。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class ExperienceEvent:
    """一条经验记忆（append-only，最小字段）。"""

    id: str
    region: str = ""  # scope：debugging/security/...；空=全局
    summary: str = ""
    details: str = ""
    triggers: list[str] = field(default_factory=list)  # 关键词召回用
    created_at: str = ""  # ISO datetime；空则 store 写入时填
    source: str = ""  # 追溯（如 consult-xxx / 手工 record）

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
