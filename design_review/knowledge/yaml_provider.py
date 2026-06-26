"""YamlKnowledgeProvider：从 yaml 文件加载案例 + 关键词 retrieve + 压缩渲染。

案例文件格式（每文件一个 list）：
    - id: ECS-BURST-001
      title: "..."
      version: {entities: ">=1.4,<1.5"}
      triggers: [Burst, BC1064, ISystem]
      category: ecs_perf
      bad_pattern: "..."
      recommended_pattern: "..."
      source: "MEMORY.md#..."   # 给人追溯，不进 prompt
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .base import Case, version_matches

logger = logging.getLogger("design_review.knowledge.yaml")


def extract_keyword_hits(text: str, cases: list[Case]) -> set[str]:
    """triggers 词库（所有 case 的 triggers 去重）中，哪些在 text 出现（大小写不敏感）。"""
    triggers = {t for c in cases for t in c.triggers if isinstance(t, str) and t}
    low = (text or "").lower()
    return {t for t in triggers if t.lower() in low}


def render_for_prompt(cases: list[Case]) -> str:
    """压缩渲染：只 title/bad_pattern/recommended_pattern/category + id（丢 source/history，省 token）。"""
    if not cases:
        return "(无命中的历史踩坑案例。)"
    lines: list[str] = []
    for c in cases:
        lines.append(f"- [{c.id}] ({c.category}) {c.title}")
        lines.append(f"  反模式: {c.bad_pattern}")
        lines.append(f"  正解: {c.recommended_pattern}")
    return "\n".join(lines)


class YamlKnowledgeProvider:
    """从目录下所有 *.yaml 加载案例（每文件一个 list）。"""

    def __init__(self, knowledge_dir: str | Path):
        self.dir = Path(knowledge_dir)
        self._cases: list[Case] = self._load()

    def _load(self) -> list[Case]:
        if not self.dir.exists():
            logger.warning("knowledge 目录不存在: %s", self.dir)
            return []
        cases: list[Case] = []
        for p in sorted(self.dir.glob("*.yaml")):
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
            except Exception as e:  # noqa: BLE001
                logger.warning("knowledge 文件解析失败 %s: %s", p, e)
                continue
            if not isinstance(data, list):
                logger.warning("knowledge 文件非 list %s，跳过", p)
                continue
            for item in data:
                cases.append(
                    Case(
                        id=item.get("id", ""),
                        title=item.get("title", ""),
                        triggers=[t for t in (item.get("triggers") or []) if isinstance(t, str)],
                        category=item.get("category", ""),
                        bad_pattern=item.get("bad_pattern", ""),
                        recommended_pattern=item.get("recommended_pattern", ""),
                        version=dict(item.get("version") or {}),
                        source=item.get("source", ""),
                    )
                )
        logger.info("knowledge 加载 %d 条案例 from %s", len(cases), self.dir)
        return cases

    def list_cases(self) -> list[Case]:
        return list(self._cases)

    def add_case(self, case: Case) -> None:
        self._cases.append(case)

    def retrieve(
        self, text: str, project_version: dict[str, str] | None = None, top_k: int = 5
    ) -> list[Case]:
        pv = project_version or {}
        candidates = [c for c in self._cases if version_matches(c.version, pv)]
        hits = extract_keyword_hits(text, candidates)
        scored = [
            (c, len({t for t in c.triggers if t in hits})) for c in candidates
        ]
        scored = [(c, s) for c, s in scored if s > 0]
        scored.sort(key=lambda x: (-x[1], x[0].id))
        return [c for c, _ in scored[:top_k]]
