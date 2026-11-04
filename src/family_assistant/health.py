"""Container health: the bot refreshes a heartbeat file while it is working; Docker checks its age.

Healthy means: the bot process is running, Telegram answered a getMe call within the last few
minutes, and no other copy of the bot has been fighting over the token recently. Yuvomi and Gmail are
not part of it, so the container is not restarted or flagged because another service is down.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

HEARTBEAT_INTERVAL = 60  # seconds between heartbeat attempts
MAX_AGE = 3 * HEARTBEAT_INTERVAL  # older than this is unhealthy
CONFLICT_GRACE = 2 * HEARTBEAT_INTERVAL  # a Telegram conflict this recent means unhealthy


def heartbeat_path() -> Path:
    # Outside /config so the appdata share is not written every minute.
    return Path(os.environ.get("HEALTH_FILE", "").strip() or Path(tempfile.gettempdir()) / "family-assistant.heartbeat")


def write_heartbeat(path: Path | None = None, now: float | None = None) -> None:
    path = path or heartbeat_path()
    path.write_text(str(time.time() if now is None else now), encoding="ascii")


def check(path: Path | None = None, now: float | None = None) -> tuple[bool, str]:
    path = path or heartbeat_path()
    now = time.time() if now is None else now
    try:
        stamp = float(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False, "no heartbeat yet (bot starting, or not reaching Telegram)"
    age = now - stamp
    if age > MAX_AGE:
        return False, f"last heartbeat {int(age)}s ago (bot not reaching Telegram, or another copy is running)"
    return True, f"ok (heartbeat {int(age)}s ago)"
