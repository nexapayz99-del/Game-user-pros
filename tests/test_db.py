import asyncio

import pytest

from conftest import run
from db import Database, redact


class Boom:
    """Collection whose operations always fail."""

    async def update_one(self, *a, **k):
        raise ConnectionError("mongodb+srv://user:SECRETPW@host down")

    async def insert_one(self, *a, **k):
        raise ConnectionError("mongodb+srv://user:SECRETPW@host down")


class FakeDB:
    state = Boom()
    events = Boom()


def broken_db():
    db = Database("mongodb://u:SECRETPW@h/", "x", secrets=["SECRETPW"])
    db._db = FakeDB()
    return db


def test_log_event_never_raises():
    assert run(broken_db().log_event("x", a=1)) is False


def test_set_paused_failure_keeps_cache_and_reports():
    db = broken_db()
    assert run(db.set_paused(True, 1)) is False
    assert db.is_paused() is True  # in-memory state still honoured
    assert db.connected is False


def test_log_error_never_raises():
    assert run(broken_db().log_error("where", RuntimeError("x"))) is False


def test_redact():
    assert redact("a SECRETPW b", ["SECRETPW", ""]) == "a *** b"


def test_error_text_is_redacted():
    db = broken_db()
    assert "SECRETPW" not in db._err(ConnectionError("pw SECRETPW"))


def test_connect_failure_raises_cleanly():
    pytest.importorskip("motor")
    db = Database("mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=300", "x")

    async def go():
        with pytest.raises(Exception):
            await asyncio.wait_for(db.connect(), 15)
        await db.close()

    run(go())
    assert db.connected is False


def test_ping_without_client_is_false():
    assert run(Database("mongodb://x", "y").ping()) is False
