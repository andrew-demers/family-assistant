"""Wiring checks against the real SDKs, with the network mocked."""

import json

import anthropic
import httpx2  # the anthropic SDK's HTTP layer

from family_assistant.actions import Actions
from family_assistant.agent import NO_TEXT, Agent
from family_assistant.bot import Bot
from family_assistant.config import Settings

from .fakes import FakeYuvomi
from .test_support import BASE_ENV


async def test_real_sdk_builds_the_request_we_expect():
    sent: list[dict] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append({"url": str(request.url), "headers": dict(request.headers), "body": json.loads(request.content)})
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": NO_TEXT}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    client = anthropic.AsyncAnthropic(
        api_key="sk-test", base_url="https://api.test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )
    result = await Agent(client, model="claude-sonnet-5-5").run(
        "Andrew", "hi", Actions(FakeYuvomi().client(), "Shopping", False),
    )
    assert result.reply == "Sorry, I did not get a reply for that. Nothing was changed."
    body = sent[0]["body"]
    assert sent[0]["url"].startswith("https://api.test/v1/messages")
    assert "server-side-fallback-2026-07-01" in sent[0]["headers"]["anthropic-beta"]
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "medium"}
    assert body["tools"][0]["strict"] is True
    assert "tool_choice" not in body


async def test_real_sdk_extraction_request():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from family_assistant.extract import EventExtractor
    from family_assistant.gmail import Email

    sent: list[dict] = []
    payload = '{"events": [{"title": "Dentist", "start": "2099-11-06T15:00", "end": null, "all_day": false, "location": null}]}'

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": payload}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    client = anthropic.AsyncAnthropic(
        api_key="sk-test", base_url="https://api.test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    )
    events = await EventExtractor(client, model="claude-sonnet-5-5").extract(
        Email("andrew", "m1", "s", "f", "d", "b"), datetime(2026, 11, 3, tzinfo=ZoneInfo("America/Chicago")),
    )
    assert [e.title for e in events] == ["Dentist"]
    assert sent[0]["output_config"]["format"]["type"] == "json_schema"
    assert sent[0]["output_config"]["effort"] == "low"
    assert "tools" not in sent[0]


def _update(app, user_id: int, text: str):
    from telegram import Update

    return Update.de_json({
        "update_id": 1,
        "message": {
            "message_id": 1, "date": 0, "text": text,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "X"},
            **({"entities": [{"type": "bot_command", "offset": 0, "length": len(text)}]} if text.startswith("/") else {}),
        },
    }, app.bot)


def test_only_allowlisted_users_match_any_handler(tmp_path):
    settings = Settings.from_env({**BASE_ENV, "CONFIG_DIR": str(tmp_path)})
    app = Bot(settings).build()
    # Button presses are checked inside Bot.on_button (tested in test_bot_phase2).
    handlers = [h for h in app.handlers[0] if hasattr(h, "filters")]
    # Every handler's filter (command and text) admits the allowed user and no one else.
    for text in ("add milk", "/start"):
        assert any(h.filters.check_update(_update(app, 111, text)) for h in handlers)
        assert not any(h.filters.check_update(_update(app, 999, text)) for h in handlers)
