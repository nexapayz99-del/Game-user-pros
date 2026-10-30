"""Regression tests for the original bug + startup/shutdown behaviour."""
import asyncio
import logging
import os
import signal
import threading

import pytest

pytest.importorskip("pyrogram")

import app as appmod
from app import Application, InstanceLock, RedactFilter, count_handlers
from conftest import run
from db import MemoryDatabase


def test_handlers_register_when_client_built_inside_loop(cfg):
    async def go():
        a = Application(cfg, db=MemoryDatabase())
        a._build_clients()
        a.register_handlers_once()
        await asyncio.sleep(0)
        return count_handlers(a.control_bot), count_handlers(a.user_client)

    assert run(go()) == (2, 1)


def test_ORIGINAL_BUG_client_built_before_loop_loses_handlers(cfg):
    """Documents the root cause: building the Client outside the running loop."""
    from pyrogram import Client

    # Python 3.10 (the VPS) implicitly creates a default loop on get_event_loop();
    # newer Pythons do not, so create it explicitly to mimic the original situation.
    stale = asyncio.new_event_loop()
    asyncio.set_event_loop(stale)
    client = Client("x", api_id=1, api_hash="0" * 32, bot_token="1:x", in_memory=True)  # built BEFORE asyncio.run

    async def go():
        appmod.register_control_handlers(client, appmod.ControlPanel(cfg, MemoryDatabase(), appmod.AppState(), lambda: None))
        await asyncio.sleep(0.2)
        return count_handlers(client)

    try:
        assert run(go()) == 0  # handlers silently never registered
    finally:
        asyncio.set_event_loop(None)
        stale.close()


def test_handlers_register_only_once(cfg):
    async def go():
        a = Application(cfg, db=MemoryDatabase())
        a._build_clients()
        a.register_handlers_once()
        with pytest.raises(RuntimeError):
            a.register_handlers_once()

    run(go())


def test_clients_use_in_memory_sessions(cfg):
    async def go():
        a = Application(cfg, db=MemoryDatabase())
        a._build_clients()
        return a.control_bot.in_memory, a.user_client.in_memory

    assert run(go()) == (True, True)


def test_instance_lock_blocks_second_copy(tmp_path):
    p = tmp_path / "lock"
    first, second = InstanceLock(p), InstanceLock(p)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


class StubClient:
    def __init__(self):
        self.is_connected = True
        self.stopped = False

    async def stop(self):
        self.stopped = True
        self.is_connected = False


def test_graceful_shutdown_stops_clients_and_db(cfg):
    async def go():
        db = MemoryDatabase()
        a = Application(cfg, db=db)
        a.control_bot, a.user_client = StubClient(), StubClient()
        await a.shutdown()
        return a, db

    a, db = run(go())
    assert a.control_bot.stopped and a.user_client.stopped
    assert db.connected is False and db.events[-1]["kind"] == "stopped"


def test_sigterm_triggers_graceful_shutdown(cfg, monkeypatch):
    async def go():
        db = MemoryDatabase()
        a = Application(cfg, db=db)
        a._build_clients = lambda: setattr(a, "control_bot", StubClient()) or setattr(a, "user_client", StubClient())
        a.register_handlers_once = lambda: None

        async def fake_start():
            return None

        a._start_control_bot = fake_start
        a._start_user_client = fake_start
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, os.kill, os.getpid(), signal.SIGTERM)
        code = await asyncio.wait_for(a.run(), 10)
        return code, a, db

    code, a, db = run(go())
    assert code == appmod.EXIT_OK and a.control_bot.stopped and a.user_client.stopped


def test_database_failure_at_startup_returns_exit_db(cfg):
    class DeadDB(MemoryDatabase):
        async def connect(self):
            raise ConnectionError("down")

    assert run(Application(cfg, db=DeadDB()).run()) == appmod.EXIT_DB


def test_user_client_failure_keeps_control_bot_up(cfg):
    async def go():
        a = Application(cfg, db=MemoryDatabase())

        class BadUser:
            async def start(self):
                raise RuntimeError("auth")

        a.user_client = BadUser()
        await a._start_user_client()
        return a.state

    st = run(go())
    assert st.user_connected is False and st.user_error == "RuntimeError"


def test_log_redaction(cfg, caplog):
    f = RedactFilter(cfg.secrets)
    rec = logging.LogRecord("t", logging.INFO, "", 1, "token=%s", (cfg.bot_token,), None)
    f.filter(rec)
    assert cfg.bot_token not in rec.getMessage() and "***" in rec.getMessage()
    try:
        raise ValueError(cfg.user_session)
    except ValueError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, "", 1, "oops", (), sys.exc_info())
    f.filter(rec)
    assert cfg.user_session not in rec.getMessage()


def test_check_config_cli_does_not_print_secrets(tmp_path, monkeypatch, capsys, env):
    envfile = tmp_path / ".env"
    envfile.write_text("\n".join(f"{k}={v}" for k, v in env.items()))
    os.chmod(envfile, 0o600)
    import config

    monkeypatch.setattr(config, "DEFAULT_ENV_PATH", envfile)
    monkeypatch.setattr(config, "load_config", lambda: config.validate(env))
    assert appmod.main(["--check-config"]) == appmod.EXIT_OK
    out = capsys.readouterr().out
    for key in ("API_HASH", "BOT_TOKEN", "USER_SESSION", "MONGO_URI"):
        assert env[key] not in out
    assert "valid" in out


def test_check_config_reports_problems_by_name(monkeypatch, capsys, env):
    import config

    env["ADMIN_IDS"] = "oops"
    monkeypatch.setattr(config, "load_config", lambda: config.validate(env))
    assert appmod.main(["--check-config"]) == appmod.EXIT_CONFIG
    assert "ADMIN_IDS" in capsys.readouterr().out
