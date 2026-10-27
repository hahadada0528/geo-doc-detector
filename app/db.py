from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import aiosqlite

from app.config import ROOT

DB_PATH = ROOT / "data" / "geo.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    aliases TEXT NOT NULL DEFAULT '[]',
    official_domains TEXT NOT NULL DEFAULT '[]',
    owner_username TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS detection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'running',
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    brand_id INTEGER,
    brand_name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    keyword_coverage REAL,
    answer_priority REAL,
    sentiment_score REAL,
    sentiment_label TEXT NOT NULL DEFAULT '',
    advice_text TEXT NOT NULL DEFAULT '',
    analysis_json TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER,
    run_id INTEGER,
    question_text TEXT NOT NULL,
    site_name TEXT NOT NULL,
    is_official INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    title TEXT,
    segment TEXT,
    answer_excerpt TEXT,
    content_level TEXT NOT NULL DEFAULT '',
    brand_name TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE SET NULL,
    FOREIGN KEY (run_id) REFERENCES detection_runs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ask_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,
    answer_text TEXT,
    search_queries TEXT,
    raw_citations TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS agent_trainings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_id TEXT NOT NULL,
    status TEXT NOT NULL,
    example_count INTEGER NOT NULL DEFAULT 0,
    site_count INTEGER NOT NULL DEFAULT 0,
    total_weight REAL NOT NULL DEFAULT 0,
    pack_dir TEXT,
    note TEXT,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month_id TEXT NOT NULL,
    activity TEXT NOT NULL,
    activity_label TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    own_tokens INTEGER NOT NULL DEFAULT 0,
    username TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


@asynccontextmanager
async def connect_db() -> AsyncIterator[aiosqlite.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with connect_db() as db:
        await db.executescript(SCHEMA)
        # 兼容旧库：补资料分级字段
        cur = await db.execute("PRAGMA table_info(citations)")
        cols = {row[1] for row in await cur.fetchall()}
        if "content_level" not in cols:
            await db.execute(
                "ALTER TABLE citations ADD COLUMN content_level TEXT NOT NULL DEFAULT ''"
            )
        if "brand_name" not in cols:
            await db.execute(
                "ALTER TABLE citations ADD COLUMN brand_name TEXT NOT NULL DEFAULT ''"
            )
        # 回填品牌名，便于删品牌后仍能展示
        await db.execute(
            """
            UPDATE citations
            SET brand_name = (
                SELECT b.name
                FROM questions q
                JOIN brands b ON b.id = q.brand_id
                WHERE q.id = citations.question_id
            )
            WHERE IFNULL(brand_name, '') = ''
              AND question_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM questions q
                JOIN brands b ON b.id = q.brand_id
                WHERE q.id = citations.question_id
              )
            """
        )
        await _migrate_citations_preserve_on_delete(db)
        await _migrate_detection_runs_analysis(db)
        await _migrate_brands_owner(db)
        await db.commit()


async def _migrate_brands_owner(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(brands)")
    cols = {row[1] for row in await cur.fetchall()}
    if "owner_username" not in cols:
        await db.execute(
            "ALTER TABLE brands ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''"
        )
    # 历史品牌归到普通用户 123
    await db.execute(
        "UPDATE brands SET owner_username = '123' WHERE IFNULL(owner_username, '') = ''"
    )


async def _migrate_detection_runs_analysis(db: aiosqlite.Connection) -> None:
    """为 detection_runs 补品牌、标题、分析字段，并尽量回填历史标题。"""
    cur = await db.execute("PRAGMA table_info(detection_runs)")
    cols = {row[1] for row in await cur.fetchall()}
    alters = [
        ("brand_id", "ALTER TABLE detection_runs ADD COLUMN brand_id INTEGER"),
        ("brand_name", "ALTER TABLE detection_runs ADD COLUMN brand_name TEXT NOT NULL DEFAULT ''"),
        ("title", "ALTER TABLE detection_runs ADD COLUMN title TEXT NOT NULL DEFAULT ''"),
        ("keyword_coverage", "ALTER TABLE detection_runs ADD COLUMN keyword_coverage REAL"),
        ("answer_priority", "ALTER TABLE detection_runs ADD COLUMN answer_priority REAL"),
        ("sentiment_score", "ALTER TABLE detection_runs ADD COLUMN sentiment_score REAL"),
        ("sentiment_label", "ALTER TABLE detection_runs ADD COLUMN sentiment_label TEXT NOT NULL DEFAULT ''"),
        ("advice_text", "ALTER TABLE detection_runs ADD COLUMN advice_text TEXT NOT NULL DEFAULT ''"),
        ("analysis_json", "ALTER TABLE detection_runs ADD COLUMN analysis_json TEXT NOT NULL DEFAULT ''"),
    ]
    for name, sql in alters:
        if name not in cols:
            await db.execute(sql)

    # 回填 brand_name / title（从引用快照）
    await db.execute(
        """
        UPDATE detection_runs
        SET brand_name = (
            SELECT c.brand_name FROM citations c
            WHERE c.run_id = detection_runs.id AND IFNULL(c.brand_name, '') != ''
            ORDER BY c.id LIMIT 1
        )
        WHERE IFNULL(brand_name, '') = ''
          AND EXISTS (
            SELECT 1 FROM citations c
            WHERE c.run_id = detection_runs.id AND IFNULL(c.brand_name, '') != ''
          )
        """
    )
    await db.execute(
        """
        UPDATE detection_runs
        SET title = TRIM(started_at || ' ' || IFNULL(brand_name, ''))
        WHERE IFNULL(title, '') = ''
        """
    )


async def _migrate_citations_preserve_on_delete(db: aiosqlite.Connection) -> None:
    """旧库 citations.question_id 为 ON DELETE CASCADE 时，改建为 SET NULL，删品牌/问题不丢引用。"""
    cur = await db.execute("PRAGMA foreign_key_list(citations)")
    fks = [dict(r) for r in await cur.fetchall()]
    q_fk = next((f for f in fks if f.get("table") == "questions"), None)
    if q_fk and str(q_fk.get("on_delete") or "").upper() == "SET NULL":
        return

    await db.execute("PRAGMA foreign_keys = OFF")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS citations_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER,
            run_id INTEGER,
            question_text TEXT NOT NULL,
            site_name TEXT NOT NULL,
            is_official INTEGER NOT NULL DEFAULT 0,
            url TEXT,
            title TEXT,
            segment TEXT,
            answer_excerpt TEXT,
            content_level TEXT NOT NULL DEFAULT '',
            brand_name TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL,
            FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE SET NULL,
            FOREIGN KEY (run_id) REFERENCES detection_runs(id) ON DELETE SET NULL
        )
        """
    )
    await db.execute(
        """
        INSERT INTO citations_new (
            id, question_id, run_id, question_text, site_name, is_official,
            url, title, segment, answer_excerpt, content_level, brand_name, added_at
        )
        SELECT
            id, question_id, run_id, question_text, site_name, is_official,
            url, title, segment, answer_excerpt,
            IFNULL(content_level, ''),
            IFNULL(brand_name, ''),
            added_at
        FROM citations
        """
    )
    await db.execute("DROP TABLE citations")
    await db.execute("ALTER TABLE citations_new RENAME TO citations")
    await db.execute("PRAGMA foreign_keys = ON")


async def delete_brand(brand_id: int) -> None:
    """删除品牌及其问题；引用保留（question_id 置空，brand_name 写回快照）。"""
    async with connect_db() as db:
        cur = await db.execute("SELECT name FROM brands WHERE id = ?", (brand_id,))
        row = await cur.fetchone()
        brand_name = (row["name"] if row else "") or ""
        await db.execute(
            """
            UPDATE citations
            SET brand_name = CASE
                WHEN IFNULL(brand_name, '') = '' THEN ?
                ELSE brand_name
            END
            WHERE question_id IN (SELECT id FROM questions WHERE brand_id = ?)
            """,
            (brand_name, brand_id),
        )
        # 先断开引用，避免旧库 CASCADE 误删；新库 ON DELETE SET NULL 同样安全
        await db.execute(
            """
            UPDATE citations
            SET question_id = NULL
            WHERE question_id IN (SELECT id FROM questions WHERE brand_id = ?)
            """,
            (brand_id,),
        )
        await db.execute("DELETE FROM questions WHERE brand_id = ?", (brand_id,))
        await db.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
        await db.commit()


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass
    return [x.strip() for x in value.split(",") if x.strip()]


def dumps_list(items: list[str]) -> str:
    return json.dumps([x.strip() for x in items if x.strip()], ensure_ascii=False)


# ---- brands ----

async def create_brand(
    name: str,
    aliases: list[str] | None = None,
    official_domains: list[str] | None = None,
    owner_username: str = "",
) -> int:
    async with connect_db() as db:
        cur = await db.execute(
            """
            INSERT INTO brands (name, aliases, official_domains, owner_username, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                dumps_list(aliases or []),
                dumps_list(official_domains or []),
                (owner_username or "").strip(),
                utc_now(),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_brands(owner_username: str | None = None) -> list[dict[str, Any]]:
    async with connect_db() as db:
        if owner_username:
            cur = await db.execute(
                "SELECT * FROM brands WHERE owner_username = ? ORDER BY id DESC",
                (owner_username,),
            )
        else:
            cur = await db.execute("SELECT * FROM brands ORDER BY id DESC")
        rows = await cur.fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["aliases"] = parse_json_list(item.get("aliases"))
            item["official_domains"] = parse_json_list(item.get("official_domains"))
            result.append(item)
        return result


async def get_brand(brand_id: int) -> dict[str, Any] | None:
    async with connect_db() as db:
        cur = await db.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
        row = await cur.fetchone()
        if not row:
            return None
        item = dict(row)
        item["aliases"] = parse_json_list(item.get("aliases"))
        item["official_domains"] = parse_json_list(item.get("official_domains"))
        return item


async def update_brand(
    brand_id: int,
    name: str,
    aliases: list[str] | None = None,
    official_domains: list[str] | None = None,
) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE brands SET name = ?, aliases = ?, official_domains = ? WHERE id = ?",
            (
                name.strip(),
                dumps_list(aliases or []),
                dumps_list(official_domains or []),
                brand_id,
            ),
        )
        await db.commit()


# ---- questions ----

async def create_question(brand_id: int, text: str) -> int:
    async with connect_db() as db:
        cur = await db.execute(
            "INSERT INTO questions (brand_id, text, status, created_at) VALUES (?, ?, 'pending', ?)",
            (brand_id, text.strip(), utc_now()),
        )
        await db.commit()
        return int(cur.lastrowid)


async def create_questions_bulk(brand_id: int, texts: list[str]) -> int:
    now = utc_now()
    rows = [(brand_id, t.strip(), "pending", now) for t in texts if t.strip()]
    if not rows:
        return 0
    async with connect_db() as db:
        await db.executemany(
            "INSERT INTO questions (brand_id, text, status, created_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        await db.commit()
        return len(rows)


async def list_questions(brand_id: int | None = None) -> list[dict[str, Any]]:
    async with connect_db() as db:
        if brand_id:
            cur = await db.execute(
                """
                SELECT q.*, b.name AS brand_name
                FROM questions q JOIN brands b ON b.id = q.brand_id
                WHERE q.brand_id = ?
                ORDER BY q.id DESC
                """,
                (brand_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT q.*, b.name AS brand_name
                FROM questions q JOIN brands b ON b.id = q.brand_id
                ORDER BY q.id DESC
                """
            )
        return [dict(r) for r in await cur.fetchall()]


async def count_questions_by_brand() -> dict[int, int]:
    async with connect_db() as db:
        cur = await db.execute(
            "SELECT brand_id, COUNT(*) AS cnt FROM questions GROUP BY brand_id"
        )
        return {int(r["brand_id"]): int(r["cnt"]) for r in await cur.fetchall()}


async def get_question(question_id: int) -> dict[str, Any] | None:
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT q.*, b.name AS brand_name
            FROM questions q JOIN brands b ON b.id = q.brand_id
            WHERE q.id = ?
            """,
            (question_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def update_question_text(question_id: int, text: str) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE questions SET text = ? WHERE id = ?",
            (text.strip(), question_id),
        )
        await db.commit()


async def delete_question(question_id: int) -> None:
    async with connect_db() as db:
        await db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        await db.commit()


async def set_question_status(question_id: int, status: str) -> None:
    async with connect_db() as db:
        await db.execute("UPDATE questions SET status = ? WHERE id = ?", (status, question_id))
        await db.commit()


async def pending_questions(brand_id: int | None = None) -> list[dict[str, Any]]:
    async with connect_db() as db:
        if brand_id:
            cur = await db.execute(
                """
                SELECT q.*, b.name AS brand_name
                FROM questions q JOIN brands b ON b.id = q.brand_id
                WHERE q.brand_id = ? AND q.status IN ('pending', 'error')
                ORDER BY q.id ASC
                """,
                (brand_id,),
            )
        else:
            cur = await db.execute(
                """
                SELECT q.*, b.name AS brand_name
                FROM questions q JOIN brands b ON b.id = q.brand_id
                WHERE q.status IN ('pending', 'error')
                ORDER BY q.id ASC
                """
            )
        return [dict(r) for r in await cur.fetchall()]


async def reset_questions_for_redetect(brand_id: int | None = None) -> int:
    """把 done/error/running 重置为 pending，便于整批重跑。"""
    async with connect_db() as db:
        if brand_id:
            cur = await db.execute(
                "UPDATE questions SET status = 'pending' WHERE brand_id = ? AND status IN ('done', 'error', 'running')",
                (brand_id,),
            )
        else:
            cur = await db.execute(
                "UPDATE questions SET status = 'pending' WHERE status IN ('done', 'error', 'running')"
            )
        await db.commit()
        return int(cur.rowcount or 0)


async def get_latest_ask_error() -> dict[str, Any] | None:
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT id, question_id, error_message, created_at
            FROM ask_logs
            WHERE status = 'error' AND error_message IS NOT NULL AND error_message != ''
            ORDER BY id DESC LIMIT 1
            """
        )
        row = await cur.fetchone()
        return dict(row) if row else None


# ---- runs / citations ----

async def create_run(
    total: int,
    brand_id: int | None = None,
    brand_name: str = "",
) -> int:
    started = utc_now()
    name = (brand_name or "").strip()
    title = f"{started} {name}".strip()
    async with connect_db() as db:
        cur = await db.execute(
            """
            INSERT INTO detection_runs
            (status, total, done, started_at, brand_id, brand_name, title)
            VALUES ('running', ?, 0, ?, ?, ?, ?)
            """,
            (total, started, brand_id, name, title),
        )
        await db.commit()
        return int(cur.lastrowid)


async def bump_run(run_id: int, done: int) -> None:
    async with connect_db() as db:
        await db.execute("UPDATE detection_runs SET done = ? WHERE id = ?", (done, run_id))
        await db.commit()


async def finish_run(run_id: int, status: str = "done", error_message: str | None = None) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE detection_runs SET status = ?, error_message = ?, finished_at = ? WHERE id = ?",
            (status, error_message, utc_now(), run_id),
        )
        await db.commit()


def _parse_run_row(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    raw = d.get("analysis_json") or ""
    if raw:
        try:
            d["analysis"] = json.loads(raw)
        except Exception:
            d["analysis"] = None
    else:
        d["analysis"] = None
    return d


async def get_run(run_id: int) -> dict[str, Any] | None:
    async with connect_db() as db:
        cur = await db.execute("SELECT * FROM detection_runs WHERE id = ?", (run_id,))
        row = await cur.fetchone()
        return _parse_run_row(row) if row else None


async def get_latest_run() -> dict[str, Any] | None:
    async with connect_db() as db:
        cur = await db.execute("SELECT * FROM detection_runs ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return _parse_run_row(row) if row else None


async def list_detection_runs(
    limit: int = 50,
    owner_username: str | None = None,
) -> list[dict[str, Any]]:
    async with connect_db() as db:
        if owner_username:
            cur = await db.execute(
                """
                SELECT r.* FROM detection_runs r
                LEFT JOIN brands b ON b.id = r.brand_id
                WHERE b.owner_username = ? OR (
                    IFNULL(r.brand_id, 0) = 0 AND EXISTS (
                        SELECT 1 FROM citations c
                        JOIN questions q ON q.id = c.question_id
                        JOIN brands bb ON bb.id = q.brand_id
                        WHERE c.run_id = r.id AND bb.owner_username = ?
                    )
                )
                ORDER BY r.id DESC
                LIMIT ?
                """,
                (owner_username, owner_username, limit),
            )
        else:
            cur = await db.execute(
                """
                SELECT * FROM detection_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [_parse_run_row(r) for r in await cur.fetchall()]


async def save_run_analysis(run_id: int, analysis: dict[str, Any]) -> None:
    async with connect_db() as db:
        await db.execute(
            """
            UPDATE detection_runs SET
                keyword_coverage = ?,
                answer_priority = ?,
                sentiment_score = ?,
                sentiment_label = ?,
                advice_text = ?,
                analysis_json = ?
            WHERE id = ?
            """,
            (
                analysis.get("keyword_coverage"),
                analysis.get("answer_priority"),
                analysis.get("sentiment_score"),
                analysis.get("sentiment_label") or "",
                analysis.get("advice") or "",
                json.dumps(analysis, ensure_ascii=False),
                run_id,
            ),
        )
        await db.commit()


async def save_ask_log(
    question_id: int,
    run_id: int | None,
    status: str,
    answer_text: str = "",
    search_queries: list[str] | None = None,
    raw_citations: list[dict] | None = None,
    error_message: str | None = None,
) -> int:
    async with connect_db() as db:
        cur = await db.execute(
            """
            INSERT INTO ask_logs
            (question_id, run_id, status, answer_text, search_queries, raw_citations, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question_id,
                run_id,
                status,
                answer_text,
                json.dumps(search_queries or [], ensure_ascii=False),
                json.dumps(raw_citations or [], ensure_ascii=False),
                error_message,
                utc_now(),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def save_citations(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    async with connect_db() as db:
        await db.executemany(
            """
            INSERT INTO citations
            (question_id, run_id, question_text, site_name, is_official, url, title, segment, answer_excerpt, content_level, brand_name, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["question_id"],
                    r.get("run_id"),
                    r["question_text"],
                    r["site_name"],
                    1 if r.get("is_official") else 0,
                    r.get("url"),
                    r.get("title"),
                    r.get("segment"),
                    r.get("answer_excerpt"),
                    r.get("content_level") or "",
                    r.get("brand_name") or "",
                    r.get("added_at") or utc_now(),
                )
                for r in rows
            ],
        )
        await db.commit()


async def update_citation_level(citation_id: int, content_level: str) -> None:
    async with connect_db() as db:
        await db.execute(
            "UPDATE citations SET content_level = ? WHERE id = ?",
            (content_level, citation_id),
        )
        await db.commit()


async def list_citations(
    brand_id: int | None = None,
    question_id: int | None = None,
    run_id: int | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    async with connect_db() as db:
        sql = """
            SELECT c.*,
                   q.brand_id AS brand_id,
                   COALESCE(NULLIF(c.brand_name, ''), b.name, '') AS brand_name
            FROM citations c
            LEFT JOIN questions q ON q.id = c.question_id
            LEFT JOIN brands b ON b.id = q.brand_id
            WHERE 1=1
        """
        params: list[Any] = []
        if brand_id:
            sql += " AND q.brand_id = ?"
            params.append(brand_id)
        if question_id:
            sql += " AND c.question_id = ?"
            params.append(question_id)
        if run_id:
            sql += " AND c.run_id = ?"
            params.append(run_id)
        sql += " ORDER BY c.id ASC LIMIT ?" if run_id else " ORDER BY c.id DESC LIMIT ?"
        params.append(limit)
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def list_ask_logs_for_run(run_id: int) -> list[dict[str, Any]]:
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT a.*, q.text AS question_text
            FROM ask_logs a
            LEFT JOIN questions q ON q.id = a.question_id
            WHERE a.run_id = ?
            ORDER BY a.id ASC
            """,
            (run_id,),
        )
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            for key in ("search_queries", "raw_citations"):
                raw = d.get(key)
                if isinstance(raw, str) and raw:
                    try:
                        d[key] = json.loads(raw)
                    except Exception:
                        pass
            rows.append(d)
        return rows


async def list_orphan_citations(limit: int = 100) -> list[dict[str, Any]]:
    """未绑定 run_id 的历史引用。"""
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT c.*,
                   q.brand_id AS brand_id,
                   COALESCE(NULLIF(c.brand_name, ''), b.name, '') AS brand_name
            FROM citations c
            LEFT JOIN questions q ON q.id = c.question_id
            LEFT JOIN brands b ON b.id = q.brand_id
            WHERE c.run_id IS NULL
            ORDER BY c.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def list_all_citations_for_training(limit: int = 20000) -> list[dict[str, Any]]:
    """训练用：尽量拉全量引用（按时间权重过滤在训练层完成）。"""
    return await list_citations(limit=limit)


async def save_agent_training(
    *,
    month_id: str,
    status: str,
    example_count: int = 0,
    site_count: int = 0,
    total_weight: float = 0.0,
    pack_dir: str | None = None,
    note: str = "",
    message: str = "",
) -> int:
    async with connect_db() as db:
        cur = await db.execute(
            """
            INSERT INTO agent_trainings
            (month_id, status, example_count, site_count, total_weight, pack_dir, note, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                month_id,
                status,
                example_count,
                site_count,
                total_weight,
                pack_dir,
                note,
                message,
                utc_now(),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def get_latest_agent_training() -> dict[str, Any] | None:
    async with connect_db() as db:
        cur = await db.execute("SELECT * FROM agent_trainings ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_agent_trainings(limit: int = 12) -> list[dict[str, Any]]:
    async with connect_db() as db:
        cur = await db.execute(
            "SELECT * FROM agent_trainings ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def search_citations_by_site(site_query: str, limit: int = 1000) -> list[dict[str, Any]]:
    """按网站名或域名模糊检索引用（含官网别名匹配）。

    查询「官网」时：合并 site_name=官网 与 is_official=1 的全部官方引用。
    """
    q = (site_query or "").strip()
    if not q:
        return []

    host = hostname_of(q if "://" in q or q.startswith("www.") else "")
    # 用户可能直接输入域名而无协议
    if not host and ("." in q and " " not in q):
        host = q.lower().removeprefix("www.")

    like = f"%{q}%"
    async with connect_db() as db:
        if q in ("官网", "官方网站", "官方站") or q.lower() in ("official", "official site"):
            cur = await db.execute(
                """
                SELECT c.*,
                       q.brand_id AS brand_id,
                       COALESCE(NULLIF(c.brand_name, ''), b.name, '') AS brand_name
                FROM citations c
                LEFT JOIN questions q ON q.id = c.question_id
                LEFT JOIN brands b ON b.id = q.brand_id
                WHERE
                    c.is_official = 1
                    OR c.site_name IN ('官网', '官方网站', '官方站')
                    OR c.site_name LIKE '%官网%'
                    OR c.site_name LIKE '%官方网站%'
                ORDER BY c.id DESC
                LIMIT ?
                """,
                (limit,),
            )
        elif host:
            host_like = f"%{host}%"
            cur = await db.execute(
                """
                SELECT c.*,
                       q.brand_id AS brand_id,
                       COALESCE(NULLIF(c.brand_name, ''), b.name, '') AS brand_name
                FROM citations c
                LEFT JOIN questions q ON q.id = c.question_id
                LEFT JOIN brands b ON b.id = q.brand_id
                WHERE
                    c.site_name LIKE ? COLLATE NOCASE
                    OR c.url LIKE ? COLLATE NOCASE
                    OR c.title LIKE ? COLLATE NOCASE
                    OR (? <> '' AND c.site_name = '官网' AND c.url LIKE ?)
                    OR (? <> '' AND c.is_official = 1 AND c.url LIKE ?)
                ORDER BY c.id DESC
                LIMIT ?
                """,
                (like, host_like, like, host, host_like, host, host_like, limit),
            )
        else:
            cur = await db.execute(
                """
                SELECT c.*,
                       q.brand_id AS brand_id,
                       COALESCE(NULLIF(c.brand_name, ''), b.name, '') AS brand_name
                FROM citations c
                LEFT JOIN questions q ON q.id = c.question_id
                LEFT JOIN brands b ON b.id = q.brand_id
                WHERE
                    c.site_name LIKE ? COLLATE NOCASE
                    OR c.url LIKE ? COLLATE NOCASE
                    OR c.title LIKE ? COLLATE NOCASE
                ORDER BY c.id DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            )
        rows = [dict(r) for r in await cur.fetchall()]
        # 投放分析展示：官网桶统一站点名，便于集中查看
        if q in ("官网", "官方网站", "官方站") or q.lower() in ("official", "official site"):
            for r in rows:
                r["site_name"] = "官网"
                r["is_official"] = 1
        return rows


async def list_distinct_sites(limit: int = 500) -> list[dict[str, Any]]:
    """站点列表：is_official / 官网类站点名合并为「官网」。"""
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT
                CASE
                    WHEN IFNULL(is_official, 0) = 1
                         OR site_name IN ('官网', '官方网站', '官方站')
                         OR site_name LIKE '%官网%'
                         OR site_name LIKE '%官方网站%'
                    THEN '官网'
                    ELSE site_name
                END AS site_name,
                COUNT(*) AS cnt
            FROM citations
            GROUP BY 1
            ORDER BY cnt DESC, site_name ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def save_token_usage(row: dict[str, Any]) -> int:
    async with connect_db() as db:
        cur = await db.execute(
            """
            INSERT INTO token_usage (
                month_id, activity, activity_label, model,
                prompt_tokens, completion_tokens, total_tokens, own_tokens,
                username, detail, meta_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("month_id") or "",
                row.get("activity") or "",
                row.get("activity_label") or "",
                row.get("model") or "",
                int(row.get("prompt_tokens") or 0),
                int(row.get("completion_tokens") or 0),
                int(row.get("total_tokens") or 0),
                int(row.get("own_tokens") or 0),
                row.get("username") or "",
                row.get("detail") or "",
                json.dumps(row.get("meta") or {}, ensure_ascii=False),
                utc_now(),
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def list_token_usage_months(
    limit: int = 24,
    username: str | None = None,
) -> list[dict[str, Any]]:
    async with connect_db() as db:
        if username is not None:
            cur = await db.execute(
                """
                SELECT month_id,
                       COUNT(*) AS op_count,
                       SUM(total_tokens) AS raw_tokens,
                       SUM(own_tokens) AS own_tokens
                FROM token_usage
                WHERE username = ?
                GROUP BY month_id
                ORDER BY month_id DESC
                LIMIT ?
                """,
                (username, limit),
            )
        else:
            cur = await db.execute(
                """
                SELECT month_id,
                       COUNT(*) AS op_count,
                       SUM(total_tokens) AS raw_tokens,
                       SUM(own_tokens) AS own_tokens
                FROM token_usage
                GROUP BY month_id
                ORDER BY month_id DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["raw_tokens"] = int(d.get("raw_tokens") or 0)
            d["own_tokens"] = int(d.get("own_tokens") or 0)
            d["op_count"] = int(d.get("op_count") or 0)
            rows.append(d)
        return rows


async def list_token_usage_users(limit: int = 100) -> list[dict[str, Any]]:
    """管理员：按用户汇总 Token。"""
    async with connect_db() as db:
        cur = await db.execute(
            """
            SELECT CASE WHEN IFNULL(username, '') = '' THEN '(未知)' ELSE username END AS username,
                   COUNT(*) AS op_count,
                   SUM(own_tokens) AS own_tokens
            FROM token_usage
            GROUP BY CASE WHEN IFNULL(username, '') = '' THEN '(未知)' ELSE username END
            ORDER BY own_tokens DESC, username ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["own_tokens"] = int(d.get("own_tokens") or 0)
            d["op_count"] = int(d.get("op_count") or 0)
            rows.append(d)
        return rows


async def list_token_usage_months_for_user(username: str, limit: int = 24) -> list[dict[str, Any]]:
    return await list_token_usage_months(limit=limit, username=username)


async def list_token_usage_by_activity(
    month_id: str,
    username: str | None = None,
) -> list[dict[str, Any]]:
    async with connect_db() as db:
        if username is not None:
            cur = await db.execute(
                """
                SELECT activity,
                       MAX(activity_label) AS activity_label,
                       COUNT(*) AS op_count,
                       SUM(total_tokens) AS raw_tokens,
                       SUM(own_tokens) AS own_tokens
                FROM token_usage
                WHERE month_id = ? AND username = ?
                GROUP BY activity
                ORDER BY own_tokens DESC, activity ASC
                """,
                (month_id, username),
            )
        else:
            cur = await db.execute(
                """
                SELECT activity,
                       MAX(activity_label) AS activity_label,
                       COUNT(*) AS op_count,
                       SUM(total_tokens) AS raw_tokens,
                       SUM(own_tokens) AS own_tokens
                FROM token_usage
                WHERE month_id = ?
                GROUP BY activity
                ORDER BY own_tokens DESC, activity ASC
                """,
                (month_id,),
            )
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["raw_tokens"] = int(d.get("raw_tokens") or 0)
            d["own_tokens"] = int(d.get("own_tokens") or 0)
            d["op_count"] = int(d.get("op_count") or 0)
            rows.append(d)
        return rows


async def list_token_usage_entries(
    month_id: str,
    activity: str | None = None,
    username: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    async with connect_db() as db:
        sql = "SELECT * FROM token_usage WHERE month_id = ?"
        params: list[Any] = [month_id]
        if username is not None:
            sql += " AND username = ?"
            params.append(username)
        if activity:
            sql += " AND activity = ?"
            params.append(activity)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = await db.execute(sql, params)
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("meta_json") or "{}")
            except Exception:
                d["meta"] = {}
            out.append(d)
        return out


def hostname_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
        return host.lower().removeprefix("www.")
    except Exception:
        return ""
