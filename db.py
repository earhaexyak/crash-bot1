"""
db.py - SQLite persistence layer for the Crash game bot.
Virtual currency only ("Coin" points). No real money / no real TON transfers.
Thread-safe via a single connection + lock (sufficient for aiogram's asyncio loop
when all DB calls go through the async wrappers below, run in a thread executor).
"""

import sqlite3
import time
import asyncio
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "game.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    balance     REAL NOT NULL DEFAULT 1000.0,
    created_at  INTEGER NOT NULL,
    banned      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    amount      REAL NOT NULL,          -- positive = credit, negative = debit
    reason      TEXT NOT NULL,          -- bet | cashout | admin_add | admin_set | admin_reset
    balance_after REAL NOT NULL,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    bet_amount  REAL NOT NULL,
    crash_point REAL NOT NULL,
    cashed_out_at REAL,                 -- NULL if lost (busted)
    payout      REAL NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS active_rounds (
    round_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL UNIQUE,   -- one live round per user at a time
    bet_amount  REAL NOT NULL,
    crash_point REAL NOT NULL,
    server_seed TEXT NOT NULL,
    start_ts    REAL NOT NULL,             -- time.time() epoch seconds
    status      TEXT NOT NULL DEFAULT 'active'  -- active | cashed | busted
);

CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_rounds_user ON rounds(user_id);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


_conn = _connect()
_lock = asyncio.Lock()


def init_db():
    with _conn:
        _conn.executescript(_SCHEMA)


# ---------- low-level sync helpers (run inside the lock from async wrappers) ----------

def _get_or_create_user_sync(user_id: int, username: str | None, starting_balance: float):
    row = _conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row:
        if username and row["username"] != username:
            _conn.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            _conn.commit()
        return dict(row)
    now = int(time.time())
    with _conn:
        _conn.execute(
            "INSERT INTO users(user_id, username, balance, created_at) VALUES (?,?,?,?)",
            (user_id, username, starting_balance, now),
        )
    row = _conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row)


def _adjust_balance_sync(user_id: int, delta: float, reason: str):
    row = _conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        raise ValueError(f"user {user_id} not found")
    new_balance = round(row["balance"] + delta, 2)
    if new_balance < 0:
        raise ValueError("insufficient balance")
    now = int(time.time())
    with _conn:
        _conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, user_id))
        _conn.execute(
            "INSERT INTO transactions(user_id, amount, reason, balance_after, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, delta, reason, new_balance, now),
        )
    return new_balance


def _set_balance_sync(user_id: int, amount: float, reason: str):
    if amount < 0:
        raise ValueError("balance cannot be negative")
    now = int(time.time())
    with _conn:
        _conn.execute(
            "INSERT OR IGNORE INTO users(user_id, username, balance, created_at) VALUES (?,?,?,?)",
            (user_id, None, 0, now),
        )
        _conn.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
        _conn.execute(
            "INSERT INTO transactions(user_id, amount, reason, balance_after, created_at) "
            "VALUES (?,?,?,?,?)",
            (user_id, amount, reason, amount, now),
        )
    return amount


def _record_round_sync(user_id, bet_amount, crash_point, cashed_out_at, payout):
    now = int(time.time())
    with _conn:
        _conn.execute(
            "INSERT INTO rounds(user_id, bet_amount, crash_point, cashed_out_at, payout, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, bet_amount, crash_point, cashed_out_at, payout, now),
        )


def _list_users_sync(search: str | None, limit: int, offset: int):
    if search:
        like = f"%{search}%"
        rows = _conn.execute(
            "SELECT * FROM users WHERE CAST(user_id AS TEXT) LIKE ? OR username LIKE ? "
            "ORDER BY user_id LIMIT ? OFFSET ?",
            (like, like, limit, offset),
        ).fetchall()
    else:
        rows = _conn.execute(
            "SELECT * FROM users ORDER BY user_id LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    return [dict(r) for r in rows]


def _get_user_sync(user_id: int):
    row = _conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def _user_stats_sync(user_id: int):
    row = _conn.execute(
        "SELECT COUNT(*) AS rounds_played, "
        "SUM(CASE WHEN cashed_out_at IS NOT NULL THEN 1 ELSE 0 END) AS wins, "
        "COALESCE(SUM(payout),0) AS total_won, "
        "COALESCE(SUM(bet_amount),0) AS total_bet "
        "FROM rounds WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return dict(row)


def _create_active_round_sync(user_id, bet_amount, crash_point, server_seed, start_ts):
    with _conn:
        cur = _conn.execute(
            "INSERT INTO active_rounds(user_id, bet_amount, crash_point, server_seed, start_ts, status) "
            "VALUES (?,?,?,?,?, 'active')",
            (user_id, bet_amount, crash_point, server_seed, start_ts),
        )
    return cur.lastrowid


def _get_active_round_sync(round_id: int):
    row = _conn.execute("SELECT * FROM active_rounds WHERE round_id=?", (round_id,)).fetchone()
    return dict(row) if row else None


def _get_active_round_by_user_sync(user_id: int):
    row = _conn.execute(
        "SELECT * FROM active_rounds WHERE user_id=? AND status='active'", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def _resolve_active_round_sync(round_id: int, status: str):
    """status = 'cashed' or 'busted'. Deletes the row after resolving so the
    user is free to start a new round; the outcome itself belongs in `rounds`."""
    with _conn:
        _conn.execute("UPDATE active_rounds SET status=? WHERE round_id=?", (status, round_id))


def _delete_active_round_sync(round_id: int):
    with _conn:
        _conn.execute("DELETE FROM active_rounds WHERE round_id=?", (round_id,))


# ---------- async wrappers used by the bot / admin panel ----------

async def get_or_create_user(user_id: int, username: str | None, starting_balance: float = 1000.0):
    async with _lock:
        return _get_or_create_user_sync(user_id, username, starting_balance)


async def adjust_balance(user_id: int, delta: float, reason: str):
    async with _lock:
        return _adjust_balance_sync(user_id, delta, reason)


async def set_balance(user_id: int, amount: float, reason: str = "admin_set"):
    async with _lock:
        return _set_balance_sync(user_id, amount, reason)


async def record_round(user_id, bet_amount, crash_point, cashed_out_at, payout):
    async with _lock:
        _record_round_sync(user_id, bet_amount, crash_point, cashed_out_at, payout)


async def get_user(user_id: int):
    async with _lock:
        return _get_user_sync(user_id)


async def list_users(search: str | None = None, limit: int = 50, offset: int = 0):
    async with _lock:
        return _list_users_sync(search, limit, offset)


async def user_stats(user_id: int):
    async with _lock:
        return _user_stats_sync(user_id)


# Sync versions for the Flask admin panel / web-app API (Flask is sync/WSGI, separate process)
get_or_create_user_sync = _get_or_create_user_sync
adjust_balance_sync = _adjust_balance_sync
set_balance_sync = _set_balance_sync
list_users_sync = _list_users_sync
get_user_sync = _get_user_sync
user_stats_sync = _user_stats_sync
record_round_sync = _record_round_sync
create_active_round_sync = _create_active_round_sync
get_active_round_sync = _get_active_round_sync
get_active_round_by_user_sync = _get_active_round_by_user_sync
resolve_active_round_sync = _resolve_active_round_sync
delete_active_round_sync = _delete_active_round_sync
