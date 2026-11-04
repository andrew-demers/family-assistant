"""SQLite state in /config: short conversation history per chat, plus an in-memory rate limiter."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

HISTORY_TTL_SECONDS = 30 * 60
HISTORY_MAX_MESSAGES = 6


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                ts REAL NOT NULL
            )"""
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS history_chat ON history (chat_id, ts)")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_emails (
                account TEXT NOT NULL,
                message_id TEXT NOT NULL,
                events_found INTEGER NOT NULL,
                processed_at REAL NOT NULL,
                PRIMARY KEY (account, message_id)
            );
            CREATE TABLE IF NOT EXISTS proposal_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL REFERENCES proposal_batches(id),
                account TEXT NOT NULL,
                email_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                sender TEXT NOT NULL,
                title TEXT NOT NULL,
                start TEXT NOT NULL,
                end_at TEXT,
                all_day INTEGER NOT NULL,
                location TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                handled_by TEXT,
                result TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposal_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                proposal_id INTEGER,
                batch_id INTEGER
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def recent_history(self, chat_id: int, now: float | None = None) -> list[dict]:
        """Recent text turns, oldest first, always starting with a user turn."""
        now = time.time() if now is None else now
        rows = self._db.execute(
            "SELECT role, content FROM history WHERE chat_id = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
            (chat_id, now - HISTORY_TTL_SECONDS, HISTORY_MAX_MESSAGES),
        ).fetchall()
        turns = [{"role": role, "content": content} for role, content in reversed(rows)]
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        # The API requires alternating roles; drop a dangling user turn at the end.
        if turns and turns[-1]["role"] == "user":
            turns.pop()
        return turns

    def append_exchange(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        now = time.time()
        self._db.executemany(
            "INSERT INTO history (chat_id, role, content, ts) VALUES (?, ?, ?, ?)",
            [(chat_id, "user", user_text, now), (chat_id, "assistant", assistant_text, now)],
        )
        self._db.execute("DELETE FROM history WHERE ts < ?", (now - HISTORY_TTL_SECONDS,))
        self._db.commit()


    # ------------------------------------------------------------ Gmail scan state

    def is_processed(self, account: str, message_id: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM processed_emails WHERE account = ? AND message_id = ?", (account, message_id),
        ).fetchone() is not None

    def mark_processed(self, account: str, message_id: str, events_found: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO processed_emails (account, message_id, events_found, processed_at) VALUES (?, ?, ?, ?)",
            (account, message_id, events_found, time.time()),
        )
        self._db.commit()

    def new_batch(self) -> int:
        cur = self._db.execute("INSERT INTO proposal_batches (created_at) VALUES (?)", (time.time(),))
        self._db.commit()
        return cur.lastrowid

    def add_proposal(self, batch_id: int, *, account: str, email_id: str, subject: str, sender: str,
                     title: str, start: str, end: str | None, all_day: bool, location: str | None,
                     status: str = "pending") -> int:
        cur = self._db.execute(
            """INSERT INTO proposals (batch_id, account, email_id, subject, sender, title, start, end_at,
                                      all_day, location, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, account, email_id, subject, sender, title, start, end, int(all_day), location, status,
             time.time()),
        )
        self._db.commit()
        return cur.lastrowid

    def has_open_proposal(self, title: str, start: str) -> bool:
        """A proposal for the same event that is still waiting or was added."""
        rows = self._db.execute(
            "SELECT title, start FROM proposals WHERE status IN ('pending', 'working', 'added') AND substr(start, 1, 10) = ?",
            (start[:10],),
        ).fetchall()
        key = " ".join(title.split()).casefold()
        return any(" ".join(t.split()).casefold() == key and s[:16] == start[:16] for t, s in rows)

    def _proposal_rows(self, where: str, args: tuple) -> list[dict]:
        cur = self._db.execute(f"SELECT * FROM proposals WHERE {where} ORDER BY id", args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_proposal(self, proposal_id: int) -> dict | None:
        rows = self._proposal_rows("id = ?", (proposal_id,))
        return rows[0] if rows else None

    def batch_proposals(self, batch_id: int) -> list[dict]:
        return self._proposal_rows("batch_id = ?", (batch_id,))

    def claim(self, proposal_id: int, handler: str) -> bool:
        """Atomically take a pending proposal. Only the first caller gets True."""
        cur = self._db.execute(
            "UPDATE proposals SET status = 'working', handled_by = ? WHERE id = ? AND status = 'pending'",
            (handler, proposal_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    def finish(self, proposal_id: int, status: str, result: str) -> None:
        self._db.execute("UPDATE proposals SET status = ?, result = ? WHERE id = ?", (status, result, proposal_id))
        self._db.commit()

    def release(self, proposal_id: int, result: str) -> None:
        """Return a claimed proposal to pending (the add failed before anything was written)."""
        self._db.execute(
            "UPDATE proposals SET status = 'pending', handled_by = NULL, result = ? WHERE id = ?",
            (result, proposal_id),
        )
        self._db.commit()

    def add_proposal_message(self, chat_id: int, message_id: int, *, proposal_id: int | None = None,
                             batch_id: int | None = None) -> None:
        self._db.execute(
            "INSERT INTO proposal_messages (chat_id, message_id, proposal_id, batch_id) VALUES (?, ?, ?, ?)",
            (chat_id, message_id, proposal_id, batch_id),
        )
        self._db.commit()

    def proposal_messages(self, *, proposal_id: int | None = None, batch_id: int | None = None) -> list[tuple[int, int]]:
        if proposal_id is not None:
            rows = self._db.execute(
                "SELECT chat_id, message_id FROM proposal_messages WHERE proposal_id = ?", (proposal_id,),
            )
        else:
            rows = self._db.execute(
                "SELECT chat_id, message_id FROM proposal_messages WHERE batch_id = ?", (batch_id,),
            )
        return [tuple(r) for r in rows.fetchall()]


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self._limit = per_minute
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[user_id]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True
