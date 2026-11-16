"""SQLite storage for debates and their event logs."""

import json
import sqlite3
import threading
import time

from .config import DATA_DIR, DB_PATH

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS debates (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                question TEXT NOT NULL,
                settings TEXT NOT NULL,
                status TEXT NOT NULL,
                cost_usd REAL NOT NULL DEFAULT 0,
                final_answer TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                debate_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (debate_id, seq)
            );
            """
        )
        # Debates that were running when the server stopped can't resume.
        _conn.execute(
            "UPDATE debates SET status='interrupted' WHERE status IN ('running','queued')"
        )
        _conn.commit()
    return _conn


def create_debate(debate_id: str, question: str, settings: dict) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO debates (id, created_at, question, settings, status) VALUES (?,?,?,?,?)",
            (debate_id, time.time(), question, json.dumps(settings), "running"),
        )
        _db().commit()


def update_debate(debate_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        _db().execute(f"UPDATE debates SET {cols} WHERE id=?", (*fields.values(), debate_id))
        _db().commit()


def add_event(debate_id: str, seq: int, event: dict) -> None:
    with _lock:
        _db().execute(
            "INSERT INTO events (debate_id, seq, data) VALUES (?,?,?)",
            (debate_id, seq, json.dumps(event)),
        )
        _db().commit()


def get_debate(debate_id: str) -> dict | None:
    with _lock:
        row = _db().execute(
            "SELECT id, created_at, question, settings, status, cost_usd, final_answer FROM debates WHERE id=?",
            (debate_id,),
        ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def list_debates(limit: int = 100) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT id, created_at, question, settings, status, cost_usd, final_answer "
            "FROM debates ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_events(debate_id: str, after: int = -1) -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT data FROM events WHERE debate_id=? AND seq>? ORDER BY seq",
            (debate_id, after),
        ).fetchall()
    return [json.loads(r[0]) for r in rows]


def delete_debate(debate_id: str) -> None:
    with _lock:
        _db().execute("DELETE FROM events WHERE debate_id=?", (debate_id,))
        _db().execute("DELETE FROM debates WHERE id=?", (debate_id,))
        _db().commit()


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "created_at": row[1],
        "question": row[2],
        "settings": json.loads(row[3]),
        "status": row[4],
        "cost_usd": row[5],
        "final_answer": row[6],
    }


def file_known(file_id: str) -> bool:
    """True if a debate produced this file (so it's safe to serve)."""
    with _lock:
        row = _db().execute(
            "SELECT 1 FROM events WHERE data LIKE ? AND data LIKE '%\"type\": \"file\"%' LIMIT 1",
            (f'%"file_id": "{file_id}"%',),
        ).fetchone()
    return row is not None
