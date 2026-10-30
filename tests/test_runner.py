import asyncio

import pytest

pytest.importorskip("pyrogram")

from pyrogram.errors import BadRequest, FloodWait

from app import AppState, GameRunner
from conftest import FakeMessage, run
from db import MemoryDatabase


class Harness:
    def __init__(self, cfg, send_errors=()):
        self.db = MemoryDatabase()
        self.state = AppState()
        self.sent = []
        self.sleeps = []
        self.now = 1000.0
        self.errors = list(send_errors)

        async def send(chat_id, text, **kw):
            if self.errors:
                raise self.errors.pop(0)
            self.sent.append((chat_id, text, kw))

        async def sleep(sec):
            self.sleeps.append(sec)
            self.now += sec

        self.runner = GameRunner(cfg, self.db, self.state, send, sleep=sleep, clock=lambda: self.now)


def group_msg(text, username="gamebot", mid=7):
    return FakeMessage(text, uid=42, username=username, chat_id=-1001234567890, mid=mid)


def test_replies_with_correct_length_word_as_reply(cfg):
    h = Harness(cfg)
    word = run(h.runner.handle(group_msg("Find a 6 letter word", mid=77)))
    assert word and len(word) == 6
    chat_id, text, kw = h.sent[0]
    assert chat_id == -1001234567890 and text == word
    assert "reply_to_message_id" not in kw  # plain message, not a reply


def test_unrelated_messages_ignored(cfg):
    h = Harness(cfg)
    for t in ("hello", "gm all", "I have 5 apples", None):
        assert run(h.runner.handle(group_msg(t))) is None
    assert h.sent == []


def test_paused_runner_does_not_reply_and_resume_does(cfg):
    h = Harness(cfg)
    run(h.db.set_paused(True, 1))
    assert run(h.runner.handle(group_msg("Find a 5 letter word"))) is None
    assert h.sent == []
    run(h.db.set_paused(False, 1))
    assert run(h.runner.handle(group_msg("Find a 5 letter word"))) is not None
    assert len(h.sent) == 1


def test_game_bot_filter_only_accepts_that_sender(cfg, env):
    import config

    env["GAME_BOT_USERNAME"] = "GameBot"
    h = Harness(config.validate(env))
    assert run(h.runner.handle(group_msg("Find a 5 letter word", username="someone_else"))) is None
    assert run(h.runner.handle(group_msg("Find a 5 letter word", username="GAMEBOT"))) is not None
    assert len(h.sent) == 1


def test_no_filter_accepts_any_sender(cfg):
    h = Harness(cfg)
    assert run(h.runner.handle(group_msg("Length: 5", username=None))) is not None


def test_min_interval_enforced(cfg):
    h = Harness(cfg)
    run(h.runner.handle(group_msg("Length: 5")))
    assert h.sleeps == []  # first send is immediate
    h.now += 1.0  # only 1 s later
    run(h.runner.handle(group_msg("Length: 6")))
    assert h.sleeps and abs(h.sleeps[0] - (cfg.send_interval - 1.0)) < 1e-6


def test_concurrent_prompts_are_serialized(cfg):
    h = Harness(cfg)
    active = {"n": 0, "max": 0}
    orig = h.runner._send

    async def tracked(*a, **kw):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.01)
        active["n"] -= 1
        return await orig(*a, **kw)

    h.runner._send = tracked

    async def go():
        await asyncio.gather(*(h.runner.handle(group_msg("Length: 5", mid=i)) for i in range(5)))

    run(go())
    assert active["max"] == 1 and len(h.sent) == 5


def test_floodwait_sleeps_then_retries(cfg):
    h = Harness(cfg, send_errors=[FloodWait(value=30)])
    word = run(h.runner.handle(group_msg("Length: 7")))
    assert word and len(h.sent) == 1
    assert any(s >= 30 for s in h.sleeps)
    assert any(e["kind"] == "flood_wait" for e in h.db.events)


def test_rpc_error_is_logged_not_raised(cfg):
    h = Harness(cfg, send_errors=[BadRequest()])
    assert run(h.runner.handle(group_msg("Length: 7"))) is None
    assert any(e["kind"] == "error" for e in h.db.events)


def test_unexpected_error_is_logged_not_raised(cfg):
    h = Harness(cfg, send_errors=[RuntimeError("boom")])
    assert run(h.runner.handle(group_msg("Length: 7"))) is None


def test_no_word_available_does_not_crash(cfg, monkeypatch):
    import app

    monkeypatch.setattr(app, "choose_word", lambda *a, **k: None)
    h = Harness(cfg)
    assert run(h.runner.handle(group_msg("Length: 7"))) is None
    assert any(e["kind"] == "word_unavailable" for e in h.db.events)


def test_wrong_length_word_is_never_sent(cfg, monkeypatch):
    import app

    monkeypatch.setattr(app, "choose_word", lambda n, **k: "toolongword")
    h = Harness(cfg)
    assert run(h.runner.handle(group_msg("Length: 5"))) is None and h.sent == []


def test_pause_during_wait_cancels_send(cfg):
    h = Harness(cfg)
    run(h.runner.handle(group_msg("Length: 5")))
    h.now += 1.0

    async def pausing_sleep(sec):
        await h.db.set_paused(True, 1)

    h.runner._sleep = pausing_sleep
    assert run(h.runner.handle(group_msg("Length: 6"))) is None
    assert len(h.sent) == 1


def test_database_failure_does_not_break_handler(cfg):
    h = Harness(cfg)

    async def bad_log(*a, **k):
        return False

    h.db.log_event = bad_log
    assert run(h.runner.handle(group_msg("Length: 5"))) is not None


def test_rejected_sender_logged_once_with_reason(cfg, env, caplog):
    import config

    env["GAME_BOT_USERNAME"] = "RealGame"
    h = Harness(config.validate(env))
    with caplog.at_level("INFO", logger="game_user"):
        for _ in range(3):
            run(h.runner.handle(group_msg("Find a 5 letter word", username="other_bot")))
    lines = [r.getMessage() for r in caplog.records if "ignored a group message" in r.getMessage()]
    assert len(lines) == 1 and "@other_bot" in lines[0] and "realgame" in lines[0]


def test_unmatched_prompt_from_game_bot_is_logged(cfg, env, caplog):
    import config

    env["GAME_BOT_USERNAME"] = "RealGame"
    h = Harness(config.validate(env))
    with caplog.at_level("INFO", logger="game_user"):
        run(h.runner.handle(group_msg("Guess the secret word now!", username="realgame")))
    assert any("no word length found" in r.getMessage() for r in caplog.records)
