"""Configuration loading and validation.

Rules:
* Every problem is reported by VARIABLE NAME only - values are never put in
  error messages, logs or summaries.
* ``.env`` next to this file is the source of truth (it overrides stale
  variables exported in an old shell / service definition).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = PROJECT_DIR / ".env"
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

REQUIRED_VARS = (
    "API_ID",
    "API_HASH",
    "BOT_TOKEN",
    "USER_SESSION",
    "MONGO_URI",
    "TARGET_GROUP_ID",
    "ADMIN_IDS",
)

_API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_=+/-]{50,}$")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_DBNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}$")


class ConfigError(Exception):
    """Raised with a list of human-readable problems (never containing values)."""

    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str = field(repr=False)
    bot_token: str = field(repr=False)
    user_session: str = field(repr=False)
    mongo_uri: str = field(repr=False)
    mongo_db: str
    target_group_id: int
    admin_ids: frozenset
    game_bot_username: str  # lower-case, no "@"; "" means "any sender"
    send_interval: float
    log_level: str
    game_mode: str = "wordseek"

    @property
    def bot_id(self) -> int:
        """Numeric bot id = the public part of the token before the colon."""
        return int(self.bot_token.split(":", 1)[0])

    @property
    def secrets(self) -> Tuple[str, ...]:
        """Every string that must never appear in logs."""
        values = [self.api_hash, self.bot_token, self.user_session, self.mongo_uri]
        try:
            password = urlsplit(self.mongo_uri).password
        except ValueError:
            password = None
        if password:
            values.extend([password, unquote(password)])
        return tuple(v for v in values if v)

    def summary(self) -> dict:
        """Safe-to-print description of the configuration."""
        return {
            "api_id": "set",
            "api_hash": "set",
            "bot_token": "set (bot id %d)" % self.bot_id,
            "user_session": "set",
            "mongo_uri": "set",
            "mongo_db": self.mongo_db,
            "target_group_id": self.target_group_id,
            "admin_ids": sorted(self.admin_ids),
            "game_bot_username": self.game_bot_username or "(any sender)",
            "send_interval_seconds": self.send_interval,
            "log_level": self.log_level,
            "game_mode": self.game_mode,
        }


def _looks_like_placeholder(value: str) -> bool:
    low = value.lower()
    return low.startswith("your_") or low.startswith("<") or low in {"changeme", "xxx", "todo"}


def parse_admin_ids(raw: str) -> frozenset:
    """Parse "1, 2,3" into frozenset({1, 2, 3}). Raises ValueError on bad input."""
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise ValueError("not a positive integer")
        ids.add(int(part))
    if not ids:
        raise ValueError("empty")
    return frozenset(ids)


def validate(env: Mapping[str, str]) -> Config:
    """Validate a mapping of environment variables and build a Config."""
    env = dict(env)
    # Accept the old key name TARGET_GROUP as an alias for TARGET_GROUP_ID.
    if not (env.get("TARGET_GROUP_ID") or "").strip() and (env.get("TARGET_GROUP") or "").strip():
        env["TARGET_GROUP_ID"] = env["TARGET_GROUP"]
    problems: List[str] = []

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    def need(name: str) -> str:
        value = get(name)
        if not value:
            problems.append(f"{name} is missing or empty")
            return ""
        if _looks_like_placeholder(value):
            problems.append(f"{name} still contains a placeholder value")
            return ""
        return value

    api_id = 0
    raw = need("API_ID")
    if raw:
        if raw.isdigit() and int(raw) > 0:
            api_id = int(raw)
        else:
            problems.append("API_ID must be a positive integer")

    api_hash = need("API_HASH")
    if api_hash and not _API_HASH_RE.match(api_hash):
        problems.append("API_HASH has an unexpected format (expected 32 hex characters)")

    bot_token = need("BOT_TOKEN")
    if bot_token and not _BOT_TOKEN_RE.match(bot_token):
        problems.append("BOT_TOKEN has an unexpected format (expected <digits>:<secret> from @BotFather)")

    user_session = need("USER_SESSION")
    if user_session and not _SESSION_RE.match(user_session):
        problems.append("USER_SESSION does not look like a Pyrogram string session (check for spaces/line breaks)")

    mongo_uri = need("MONGO_URI")
    if mongo_uri and not mongo_uri.startswith(("mongodb://", "mongodb+srv://")):
        problems.append("MONGO_URI must start with mongodb:// or mongodb+srv://")

    mongo_db = get("MONGO_DB") or "game_user"
    if not _DBNAME_RE.match(mongo_db):
        problems.append("MONGO_DB may only contain letters, digits, '_' and '-' (max 63 chars)")

    target_group_id = 0
    raw = need("TARGET_GROUP_ID")
    if raw:
        try:
            target_group_id = int(raw)
            if target_group_id == 0:
                raise ValueError
        except ValueError:
            problems.append("TARGET_GROUP_ID must be a non-zero numeric chat id (e.g. -1001234567890)")

    admin_ids: frozenset = frozenset()
    raw = need("ADMIN_IDS")
    if raw:
        try:
            admin_ids = parse_admin_ids(raw)
        except ValueError:
            problems.append("ADMIN_IDS must be comma-separated positive numeric Telegram user IDs")

    game_bot = get("GAME_BOT_USERNAME").lstrip("@")
    if game_bot and not _USERNAME_RE.match(game_bot):
        problems.append("GAME_BOT_USERNAME is not a valid Telegram username")

    interval = 5.0
    raw = get("SEND_INTERVAL_SECONDS")
    if raw:
        try:
            interval = float(raw)
            if not (1.0 <= interval <= 3600.0):
                raise ValueError
        except ValueError:
            problems.append("SEND_INTERVAL_SECONDS must be a number between 1 and 3600")
            interval = 5.0

    log_level = (get("LOG_LEVEL") or "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        problems.append("LOG_LEVEL must be one of " + ", ".join(VALID_LOG_LEVELS))
        log_level = "INFO"

    game_mode = (get("GAME_MODE") or "wordseek").lower()
    if game_mode not in ("wordseek", "simple"):
        problems.append("GAME_MODE must be wordseek or simple")
        game_mode = "wordseek"

    if problems:
        raise ConfigError(problems)

    return Config(
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        user_session=user_session,
        mongo_uri=mongo_uri,
        mongo_db=mongo_db,
        target_group_id=target_group_id,
        admin_ids=admin_ids,
        game_bot_username=game_bot.lower(),
        send_interval=interval,
        log_level=log_level,
        game_mode=game_mode,
    )


def env_file_warnings(path: Path = DEFAULT_ENV_PATH) -> List[str]:
    """Non-fatal findings about the .env file itself."""
    try:
        mode = os.stat(path).st_mode
    except FileNotFoundError:
        return [f"No .env file at {path}; using the process environment only"]
    except OSError as exc:
        return [f"Cannot stat .env ({type(exc).__name__})"]
    if mode & 0o077:
        return [".env is readable by other users - run: chmod 600 .env"]
    return []


def load_config(env: Optional[Mapping[str, str]] = None, dotenv_path: Path = DEFAULT_ENV_PATH) -> Config:
    """Load .env (when ``env`` is not supplied) and validate."""
    if env is None:
        if Path(dotenv_path).exists():
            from dotenv import load_dotenv

            # override=True: the .env file wins over stale exported variables.
            load_dotenv(dotenv_path, override=True)
        env = os.environ
    return validate(env)
