import base64
import json

import httpx
import pytest

from family_assistant.gmail import MAX_BODY_CHARS, GmailAccount, GmailAuthError, parse_message, token_path


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(payload: dict, id_: str = "m1") -> dict:
    return {"id": id_, "snippet": "snip", "payload": payload}


HEADERS = [
    {"name": "From", "value": "Smile Dental <office@smile.test>"},
    {"name": "Subject", "value": "Appointment reminder"},
    {"name": "Date", "value": "Tue, 3 Nov 2026 09:00:00 -0600"},
]


def test_parse_prefers_plain_text_and_skips_attachments():
    msg = message({
        "mimeType": "multipart/mixed", "headers": HEADERS, "parts": [
            {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("See you Friday at 3pm.")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
            ]},
            {"mimeType": "text/plain", "filename": "notes.txt", "body": {"attachmentId": "a1"}},
        ],
    })
    email = parse_message("andrew", msg)
    assert email.body == "See you Friday at 3pm."
    assert email.subject == "Appointment reminder"
    assert email.sender == "Smile Dental <office@smile.test>"
    assert email.account == "andrew" and email.message_id == "m1"


def test_parse_html_only_strips_tags_and_scripts():
    html = "<html><head><style>x{}</style></head><body><p>Party&nbsp;Sat</p><script>evil()</script><br>2pm</body></html>"
    email = parse_message("a", message({"mimeType": "text/html", "headers": HEADERS, "body": {"data": b64(html)}}))
    assert "Party" in email.body and "2pm" in email.body
    assert "evil" not in email.body and "<" not in email.body


def test_parse_truncates_long_bodies():
    email = parse_message("a", message({"mimeType": "text/plain", "headers": HEADERS,
                                        "body": {"data": b64("x" * (MAX_BODY_CHARS + 10))}}))
    assert len(email.body) == MAX_BODY_CHARS and email.truncated


def test_token_path_rejects_bad_labels(tmp_path):
    assert token_path(tmp_path, "andrew") == tmp_path / "google" / "andrew.json"
    with pytest.raises(ValueError):
        token_path(tmp_path, "../etc")


def write_token(tmp_path) -> GmailAccount:
    path = token_path(tmp_path, "andrew")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "token": "access-123", "refresh_token": "refresh-456", "client_id": "cid", "client_secret": "cs",
        "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2099-01-01T00:00:00Z",
    }))
    return path


async def test_account_lists_and_reads_with_bearer_token(tmp_path):
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer access-123"
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"] == "newer_than:2d"
            return httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t"}]})
        assert request.url.params["format"] == "full"
        return httpx.Response(200, json=message({"mimeType": "text/plain", "headers": HEADERS,
                                                 "body": {"data": b64("hi")}}))

    account = GmailAccount("andrew", write_token(tmp_path), transport=httpx.MockTransport(handle))
    assert await account.list_message_ids("newer_than:2d", 10) == ["m1"]
    email = await account.get_email("m1")
    assert email.body == "hi"
    assert str(seen[0].url).startswith("https://gmail.googleapis.com/gmail/v1/users/me/messages")
    await account.aclose()


async def test_missing_token_and_401_raise_auth_error(tmp_path):
    missing = GmailAccount("sam", token_path(tmp_path, "sam"))
    with pytest.raises(GmailAuthError, match="run gmail-auth"):
        await missing.list_message_ids("x", 1)

    account = GmailAccount("andrew", write_token(tmp_path),
                           transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(GmailAuthError):
        await account.list_message_ids("x", 1)
