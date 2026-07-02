"""inspect_activation：调 wake_gate（无模型，read-only）+ 扁平化 debug 视图 + plain-language explain。

wake_gate 已验源码为纯读（models_called=False、无 record_*/memory_store.append、_reverse_wake_hook 是 stub）。
本 view 只重排它的返回 + 写一句「发生了什么/漏了谁」。绝不调模型、绝不写。
"""
from __future__ import annotations

from ..core.wake import wake_gate


def inspect_activation(
    *,
    goal: str = "",
    problem: str = "",
    context: str = "",
    files: dict | None = None,
    gold_regions: list[str] | None = None,
    escalate_confidence: float = 0.5,
    shadow_wake_threshold: float | None = None,
    top_k: int = 3,
    sentinel: bool = True,
    shadow_top_n: int = 3,
    regions_dir: str | None = None,
) -> dict:
    """重跑 wake_gate（cheap，无模型）并扁平化。regions_dir=None → 用内置默认。"""
    kw = dict(
        goal=goal, problem=problem, context=context, files=files or {},
        gold_regions=gold_regions, escalate_confidence=escalate_confidence,
        shadow_wake_threshold=shadow_wake_threshold, top_k=top_k, sentinel=sentinel,
        shadow_top_n=shadow_top_n,
    )
    if regions_dir is not None:
        kw["regions_dir"] = regions_dir  # None 会覆盖默认 → 只在非 None 时传
    result = wake_gate(**kw)
    return _summarize_activation(result)


def _summarize_activation(result: dict) -> dict:
    act = result.get("activated_regions") or {}
    metrics = result.get("wake_metrics") or {}
    trace = result.get("trace") or {}

    woken = act.get("woken") or []
    retrieved = [
        {"id": r.get("id"), "score": r.get("score"), "source": r.get("source")}
        for r in (act.get("retrieved") or [])
    ]
    shadow = [
        {"id": s.get("id"), "confidence": s.get("confidence"),
         "promoted": s.get("promoted"), "reason": s.get("reason")}
        for s in (act.get("shadow") or [])
    ]

    missed = metrics.get("missed") or []
    false_wake = metrics.get("false_wake") or []
    scored = metrics.get("metrics_status") == "scored"

    bits: list[str] = []
    if woken:
        bits.append(f"唤醒 {len(woken)} 个 region：{', '.join(woken)}")
    else:
        bits.append("没有唤醒任何 region（输入太短或无 trigger 命中）")
    if scored:
        if missed:
            bits.append(f"⚠️ 漏唤醒 {len(missed)} 个 gold region：{', '.join(missed)}（该醒没醒）")
        else:
            bits.append("✅ gold region 全部命中（无漏唤醒）")
        if false_wake:
            bits.append(f"误唤醒 {len(false_wake)} 个非 gold：{', '.join(false_wake)}")
    else:
        bits.append("未给 gold_regions → unscored（无法判漏唤醒，绝不伪装 0-漏）")
    sp = trace.get("shadow_promoted") or 0
    if sp:
        bits.append(f"shadow fallback 提升 {sp} 个 near-threshold region")
    sh = trace.get("sentinel_hits") or []
    if sh:
        bits.append(f"sentinel 兜底唤醒 {len(sh)} 个：{', '.join(s.get('region', '') for s in sh)}")

    return {
        "woken": woken,
        "retrieved": retrieved,
        "escalated": act.get("escalated") or [],
        "shadow": shadow,
        "reasons": act.get("reasons") or {},
        "confidence": act.get("confidence") or {},
        "wake_metrics": metrics,
        "trace": {
            "strategy": trace.get("strategy"),
            "escalate_confidence": trace.get("escalate_confidence"),
            "shadow_wake_threshold": trace.get("shadow_wake_threshold"),
            "shadow_promoted": sp,
            "sentinel_hits": sh,
            "models_called": trace.get("models_called"),
        },
        "suggested_actions": result.get("suggested_actions") or [],
        "explain": " | ".join(bits),
    }
