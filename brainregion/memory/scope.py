"""MemoryScope:memory 召回的 region 范围值对象(Phase A:region 维度)。

selective context 的第一块(roadmap):一个项目的记忆不该 bleed 进另一个项目。wake 已决定该任务
激活哪些 region → MemoryScope 把召回 scope 到这些 region(∪ 全局/未标注)。

单一真相源:MemoryProvider 的 records/DB 两路都用 ``matches()`` 过滤,不重复逻辑。
``scope=None``(在 provider/store 签名里,裸 None)= unscoped 全部(向后兼容)。

未来加维度(project/tag/hierarchy)= 扩 ``matches()`` + 加字段,签名不变。今天只 regions + include_global。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryScope:
    """Memory 召回范围。regions 空 + include_global=True = 只召回全局(region="")。"""

    regions: frozenset[str] = frozenset()
    include_global: bool = True  # region="" 全局记忆始终通过(软过滤:无项目归属,总可能相关)

    def matches(self, region: str) -> bool:
        """该 region 的记忆是否落在 scope 内(单一过滤真相源)。"""
        r = region or ""
        if r in self.regions:
            return True
        return self.include_global and r == ""
