# Game User Pro

Telegram control bot (bot token) + authorized user client (your own string session) that plays
WordSeek in one target group (keeps guessing until solved), or in `GAME_MODE=simple` answers
"find an N-letter word" prompts with one word. Python 3.10, Pyrogram 2.0.106, Motor, MongoDB Atlas.

Use only an account you own or are authorized to operate. Never share OTPs, 2FA passwords,
session strings, bot tokens or Mongo URIs.

## The original bug (why /start did nothing)

The old `App.__init__` created both `pyrogram.Client` objects and called `register_handlers()`
**before** `asyncio.run()` started the real event loop. Pyrogram 2.0.x stores
`asyncio.get_event_loop()` at construction time, and `add_handler()` schedules the registration on
that stored loop. That loop never ran, so **no handler was ever registered**. `Client.start()` still
succeeded (it authenticates on the loop that does run), which is why both clients looked healthy.

Evidence (run against the real Pyrogram 2.0.106 `Dispatcher` source, no Telegram involved):
old pattern -> `handlers_registered=0`, new pattern -> `handlers_registered=1`.
This is covered by `tests/test_lifecycle.py::test_ORIGINAL_BUG_...`.

Fix: clients are constructed inside `Application.run()` (the running loop); handlers are registered
exactly once and the count is verified at startup (the app refuses to start if it is wrong).
Other hardening: `in_memory=True` sessions (a stale `*.session` file is reused and silently ignores a
changed `BOT_TOKEN`), bot identity check against the token, and Pyrogram's `idle()` replaced by our own
SIGINT/SIGTERM handling.

Honest limit: this root cause was reproduced offline. I could not test against your live bot, so run
`diagnose.py` (below) on the VPS to confirm on your system.

## Handler groups

| Client | Group | Handler |
|---|---|---|
| control bot | 0 | private + `/start /help /status /health /pause /resume /session /join /leave` |
| control bot | 1 | inline-button callbacks (always answered) |
| user client | 0 | text messages in `TARGET_GROUP_ID` only (incoming, optional `GAME_BOT_USERNAME` filter) |

No catch-all handlers. Replies are plain text (no Markdown parse errors). Unauthorized users get
"Not authorized". `/start` works while paused.

## Deploy on Ubuntu (Termius)

See **DEPLOY.md** - one command block per step, run them one at a time.

`.env` keys: `API_ID API_HASH BOT_TOKEN USER_SESSION MONGO_URI MONGO_DB TARGET_GROUP_ID ADMIN_IDS
GAME_BOT_USERNAME SEND_INTERVAL_SECONDS LOG_LEVEL` (see `.env.example`). `ADMIN_IDS` is comma-separated.
Atlas: allow the VPS IP in Network Access.

## Troubleshooting: bot connects but does not answer

1. `sudo systemctl stop game-user`, then `python diagnose.py --wait 120` and send `/start`.
2. `FAIL ... no private message reached the dispatcher`: wrong bot chat, token used by another
   process/server (revoke and regenerate the token in @BotFather, update `.env`), or old copy running (`pgrep -af app.py`).
3. `FAIL handlers registered`: you are running old code or the wrong venv (`which python`).
4. Replies say "Not authorized": your numeric ID is not in `ADMIN_IDS` (get it from @userinfobot).
5. `bot identity differs from BOT_TOKEN`: stale `*.session` file; delete `*.session*`.
6. `/status` says user account NOT CONNECTED: `USER_SESSION` invalid/revoked; the control bot stays up on purpose.
7. Exit code 4 / "lock held": another copy is running. Exit code 2: fix `.env` (messages name the variable only).
8. Commands work but the game is not played: run `python diagnose.py --game --wait 120` (see DEPLOY.md step 15).
9. Logs show tracebacks for every failed reply; secrets are redacted by a log filter.

## Tests

`python -m pytest -q` (mocks only; no credentials needed). Needs real accounts and cannot be automated here:
live `/start` reply, `/join`, `/leave`, real MongoDB Atlas connection, real FloodWait behaviour,
and the game-prompt flow in your group. `diagnose.py` covers the first two against your real bot.

## WordSeek mode
Word lists in `data/` are from the MIT-licensed WordSeek project (see `data/WORDSEEK-LICENSE.txt`).
`wordseek.py` reads each letter's state from the feedback buttons (`wh:<state>:<LETTER>`), filters the
candidates with the game's own scoring rules and picks the guess that splits them best.
Offline simulation (real lists, real rules): average 4.4 / 3.9 / 3.2 guesses for 4 / 5 / 6 letters,
no failures in 360 games. Not verified: reading the real bot's messages with Pyrogram 2.0.106 (needs a live game).
