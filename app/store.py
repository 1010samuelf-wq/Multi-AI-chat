"""SQLite storage: users, login sessions, debates and their event logs."""

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
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(debates)")]
        if "user_id" not in cols:
            _conn.execute("ALTER TABLE debates ADD COLUMN user_id INTEGER")
        # Debates that were running when the server stopped can't resume.
        _conn.execute(
            "UPDATE debates SET status='interrupted' WHERE status IN ('running','queued')"
        )
        _conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        _conn.commit()
    return _conn


def _q(sql: str, args=(), one=False, write=False):
    with _lock:
        cur = _db().execute(sql, args)
        if write:
            _db().commit()
            return cur
        return cur.fetchone() if one else cur.fetchall()


# ---- users & sessions ------------------------------------------------------

def count_users() -> int:
    return _q("SELECT COUNT(*) FROM users", one=True)[0]


def create_user(username: str, password_hash: str, is_admin: bool) -> int:
    cur = _q("INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
             (username, password_hash, int(is_admin), time.time()), write=True)
    return cur.lastrowid


def _user(row) -> dict | None:
    if not row:
        return None
    return {"id": row[0], "username": row[1], "password_hash": row[2],
            "is_admin": bool(row[3]), "created_at": row[4]}


def get_user_by_name(username: str) -> dict | None:
    return _user(_q("SELECT id, username, password_hash, is_admin, created_at FROM users "
                    "WHERE username=?", (username,), one=True))


def get_user(user_id: int) -> dict | None:
    return _user(_q("SELECT id, username, password_hash, is_admin, created_at FROM users "
                    "WHERE id=?", (user_id,), one=True))


def list_users() -> list[dict]:
    rows = _q(
        "SELECT u.id, u.username, u.is_admin, u.created_at, "
        "COUNT(d.id), COALESCE(SUM(d.cost_usd), 0) "
        "FROM users u LEFT JOIN debates d ON d.user_id = u.id "
        "GROUP BY u.id ORDER BY u.username"
    )
    return [{"id": r[0], "username": r[1], "is_admin": bool(r[2]), "created_at": r[3],
             "chats": r[4], "spent_usd": r[5]} for r in rows]


def set_password(user_id: int, password_hash: str) -> None:
    _q("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id), write=True)


def delete_user(user_id: int) -> None:
    _q("DELETE FROM sessions WHERE user_id=?", (user_id,), write=True)
    _q("DELETE FROM users WHERE id=?", (user_id,), write=True)


def create_session(token: str, user_id: int, expires_at: float) -> None:
    _q("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
       (token, user_id, expires_at), write=True)


def session_user(token: str) -> dict | None:
    row = _q("SELECT user_id, expires_at FROM sessions WHERE token=?", (token,), one=True)
    if not row or row[1] < time.time():
        return None
    return get_user(row[0])


def delete_session(token: str) -> None:
    _q("DELETE FROM sessions WHERE token=?", (token,), write=True)


def delete_user_sessions(user_id: int, except_token: str | None = None) -> None:
    _q("DELETE FROM sessions WHERE user_id=? AND token != ?", (user_id, except_token or ""),
       write=True)


def claim_unowned_debates(user_id: int) -> None:
    """Debates from before accounts existed go to the first admin."""
    _q("UPDATE debates SET user_id=? WHERE user_id IS NULL", (user_id,), write=True)


# ---- debates ---------------------------------------------------------------

_COLS = "id, created_at, question, settings, status, cost_usd, final_answer, user_id"


def create_debate(debate_id: str, question: str, settings: dict, user_id: int | None) -> None:
    _q("INSERT INTO debates (id, created_at, question, settings, status, user_id) "
       "VALUES (?,?,?,?,?,?)",
       (debate_id, time.time(), question, json.dumps(settings), "running", user_id), write=True)


def update_debate(debate_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    _q(f"UPDATE debates SET {cols} WHERE id=?", (*fields.values(), debate_id), write=True)


def add_event(debate_id: str, seq: int, event: dict) -> None:
    _q("INSERT INTO events (debate_id, seq, data) VALUES (?,?,?)",
       (debate_id, seq, json.dumps(event)), write=True)


def get_debate(debate_id: str) -> dict | None:
    row = _q(f"SELECT {_COLS} FROM debates WHERE id=?", (debate_id,), one=True)
    return _row_to_dict(row) if row else None


def list_debates(user_id: int | None = None, limit: int = 200) -> list[dict]:
    if user_id is None:
        rows = _q(f"SELECT {_COLS} FROM debates ORDER BY created_at DESC LIMIT ?", (limit,))
    else:
        rows = _q(f"SELECT {_COLS} FROM debates WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                  (user_id, limit))
    return [_row_to_dict(r) for r in rows]


def get_events(debate_id: str, after: int = -1) -> list[dict]:
    rows = _q("SELECT data FROM events WHERE debate_id=? AND seq>? ORDER BY seq",
              (debate_id, after))
    return [json.loads(r[0]) for r in rows]


def delete_debate(debate_id: str) -> None:
    _q("DELETE FROM events WHERE debate_id=?", (debate_id,), write=True)
    _q("DELETE FROM debates WHERE id=?", (debate_id,), write=True)


def file_owner(file_id: str) -> int | None:
    """The user whose debate produced this file, or None if no debate did."""
    row = _q(
        "SELECT d.user_id FROM events e JOIN debates d ON d.id = e.debate_id "
        "WHERE e.data LIKE ? AND e.data LIKE '%\"type\": \"file\"%' LIMIT 1",
        (f'%"file_id": "{file_id}"%',), one=True)
    return row[0] if row else None


def _row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "created_at": row[1],
        "question": row[2],
        "settings": json.loads(row[3]),
        "status": row[4],
        "cost_usd": row[5],
        "final_answer": row[6],
        "user_id": row[7],
    }
