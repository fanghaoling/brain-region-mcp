"""评测 ledger 存储：SQLite 主 + JSONL 导出（对齐 reviews_db 模式 + GPT Strong Rec 1）。

SQLite 让尺子成为活资产：可 SELECT 聚合（on vs off 跨 run、某变体 p95 延迟）。
JSONL 仅 --export 人读/备份。append-only：每次 run 新 run_id，不覆盖历史。

表：eval_runs（每 run 一行）/ eval_case_records（每 task×variant 一行）/ eval_blind_judgements
（每 task×judge×variant 一行，per-judge）。
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
from pathlib import Path

from brainregion import __version__ as _BR_VERSION

logger = logging.getLogger("brainregion.eval.store")

# summary blob 的 schema 版本（Inspector 据此解读，免写 if-has-field 散判）。只增不改；
# summary 结构发生不兼容变更时 +1。旧 run 无 __provenance__ → Inspector 显示 unknown。
SUMMARY_SCHEMA_VERSION = 1


def _db_path() -> Path:
    root = os.environ.get("UNITY_PROJECT_ROOT", ".")
    p = Path(root) / ".brain-region" / "eval" / "eval.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:  # noqa: BLE001
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_runs (
            run_id TEXT PRIMARY KEY,
            date TEXT,
            git_sha TEXT,
            variants TEXT,
            judge_models TEXT,
            rubric_hash TEXT,
            knowledge_hash TEXT,
            reviewer_hash TEXT,
            defaults_hash TEXT,
            n_tasks INTEGER,
            summary TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_case_records (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            variant TEXT NOT NULL,
            report_summary TEXT,
            retrieved_case_ids TEXT,
            cost TEXT,
            latency_ms REAL,
            outputs_json TEXT,
            error TEXT,
            PRIMARY KEY (run_id, task_id, variant)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_blind_judgements (
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            judge_id TEXT NOT NULL,
            judge_model TEXT NOT NULL,
            rubric_hash TEXT,
            variant TEXT NOT NULL,
            blind INTEGER,
            scores TEXT,
            reason TEXT,
            judge_cost_usd REAL,
            PRIMARY KEY (run_id, task_id, judge_id, variant)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_calibrations (
            judge_id TEXT NOT NULL,
            judge_model TEXT NOT NULL,
            rubric_hash TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            gold_version TEXT,
            agreement_rate REAL,
            wilson_lower REAL,
            threshold REAL,
            passed INTEGER,
            run_id TEXT,
            date TEXT,
            summary TEXT,
            PRIMARY KEY (judge_id, judge_model, rubric_hash, prompt_hash, gold_version)
        )
        """
    )
    conn.commit()
    return conn


def _connect_readonly() -> sqlite3.Connection:
    """只读连接（Inspector 专用）：复用 _connect 建表（幂等）+ WAL + busy_timeout，再开 query_only。

    query_only=ON 后任何写（INSERT/UPDATE/DELETE/ALTER）抛 sqlite3.OperationalError —— 即便
    Inspector 代码误调写也拒，defense-in-depth。
    """
    conn = _connect()
    conn.execute("PRAGMA query_only=ON")
    return conn


def _loads(s) -> dict:
    try:
        v = json.loads(s) if s else {}
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _loads_list(s) -> list:
    try:
        v = json.loads(s) if s else []
        return v if isinstance(v, list) else []
    except Exception:  # noqa: BLE001
        return []


def record_calibration(rec, summary: dict) -> None:
    """落 advice judge 校准 artifact（五元组 upsert）。"""
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO eval_calibrations(judge_id,judge_model,rubric_hash,prompt_hash,gold_version,"
            "agreement_rate,wilson_lower,threshold,passed,run_id,date,summary) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(judge_id,judge_model,rubric_hash,prompt_hash,gold_version) DO UPDATE SET "
            "  agreement_rate=excluded.agreement_rate,wilson_lower=excluded.wilson_lower,"
            "  threshold=excluded.threshold,passed=excluded.passed,run_id=excluded.run_id,"
            "  date=excluded.date,summary=excluded.summary",
            (rec.judge_id, rec.judge_model, rec.rubric_hash, rec.prompt_hash, rec.gold_version,
             rec.agreement_rate, rec.wilson_lower, rec.threshold, 1 if rec.passed else 0,
             rec.run_id, rec.date, json.dumps(summary, ensure_ascii=False, default=str)),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_calibration 失败: %s", e)


def lookup_calibration(judge_id, judge_model, rubric_hash, prompt_hash, gold_version=None) -> dict | None:
    """查最新匹配的校准 artifact（outcome gate 出判定前强制验证）。无 → None（→ CALIBRATION_REQUIRED）。

    gold_version=None → 不按 gold 过滤（gate 匹配键 = judge+rubric+prompt；gold 版本仅记录供追溯，
    outcome 命令不知 gold 版本，故按三键取最新）。传具体值则精确匹配。
    """
    conn = _connect()
    if gold_version is None:
        row = conn.execute(
            "SELECT * FROM eval_calibrations WHERE judge_id=? AND judge_model=? AND rubric_hash=? "
            "AND prompt_hash=? ORDER BY date DESC LIMIT 1",
            (judge_id, judge_model, rubric_hash, prompt_hash),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM eval_calibrations WHERE judge_id=? AND judge_model=? AND rubric_hash=? "
            "AND prompt_hash=? AND gold_version=? ORDER BY date DESC LIMIT 1",
            (judge_id, judge_model, rubric_hash, prompt_hash, gold_version),
        ).fetchone()
    return dict(row) if row else None


def _as_json(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), ensure_ascii=False, default=str)


def record_run(entry) -> None:
    try:
        conn = _connect()
        # Provenance stamp：每条落库 run 的 summary 标 __provenance__（版本 + schema）。单点 chokepoint
        # （review/outcome 两路都过 record_run）。setdefault 不覆盖 caller 预填；本地 copy 不污染入参。
        summary = dict(entry.summary or {})
        summary.setdefault("__provenance__", _provenance())
        conn.execute(
            "INSERT INTO eval_runs(run_id,date,git_sha,variants,judge_models,rubric_hash,"
            "knowledge_hash,reviewer_hash,defaults_hash,n_tasks,summary) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "  date=excluded.date,git_sha=excluded.git_sha,variants=excluded.variants,"
            "  judge_models=excluded.judge_models,rubric_hash=excluded.rubric_hash,"
            "  knowledge_hash=excluded.knowledge_hash,reviewer_hash=excluded.reviewer_hash,"
            "  defaults_hash=excluded.defaults_hash,n_tasks=excluded.n_tasks,summary=excluded.summary",
            (
                entry.run_id, entry.date, entry.git_sha,
                json.dumps(entry.variants, ensure_ascii=False),
                json.dumps(entry.judge_models, ensure_ascii=False),
                entry.rubric_hash, entry.knowledge_hash, entry.reviewer_hash,
                entry.defaults_hash, entry.n_tasks,
                json.dumps(summary, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_run 失败: %s", e)


def _provenance() -> dict:
    return {"brainregion_version": _BR_VERSION, "summary_schema": SUMMARY_SCHEMA_VERSION}


def record_case(rec) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO eval_case_records(run_id,task_id,variant,report_summary,"
            "retrieved_case_ids,cost,latency_ms,outputs_json,error) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,task_id,variant) DO UPDATE SET "
            "  report_summary=excluded.report_summary,retrieved_case_ids=excluded.retrieved_case_ids,"
            "  cost=excluded.cost,latency_ms=excluded.latency_ms,outputs_json=excluded.outputs_json,"
            "  error=excluded.error",
            (
                rec.run_id, rec.task_id, rec.variant,
                json.dumps(rec.report_summary, ensure_ascii=False, default=str),
                json.dumps(rec.retrieved_case_ids, ensure_ascii=False),
                json.dumps(rec.cost, ensure_ascii=False, default=str),
                rec.latency_ms, rec.outputs_json, rec.error,
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_case 失败: %s", e)


def record_judgement(j) -> None:
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO eval_blind_judgements(run_id,task_id,judge_id,judge_model,rubric_hash,"
            "variant,blind,scores,reason,judge_cost_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,task_id,judge_id,variant) DO UPDATE SET "
            "  judge_model=excluded.judge_model,rubric_hash=excluded.rubric_hash,blind=excluded.blind,"
            "  scores=excluded.scores,reason=excluded.reason,judge_cost_usd=excluded.judge_cost_usd",
            (
                j.run_id, j.task_id, j.judge_id, j.judge_model, j.rubric_hash,
                j.variant, 1 if j.blind else 0,
                json.dumps(j.scores, ensure_ascii=False, default=str),
                j.reason, j.judge_cost_usd,
            ),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("eval record_judgement 失败: %s", e)


def export_jsonl(run_id: str, path) -> int:
    """把一次 run 的所有记录导成 JSONL（人读/备份）。返回写入行数。"""
    conn = _connect()
    rows = []
    run = conn.execute("SELECT * FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
    if run:
        rows.append({"kind": "run", **dict(run)})
    for r in conn.execute("SELECT * FROM eval_case_records WHERE run_id=?", (run_id,)).fetchall():
        rows.append({"kind": "case", **dict(r)})
    for r in conn.execute("SELECT * FROM eval_blind_judgements WHERE run_id=?", (run_id,)).fetchall():
        rows.append({"kind": "judgement", **dict(r)})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return len(rows)


# ───────────────────────── Inspector read 路径（只读 SELECT，参数化，query_only 连接）─────────────────────────
# 镜像 export_jsonl 的 SELECT，但返回内存 dict（report_summary/scores 等 JSON 字段已解析）。
# Inspector 只调这些 + lookup_calibration；禁用 record_* / f-string 拼 SQL（run_id/judge_id 来自外部）。


def list_runs(limit: int = 20) -> list[dict]:
    """最近 N run（最新优先，SQL 层 ORDER BY date DESC LIMIT，非 Python 切片）。

    每行：{run_id, date, git_sha, n_tasks, variants(list), judge_models(list), summary(dict)}。
    summary 一并取（history view 要算 status/cost，单查询免 N+1）。
    """
    limit = max(1, min(int(limit), 500))
    conn = _connect_readonly()
    rows = conn.execute(
        "SELECT run_id, date, git_sha, n_tasks, variants, judge_models, summary "
        "FROM eval_runs ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["variants"] = _loads_list(d.get("variants"))
        d["judge_models"] = _loads_list(d.get("judge_models"))
        d["summary"] = _loads(d.get("summary"))
        out.append(d)
    return out


def fetch_run(run_id: str) -> dict | None:
    """单 run 元数据 + summary（已解析）。无 → None。"""
    conn = _connect_readonly()
    row = conn.execute("SELECT * FROM eval_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["summary"] = _loads(d.get("summary"))
    d["variants"] = _loads_list(d.get("variants"))
    d["judge_models"] = _loads_list(d.get("judge_models"))
    return d


def fetch_cases(run_id: str) -> list[dict]:
    """某 run 全部 case record（report_summary/retrieved_case_ids/cost 已解析）。run_id 参数化。"""
    conn = _connect_readonly()
    rows = conn.execute(
        "SELECT * FROM eval_case_records WHERE run_id=? ORDER BY task_id, variant",
        (run_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["report_summary"] = _loads(d.get("report_summary"))
        d["retrieved_case_ids"] = _loads_list(d.get("retrieved_case_ids"))
        d["cost"] = _loads(d.get("cost"))
        out.append(d)
    return out


def fetch_judgements(run_id: str) -> list[dict]:
    """某 run 全部盲评（scores 已解析）。run_id 参数化。"""
    conn = _connect_readonly()
    rows = conn.execute(
        "SELECT * FROM eval_blind_judgements WHERE run_id=? ORDER BY task_id, variant, judge_id",
        (run_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scores"] = _loads(d.get("scores"))
        out.append(d)
    return out


def fetch_calibrations(judge_id: str | None = None) -> list[dict]:
    """校准 artifact（可按 judge_id 过滤，最新优先）。每行 summary 已解析、passed 转 bool。"""
    conn = _connect_readonly()
    if judge_id:
        rows = conn.execute(
            "SELECT * FROM eval_calibrations WHERE judge_id=? ORDER BY date DESC",
            (judge_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM eval_calibrations ORDER BY date DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = _loads(d.get("summary"))
        d["passed"] = bool(d.get("passed"))
        out.append(d)
    return out
