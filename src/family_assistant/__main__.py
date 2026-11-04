"""Entry point.

    python -m family_assistant          run the Telegram bot
    python -m family_assistant check    read-only check of the Yuvomi connection and grocery list name
    python -m family_assistant gmail-auth <label>   one-time Gmail consent (run on a PC with a browser)
    python -m family_assistant heb-stores "<address>"   find your HEB store ID
    python -m family_assistant heb-login [--browser]  save HEB credentials / log in
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


def _heb_command() -> str:
    return os.environ.get("HEB_MCP_COMMAND", "").strip() or "texas-grocery-mcp"


async def heb_stores(address: str) -> int:
    from .heb import HebError, HebMcp

    try:
        async with HebMcp(_heb_command(), _local_config_dir()) as heb:
            result = await heb.call("store_search", {"address": address})
    except HebError as exc:
        print(f"PROBLEM: {exc}")
        return 1
    stores = result.get("stores") or []
    if not stores:
        print(f"The HEB tool found no stores near {address!r}.")
        if result.get("error"):
            print(f"It said: {result['error']}")
        failed = [a for a in result.get("attempts") or [] if a.get("result") == "error"]
        if failed:
            print(f"{len(failed)} lookup(s) to HEB failed, so this is likely the tool, not your address.")
            print("Find your store's numeric ID on heb.com instead (see README, HEB section).")
        return 1
    for s in stores:
        online = "curbside" if s.get("supports_curbside") else "in-store only"
        miles = f"{s['distance_miles']} mi, " if s.get("distance_miles") is not None else ""
        print(f"  {s.get('store_id'):>6}  {s.get('name')} - {s.get('address')} ({miles}{online})")
    print("Put the ID of your curbside store in HEB_DEFAULT_STORE.")
    return 0


async def heb_login(use_browser: bool) -> int:
    """Save HEB credentials (container) or log in with a visible browser (PC)."""
    import getpass

    from .heb import HebError, HebMcp

    config_dir = _local_config_dir()
    try:
        async with HebMcp(_heb_command(), config_dir) as heb:
            if not use_browser:
                print("Your HEB email and password are stored encrypted in the heb folder of CONFIG_DIR.")
                email = input("HEB email: ").strip()
                password = getpass.getpass("HEB password (not shown): ")
                saved = await heb.call("session_save_credentials", {"email": email, "password": password})
                del password
                if not saved.get("success"):
                    print(f"PROBLEM: {saved.get('error') or 'could not save credentials'}")
                    return 1
                print(saved.get("message", "Credentials saved."))
            args = {"headless": False, "use_saved_credentials": False} if use_browser else \
                   {"headless": True, "use_saved_credentials": True}
            for _ in range(10):
                result = await heb.call("session_refresh", args)
                status = result.get("status")
                if status == "success":
                    break
                if status == "human_action_required":
                    action = result.get("action") or "login"
                    if not use_browser:
                        print(f"HEB wants a person to complete '{action}', which cannot be seen from here.")
                        print("Run `python -m family_assistant heb-login --browser` on a PC instead (see README).")
                        return 1
                    answer = input(f"Complete the {action} in the browser window, then press Enter (q to quit): ")
                    if answer.strip().lower() == "q":
                        return 1
                    continue
                print(f"PROBLEM: login failed ({result.get('error_type') or result.get('error') or 'no details'}).")
                return 1
            else:
                print("PROBLEM: gave up after 10 attempts.")
                return 1
            auth = await heb.call("cart_check_auth")
    except HebError as exc:
        print(f"PROBLEM: {exc}")
        return 1
    if not auth.get("authenticated"):
        print("PROBLEM: HEB still reports you as logged out.")
        return 1
    print(f"OK: logged in to HEB. Session saved under {config_dir / 'heb'}.")
    if use_browser:
        print("Copy auth.json from that folder to the container's /config/heb/.")
    return 0


def main() -> int:
    load_dotenv()
    if len(sys.argv) > 1 and sys.argv[1] == "heb-stores":
        if len(sys.argv) < 3:
            print('Usage: python -m family_assistant heb-stores "<address or ZIP>"')
            return 2
        return asyncio.run(heb_stores(" ".join(sys.argv[2:])))
    if len(sys.argv) > 1 and sys.argv[1] == "heb-login":
        return asyncio.run(heb_login(use_browser="--browser" in sys.argv[2:]))
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
