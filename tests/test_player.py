import asyncio
import random
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("pyrogram")

import config
import wordseek as ws
from app import AppState, GameRunner, WordSeekPlayer
from conftest import FakeMessage
from db import MemoryDatabase

CHAT = -1001234567890
NAMES = {"c": "correct", "p": "present", "a": "absent"}


def bot_msg(text="", markup=None, mid=1):
    return FakeMessage(text, uid=42, username="WordSeekBot", chat_id=CHAT, mid=mid, reply_markup=markup)


def feedback(history):
    rows = [[NS(text=ch.upper(), callback_data=f"wh:{NAMES[p]}:{ch.upper()}") for ch, p in zip(g, pat)]
            for g, pat in history]
    return NS(inline_keyboard=rows)


class FakeWordSeekServer:
    """Behaves like the real game: valid-word check, shared history, feedback buttons, win message."""

    def __init__(self, secret, others_guess=()):
        self.secret = secret
        self.valid = set(ws.valid_guesses(len(secret)))
        self.history = []
        self.guesses = []
        self.player = None
        self.mid = 100
        self.pending = list(others_guess)

    async def send(self, chat_id, text, **kw):  # replaces pyrogram's send_message
        self.guesses.append(text)
        asyncio.get_running_loop().create_task(self._respond(text))

    async def _respond(self, guess):
        await asyncio.sleep(0)
        self.mid += 1
        if guess not in self.valid:
            msg = bot_msg(f"{guess} is not a valid {len(guess)}-letter word.", mid=self.mid)
        elif any(g == guess for g, _ in self.history):
            msg = bot_msg("Someone has already guessed your word. Please try another one!", mid=self.mid)
        elif guess == self.secret:
            msg = bot_msg(f"Congrats! You guessed it correctly.\nCorrect Word: {guess}\nAdded 20 to the leaderboard.", mid=self.mid)
        else:
            self.history.append((guess, ws.pattern(guess, self.secret)))
            msg = bot_msg("", feedback(self.history), mid=self.mid)
        await self.player.handle(msg)
        if self.pending and not msg.text.startswith("Congrats"):  # another human guesses too
            other = self.pending.pop(0)
            if other != self.secret and other in self.valid and all(g != other for g, _ in self.history):
                self.history.append((other, ws.pattern(other, self.secret)))
                self.mid += 1
                await self.player.handle(bot_msg("", feedback(self.history), mid=self.mid))


def make(cfg_env, secret, **kw):
    cfg = config.validate({**cfg_env, "GAME_BOT_USERNAME": "WordSeekBot"})
    db, state = MemoryDatabase(), AppState()
    server = FakeWordSeekServer(secret, **kw)

    async def no_sleep(_):
        return None

    runner = GameRunner(cfg, db, state, server.send, sleep=no_sleep, clock=lambda: 0.0)
    player = WordSeekPlayer(cfg, db, state, runner)
    server.player = player
    return player, server, db, state


async def wait_until(pred, timeout=20):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.parametrize("secret", ["plant", "crane", "zesty", "ghost"])
def test_plays_full_game_until_it_wins(env, secret):
    async def go():
        player, server, db, state = make(env, secret)
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        assert await wait_until(lambda: player.game is None and db.events and db.events[-1]["kind"] == "game_finished")
        return server, db

    server, db = asyncio.run(go())
    assert server.guesses[-1] == secret
    assert 1 <= len(server.guesses) <= 12
    finished = [e for e in db.events if e["kind"] == "game_finished"][0]
    assert finished["result"] == "won" and finished["mine"] is True


@pytest.mark.parametrize("length", [4, 6])
def test_other_lengths(env, length):
    secret = ws.answers(length)[50]

    async def go():
        player, server, db, state = make(env, secret)
        await player.handle(bot_msg(f"Game started! Guess the {length}-letter word!"))
        assert await wait_until(lambda: player.game is None and any(e["kind"] == "game_finished" for e in db.events))
        return server

    assert asyncio.run(go()).guesses[-1] == secret


def test_uses_other_players_guesses_on_the_board(env):
    async def go():
        player, server, db, state = make(env, "plant", others_guess=["stone", "blame", "fruit"])
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        assert await wait_until(lambda: player.game is None and any(e["kind"] == "game_finished" for e in db.events))
        return server

    server = asyncio.run(go())
    assert server.guesses[-1] == "plant"


def test_joins_game_already_in_progress(env):
    async def go():
        player, server, db, state = make(env, "plant")
        server.history = [("crane", ws.pattern("crane", "plant"))]
        await player.handle(bot_msg("", feedback(server.history), mid=99))  # no "started" message seen
        assert await wait_until(lambda: player.game is None and any(e["kind"] == "game_finished" for e in db.events))
        return server

    assert asyncio.run(go()).guesses[-1] == "plant"


def test_invalid_word_is_not_retried(env):
    async def go():
        player, server, db, state = make(env, "plant")
        server.valid.discard(ws.OPENERS[5])  # game refuses the opener
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        assert await wait_until(lambda: player.game is None and any(e["kind"] == "game_finished" for e in db.events))
        return server

    server = asyncio.run(go())
    assert server.guesses.count(ws.OPENERS[5]) == 1 and server.guesses[-1] == "plant"


def test_paused_runner_does_not_guess_and_resume_continues(env):
    async def go():
        player, server, db, state = make(env, "plant")
        await db.set_paused(True, 1)
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        await asyncio.sleep(0.2)
        assert server.guesses == []
        await db.set_paused(False, 1)
        await player.handle(bot_msg("", feedback([("crane", ws.pattern("crane", "plant"))]), mid=5))
        assert await wait_until(lambda: any(e["kind"] == "game_finished" for e in db.events))
        return server

    assert asyncio.run(go()).guesses[-1] == "plant"


def test_other_senders_and_chatter_ignored(env):
    async def go():
        player, server, db, state = make(env, "plant")
        await player.handle(FakeMessage("Game started! Guess the 5-letter word!", uid=7, username="human", chat_id=CHAT))
        await player.handle(bot_msg("hello everyone"))
        await asyncio.sleep(0.1)
        return player, server

    player, server = asyncio.run(go())
    assert player.game is None and server.guesses == []


def test_game_over_and_ended_stop_the_worker(env):
    async def go():
        player, server, db, state = make(env, "plant")
        server.pending = []
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        await wait_until(lambda: server.guesses)
        await player.handle(bot_msg("🎮 Game Ended\nCorrect Word: plant"))
        await asyncio.sleep(0.1)
        return player, db

    player, db = asyncio.run(go())
    assert player.game is None and db.events[-1]["result"] == "ended"


def test_new_game_replaces_old_one(env):
    async def go():
        player, server, db, state = make(env, "plant")
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        first = player.game
        await player.handle(bot_msg("Game started! Guess the 4-letter word!"))
        assert player.game is not first and player.game.length == 4
        player._cancel_worker()

    asyncio.run(go())


def test_unreadable_feedback_is_reported_once(env, caplog):
    async def go():
        player, server, db, state = make(env, "plant")
        with caplog.at_level("WARNING", logger="game_user"):
            for _ in range(3):
                await player.handle(bot_msg("5-letter mode\n2/30 guesses"))

    asyncio.run(go())
    assert sum("NO readable letter buttons" in r.getMessage() for r in caplog.records) == 1


def test_no_feedback_timeout_stops_after_two_tries(env, monkeypatch):
    async def go():
        player, server, db, state = make(env, "plant")

        async def silent(chat_id, text, **kw):
            server.guesses.append(text)

        player.runner._send = silent
        monkeypatch.setattr(WordSeekPlayer, "FEEDBACK_TIMEOUT", 0.05)
        await player.handle(bot_msg("Game started! Guess the 5-letter word!"))
        await wait_until(lambda: player._worker is None or player._worker.done())
        return server

    assert len(asyncio.run(go()).guesses) == 2


def test_solver_stuck_is_logged_not_crashing(env):
    async def go():
        player, server, db, state = make(env, "plant")
        await player.handle(bot_msg("", feedback([("crane", "ccccc"), ("pilot", "ccccc")]), mid=3))
        await wait_until(lambda: any(e["kind"] == "solver_stuck" for e in db.events))
        return db

    assert any(e["kind"] == "solver_stuck" for e in asyncio.run(go()).events)
