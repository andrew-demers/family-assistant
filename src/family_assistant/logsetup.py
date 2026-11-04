"""Logging with secret redaction."""

from __future__ import annotations

import logging
import re

# Long digit runs (card numbers) and bot-token-shaped strings, in addition to known secrets.
_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}(\d{4})\b")
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED]")
    text = _BOT_TOKEN_RE.sub("[REDACTED]", text)
    return _CARD_RE.sub(lambda m: f"[CARD ...{m.group(1)}]", text)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message, self._secrets)
        record.args = None
        return True


def setup_logging(level: str, secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full request URLs at INFO; the Telegram one contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
