"""Game User Pro - control bot + authorized user client.

ROOT CAUSE FIXED HERE (see README "The original bug"):
    The original code created both ``pyrogram.Client`` objects (and registered
    handlers) in ``App.__init__``, *before* ``asyncio.run()`` created the real
    event loop. Pyrogram 2.0.x captures ``asyncio.get_event_loop()`` when a
    Client/Dispatcher is constructed, so ``add_handler()`` scheduled its
    registration task on a loop that never ran. Result: clients connected and
    authenticated fine, but ZERO handlers were registered, so /start (and every
    other update) was silently dropped.
    Fix: every Client is constructed INSIDE the running loop (``Application.run``),
    and handlers are registered exactly once, then verified.

Game modes (GAME_MODE): wordseek = plays WordSeek until solved (default);
    simple = answers 'N-letter word' prompts with one dictionary word.

Handler groups (lower runs first; one handler per group may run per update):
    control bot : group 0 = private command handler
                  group 1 = callback-query handler
    user client : group 0 = game-prompt handler (target group only)
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import re
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import config as cfgmod
from config import Config, ConfigError
from db import Database
from prompts import extract_length
from wordseek import choose_guess, classify_text, rows_from_markup
from words import RecentWords, choose_word

log = logging.getLogger("game_user")

EXIT_OK, EXIT_CONFIG, EXIT_DB, EXIT_LOCKED, EXIT_FATAL = 0, 2, 3, 4, 1

COMMANDS = ("start", "help", "status", "health", "pause", "resume", "session", "join", "leave")
ADMIN_ONLY_NOTICE = "Not authorized. This control panel is restricted to configured administrators."
TELEGRAM_CALL_TIMEOUT = 20


# --------------------------------------------------------------------------
# Logging with secret redaction
# --------------------------------------------------------------------------
class RedactFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
                record.exc_info = None
                record.exc_text = None
        except Exception:
            return True
        for secret in self._secrets:
            text = text.replace(secret, "***")
        record.msg, record.args = text, None
        return True


def setup_logging(cfg: Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter(cfg.secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    logging.getLogger("pyrogram").setLevel(logging.WARNING)  # very chatty at INFO


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------
_CMD_RE = re.compile(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", re.S)
_USERNAME_REF = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
_LINK_REF = re.compile(r"^(?:https?://)?(?:t|telegram)\.me/(?:\+|joinchat/)?[A-Za-z0-9_-]{4,}/?$", re.I)
_NUMERIC_REF = re.compile(r"^-?\d{5,}$")


def parse_command(text: Optional[str], bot_username: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """'/Start@MyBot  x y' -> ('start', 'x y'). None if not a command for this bot."""
    if not text:
        return None
    match = _CMD_RE.match(text.strip())
    if not match:
        return None
    cmd, addressed_to, args = match.group(1).lower(), match.group(2), match.group(3)
    if addressed_to and bot_username and addressed_to.lower() != bot_username.lower():
        return None
    return cmd, (args or "").strip()


_PUBLIC_LINK = re.compile(r"^(?:https?://)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?$", re.I)


def normalize_chat_ref(arg: str) -> Optional[Any]:
    """Validate a /join or /leave argument. Returns str / int, or None if invalid.

    Public links (t.me/name) become "@name": Pyrogram only accepts @username,
    numeric ids or *invite* links (t.me/+hash, t.me/joinchat/hash).
    """
    arg = (arg or "").strip()
    public = _PUBLIC_LINK.match(arg)
    if public:
        return "@" + public.group(1)
    if _NUMERIC_REF.match(arg):
        return int(arg)
    if _LINK_REF.match(arg) or _USERNAME_REF.match(arg):
        return arg
    return None


def is_admin(admin_ids, user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in admin_ids


# --------------------------------------------------------------------------
# Single-instance protection
# --------------------------------------------------------------------------
class InstanceLock:
    """flock-based lock so two copies cannot share one user session on a host."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


# --------------------------------------------------------------------------
# Telegram reply helper
# --------------------------------------------------------------------------
async def safe_reply(message, text: str, **kwargs) -> bool:
    """Reply as plain text (no Markdown to break). Logs success/failure with traceback."""
    from pyrogram import enums
    from pyrogram.errors import FloodWait

    for attempt in (1, 2):
        try:
            await message.reply_text(text, parse_mode=enums.ParseMode.DISABLED, **kwargs)
            log.info("reply sent ok (chars=%d)", len(text))
            return True
        except FloodWait as exc:
            if attempt == 2:
                log.error("reply failed: FloodWait persists")
                return False
            log.warning("reply FloodWait %ss", exc.value)
            await asyncio.sleep(min(int(exc.value) + 1, 60))
        except Exception:
            log.exception("reply FAILED")
            return False
    return False


def controls_keyboard():
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Resume", callback_data="runner:resume"),
                InlineKeyboardButton("Pause", callback_data="runner:pause"),
            ],
            [
                InlineKeyboardButton("Status", callback_data="runner:status"),
                InlineKeyboardButton("Session", callback_data="runner:session"),
            ],
            [InlineKeyboardButton("Help", callback_data="runner:help")],
        ]
    )


# --------------------------------------------------------------------------
# Control panel (all private-chat commands)
# --------------------------------------------------------------------------
class ControlPanel:
    def __init__(self, cfg: Config, db, runner_state: "AppState", get_user_client: Callable[[], Any],
                 bot_username: Optional[str] = None, keyboard: Callable[[], Any] = controls_keyboard):
        self.cfg = cfg
        self.db = db
        self.state = runner_state
        self._get_user_client = get_user_client
        self.bot_username = bot_username
        self._keyboard = keyboard

    # -- text builders ---------------------------------------------------
    HELP_TEXT = (
        "GAME RUNNER - CONTROL PANEL\n\n"
        "/status - application, account, target and pause state\n"
        "/health - quick health check\n"
        "/pause - pause automatic game replies\n"
        "/resume - resume automatic game replies\n"
        "/session - connected account ID and username\n"
        "/join @username - join a public group\n"
        "/leave @username - leave a group\n"
        "/leave target - leave the configured target group\n\n"
        "The session string is secret and is never shown."
    )

    def _user_client(self):
        client = self._get_user_client()
        return client if (client is not None and self.state.user_connected) else None

    async def status_text(self) -> str:
        paused = self.db.is_paused()
        lines = [
            "RUNNER STATUS",
            f"Runner: {'PAUSED' if paused else 'ACTIVE'}",
            f"Control bot: online (id {self.cfg.bot_id})",
            f"Database: {'connected' if self.db.connected else 'UNREACHABLE (using cached state)'}",
        ]
        client = self._user_client()
        if client is None:
            lines.append("User account: NOT CONNECTED" + (f" ({self.state.user_error})" if self.state.user_error else ""))
        else:
            try:
                me = await asyncio.wait_for(client.get_me(), TELEGRAM_CALL_TIMEOUT)
                lines.append(f"User account: connected (id {me.id})")
            except Exception as exc:
                lines.append(f"User account: ERROR ({type(exc).__name__})")
        lines.append(f"Target group: {self.cfg.target_group_id}")
        if client is not None:
            try:
                chat = await asyncio.wait_for(client.get_chat(self.cfg.target_group_id), TELEGRAM_CALL_TIMEOUT)
                lines.append(f"Target title: {chat.title or '(no title)'}")
            except Exception as exc:
                lines.append(f"Target access: NOT ACCESSIBLE ({type(exc).__name__})")
        lines.append(f"Game bot filter: {self.cfg.game_bot_username or '(any sender)'}")
        if self.bot_username and self.cfg.game_bot_username == self.bot_username.lower():
            lines.append("WARNING: GAME_BOT_USERNAME is this control bot itself. Set it to the GAME bot's "
                         "username, or leave it empty to accept prompts from anyone in the group.")
        lines.append(f"Game mode: {self.cfg.game_mode} ({self.state.game_info})")
        lines.append(f"Send interval: {self.cfg.send_interval:g}s")
        lines.append(f"Replies sent this run: {self.state.replies_sent}")
        return "\n".join(lines)

    async def health_text(self) -> str:
        db_ok = await self.db.ping()
        return (
            "HEALTH\n"
            f"control bot: ok\n"
            f"user client: {'ok' if self.state.user_connected else 'DOWN'}\n"
            f"database: {'ok' if db_ok else 'DOWN'}\n"
            f"runner: {'paused' if self.db.is_paused() else 'active'}"
        )

    async def session_text(self) -> str:
        client = self._user_client()
        if client is None:
            return "User account is not connected. Check USER_SESSION and the service logs."
        me = await asyncio.wait_for(client.get_me(), TELEGRAM_CALL_TIMEOUT)
        username = f"@{me.username}" if me.username else "(no username)"
        return f"CONNECTED ACCOUNT\nID: {me.id}\nUsername: {username}\nThe session string is never displayed."

    async def pause_text(self, paused: bool, admin_id: int) -> str:
        persisted = await self.db.set_paused(paused, admin_id)
        word = "paused" if paused else "resumed"
        note = "" if persisted else "\nWarning: could not save to the database; the change applies only until restart."
        return f"Runner {word}.{note}"

    async def join_text(self, arg: str) -> str:
        if not arg:
            return "Usage: /join @publicgroup"
        ref = normalize_chat_ref(arg)
        if ref is None:
            return "That does not look like a @username, t.me link or numeric chat ID."
        client = self._user_client()
        if client is None:
            return "User account is not connected."
        try:
            chat = await asyncio.wait_for(client.join_chat(ref), TELEGRAM_CALL_TIMEOUT)
            return f"Joined: {chat.title or 'chat'} (ID {chat.id})"
        except Exception as exc:
            log.exception("join failed")
            return f"Could not join: {type(exc).__name__}"

    async def leave_text(self, arg: str) -> str:
        if not arg:
            return "Usage: /leave @username  or  /leave target"
        ref: Any = self.cfg.target_group_id if arg.strip().lower() == "target" else normalize_chat_ref(arg)
        if ref is None:
            return "That does not look like a @username or numeric chat ID."
        client = self._user_client()
        if client is None:
            return "User account is not connected."
        try:
            chat = await asyncio.wait_for(client.get_chat(ref), TELEGRAM_CALL_TIMEOUT)
            await asyncio.wait_for(client.leave_chat(chat.id), TELEGRAM_CALL_TIMEOUT)
            return f"Left: {chat.title or 'chat'} (ID {chat.id})"
        except Exception as exc:
            log.exception("leave failed")
            return f"Could not leave: {type(exc).__name__}"

    # -- entry points ------------------------------------------------------
    async def handle_command(self, message) -> bool:
        uid = message.from_user.id if getattr(message, "from_user", None) else None
        log.info("private message received uid=%s", uid)
        parsed = parse_command(getattr(message, "text", None), self.bot_username)
        if parsed is None or parsed[0] not in COMMANDS:
            log.info("not a known command uid=%s", uid)
            return False
        cmd, arg = parsed
        authorized = is_admin(self.cfg.admin_ids, uid)
        log.info("command=/%s uid=%s authorized=%s", cmd, uid, authorized)
        if not authorized:
            await safe_reply(message, ADMIN_ONLY_NOTICE)
            return True
        try:
            if cmd in ("start", "help"):
                text = self.HELP_TEXT
            elif cmd == "status":
                text = await self.status_text()
            elif cmd == "health":
                text = await self.health_text()
            elif cmd == "session":
                text = await self.session_text()
            elif cmd == "pause":
                text = await self.pause_text(True, uid)
            elif cmd == "resume":
                text = await self.pause_text(False, uid)
            elif cmd == "join":
                text = await self.join_text(arg)
            else:  # leave
                text = await self.leave_text(arg)
        except Exception as exc:
            log.exception("command /%s failed", cmd)
            await self.db.log_error(f"cmd:{cmd}", exc, uid=uid)
            text = f"Command /{cmd} failed: {type(exc).__name__}. See the service log for details."
        markup = self._keyboard() if cmd in ("start", "help", "status", "pause", "resume") else None
        ok = await safe_reply(message, text, **({"reply_markup": markup} if markup else {}))
        if not ok and markup is not None:  # retry once without the keyboard
            await safe_reply(message, text)
        return True

    async def handle_callback(self, query) -> None:
        answered = False
        try:
            uid = query.from_user.id if query.from_user else None
            action = (query.data or "").split(":")[-1]
            log.info("callback action=%s uid=%s authorized=%s", action, uid, is_admin(self.cfg.admin_ids, uid))
            if not is_admin(self.cfg.admin_ids, uid):
                await query.answer("Not authorized.", show_alert=True)
                answered = True
                return
            if action in ("pause", "resume"):
                text = await self.pause_text(action == "pause", uid)
            elif action == "status":
                text = await self.status_text()
            elif action == "session":
                text = await self.session_text()
            elif action == "help":
                text = self.HELP_TEXT
            else:
                text = "Unknown action."
            if len(text) <= 190 and "\n" not in text:
                await query.answer(text)
            else:
                await query.answer("Done")
                answered = True
                await safe_reply(query.message, text, reply_markup=self._keyboard())
            answered = True
        except Exception:
            log.exception("callback failed")
        finally:
            if not answered:
                try:
                    await query.answer("Something went wrong.", show_alert=True)
                except Exception:
                    log.exception("callback answer failed")


# --------------------------------------------------------------------------
# Game runner (user client)
# --------------------------------------------------------------------------
class AppState:
    def __init__(self):
        self.user_connected = False
        self.user_error: str = ""
        self.replies_sent = 0
        self.game_info = "idle"


class GameRunner:
    def __init__(self, cfg: Config, db, state: AppState, send: Callable[..., Awaitable[Any]],
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.db = db
        self.state = state
        self._send = send
        self._sleep = sleep
        self._clock = clock
        self.lock = asyncio.Lock()
        self.last_send_at: Optional[float] = None
        self.recent = RecentWords()
        self._rejected_seen: set = set()

    @staticmethod
    def sender_label(message) -> str:
        sender = getattr(message, "from_user", None)
        if sender is None:
            return "unknown sender"
        name = getattr(sender, "username", None)
        return f"@{name}" if name else f"id {getattr(sender, 'id', '?')}"

    def sender_allowed(self, message) -> bool:
        wanted = self.cfg.game_bot_username
        if not wanted:
            return True
        sender = getattr(message, "from_user", None)
        username = (getattr(sender, "username", None) or "").lower()
        return username == wanted

    async def handle(self, message) -> Optional[str]:
        """Process one group message. Returns the word sent, else None."""
        if self.db.is_paused():
            return None
        text = getattr(message, "text", None)
        if not text:
            return None
        if not self.sender_allowed(message):
            label = self.sender_label(message)
            if label not in self._rejected_seen and len(self._rejected_seen) < 50:
                self._rejected_seen.add(label)
                log.info("ignored a group message from %s: GAME_BOT_USERNAME is @%s "
                         "(logged once per sender)", label, self.cfg.game_bot_username)
            return None
        length = extract_length(text)
        if length is None:
            level = logging.INFO if self.cfg.game_bot_username else logging.DEBUG
            log.log(level, "no word length found in message from %s: %r", self.sender_label(message), text[:120])
            return None
        log.info("prompt detected: length=%d from %s", length, self.sender_label(message))
        word = choose_word(length, avoid=self.recent.as_set())
        if not word or len(word) != length:
            log.warning("no dictionary word for length %s", length)
            await self.db.log_event("word_unavailable", length=length)
            return None
        sent = await self.send_text(message.chat.id, word, tag="word")
        if sent:
            self.recent.add(word)
            log.info("replied with a %d-letter word", length)
            return word
        return None

    async def send_text(self, chat_id: int, text: str, tag: str = "word") -> bool:
        """Serialized, rate-limited PLAIN message (never a reply or mention) with FloodWait/RPC handling.
        True if the message was sent."""
        from pyrogram.errors import FloodWait, RPCError

        async with self.lock:
            if self.db.is_paused():
                return False
            for attempt in (1, 2):
                if self.last_send_at is not None:
                    wait = self.cfg.send_interval - (self._clock() - self.last_send_at)
                    if wait > 0:
                        await self._sleep(wait)
                if self.db.is_paused():  # may have been paused while waiting
                    return False
                try:
                    await self._send(chat_id, text)
                except FloodWait as exc:
                    delay = int(exc.value) + 1
                    log.warning("FloodWait: sleeping %ss (attempt %d)", delay, attempt)
                    await self.db.log_event("flood_wait", seconds=delay)
                    await self._sleep(delay)
                    self.last_send_at = self._clock()
                    continue
                except RPCError as exc:
                    log.exception("RPC error while sending")
                    await self.db.log_error("game_send", exc, length=len(text))
                    return False
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("unexpected error while sending")
                    await self.db.log_error("game_send_unexpected", exc, length=len(text))
                    return False
                self.last_send_at = self._clock()
                self.state.replies_sent += 1
                await self.db.log_event("word_sent" if tag == "word" else "guess_sent", length=len(text))
                return True
        return False


class _Game:
    def __init__(self, length: int, chat_id: int, anchor_id: Optional[int] = None):
        self.length = length
        self.chat_id = chat_id
        self.anchor_id = anchor_id          # id of the latest game-bot message (informational only)
        self.history: list = []             # [(guess, pattern)] rebuilt from the bot's feedback
        self.excluded: set = set()          # words the game rejected
        self.tried: set = set()             # words this account already sent
        self.last_guess: Optional[str] = None
        self.guesses_sent = 0
        self.timeouts = 0


_CORRECT_WORD = re.compile(r"Correct Word:\s*([A-Za-z]+)", re.I)


class WordSeekPlayer:
    """Plays WordSeek in the target group: keeps guessing until the game ends.

    Flow: "Game started!" -> opener -> bot posts feedback listing ALL guesses (buttons carry
    wh:<state>:<letter>) -> solver narrows candidates -> next guess ... until
    "Congrats"/"Game Over"/"Game Ended". Every guess goes through GameRunner.send_text
    (lock, minimum interval, FloodWait handling, pause check).
    """

    FEEDBACK_TIMEOUT = 20.0
    MAX_TIMEOUTS = 2

    def __init__(self, cfg: Config, db, state: AppState, runner: GameRunner):
        self.cfg, self.db, self.state, self.runner = cfg, db, state, runner
        self.game: Optional[_Game] = None
        self._worker: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._warned_unreadable = False

    # -- incoming messages ------------------------------------------------------
    async def handle(self, message) -> None:
        if not self.runner.sender_allowed(message):
            label = self.runner.sender_label(message)
            if label not in self.runner._rejected_seen and len(self.runner._rejected_seen) < 50:
                self.runner._rejected_seen.add(label)
                log.info("ignored a group message from %s: GAME_BOT_USERNAME is @%s "
                         "(logged once per sender)", label, self.cfg.game_bot_username)
            return
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        history, length = rows_from_markup(getattr(message, "reply_markup", None),
                                           self.game.length if self.game else None)
        if history:
            await self._on_feedback(message, history, length)
            return
        kind, data = classify_text(text)
        game = self.game
        if kind == "started":
            self._start_game(message, data)
        elif kind == "invalid" and game:
            game.excluded.add(data)
            log.info("game rejected %r as not a valid word", data)
            self._wake.set()
        elif kind == "duplicate" and game and game.last_guess:
            game.excluded.add(game.last_guess)
            log.info("%r was already guessed by someone", game.last_guess)
            self._wake.set()
        elif kind in ("won", "over", "ended"):
            await self._finish(kind, text)
        elif re.search(r"\d+/30 guesses", text) and not self._warned_unreadable:
            self._warned_unreadable = True
            log.warning("game feedback message has NO readable letter buttons (has_markup=%s). "
                        "Run: python diagnose.py --game  and see DEPLOY.md step 15.",
                        getattr(message, "reply_markup", None) is not None)
        else:
            log.debug("ignored game-bot message: %r", text[:80])

    def _start_game(self, message, length: int) -> None:
        self._cancel_worker()
        self.game = _Game(length, message.chat.id, anchor_id=message.id)
        self._wake.clear()
        self.state.game_info = f"WordSeek {length}-letter game started"
        log.info("new WordSeek game: %d letters", length)
        self._ensure_worker()

    async def _on_feedback(self, message, history, length) -> None:
        if self.game is None or self.game.chat_id != message.chat.id:
            self.game = _Game(length or len(history[0][0]), message.chat.id)
            log.info("joined a running WordSeek game (%d letters, %d guesses so far)", self.game.length, len(history))
        game = self.game
        if length and length != game.length:
            game.length, game.history = length, []
        if len(history) >= len(game.history):
            game.history = history
        game.anchor_id = message.id
        game.timeouts = 0
        self.state.game_info = f"WordSeek {game.length}-letter: {len(game.history)} guesses on board, {game.guesses_sent} sent by me"
        self._wake.set()
        self._ensure_worker()

    async def _finish(self, kind: str, text: str) -> None:
        game = self.game
        if game is None:
            return
        m = _CORRECT_WORD.search(text)
        word = m.group(1).lower() if m else None
        mine = bool(word and word == game.last_guess)
        log.info("game finished (%s)%s; guesses sent by me: %d%s", kind, f" word={word}" if word else "",
                 game.guesses_sent, " - I WON" if (kind == "won" and mine) else "")
        await self.db.log_event("game_finished", result=kind, mine=mine, guesses=game.guesses_sent,
                                length=game.length)
        self.game = None
        self.state.game_info = "idle (last game: %s)" % kind
        self._cancel_worker()

    # -- worker --------------------------------------------------------------------
    def _cancel_worker(self) -> None:
        worker = self._worker
        self._worker = None
        self._wake.set()
        if worker is not None and not worker.done() and worker is not asyncio.current_task():
            worker.cancel()

    def _ensure_worker(self) -> None:
        if self.game is not None and (self._worker is None or self._worker.done()):
            self._worker = asyncio.get_running_loop().create_task(self._play(self.game))

    async def _play(self, game: _Game) -> None:
        try:
            while self.game is game and not self.db.is_paused():
                self._wake.clear()
                word = await asyncio.to_thread(choose_guess, game.length, list(game.history),
                                               game.excluded | game.tried)
                if word is None:
                    log.warning("solver found no word consistent with the board - stopping for this game")
                    await self.db.log_event("solver_stuck", length=game.length, rows=len(game.history))
                    break
                if self.game is not game:
                    break
                if not await self.runner.send_text(game.chat_id, word, tag="guess"):
                    break
                game.last_guess = word
                game.tried.add(word)
                game.guesses_sent += 1
                log.info("guess #%d sent (%d-letter game, %d rows on board)", game.guesses_sent, game.length, len(game.history))
                try:
                    await asyncio.wait_for(self._wake.wait(), self.FEEDBACK_TIMEOUT)
                except asyncio.TimeoutError:
                    game.timeouts += 1
                    log.warning("no reply from the game bot after my guess (%d/%d)", game.timeouts, self.MAX_TIMEOUTS)
                    if game.timeouts >= self.MAX_TIMEOUTS:
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("WordSeek player crashed")


# --------------------------------------------------------------------------
# Handler registration (called exactly once, inside the running loop)
# --------------------------------------------------------------------------
def register_control_handlers(client, panel: ControlPanel) -> None:
    from pyrogram import filters
    from pyrogram.handlers import CallbackQueryHandler, MessageHandler

    async def on_command(_client, message):
        try:
            await panel.handle_command(message)
        except Exception:
            log.exception("control handler crashed")

    async def on_callback(_client, query):
        await panel.handle_callback(query)

    client.add_handler(MessageHandler(on_command, filters.private & filters.command(list(COMMANDS))), group=0)
    client.add_handler(CallbackQueryHandler(on_callback), group=1)


def register_user_handlers(client, runner: GameRunner, target_group_id: int, player: Optional["WordSeekPlayer"] = None) -> None:
    from pyrogram import filters
    from pyrogram.handlers import MessageHandler

    target = player if player is not None else runner

    async def on_group_message(_client, message):
        try:
            await target.handle(message)
        except Exception:
            log.exception("game handler crashed")

    # No filters.text: WordSeek feedback messages carry buttons and may have no plain text.
    flt = filters.chat(target_group_id) & filters.incoming
    client.add_handler(MessageHandler(on_group_message, flt), group=0)


def count_handlers(client) -> int:
    return sum(len(v) for v in client.dispatcher.groups.values())


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class Application:
    def __init__(self, cfg: Config, db=None):
        # NOTE: no Pyrogram objects are created here. See module docstring.
        self.cfg = cfg
        self.db = db or Database(cfg.mongo_uri, cfg.mongo_db, cfg.secrets)
        self.state = AppState()
        self.control_bot = None
        self.user_client = None
        self.stop_event: Optional[asyncio.Event] = None
        self._registered = False

    def _build_clients(self) -> None:
        from pyrogram import Client

        assert asyncio.get_running_loop() is not None  # must be inside the loop
        # in_memory=True: no *.session files. A stale session file would be reused
        # and silently IGNORE a changed BOT_TOKEN.
        self.control_bot = Client(
            "game_control_bot", api_id=self.cfg.api_id, api_hash=self.cfg.api_hash,
            bot_token=self.cfg.bot_token, in_memory=True,
        )
        self.user_client = Client(
            "game_owner_account", api_id=self.cfg.api_id, api_hash=self.cfg.api_hash,
            session_string=self.cfg.user_session, in_memory=True,
        )

    def register_handlers_once(self) -> None:
        if self._registered:
            raise RuntimeError("handlers already registered")
        self.panel = ControlPanel(self.cfg, self.db, self.state, lambda: self.user_client)
        self.runner = GameRunner(self.cfg, self.db, self.state,
                                 send=lambda chat_id, text, **kw: self.user_client.send_message(chat_id, text, **kw))
        self.player = WordSeekPlayer(self.cfg, self.db, self.state, self.runner) if self.cfg.game_mode == "wordseek" else None
        register_control_handlers(self.control_bot, self.panel)
        register_user_handlers(self.user_client, self.runner, self.cfg.target_group_id, self.player)
        self._registered = True

    async def _start_control_bot(self) -> None:
        await self.control_bot.start()
        await asyncio.sleep(0)  # let queued add_handler tasks run
        me = await self.control_bot.get_me()
        self.panel.bot_username = me.username
        if me.id != self.cfg.bot_id:
            raise RuntimeError("Control bot identity does not match BOT_TOKEN")
        log.info("control bot online as @%s (id %s), handlers=%d", me.username, me.id, count_handlers(self.control_bot))
        if self.cfg.game_bot_username and me.username and me.username.lower() == self.cfg.game_bot_username:
            log.warning("GAME_BOT_USERNAME (@%s) is the CONTROL bot itself - game prompts from the real game bot "
                        "will be ignored. Set it to the game bot's username or leave it empty.", me.username)
        if count_handlers(self.control_bot) < 2:
            raise RuntimeError("Control bot handlers were not registered")

    async def _start_user_client(self) -> None:
        try:
            await self.user_client.start()
            me = await self.user_client.get_me()
            self.state.user_connected = True
            log.info("user client connected as id=%s, handlers=%d", me.id, count_handlers(self.user_client))
            try:
                chat = await asyncio.wait_for(self.user_client.get_chat(self.cfg.target_group_id), TELEGRAM_CALL_TIMEOUT)
                log.info("target group accessible: %s", chat.title or chat.id)
            except Exception as exc:
                log.warning("target group %s is NOT accessible by the user account (%s): is the account a member "
                            "and is TARGET_GROUP_ID correct?", self.cfg.target_group_id, type(exc).__name__)
        except Exception as exc:
            # Degraded mode: control bot stays up so /status explains what is wrong.
            self.state.user_connected = False
            self.state.user_error = type(exc).__name__
            log.exception("user client failed to start (control bot stays online)")
            await self.db.log_error("user_start", exc)

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        try:
            await self.db.connect()
        except Exception as exc:
            log.error("MongoDB connection failed: %s", type(exc).__name__)
            return EXIT_DB
        code = EXIT_OK
        try:
            self._build_clients()
            self.register_handlers_once()
            await self._start_control_bot()
            await self._start_user_client()
            await self.db.log_event("started", user_connected=self.state.user_connected)
            log.info("READY - waiting for updates")
            await self.stop_event.wait()
            log.info("shutdown requested")
        except Exception:
            log.exception("fatal error")
            code = EXIT_FATAL
        finally:
            await self.shutdown()
        return code

    async def shutdown(self) -> None:
        for name, client in (("user client", self.user_client), ("control bot", self.control_bot)):
            if client is None:
                continue
            try:
                if getattr(client, "is_connected", False):
                    await asyncio.wait_for(client.stop(), 15)
                    log.info("%s stopped", name)
            except Exception:
                log.exception("error stopping %s", name)
        try:
            await self.db.log_event("stopped")
        finally:
            await self.db.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Game User Pro")
    parser.add_argument("--check-config", action="store_true", help="validate .env and exit (no network)")
    args = parser.parse_args(argv)

    try:
        cfg = cfgmod.load_config()
    except ConfigError as exc:
        print("Configuration errors:")
        for problem in exc.problems:
            print(f"  - {problem}")
        return EXIT_CONFIG

    for warning in cfgmod.env_file_warnings():
        print(f"warning: {warning}")
    if args.check_config:
        print("Configuration is valid. (Credentials were NOT tested against Telegram or MongoDB.)")
        for key, value in cfg.summary().items():
            print(f"  {key}: {value}")
        return EXIT_OK

    setup_logging(cfg)
    lock = InstanceLock(cfgmod.PROJECT_DIR / ".game-user.lock")
    if not lock.acquire():
        log.error("Another copy is already running (lock held). Refusing to start a second one.")
        return EXIT_LOCKED
    try:
        return asyncio.run(Application(cfg).run())
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
