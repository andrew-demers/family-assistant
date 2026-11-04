import pytest

from family_assistant.actions import ActionResult
from family_assistant.config import ConfigError, Settings
from family_assistant.logsetup import redact
from family_assistant.render import render_confirmation
from family_assistant.store import RateLimiter, Store

BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:abc",
    "ALLOWED_TELEGRAM_IDS": "111, 222",
    "TELEGRAM_USER_NAMES": "Andrew,Sam",
    "ANTHROPIC_API_KEY": "sk-test",
    "CLAUDE_MODEL": "claude-sonnet-5-5",
    "YUVOMI_URL": "http://yuvomi:3000/",
    "YUVOMI_API_TOKEN": "yv-token",
}


def test_settings_parse_and_defaults():
    s = Settings.from_env(BASE_ENV)
    assert s.allowed_telegram_ids == {111, 222}
    assert s.user_names == {111: "Andrew", 222: "Sam"}
    assert s.yuvomi_url == "http://yuvomi:3000"
    assert s.dry_run is True
    assert s.yuvomi_grocery_list == "Shopping"
    assert str(s.tz) == "America/Chicago"


@pytest.mark.parametrize("override", [
    {"ALLOWED_TELEGRAM_IDS": "abc"},
    {"CLAUDE_MODEL": ""},
    {"DRY_RUN": "maybe"},
    {"CLAUDE_EFFORT": "max"},
    {"YUVOMI_URL": "192.168.1.10:3000"},
    {"YUVOMI_URL": "http://yuvomi:3000/api/v1"},
])
def test_settings_reject_bad_values(override):
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE_ENV, **override})


def test_redact_secrets_cards_and_bot_tokens():
    out = redact(
        "key sk-ant-secret123 card 4111 1111 1111 1234 bot 1234567890:AAH" + "x" * 33,
        ["sk-ant-secret123"],
    )
    assert "sk-ant-secret123" not in out
    assert "4111" not in out and "[CARD ...1234]" in out
    assert "AAH" not in out


def test_render_event_and_duplicates():
    results = [
        ActionResult("event", "done", "Dentist", "Calendar", record={
            "id": 9, "title": "Dentist", "start_datetime": "2026-11-06T15:00", "end_datetime": "2026-11-06T16:00",
            "all_day": 0, "location": "Main St"}),
        ActionResult("grocery", "duplicate", "milk", "Shopping", record={"id": 3, "name": "Milk"}),
    ]
    assert render_confirmation(results, dry_run=False) == (
        "Done.\nAdded to Calendar: Dentist, Fri Nov 6, 3:00 PM - 4:00 PM at Main St [#9].\n"
        "Already on Shopping, skipped: Milk [#3]."
    )


def test_history_alternates_and_expires(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    store.append_exchange(1, "add an event dentist", "What day and time?")
    assert store.recent_history(1) == [
        {"role": "user", "content": "add an event dentist"},
        {"role": "assistant", "content": "What day and time?"},
    ]
    assert store.recent_history(2) == []
    import time

    assert store.recent_history(1, now=time.time() + 31 * 60) == []
    store.close()


def test_rate_limiter():
    limiter = RateLimiter(2)
    assert limiter.allow(1, now=0) and limiter.allow(1, now=1)
    assert not limiter.allow(1, now=2)
    assert limiter.allow(2, now=2)
    assert limiter.allow(1, now=62)
