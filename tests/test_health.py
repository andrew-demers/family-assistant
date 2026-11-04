"""Container health check: heartbeat file freshness, and when the bot refreshes it."""

import time
from types import SimpleNamespace

from telegram.error import Conflict, NetworkError

from family_assistant import health
from family_assistant.bot import Bot
from family_assistant.config import Settings

from .test_support import BASE_ENV


def test_check_fresh_stale_and_missing(tmp_path):
    path = tmp_path / "hb"
    assert health.check(path)[0] is False
    health.write_heartbeat(path, now=1000.0)
    assert health.check(path, now=1000.0 + health.MAX_AGE - 1) == (True, f"ok (heartbeat {health.MAX_AGE - 1}s ago)")
    ok, detail = health.check(path, now=1000.0 + health.MAX_AGE + 1)
    assert not ok and "another copy" in detail
    path.write_text("garbage")
    assert health.check(path)[0] is False


class FakeTgBot:
    def __init__(self, fail=False):
        self.fail = fail

    async def get_me(self):
        if self.fail:
            raise NetworkError("down")
        return SimpleNamespace(username="bot")


def make_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_FILE", str(tmp_path / "hb"))
    return Bot(Settings.from_env({**BASE_ENV, "CONFIG_DIR": str(tmp_path)}))


async def test_heartbeat_written_only_when_telegram_answers(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch)
    await bot.heartbeat(SimpleNamespace(bot=FakeTgBot(fail=True)))
    assert not (tmp_path / "hb").exists()
    await bot.heartbeat(SimpleNamespace(bot=FakeTgBot()))
    assert health.check()[0] is True


async def test_recent_conflict_stops_the_heartbeat(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch)
    await bot.on_error(None, SimpleNamespace(error=Conflict("terminated by other getUpdates request")))
    await bot.heartbeat(SimpleNamespace(bot=FakeTgBot()))
    assert not (tmp_path / "hb").exists()
    bot.last_conflict = time.monotonic() - health.CONFLICT_GRACE - 1
    await bot.heartbeat(SimpleNamespace(bot=FakeTgBot()))
    assert (tmp_path / "hb").exists()


def test_heartbeat_job_scheduled(tmp_path, monkeypatch):
    app = make_bot(tmp_path, monkeypatch).build()
    assert "heartbeat" in [j.name for j in app.job_queue.jobs()]
