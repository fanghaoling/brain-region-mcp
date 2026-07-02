"""HtmlRenderer:BrainSnapshot → 自包含静态 HTML dashboard(可视化 Phase 1 唯一 renderer)。

- **自包含**:内联 <style>,无外部请求、无 <script src>、零 JS(纯静态最安全)。
- **XSS 安全**:所有插值经 html.escape()(stdlib)——memory summary / region 名 / explain / reasons
  全是用户或内部内容,等同 core/context.py 的 data-fencing 思路。
- region-centric:hero 是 region snapshots 网格;默认(无查询)无 Activation 段。
- **界面文案统一中文**(方便调试);数据值(gate 决策 GO/NO_GO、run_id、status 枚举)保留原样。
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

from ..inspector.render import status_symbol
from .snapshot import BrainSnapshot

_DOCTYPE = "<!DOCTYPE html>"

# ── 中文标签映射(数据枚举原样存,显示时本地化;CSS class 仍用原始枚举值)──────────
_GOV_STATUS_LABELS = {"active": "活跃", "pending": "待核实",
                      "superseded": "已覆盖", "wrong": "错误"}
_STAGE_LABELS = {"wake": "唤醒", "retrieve": "检索", "memory": "记忆",
                 "consult": "会诊", "judge": "评审"}
_STAGE_STATUS_LABELS = {"SUCCESS": "成功", "FAILED": "失败", "SKIPPED": "跳过",
                        "UNKNOWN": "未知", "NOT_INSTRUMENTED": "未埋点"}


def _esc(v) -> str:
    """HTML 转义任意值(防 XSS)。None → 空串。"""
    return html.escape("" if v is None else str(v), quote=True)


def _gov_label(status: str) -> str:
    return _GOV_STATUS_LABELS.get(status or "", status or "")


def _stage_label(name: str) -> str:
    return _STAGE_LABELS.get(name or "", name or "")


def _stage_status_label(name: str) -> str:
    return _STAGE_STATUS_LABELS.get(name or "", name or "")


def _fmt_ts(iso: str) -> str:
    """ISO 时间戳 → 可读(失败原样返回)。"""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # noqa: BLE001
        return iso


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; background: #f5f6f8; color: #1f2329; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 24px 20px 60px; }
header { margin-bottom: 20px; }
header h1 { margin: 0 0 4px; font-size: 22px; }
header .meta { color: #8a9099; font-size: 13px; }
section { background: #fff; border: 1px solid #e8eaed; border-radius: 10px;
          padding: 16px 18px; margin-bottom: 16px; }
section h2 { margin: 0 0 12px; font-size: 15px; color: #4a5159;
             text-transform: uppercase; letter-spacing: .04em; }
/* KPI 行 */
.kpis { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.kpi { flex: 1 1 200px; background: #fff; border: 1px solid #e8eaed; border-left: 4px solid #c9ced6;
       border-radius: 10px; padding: 14px 16px; }
.kpi .label { font-size: 12px; color: #8a9099; text-transform: uppercase; letter-spacing: .04em; }
.kpi .value { font-size: 22px; font-weight: 600; margin: 4px 0 2px; }
.kpi .hint { font-size: 12px; color: #8a9099; }
.kpi.ok { border-left-color: #2ea44f; }
.kpi.warn { border-left-color: #d9a300; }
.kpi.bad { border-left-color: #d73a49; }
/* region 网格(hero)*/
.regions { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.region { border: 1px solid #e8eaed; border-radius: 8px; padding: 12px; background: #fafbfc; }
.region .name { font-weight: 600; font-size: 14px; margin-bottom: 6px; word-break: break-all; }
.region .nums { font-size: 12px; color: #5a626c; }
.region .nums b { color: #1f2329; }
.badge { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
         font-weight: 600; margin-left: 6px; vertical-align: middle; }
.badge.woke { background: #e6f4ea; color: #1a7f37; }
.badge.quiet { background: #f0f1f3; color: #8a9099; }
/* 通用表 */
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eef0f2; }
th { color: #8a9099; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
td.mono, th.mono { font-family: ui-monospace, "SFMono-Regular", Consolas, monospace; font-size: 12px; }
/* 状态色 */
.dec { font-weight: 600; }
.dec-go, .dec-OK { color: #1a7f37; }
.dec-no_go, .dec-FAIL, .dec-fail { color: #cf222e; }
.dec-inconclusive { color: #b08800; }
.dec-neutral { color: #8a9099; }
.chip { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 8px; margin-right: 4px; }
.chip.active { background: #e6f4ea; color: #1a7f37; }
.chip.pending { background: #fff8c5; color: #7d5e00; }
.chip.superseded, .chip.wrong { background: #ffebe9; color: #cf222e; }
.muted { color: #8a9099; }
.timeline td.sym { text-align: center; font-size: 14px; }
.empty { color: #8a9099; font-size: 13px; padding: 8px 0; }
.explain { font-size: 13px; line-height: 1.6; color: #4a5159; }
"""


class HtmlRenderer:
    """BrainSnapshot → 自包含 HTML 字符串。"""

    def render(self, snapshot: BrainSnapshot) -> str:
        parts = [
            _DOCTYPE,
            "<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            f"<title>{_esc('BrainRegion 快照')}</title>",
            f"<style>{_CSS}</style></head><body><div class=\"wrap\">",
            self._header(snapshot),
            self._kpis(snapshot.kpis),
            self._regions(snapshot.regions, snapshot.has_query),
            self._memory(snapshot.memory),
            self._runs(snapshot.runs),
            self._calibration(snapshot.calibration),
        ]
        if snapshot.activation is not None:
            parts.append(self._activation(snapshot.activation))
        parts.append("</div></body></html>")
        return "".join(parts)

    # ── 段 ────────────────────────────────────────────────────────────────────
    def _header(self, s: BrainSnapshot) -> str:
        return (
            "<header><h1>🧠 BrainRegion 脑状态快照</h1>"
            f"<div class=\"meta\">生成于 {_esc(_fmt_ts(s.generated_at))}"
            f" · brainregion {_esc(s.brainregion_version)}"
            f" · snapshot schema {_esc(s.schema_version)}</div></header>"
        )

    def _kpis(self, kpis) -> str:
        cards = []
        for k in kpis:
            cards.append(
                f"<div class=\"kpi {_esc(k.status)}\"><div class=\"label\">{_esc(k.label)}</div>"
                f"<div class=\"value\">{_esc(k.value)}</div>"
                f"<div class=\"hint\">{_esc(k.hint)}</div></div>"
            )
        return f"<div class=\"kpis\">{''.join(cards)}</div>"

    def _regions(self, regions, has_query: bool) -> str:
        if not regions:
            return "<section><h2>脑区</h2><div class=\"empty\">暂无脑区</div></section>"
        cards = []
        for r in regions:
            inactive = max(0, r.total - r.recallable)
            if r.woke == "yes":
                badge = "<span class=\"badge woke\">已唤醒</span>"
            elif has_query:
                badge = "<span class=\"badge quiet\">—</span>"
            else:
                badge = ""
            nums = f"<b>{_esc(r.total)}</b> 条 · <b>{_esc(r.recallable)}</b> 可召回"
            if inactive:
                nums += f" · <span class=\"muted\">{_esc(inactive)} 失效</span>"
            cards.append(f"<div class=\"region\"><div class=\"name\">{_esc(r.region)}{badge}</div>"
                         f"<div class=\"nums\">{nums}</div></div>")
        return f"<section><h2>脑区</h2><div class=\"regions\">{''.join(cards)}</div></section>"

    def _memory(self, memory: dict) -> str:
        if not memory:
            return ""
        health = memory.get("health") or {}
        by_status = health.get("by_status") or {}
        status_chips = "".join(
            f"<span class=\"chip {_esc(s)}\">{_esc(_gov_label(s))} {_esc(n)}</span>"
            for s, n in sorted(by_status.items()) if n
        )
        recallable = health.get("recallable", 0)
        non_recallable = health.get("non_recallable", 0)
        expired = health.get("expired_count", 0)
        parts = [
            "<section><h2>记忆健康</h2>",
            f"<div class=\"explain\">{_esc(recallable)} 可召回 · {_esc(non_recallable)} 失效"
            f" · {_esc(expired)} 过期 · 共 {_esc(memory.get('total', 0))}</div>",
            f"<div style=\"margin:10px 0\">{status_chips}</div>" if status_chips else "",
        ]
        preview = memory.get("preview") or []
        if preview:
            parts.append("<table><tbody>")
            for e in preview:
                st = e.get("status", "active")
                parts.append(
                    "<tr>"
                    f"<td class=\"mono\">{_esc(e.get('region') or '(global)')}</td>"
                    f"<td>{_esc(e.get('summary'))}</td>"
                    f"<td><span class=\"chip {_esc(st)}\">{_esc(_gov_label(st))}</span></td>"
                    f"<td class=\"muted\">{_esc(e.get('age_days'))} 天</td>"
                    "</tr>"
                )
            parts.append("</tbody></table>")
        parts.append("</section>")
        return "".join(parts)

    def _runs(self, runs: dict) -> str:
        if not runs:
            return "<section><h2>最近 Run</h2><div class=\"empty\">无 Run</div></section>"
        if "gate" in runs or "timeline" in runs:  # run_id 单 run 详情
            return self._run_detail(runs)
        return self._run_history(runs)

    def _run_history(self, runs: dict) -> str:
        history = runs.get("history") or []
        if not history:
            return "<section><h2>最近 Run</h2><div class=\"empty\">无 Run</div></section>"
        rows = []
        for r in history:
            rows.append(
                "<tr>"
                f"<td class=\"mono\">{_esc(r.get('run_id'))}</td>"
                f"<td>{_esc(_fmt_ts(r.get('date')))}</td>"
                f"<td><span class=\"dec {_dec_class(r.get('status'))}\">{_esc(r.get('status'))}</span></td>"
                f"<td>{_esc(_fmt_cost(r.get('cost_usd')))}</td>"
                f"<td>{_esc(r.get('n_tasks'))}</td>"
                "</tr>"
            )
        return ("<section><h2>最近 Run</h2><table><thead><tr>"
                "<th>Run</th><th>时间</th><th>闸门</th><th>成本</th><th>任务</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></section>")

    def _run_detail(self, runs: dict) -> str:
        run = runs.get("run") or {}
        gate = runs.get("gate") or {}
        decision = gate.get("decision")
        parts = [
            "<section><h2>Run 详情</h2>",
            f"<div class=\"explain\"><b>{_esc(run.get('run_id'))}</b>"
            f" · {_esc(run.get('n_tasks'))} 个任务 · {_esc(_fmt_ts(run.get('date')))}</div>",
            f"<div style=\"margin:8px 0\">闸门：<span class=\"dec {_dec_class(decision)}\">{_esc(decision or '—')}</span></div>",
        ]
        timeline = runs.get("timeline") or []
        if timeline:
            stage_names = list((timeline[0].get("stages") or {}).keys())
            head = "".join(f"<th class=\"sym\">{_esc(_stage_label(s))}</th>" for s in stage_names)
            body = []
            for row in timeline:
                syms = row.get("symbols") or {}
                stages = row.get("stages") or {}
                cells = "".join(
                    f"<td class=\"sym\" title=\"{_esc(_stage_status_label(stages.get(s, '')))}\">"
                    f"{_esc(syms.get(s, '?'))}</td>"
                    for s in stage_names
                )
                body.append(
                    f"<tr><td class=\"mono\">{_esc(row.get('task_id'))}</td>"
                    f"<td class=\"mono\">{_esc(row.get('variant'))}</td>{cells}</tr>"
                )
            parts.append(
                "<table class=\"timeline\"><thead><tr><th>任务</th><th>变体</th>"
                f"{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
            )
            # 图例
            legend = " ".join(
                f"{_esc(status_symbol(s))}={_esc(_stage_status_label(s))}"
                for s in ("SUCCESS", "FAILED", "SKIPPED", "UNKNOWN", "NOT_INSTRUMENTED")
            )
            parts.append(f"<div class=\"muted\" style=\"margin-top:6px;font-size:12px\">{legend}</div>")
        parts.append("</section>")
        return "".join(parts)

    def _calibration(self, cal: dict) -> str:
        if not cal:
            return ""
        blocked = cal.get("am_i_blocked")
        badge = ("<span class=\"badge\" style=\"background:#ffebe9;color:#cf222e\">阻塞</span>"
                 if blocked else "<span class=\"badge woke\">通过</span>")
        parts = [
            f"<section><h2>校准 {badge}</h2>",
            f"<div class=\"explain\">{_esc(cal.get('passed_count', 0))}/{_esc(cal.get('n', 0))} 个 judge 已校准</div>",
        ]
        not_passed = cal.get("not_passed") or []
        if not_passed:
            parts.append("<table><thead><tr><th>judge</th><th>模型</th><th>Wilson 下界</th><th>阈值</th></tr></thead><tbody>")
            for r in not_passed:
                parts.append(
                    "<tr>"
                    f"<td class=\"mono\">{_esc(r.get('judge_id'))}</td>"
                    f"<td>{_esc(r.get('judge_model'))}</td>"
                    f"<td>{_esc(_fmt_num(r.get('wilson_lower')))}</td>"
                    f"<td>{_esc(_fmt_num(r.get('threshold')))}</td>"
                    "</tr>"
                )
            parts.append("</tbody></table>")
        parts.append("</section>")
        return "".join(parts)

    def _activation(self, act: dict) -> str:
        metrics = act.get("wake_metrics") or {}
        woken = act.get("woken") or []
        rows = [
            ("唤醒", ", ".join(woken)),
            ("命中", ", ".join(metrics.get("hit") or [])),
            ("漏唤醒", ", ".join(metrics.get("missed") or [])),
            ("误唤醒", ", ".join(metrics.get("false_wake") or [])),
            ("评分状态", metrics.get("metrics_status")),
        ]
        body = "".join(
            f"<tr><td>{_esc(label)}</td><td>{_esc(val) or '—'}</td></tr>" for label, val in rows
        )
        return (
            "<section><h2>激活</h2>"
            f"<div class=\"explain\">{_esc(act.get('explain'))}</div>"
            f"<table><thead><tr><th>指标</th><th>值</th></tr></thead><tbody>{body}</tbody></table></section>"
        )


def _dec_class(dec) -> str:
    """gate/run decision → CSS class。"""
    d = (dec or "").upper()
    if d == "GO" or d == "OK":
        return "dec-go"
    if "NO_GO" in d or "FAIL" in d:
        return "dec-no_go"
    if "INCONCLUSIVE" in d:
        return "dec-inconclusive"
    return "dec-neutral"


def _fmt_cost(v) -> str:
    if v is None:
        return "—"
    try:
        return f"${float(v):.4f}"
    except Exception:  # noqa: BLE001
        return str(v)


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.3f}"
    except Exception:  # noqa: BLE001
        return str(v)
