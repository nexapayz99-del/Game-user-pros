import asyncio

import pytest

import app as appmod
from app import AppState, ControlPanel, normalize_chat_ref, parse_command
from conftest import FakeMessage, FakeQuery, FakeUserClient, run
from db import MemoryDatabase

pytest.importorskip("pyrogram")


def make_panel(cfg, connected=True):
    db = MemoryDatabase()
    state = AppState()
    state.user_connected = connected
    client = FakeUserClient()
    panel = ControlPanel(cfg, db, state, lambda: client, bot_username="MyBot", keyboard=lambda: "KB")
    return panel, db, state, client


# ---- command parsing ------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/start", ("start", "")),
        ("/START", ("start", "")),
        ("/start@MyBot", ("start", "")),
        ("/start@mybot", ("start", "")),
        ("/join @group_name", ("join", "@group_name")),
        ("/leave   target ", ("leave", "target")),
        ("/status extra words", ("status", "extra words")),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text, "MyBot") == expected


@pytest.mark.parametrize("text", ["start", "hello", "", None, "/", "/start@OtherBot", " text /start"])
def test_parse_command_rejects(text):
    assert parse_command(text, "MyBot") is None


@pytest.mark.parametrize(
    "arg,expected",
    [("@public_group", "@public_group"), ("public_group", "public_group"), ("-1001234567890", -1001234567890),
     ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123"), ("t.me/somegroup", "@somegroup"), ("https://t.me/GupshupMusiczone", "@GupshupMusiczone"), ("https://t.me/joinchat/AbCdEf12", "https://t.me/joinchat/AbCdEf12")],
)
def test_normalize_chat_ref_ok(arg, expected):
    assert normalize_chat_ref(arg) == expected


@pytest.mark.parametrize("arg", ["", "a b", "@x", "rm -rf /", "https://evil.example/x"])
def test_normalize_chat_ref_bad(arg):
    assert normalize_chat_ref(arg) is None


# ---- authorization ---------------------------------------------------------
def test_is_admin():
    assert appmod.is_admin({1, 2}, 1)
    assert not appmod.is_admin({1, 2}, 3)
    assert not appmod.is_admin({1, 2}, None)


def test_unauthorized_user_gets_clear_rejection(cfg):
    panel, db, *_ = make_panel(cfg)
    msg = FakeMessage("/start", uid=999)
    assert run(panel.handle_command(msg)) is True
    assert len(msg.replies) == 1 and "Not authorized" in msg.replies[0][0]


@pytest.mark.parametrize("cmd", ["/pause", "/resume", "/join @abcde", "/leave target", "/status", "/session"])
def test_unauthorized_cannot_run_admin_commands(cfg, cmd):
    panel, db, state, client = make_panel(cfg)
    msg = FakeMessage(cmd, uid=999)
    run(panel.handle_command(msg))
    assert "Not authorized" in msg.replies[0][0]
    assert not db.is_paused() and client.joined == [] and client.left == []


def test_sender_missing_is_rejected(cfg):
    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/start", uid=None)
    run(panel.handle_command(msg))
    assert "Not authorized" in msg.replies[0][0]


# ---- /start and friends ----------------------------------------------------
def test_start_replies_with_panel_for_admin(cfg):
    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/start", uid=111)
    run(panel.handle_command(msg))
    text, kwargs = msg.replies[0]
    assert "CONTROL PANEL" in text and kwargs["reply_markup"] == "KB"
    assert "S" * 20 not in text  # session never shown


def test_start_works_while_paused(cfg):
    panel, db, *_ = make_panel(cfg)
    run(db.set_paused(True, 111))
    msg = FakeMessage("/start", uid=222)
    run(panel.handle_command(msg))
    assert "CONTROL PANEL" in msg.replies[0][0]


def test_start_addressed_to_other_bot_ignored(cfg):
    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/start@OtherBot", uid=111)
    assert run(panel.handle_command(msg)) is False and msg.replies == []


def test_unknown_command_not_handled(cfg):
    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/dance", uid=111)
    assert run(panel.handle_command(msg)) is False and msg.replies == []


def test_keyboard_failure_falls_back_to_plain_reply(cfg):
    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/start", uid=111, fail_with_markup=True)
    run(panel.handle_command(msg))
    assert len(msg.replies) == 1 and "reply_markup" not in msg.replies[0][1]


def test_total_reply_failure_does_not_raise(cfg):
    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/start", uid=111, fail_reply=5)
    assert run(panel.handle_command(msg)) is True  # logged, not raised


def test_replies_are_plain_text(cfg):
    from pyrogram import enums

    panel, *_ = make_panel(cfg)
    msg = FakeMessage("/status", uid=111)
    run(panel.handle_command(msg))
    assert msg.replies[0][1]["parse_mode"] == enums.ParseMode.DISABLED


def test_status_session_health(cfg):
    panel, db, state, client = make_panel(cfg)
    for cmd, needle in (("/status", "ACTIVE"), ("/session", "999"), ("/health", "database: ok")):
        msg = FakeMessage(cmd, uid=111)
        run(panel.handle_command(msg))
        assert needle in msg.replies[0][0], cmd
    msg = FakeMessage("/session", uid=111)
    run(panel.handle_command(msg))
    assert "owner_acc" in msg.replies[0][0] and cfg.user_session not in msg.replies[0][0]


def test_status_when_user_client_down(cfg):
    panel, db, state, client = make_panel(cfg, connected=False)
    state.user_error = "AuthKeyUnregistered"
    msg = FakeMessage("/status", uid=111)
    run(panel.handle_command(msg))
    assert "NOT CONNECTED" in msg.replies[0][0] and "AuthKeyUnregistered" in msg.replies[0][0]


def test_pause_resume_persist_and_status(cfg):
    panel, db, *_ = make_panel(cfg)
    m1 = FakeMessage("/pause", uid=111)
    run(panel.handle_command(m1))
    assert db.is_paused() and "paused" in m1.replies[0][0]
    m2 = FakeMessage("/status", uid=111)
    run(panel.handle_command(m2))
    assert "PAUSED" in m2.replies[0][0]
    m3 = FakeMessage("/resume", uid=111)
    run(panel.handle_command(m3))
    assert not db.is_paused() and "resumed" in m3.replies[0][0]
    assert [e["kind"] for e in db.events] == ["paused", "resumed"]


def test_pause_warns_when_persist_fails(cfg):
    panel, db, *_ = make_panel(cfg)
    db.persist_ok = False
    msg = FakeMessage("/pause", uid=111)
    run(panel.handle_command(msg))
    assert db.is_paused() and "could not save" in msg.replies[0][0]


def test_join_and_leave(cfg):
    panel, db, state, client = make_panel(cfg)
    run(panel.handle_command(FakeMessage("/join @some_group", uid=111)))
    assert client.joined == ["@some_group"]
    run(panel.handle_command(FakeMessage("/leave target", uid=111)))
    assert client.left == [cfg.target_group_id]
    run(panel.handle_command(FakeMessage("/leave @some_group", uid=111)))
    assert len(client.left) == 2


def test_join_usage_and_invalid(cfg):
    panel, *_ = make_panel(cfg)
    m1 = FakeMessage("/join", uid=111)
    m2 = FakeMessage("/join !!!bad", uid=111)
    run(panel.handle_command(m1))
    run(panel.handle_command(m2))
    assert "Usage" in m1.replies[0][0] and "does not look like" in m2.replies[0][0]


def test_command_exception_is_reported_not_raised(cfg):
    panel, db, state, client = make_panel(cfg)

    async def boom():
        raise RuntimeError("kaput")

    panel.status_text = boom
    msg = FakeMessage("/status", uid=111)
    assert run(panel.handle_command(msg)) is True
    assert "failed" in msg.replies[0][0]
    assert any(e["kind"] == "error" for e in db.events)


# ---- callbacks ---------------------------------------------------------------
def test_callbacks_are_always_answered(cfg):
    panel, db, *_ = make_panel(cfg)
    for data in ("runner:pause", "runner:resume", "runner:status", "runner:session", "runner:help", "runner:bogus"):
        q = FakeQuery(data, uid=111)
        run(panel.handle_callback(q))
        assert q.answers, data


def test_callback_unauthorized(cfg):
    panel, db, *_ = make_panel(cfg)
    q = FakeQuery("runner:pause", uid=5)
    run(panel.handle_callback(q))
    assert q.answers == [("Not authorized.", True)] and not db.is_paused()


def test_callback_pause_changes_state(cfg):
    panel, db, *_ = make_panel(cfg)
    run(panel.handle_callback(FakeQuery("runner:pause", uid=111)))
    assert db.is_paused()


def test_callback_exception_still_answers(cfg):
    panel, db, *_ = make_panel(cfg)

    async def boom():
        raise RuntimeError("x")

    panel.status_text = boom
    q = FakeQuery("runner:status", uid=111)
    run(panel.handle_callback(q))
    assert q.answers and q.answers[-1][1] is True


def test_status_warns_when_filter_is_the_control_bot(cfg, env):
    import config

    env["GAME_BOT_USERNAME"] = "MyBot"
    panel, *_ = make_panel(config.validate(env))
    msg = FakeMessage("/status", uid=111)
    run(panel.handle_command(msg))
    assert "WARNING" in msg.replies[0][0]
