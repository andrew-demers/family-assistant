"""Settings, read only from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    pass


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value


def _bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _heb_store(env: dict[str, str]) -> str:
    store = env.get("HEB_DEFAULT_STORE", "").strip()
    if store and not store.isdigit():
        raise ConfigError("HEB_DEFAULT_STORE must be a numeric HEB store ID (find it with heb-stores)")
    return store


def validate_yuvomi_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError("YUVOMI_URL must start with http:// or https://, e.g. http://192.168.1.10:3000")
    if url.lower().endswith("/api/v1"):
        raise ConfigError("YUVOMI_URL should be the base address without /api/v1")
    return url


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_telegram_ids: frozenset[int]
    user_names: dict[int, str]
    anthropic_api_key: str
    claude_model: str
    claude_effort: str
    claude_refusal_fallback: bool
    max_tool_iterations: int
    max_tokens_per_message: int
    rate_limit_per_minute: int
    yuvomi_url: str
    yuvomi_api_token: str
    yuvomi_grocery_list: str
    tz: ZoneInfo
    dry_run: bool
    config_dir: Path
    log_level: str
    # Phase 2: Gmail. Empty gmail_accounts disables the scan.
    gmail_accounts: tuple[str, ...] = ()
    gmail_account_names: dict[str, str] = field(default_factory=dict)
    gmail_event_mode: str = "propose"
    gmail_query: str = "newer_than:2d -category:promotions -category:social"
    gmail_max_per_scan: int = 25
    gmail_scan_times: tuple[time, ...] = ()
    # Phase 3: HEB. Empty store disables ordering.
    heb_store_id: str = ""
    heb_mcp_command: str = "texas-grocery-mcp"

    @property
    def db_path(self) -> Path:
        return self.config_dir / "assistant.sqlite3"

    @property
    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [self.telegram_bot_token, self.anthropic_api_key, self.yuvomi_api_token]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        ids_raw = _required(env, "ALLOWED_TELEGRAM_IDS")
        try:
            ids = [int(part) for part in ids_raw.split(",") if part.strip()]
        except ValueError as exc:
            raise ConfigError("ALLOWED_TELEGRAM_IDS must be comma-separated numeric IDs") from exc
        if not ids:
            raise ConfigError("ALLOWED_TELEGRAM_IDS is empty")

        names = [n.strip() for n in env.get("TELEGRAM_USER_NAMES", "").split(",")]
        user_names = {uid: name for uid, name in zip(ids, names) if name}

        effort = env.get("CLAUDE_EFFORT", "medium").strip().lower() or "medium"
        if effort not in {"low", "medium", "high"}:
            raise ConfigError("CLAUDE_EFFORT must be low, medium or high")

        tz_name = env.get("TZ", "America/Chicago").strip() or "America/Chicago"
        tz = ZoneInfo(tz_name)

        accounts = tuple(a.strip().lower() for a in env.get("GMAIL_ACCOUNTS", "").split(",") if a.strip())
        for label in accounts:
            if not re.fullmatch(r"[a-z0-9_-]{1,32}", label):
                raise ConfigError(f"GMAIL_ACCOUNTS label {label!r}: use lowercase letters, digits, - or _")
        account_names = {
            label: name.strip()
            for label, name in zip(accounts, env.get("GMAIL_ACCOUNT_NAMES", "").split(","))
            if name.strip()
        }
        mode = env.get("GMAIL_EVENT_MODE", "propose").strip().lower() or "propose"
        if mode not in {"propose", "auto"}:
            raise ConfigError("GMAIL_EVENT_MODE must be propose or auto")
        scan_times = []
        for raw in (env.get("GMAIL_SCAN_TIMES", "07:00,17:00").split(",")):
            raw = raw.strip()
            if not raw:
                continue
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw):
                raise ConfigError(f"GMAIL_SCAN_TIMES entry {raw!r} must be HH:MM")
            scan_times.append(time(int(raw[:2]), int(raw[3:]), tzinfo=tz))

        return cls(
            telegram_bot_token=_required(env, "TELEGRAM_BOT_TOKEN"),
            allowed_telegram_ids=frozenset(ids),
            user_names=user_names,
            anthropic_api_key=_required(env, "ANTHROPIC_API_KEY"),
            claude_model=_required(env, "CLAUDE_MODEL"),
            claude_effort=effort,
            claude_refusal_fallback=_bool(env, "CLAUDE_REFUSAL_FALLBACK", True),
            max_tool_iterations=_int(env, "MAX_TOOL_ITERATIONS", 6),
            max_tokens_per_message=_int(env, "MAX_TOKENS_PER_MESSAGE", 60000),
            rate_limit_per_minute=_int(env, "RATE_LIMIT_PER_MINUTE", 10),
            yuvomi_url=validate_yuvomi_url(_required(env, "YUVOMI_URL")),
            yuvomi_api_token=_required(env, "YUVOMI_API_TOKEN"),
            yuvomi_grocery_list=env.get("YUVOMI_GROCERY_LIST", "Shopping").strip() or "Shopping",
            tz=tz,
            dry_run=_bool(env, "DRY_RUN", True),
            config_dir=Path(env.get("CONFIG_DIR", "/config")),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            gmail_accounts=accounts,
            gmail_account_names=account_names,
            gmail_event_mode=mode,
            gmail_query=env.get("GMAIL_QUERY", "").strip() or "newer_than:2d -category:promotions -category:social",
            gmail_max_per_scan=_int(env, "GMAIL_MAX_PER_SCAN", 25),
            gmail_scan_times=tuple(scan_times),
            heb_store_id=_heb_store(env),
            heb_mcp_command=env.get("HEB_MCP_COMMAND", "").strip() or "texas-grocery-mcp",
        )
