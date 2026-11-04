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
