"""Memory governance:生命周期状态 + 过期谓词(v6 stage 1)。

单一真相源:provider 的 retrieve 过滤 + Inspector 的 Health 都调本模块。未来 confidence/decay/
quarantine/anti_triggers = 扩本文件(provider 永远只调 ``filter_events`` / ``is_recallable``)。

**status 状态转换图(文档,不代码强制)**:status 是人工纠错 → **自由可逆**(误标必须能回滚,即
「可恢复」测试点)。任意两状态可互转(active↔pending↔wrong↔superseded,以及任意→active 回到 live)。
**不上 transition guard** —— guard 会堵死 wrong→active / superseded→active 这类「标错了改回来」的修正。

stage 1 只做**手动生命周期 + 时间过期**;auto-confidence(outcome-eval reliability 飞轮)/ decay /
anti_triggers / quarantine 全 defer(见 base.py docstring:confidence 应来自 eval,非人工)。
"""
from __future__ import annotations

import time

# status 常量(代码比较用常量,防 "supersded" 拼写静默漏滤)。
ACTIVE = "active"            # live,默认(向后兼容:既有行无 status → active)
PENDING = "pending"          # 待核实(照召回,Inspector 标黄)
SUPERSEDED = "superseded"    # 被新记忆覆盖(superseded_by 指向新 id;退召回)
WRONG = "wrong"              # 错(退召回)

# 退出召回(被治理剔除)的状态。pending/active 照召回。
_INACTIVE_STATUSES = (SUPERSEDED, WRONG)


def is_expired(valid_until_ts: int, now_ts: int | None = None) -> bool:
    """valid_until_ts(Unix 秒)> 0 且已过当前时间 → True。0/None/未来 = 未过期(永不滤)。"""
    if not valid_until_ts:
        return False
    now = int(now_ts) if now_ts is not None else int(time.time())
    return int(valid_until_ts) < now


def is_recallable(event, now_ts: int | None = None) -> bool:
    """active/pending 且未过期 → 召回;superseded/wrong/expired → 剔除。

    缺省容错:无 status 属性 → 视作 active(向后兼容老 ExperienceEvent)。
    """
    status = getattr(event, "status", ACTIVE) or ACTIVE
    if status in _INACTIVE_STATUSES:
        return False
    return not is_expired(getattr(event, "valid_until_ts", 0) or 0, now_ts)


def filter_events(events, now_ts: int | None = None):
    """批量治理过滤 + 统计。返回 ``(recallable_list, stats)``。

    stats = ``{candidates_after_governance, removed_superseded, removed_wrong, removed_expired}``。
    provider 只 ``meta.update(stats)``;未来加 confidence/quarantine 维度 = 扩本函数,provider 不动。
    """
    removed_superseded = removed_wrong = removed_expired = 0
    out = []
    for e in events:
        status = getattr(e, "status", ACTIVE) or ACTIVE
        if status == SUPERSEDED:
            removed_superseded += 1
            continue
        if status == WRONG:
            removed_wrong += 1
            continue
        if is_expired(getattr(e, "valid_until_ts", 0) or 0, now_ts):
            removed_expired += 1
            continue
        out.append(e)
    stats = {
        "candidates_after_governance": len(out),
        "removed_superseded": removed_superseded,
        "removed_wrong": removed_wrong,
        "removed_expired": removed_expired,
    }
    return out, stats
