"""Experience Memory：第一个 ContextProvider（memory 脑区扶正，Phase2A）。

append-only 结构化经验记忆 + 关键词召回。store 走 SQLite（复用 reviews_db 的
brain_region_reviews.db）；provider 把 ExperienceEvent 包成 ContextBlock(framing=data)，
经 consult 注入主 context（config memory_inject 门控，默认关）。

import 无 DB 副作用（_connect 只在 accessor 内调）。
"""
from . import store
from .base import ExperienceEvent
from .provider import MemoryProvider

__all__ = ["ExperienceEvent", "MemoryProvider", "store"]
