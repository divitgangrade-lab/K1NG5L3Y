"""SQLite persistence for Kingsley (tasks, transcripts, events, approvals, memory).

A single connection guarded by a lock is plenty for a single-user local daemon
and keeps the app dependency-free.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from kingsley import config

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'open',
    due TEXT DEFAULT '',
    source TEXT DEFAULT 'manual',
    calendar_event_id TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    kind TEXT DEFAULT 'speech',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT DEFAULT 'info',
    source TEXT DEFAULT 'kingsley',
    message TEXT NOT NULL,
    data TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    task TEXT DEFAULT 'default',
    provider TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    briefing TEXT NOT NULL,
    sources TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            config.HOME.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.executescript(_SCHEMA)
            _CONN.commit()
        return _CONN


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = connect()
    with _LOCK:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return cur


def query(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    conn = connect()
    with _LOCK:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def now() -> float:
    return time.time()


# --- events -----------------------------------------------------------------


def add_event(message: str, *, level: str = "info", source: str = "kingsley", data: Any = None) -> dict[str, Any]:
    payload = json.dumps(data) if data is not None else ""
    cur = execute(
        "INSERT INTO events (level, source, message, data, created_at) VALUES (?,?,?,?,?)",
        (level, source, message[:2000], payload[:4000], now()),
    )
    return {"id": cur.lastrowid, "level": level, "source": source, "message": message, "created_at": now()}


def recent_events(limit: int = 60) -> list[dict[str, Any]]:
    return query("SELECT id, level, source, message, created_at FROM events ORDER BY id DESC LIMIT ?", (limit,))


# --- tasks ------------------------------------------------------------------


def add_task(
    title: str,
    *,
    detail: str = "",
    priority: str = "normal",
    due: str = "",
    source: str = "manual",
) -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise ValueError("task title is required")
    existing = query(
        "SELECT id FROM tasks WHERE lower(title)=? AND status='open'", (title.lower(),)
    )
    if existing:
        return get_task(existing[0]["id"])
    cur = execute(
        "INSERT INTO tasks (title, detail, priority, due, source, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (title[:300], detail[:4000], priority, due, source, now(), now()),
    )
    return get_task(cur.lastrowid)


def get_task(task_id: int) -> dict[str, Any]:
    rows = query("SELECT * FROM tasks WHERE id=?", (task_id,))
    return rows[0] if rows else {}


def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return query("SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC", (status,))
    return query(
        "SELECT * FROM tasks ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,"
        " created_at DESC LIMIT 200"
    )


def update_task(task_id: int, **fields: Any) -> dict[str, Any]:
    allowed = {"title", "detail", "priority", "status", "due", "calendar_event_id"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            params.append(value)
    if sets:
        params.extend([now(), task_id])
        execute(f"UPDATE tasks SET {', '.join(sets)}, updated_at=? WHERE id=?", params)
    return get_task(task_id)


def delete_task(task_id: int) -> None:
    execute("DELETE FROM tasks WHERE id=?", (task_id,))


# --- transcripts ------------------------------------------------------------


def add_transcript(text: str, kind: str = "speech") -> dict[str, Any]:
    cur = execute(
        "INSERT INTO transcripts (text, kind, created_at) VALUES (?,?,?)",
        (text[:4000], kind, now()),
    )
    return {"id": cur.lastrowid, "text": text, "kind": kind, "created_at": now()}


def recent_transcripts(limit: int = 80) -> list[dict[str, Any]]:
    return query("SELECT * FROM transcripts ORDER BY id DESC LIMIT ?", (limit,))


# --- approvals --------------------------------------------------------------


def add_approval(path: str, content: str, summary: str = "") -> dict[str, Any]:
    cur = execute(
        "INSERT INTO approvals (path, content, summary, created_at) VALUES (?,?,?,?)",
        (path, content, summary[:1000], now()),
    )
    return get_approval(cur.lastrowid)


def get_approval(approval_id: int) -> dict[str, Any]:
    rows = query("SELECT * FROM approvals WHERE id=?", (approval_id,))
    return rows[0] if rows else {}


def list_approvals(status: str | None = "pending") -> list[dict[str, Any]]:
    if status:
        return query("SELECT * FROM approvals WHERE status=? ORDER BY id DESC", (status,))
    return query("SELECT * FROM approvals ORDER BY id DESC LIMIT 100")


def set_approval_status(approval_id: int, status: str) -> dict[str, Any]:
    execute("UPDATE approvals SET status=? WHERE id=?", (status, approval_id))
    return get_approval(approval_id)


# --- chat messages ----------------------------------------------------------


def add_message(role: str, text: str, task: str = "default", provider: str = "") -> None:
    execute(
        "INSERT INTO messages (role, text, task, provider, created_at) VALUES (?,?,?,?,?)",
        (role, text[:12000], task, provider, now()),
    )


def recent_messages(limit: int = 40) -> list[dict[str, Any]]:
    rows = query("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    return list(reversed(rows))


# --- intel ------------------------------------------------------------------


def add_intel(topic: str, briefing: str, sources: Iterable[dict[str, str]] = ()) -> dict[str, Any]:
    cur = execute(
        "INSERT INTO intel (topic, briefing, sources, created_at) VALUES (?,?,?,?)",
        (topic[:200], briefing[:20000], json.dumps(list(sources))[:8000], now()),
    )
    rows = query("SELECT * FROM intel WHERE id=?", (cur.lastrowid,))
    return rows[0] if rows else {}


def recent_intel(limit: int = 10) -> list[dict[str, Any]]:
    return query("SELECT * FROM intel ORDER BY id DESC LIMIT ?", (limit,))


# --- kv (google tokens, misc) ----------------------------------------------


def kv_set(key: str, value: Any) -> None:
    execute(
        "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def kv_get(key: str, default: Any = None) -> Any:
    rows = query("SELECT value FROM kv WHERE key=?", (key,))
    if not rows:
        return default
    try:
        return json.loads(rows[0]["value"])
    except json.JSONDecodeError:
        return default


def counts() -> dict[str, int]:
    return {
        "tasks_open": (query("SELECT COUNT(*) c FROM tasks WHERE status='open'") or [{"c": 0}])[0]["c"],
        "tasks_done": (query("SELECT COUNT(*) c FROM tasks WHERE status='done'") or [{"c": 0}])[0]["c"],
        "approvals_pending": (query("SELECT COUNT(*) c FROM approvals WHERE status='pending'") or [{"c": 0}])[0]["c"],
        "transcripts": (query("SELECT COUNT(*) c FROM transcripts") or [{"c": 0}])[0]["c"],
        "events": (query("SELECT COUNT(*) c FROM events") or [{"c": 0}])[0]["c"],
    }
