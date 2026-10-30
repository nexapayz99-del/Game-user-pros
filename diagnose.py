"""Standalone diagnostic: why does the control bot not answer /start?

Usage:
    python diagnose.py                 # checks + waits 90 s for you to send /start
    python diagnose.py --wait 180      # wait longer
    python diagnose.py --no-wait       # checks only
    python diagnose.py --ping          # also DM the first admin (they must have pressed Start once)
    python diagnose.py --game --wait 120   # dry-run: what does the user client see in the target group?

It uses ONE asyncio loop, builds the Client inside it, starts it, runs the REAL
ControlPanel handlers, and stops cleanly. It prints PASS/FAIL/INFO only for what
it actually observed. It never prints tokens, hashes, sessions or the Mongo URI.
STOP the service first:  sudo systemctl stop game-user
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path

import config as cfgmod
from config import ConfigError

RESULTS = []


def report(status: str, name: str, detail: str = "") -> None:
    RESULTS.append((status, name))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""), flush=True)


def other_copies() -> list:
    """PIDs of other local processes running app.py / diagnose.py (Linux /proc scan)."""
    found = []
    me = os.getpid()
    proc = Path("/proc")
    if not proc.exists():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except OSError:
            continue
        if ("python" in cmd) and ("app.py" in cmd or "diagnose.py" in cmd):
            found.append((int(entry.name), cmd.strip()[:120]))
    return found


async def amain(args) -> int:
    # 1/12: environment
    report("INFO", "python", sys.version.split()[0] + " at " + sys.executable)
    try:
        import pyrogram

        report("INFO", "pyrogram", getattr(pyrogram, "__version__", "?"))
    except Exception:
        report("FAIL", "import pyrogram", traceback.format_exc(limit=1))
        return 1

    try:
        cfg = cfgmod.load_config()
    except ConfigError as exc:
        for problem in exc.problems:
            report("FAIL", "config", problem)
        return 2
    report("PASS", "config valid", f"admins={sorted(cfg.admin_ids)} target={cfg.target_group_id}")

    copies = other_copies()
    if copies:
        for pid, cmd in copies:
            report("FAIL", "another process is running", f"pid {pid}: {cmd}")
    else:
        report("PASS", "no other local app.py/diagnose.py process found")

    from app import AppState, ControlPanel, count_handlers, register_control_handlers
    from db import Database, MemoryDatabase

    db = Database(cfg.mongo_uri, cfg.mongo_db, cfg.secrets)
    try:
        await db.connect()
        report("PASS", "MongoDB reachable")
    except Exception as exc:
        report("FAIL", "MongoDB", type(exc).__name__ + " (continuing with in-memory state)")
        db = MemoryDatabase()

    from pyrogram import Client, filters
    from pyrogram.handlers import MessageHandler

    # Client is built INSIDE the running loop - this is the fix for the original bug.
    bot = Client("diag_bot", api_id=cfg.api_id, api_hash=cfg.api_hash, bot_token=cfg.bot_token, in_memory=True)
    state = AppState()
    panel = ControlPanel(cfg, db, state, lambda: None)
    seen = {"private": 0, "start": 0, "replied": 0}

    async def raw_probe(_c, message):  # group -1: observe only, never stops propagation
        seen["private"] += 1
        uid = message.from_user.id if message.from_user else None
        txt = (message.text or "")[:20]
        print(f"   probe: private message uid={uid} text={txt!r} admin={uid in cfg.admin_ids}", flush=True)
        if txt.lower().startswith("/start"):
            seen["start"] += 1

    register_control_handlers(bot, panel)
    bot.add_handler(MessageHandler(raw_probe, filters.private), group=-1)

    try:
        await bot.start()
        await asyncio.sleep(0)
        me = await bot.get_me()
        report("PASS", "bot authenticated", f"@{me.username} id={me.id}")
        if me.id == cfg.bot_id:
            report("PASS", "bot identity matches BOT_TOKEN")
        else:
            report("FAIL", "bot identity differs from BOT_TOKEN", "a stale session file or wrong token is in use")
        n = count_handlers(bot)
        report("PASS" if n >= 3 else "FAIL", "handlers registered on the running client", f"count={n}")

        if args.ping:
            admin = sorted(cfg.admin_ids)[0]
            try:
                await bot.send_message(admin, "Diagnostic ping: if you see this, the bot can message you.")
                report("PASS", "outbound message to first admin")
            except Exception as exc:
                report("FAIL", "outbound message to first admin", type(exc).__name__ + " (admin must press Start in the bot chat first)")

        if not args.no_wait:
            print(f"\n>>> Open Telegram and send /start to @{me.username} now (waiting {args.wait}s)...\n", flush=True)
            for _ in range(args.wait):
                if seen["start"]:
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(2)  # allow the reply to be sent
            if seen["private"] == 0:
                report("FAIL", "no private message reached the dispatcher",
                       "token used elsewhere? wrong bot chat? old process? see README troubleshooting")
            else:
                report("PASS", "private message reached the dispatcher", f"count={seen['private']}")
                report("INFO", "reply", "check Telegram: you should have received the control panel text")
    except Exception:
        report("FAIL", "client run", traceback.format_exc(limit=3))
    finally:
        try:
            if bot.is_connected:
                await bot.stop()
                report("PASS", "client stopped cleanly")
        except Exception:
            report("FAIL", "client stop", traceback.format_exc(limit=2))
        await db.close()

    print("\nSUMMARY: " + ", ".join(f"{s}:{n}" for s, n in RESULTS))
    return 0 if not any(s == "FAIL" for s, _ in RESULTS) else 1


async def game_watch(seconds: int) -> int:
    """Dry run: show what the user client sees in the target group. Sends NOTHING."""
    try:
        cfg = cfgmod.load_config()
    except ConfigError as exc:
        for problem in exc.problems:
            report("FAIL", "config", problem)
        return 2
    from pyrogram import Client, filters
    from pyrogram.handlers import MessageHandler
    from prompts import extract_length
    from words import choose_word

    user = Client("diag_user", api_id=cfg.api_id, api_hash=cfg.api_hash, session_string=cfg.user_session, in_memory=True)
    other_chats = {}

    from wordseek import choose_guess, classify_text, rows_from_markup

    async def probe(_c, m):
        sender = m.from_user
        uname = (sender.username or "").lower() if sender else ""
        allowed = (not cfg.game_bot_username) or uname == cfg.game_bot_username
        text = m.text or m.caption or ""
        markup = getattr(m, "reply_markup", None)
        history, length = rows_from_markup(markup)
        kind, data = classify_text(text)
        n_buttons = sum(len(r) for r in (getattr(markup, "inline_keyboard", None) or []))
        print(f"   target msg: sender id={sender.id if sender else None} @{uname or '-'} passes_filter={allowed} "
              f"kind={kind} has_buttons={n_buttons} board_rows={len(history)} text={text[:60]!r}", flush=True)
        if n_buttons and not history:
            sample = [getattr(b, 'callback_data', None) for r in markup.inline_keyboard for b in r][:3]
            print(f"   !! buttons present but not WordSeek letter buttons; first callback data: {sample}", flush=True)
        if allowed and history:
            guess = choose_guess(length, history)
            print(f"   -> board readable. WOULD guess {guess!r} (dry run, nothing sent)", flush=True)
        elif allowed and kind == "started":
            print(f"   -> new game detected ({data} letters). WOULD open with the first guess (dry run)", flush=True)
        elif allowed and "/30 guesses" in text and not history:
            print("   !! feedback message arrived WITHOUT readable letter buttons - Pyrogram 2.0.106 cannot see them", flush=True)

    async def other(_c, m):  # helps find a wrong TARGET_GROUP_ID
        if m.chat.id != cfg.target_group_id and m.chat.id not in other_chats and len(other_chats) < 10:
            other_chats[m.chat.id] = m.chat.title
            print(f"   other chat seen: id={m.chat.id} title={m.chat.title!r}", flush=True)

    user.add_handler(MessageHandler(probe, filters.chat(cfg.target_group_id)), group=0)
    user.add_handler(MessageHandler(other, filters.group), group=1)
    try:
        await user.start()
        await asyncio.sleep(0)
        me = await user.get_me()
        report("PASS", "user client connected", f"id={me.id}")
        try:
            chat = await user.get_chat(cfg.target_group_id)
            report("PASS", "target group accessible", repr(chat.title))
        except Exception as exc:
            report("FAIL", "target group NOT accessible", type(exc).__name__ + " - wrong TARGET_GROUP_ID or account not a member")
        print(f"\n>>> Start a game round in the group now (watching {seconds}s, nothing will be sent)...\n", flush=True)
        await asyncio.sleep(seconds)
    except Exception:
        report("FAIL", "user client run", traceback.format_exc(limit=3))
    finally:
        try:
            if user.is_connected:
                await user.stop()
        except Exception:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", action="store_true", help="dry-run watch of the target group (user client)")
    parser.add_argument("--wait", type=int, default=90)
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--ping", action="store_true")
    args = parser.parse_args()
    if args.game:
        return asyncio.run(game_watch(args.wait))
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
