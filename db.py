"""MongoDB (Motor) persistence: pause state, admin changes, events, errors.

Design rules
* The Motor client is created inside ``connect()`` so it binds to the running
  event loop (same class of bug as the original Pyrogram problem).
* ``log_event`` / ``set_paused`` never raise: a database hiccup must never
  crash a Telegram handler. The last known pause state is cached in memory.
* Error text stored/logged is redacted and truncated; secrets never reach Mongo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("game_user.db")

STATE_ID = "runner"
EVENT_TTL_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every known secret in ``text`` with ***."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


class Database:
    def __init__(self, uri: str, db_name: str, secrets: Iterable[str] = ()):
        self._uri = uri
        self._db_name = db_name
        self._secrets = tuple(secrets)
        self._client = None
        self._db = None
        self._paused = False  # cache; authoritative copy lives in Mongo
        self.connected = False

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        """Create the client, verify the connection with a ping, load state.

        Raises on failure (startup should fail fast with a clear message).
        """
        from motor.motor_asyncio import AsyncIOMotorClient

        self._client = AsyncIOMotorClient(
            self._uri,
            serverSelectionTimeoutMS=8000,
            connectTimeoutMS=8000,
            appname="game-user-pro",
        )
        self._db = self._client[self._db_name]
        await self._client.admin.command("ping")
        self.connected = True
        try:
            await self._db.events.create_index("ts", expireAfterSeconds=EVENT_TTL_DAYS * 86400)
        except Exception as exc:  # index problems are not fatal
            log.warning("Could not create events TTL index: %s", self._err(exc))
        doc = await self._db.state.find_one({"_id": STATE_ID})
        self._paused = bool(doc and doc.get("paused", False))
        log.info("MongoDB connected (db=%s, paused=%s)", self._db_name, self._paused)

    async def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception as exc:
                log.warning("Error closing MongoDB client: %s", self._err(exc))
        self.connected = False

    async def ping(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.admin.command("ping")
            self.connected = True
        except Exception as exc:
            self.connected = False
            log.warning("MongoDB ping failed: %s", self._err(exc))
        return self.connected

    # -- helpers -----------------------------------------------------------
    def _err(self, exc: BaseException) -> str:
        return redact(f"{type(exc).__name__}: {exc}", self._secrets)[:300]

    # -- pause state ---------------------------------------------------------
    def is_paused(self) -> bool:
        """Synchronous, cache-backed (no DB round trip per game message)."""
        return self._paused

    async def set_paused(self, paused: bool, admin_id: Optional[int] = None) -> bool:
        """Update the cache immediately, then persist. Returns True if persisted."""
        self._paused = bool(paused)
        try:
            await self._db.state.update_one(
                {"_id": STATE_ID},
                {"$set": {"paused": self._paused, "updated_at": _now(), "updated_by": admin_id}},
                upsert=True,
            )
        except Exception as exc:
            log.error("Could not persist pause state: %s", self._err(exc))
            self.connected = False
            return False
        await self.log_event("paused" if paused else "resumed", admin_id=admin_id)
        return True

    # -- events ------------------------------------------------------------
    async def log_event(self, kind: str, **fields: Any) -> bool:
        """Best-effort operational event. Never raises."""
        try:
            doc: Dict[str, Any] = {"ts": _now(), "kind": kind}
            for key, value in fields.items():
                doc[key] = redact(value, self._secrets)[:500] if isinstance(value, str) else value
            await self._db.events.insert_one(doc)
            return True
        except Exception as exc:
            log.warning("event %r not stored: %s", kind, self._err(exc))
            return False

    async def log_error(self, where: str, exc: BaseException, **fields: Any) -> bool:
        return await self.log_event("error", where=where, error=self._err(exc), **fields)

    async def recent_events(self, limit: int = 5) -> List[dict]:
        try:
            cursor = self._db.events.find({}, {"_id": 0}).sort("ts", -1).limit(limit)
            return [d async for d in cursor]
        except Exception as exc:
            log.warning("recent_events failed: %s", self._err(exc))
            return []


class MemoryDatabase:
    """Same interface, no Mongo. Used by tests and by diagnose.py fallback."""

    def __init__(self):
        self._paused = False
        self.connected = True
        self.events: List[dict] = []
        self.persist_ok = True

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        self.connected = False

    async def ping(self) -> bool:
        return self.connected

    def is_paused(self) -> bool:
        return self._paused

    async def set_paused(self, paused: bool, admin_id: Optional[int] = None) -> bool:
        self._paused = bool(paused)
        await self.log_event("paused" if paused else "resumed", admin_id=admin_id)
        return self.persist_ok

    async def log_event(self, kind: str, **fields: Any) -> bool:
        self.events.append({"kind": kind, **fields})
        return True

    async def log_error(self, where: str, exc: BaseException, **fields: Any) -> bool:
        return await self.log_event("error", where=where, error=f"{type(exc).__name__}: {exc}", **fields)

    async def recent_events(self, limit: int = 5) -> List[dict]:
        return list(reversed(self.events[-limit:]))
