"""Entry point.

    python -m family_assistant          run the Telegram bot
    python -m family_assistant check    read-only check of the Yuvomi connection and grocery list name
    python -m family_assistant gmail-auth <label>   one-time Gmail consent (run on a PC with a browser)
    python -m family_assistant healthcheck   exit 0 if the running bot's heartbeat is fresh (Docker HEALTHCHECK)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from pathlib import Path

from .config import ConfigError, Settings, validate_yuvomi_url
from .logsetup import setup_logging
from .yuvomi import YuvomiClient, YuvomiError


def load_dotenv(path: Path = Path(".env")) -> None:
    """For local runs without Docker: copy KEY=VALUE lines into the environment.

    Variables already set in the environment win. Docker passes .env itself via env_file.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


async def check() -> int:
    url = os.environ.get("YUVOMI_URL", "").strip()
    token = os.environ.get("YUVOMI_API_TOKEN", "").strip()
    wanted = os.environ.get("YUVOMI_GROCERY_LIST", "Shopping").strip() or "Shopping"
    if not url or not token:
        print("Set YUVOMI_URL and YUVOMI_API_TOKEN in .env (or the environment) first.")
        return 2
    try:
        url = validate_yuvomi_url(url)
    except ConfigError as exc:
        print(f"PROBLEM: {exc}")
        return 2
    client = YuvomiClient(url, token)
    ok = True
    try:
        try:
            lists = await client.list_shopping_lists()
            print("Shopping lists in Yuvomi:")
            for item in lists:
                ro = " (read-only)" if item.get("accepts_new_items") == 0 else ""
                print(f'  - "{item.get("name")}" id={item.get("id")} open items={item.get("item_total", 0) - item.get("item_checked", 0)}{ro}')
            match = [i for i in lists if (i.get("name") or "").casefold() == wanted.casefold()]
            if match:
                print(f'OK: grocery list "{wanted}" found (id={match[0]["id"]}).')
            else:
                ok = False
                print(f'PROBLEM: no list named "{wanted}". Set YUVOMI_GROCERY_LIST to one of the names above.')
        except YuvomiError as exc:
            ok = False
            hint = " Check the token and its shopping scope." if exc.status in (401, 403) else ""
            print(f"Shopping: FAILED ({exc}).{hint}")
        for label, call in (("Tasks", client.list_tasks()), ("Calendar", client.list_events("2000-01-01", "2000-01-02"))):
            try:
                await call
                print(f"{label}: OK (read access works)")
            except YuvomiError as exc:
                ok = False
                print(f"{label}: FAILED ({exc})")
    finally:
        await client.aclose()
    return 0 if ok else 1


def gmail_auth(label: str) -> int:
    """One-time Google consent for one Gmail account, in a local browser."""
    from .gmail import authorize_account

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env first.")
        return 2
    config_dir = _local_config_dir()
    print(f"Opening a browser. Sign in with the Gmail account for '{label}' and allow read-only access.")
    try:
        path = authorize_account(config_dir, label.lower(), client_id, client_secret)
    except ValueError as exc:
        print(f"PROBLEM: {exc}")
        return 2
    print(f"Saved token to {path.resolve()}")
    print("Copy it to the container's /config/google/ folder (on Unraid: appdata/family-assistant/google/).")
    return 0


def _local_config_dir() -> Path:
    return Path(os.environ.get("CONFIG_DIR", "").strip() or "config")


def main() -> int:
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "healthcheck":
        from .health import check as health_check

        ok, detail = health_check()
        print(detail)
        return 0 if ok else 1
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        return asyncio.run(check())
    if len(sys.argv) > 1 and sys.argv[1] == "gmail-auth":
        if len(sys.argv) != 3:
            print("Usage: python -m family_assistant gmail-auth <account-label>   (e.g. andrew)")
            return 2
        return gmail_auth(sys.argv[2])
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, settings.secrets)

    from .bot import Bot

    logging.getLogger(__name__).info(
        "starting: model=%s dry_run=%s allowed_users=%d", settings.claude_model, settings.dry_run,
        len(settings.allowed_telegram_ids),
    )
    Bot(settings).build().run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
