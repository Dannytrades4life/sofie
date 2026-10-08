"""Async SQLite storage.

Every table is keyed by guild_id so the same schema works for one server or many.
To move to Postgres later, re-implement this class on asyncpg with the same method
names; the SQL is plain and portable apart from the UPSERT syntax (also valid in Postgres).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS members (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    xp INTEGER NOT NULL DEFAULT 0, level INTEGER NOT NULL DEFAULT 0, last_xp_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS member_activity (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL, messages INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS daily_stats (
    guild_id INTEGER NOT NULL, day TEXT NOT NULL,
    joins INTEGER NOT NULL DEFAULT 0, leaves INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, day)
);
-- Owner's trades. `data` holds the current values, `original` the values first posted.
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',            -- open | closed
    data TEXT NOT NULL, original TEXT NOT NULL,
    entry_channel_id INTEGER, entry_message_id INTEGER,
    result_channel_id INTEGER, result_message_id INTEGER,
    created_at REAL NOT NULL, closed_at REAL
);
-- Unconfirmed trades read from text, screenshots or journals, waiting for the owner's OK.
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL, image_path TEXT, status TEXT NOT NULL DEFAULT 'pending', created_by INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    guild_id INTEGER NOT NULL, root TEXT NOT NULL, name TEXT, tick_size REAL NOT NULL, tick_value REAL NOT NULL,
    data_ticker TEXT, market_posts INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, root)
);
-- Every message the bot publishes (so /edit can find and change it).
CREATE TABLE IF NOT EXISTS posts (
    message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
    feature TEXT NOT NULL, kind TEXT NOT NULL, ref_id INTEGER, created_at REAL NOT NULL,
    reactions INTEGER, replies INTEGER, measured_at REAL
);
CREATE TABLE IF NOT EXISTS edit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, target TEXT NOT NULL,
    field TEXT NOT NULL, old_value TEXT, new_value TEXT, editor_id INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rulebook (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, text TEXT NOT NULL,
    created_by INTEGER, created_at REAL NOT NULL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_time ON actions (guild_id, created_at);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, name TEXT NOT NULL,
    link TEXT, rules TEXT, sizes TEXT, notes TEXT, UNIQUE (guild_id, name)
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    data TEXT NOT NULL, channel_id INTEGER, message_id INTEGER, ends_at REAL, created_by INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL, tickets INTEGER NOT NULL DEFAULT 1, entered_at REAL NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS giveaway_winners (
    giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', -- pending|claimed|expired
    drawn_at REAL NOT NULL, claim_by REAL NOT NULL, PRIMARY KEY (giveaway_id, user_id)
);
CREATE TABLE IF NOT EXISTS giveaway_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, giveaway_id INTEGER NOT NULL, actor_id INTEGER, event TEXT NOT NULL,
    detail TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    guild_id INTEGER NOT NULL, invitee_id INTEGER NOT NULL, inviter_id INTEGER, code TEXT,
    joined_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',  -- pending | valid | left | rejected
    PRIMARY KEY (guild_id, invitee_id)
);
CREATE TABLE IF NOT EXISTS member_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    data TEXT NOT NULL, message_id INTEGER, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_entries (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, day TEXT NOT NULL, message_id INTEGER,
    PRIMARY KEY (guild_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, week TEXT NOT NULL, idea TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned', run_at REAL, message_id INTEGER, score REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL
);
-- Server rebuild: each applied change with what's needed to undo it.
CREATE TABLE IF NOT EXISTS rebuild_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, stage INTEGER NOT NULL,
    op TEXT NOT NULL, undo TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
"""

_STAT_FIELDS = {"joins", "leaves"}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._settings: dict[tuple[int, str], str | None] = {}

    async def connect(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()
        async with self.conn.execute("SELECT guild_id, key, value FROM settings") as cur:
            for row in await cur.fetchall():
                self._settings[(row["guild_id"], row["key"])] = row["value"]

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ---- generic helpers ----
    async def execute(self, sql: str, *params: Any) -> int:
        async with self.conn.execute(sql, params) as cur:
            last = cur.lastrowid
        await self.conn.commit()
        return last

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        await self.conn.executemany(sql, rows)
        await self.conn.commit()

    async def fetchone(self, sql: str, *params: Any) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, *params: Any) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ---- settings (cached) ----
    def get_setting(self, guild_id: int, key: str, default: str | None = None) -> str | None:
        return self._settings.get((guild_id, key), default)

    def get_json(self, guild_id: int, key: str, default: Any = None) -> Any:
        raw = self.get_setting(guild_id, key)
        return default if raw is None else json.loads(raw)

    async def set_setting(self, guild_id: int, key: str, value: Any) -> None:
        value = None if value is None else str(value)
        self._settings[(guild_id, key)] = value
        await self.execute(
            "INSERT INTO settings (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (guild_id, key) DO UPDATE SET value = excluded.value",
            guild_id, key, value,
        )

    async def set_json(self, guild_id: int, key: str, value: Any) -> None:
        await self.set_setting(guild_id, key, json.dumps(value))

    # ---- stats & logs ----
    async def bump_stat(self, guild_id: int, day: str, field: str, n: int = 1) -> None:
        if field not in _STAT_FIELDS:
            raise ValueError(field)
        await self.execute(
            f"INSERT INTO daily_stats (guild_id, day, {field}) VALUES (?, ?, ?) "
            f"ON CONFLICT (guild_id, day) DO UPDATE SET {field} = {field} + excluded.{field}",
            guild_id, day, n,
        )

    async def add_activity(self, rows: list[tuple[int, int, str, int]]) -> None:
        if rows:
            await self.executemany(
                "INSERT INTO member_activity (guild_id, user_id, day, messages) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, day) DO UPDATE SET messages = messages + excluded.messages",
                rows,
            )

    async def log_action(self, guild_id: int, kind: str, detail: str) -> None:
        await self.execute(
            "INSERT INTO actions (guild_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
            guild_id, kind, detail[:1000], time.time(),
        )

    # ---- approvals ----
    async def create_approval(self, guild_id: int, kind: str, summary: str, payload: dict) -> int:
        return await self.execute(
            "INSERT INTO approvals (guild_id, kind, summary, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            guild_id, kind, summary, json.dumps(payload), time.time(),
        )

    async def decide_approval(self, approval_id: int, status: str) -> aiosqlite.Row | None:
        row = await self.fetchone("SELECT * FROM approvals WHERE id = ?", approval_id)
        if row is None or row["status"] != "pending":
            return row
        await self.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?", status, time.time(), approval_id)
        return row

    # ---- trades (JSON rows) ----
    async def get_trade(self, guild_id: int, trade_id: int) -> dict | None:
        row = await self.fetchone("SELECT * FROM trades WHERE id = ? AND guild_id = ?", trade_id, guild_id)
        if row is None:
            return None
        out = dict(row)
        out["data"] = json.loads(row["data"])
        out["original"] = json.loads(row["original"])
        return out

    async def edit_log(self, guild_id: int, target: str, field: str, old: Any, new: Any, editor_id: int) -> None:
        await self.execute(
            "INSERT INTO edit_log (guild_id, target, field, old_value, new_value, editor_id, created_at) VALUES (?,?,?,?,?,?,?)",
            guild_id, target, field, None if old is None else str(old), None if new is None else str(new), editor_id, time.time(),
        )
