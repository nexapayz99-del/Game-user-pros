# Deploy (Ubuntu 22.04, Python 3.10, run as root in /root/Game-user-pro)

Run each block separately, in order. Do NOT paste comment lines or the whole file at once.
Never paste or screenshot your .env (it holds secrets).

## 0. Rotate secrets that were exposed
- Bot token: Telegram @BotFather -> /revoke -> pick the bot -> put the NEW token in .env
- MongoDB Atlas -> Database Access -> edit the user -> new password (URL-encode special characters)
- User session: Telegram -> Settings -> Devices -> terminate that session, then generate a new string session
- API hash: keep it private (it cannot be regenerated on my.telegram.org)

## 1. Go to the home folder and remove a stray venv created by a failed earlier attempt
    cd /root
    rm -rf /root/.venv

## 2. Confirm the backup of the old project exists
    ls -d /root/Game-user-pro.bak.*

## 3. Upload Game-user-pro.zip to /root (Termius -> SFTP -> drag into /root), then check it
    ls -l /root/Game-user-pro.zip

## 4. Stop the old service/process (only this app)
    systemctl stop game-user 2>/dev/null; systemctl disable game-user 2>/dev/null; true
    pgrep -af "Game-user-pro.*app.py"

If step 4 prints a python process line, stop it with: pkill -f "Game-user-pro.*app.py"

## 5. Unpack the new project (keeps your existing .env)
    cd /root
    unzip -o Game-user-pro.zip
    cd /root/Game-user-pro
    ls

You must see app.py, config.py, requirements-dev.txt, DEPLOY.md here.

## 6. Fresh virtual environment and pinned dependencies
    cd /root/Game-user-pro
    rm -rf .venv
    python3 -m venv .venv
    . .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements-dev.txt

## 7. Fix .env (rename old key, add database name, lock permissions, remove stale sessions)
    cd /root/Game-user-pro
    sed -i 's/^TARGET_GROUP=/TARGET_GROUP_ID=/' .env
    grep -q '^MONGO_DB=' .env || echo 'MONGO_DB=game_user' >> .env
    chmod 600 .env
    rm -f *.session *.session-journal

Edit values if needed: nano .env

## 8. Validate configuration (prints no secrets)
    cd /root/Game-user-pro && . .venv/bin/activate
    python app.py --check-config

## 9. Run the automated tests
    python -m pytest -q

## 10. Diagnose against real Telegram (then send /start to your bot within 2 minutes)
    python diagnose.py --wait 120

## 11. Run interactively (send /start, /status, /pause, /resume; Ctrl+C to stop)
    python app.py

## 12. Install and start the service
    cp /root/Game-user-pro/game-user.service /etc/systemd/system/game-user.service
    systemctl daemon-reload
    systemctl enable --now game-user
    systemctl status game-user --no-pager

## 13. Logs
    journalctl -u game-user -f
    journalctl -u game-user -n 100 --no-pager

## 14. Restart / stop / update
    systemctl restart game-user
    systemctl stop game-user
    # update: stop, unzip -o new zip over the folder, then
    cd /root/Game-user-pro && . .venv/bin/activate && pip install -r requirements.txt && systemctl start game-user

## 15. Bot answers commands but does not play the game
Stop the service / interactive run first (Ctrl+C or: systemctl stop game-user), then:

    cd /root/Game-user-pro && . .venv/bin/activate
    python diagnose.py --game --wait 120

Start a game round in the group while it runs. It prints every message the user account sees in the
target group, whether it passes the GAME_BOT_USERNAME filter, the detected length, and the word it
WOULD send (dry run, nothing is sent). Reading it:
- No "target msg" lines and "other chat seen" shows your group under a different id -> wrong TARGET_GROUP_ID.
- "target group NOT accessible" -> the user account is not a member of that group.
- passes_filter=False -> GAME_BOT_USERNAME is wrong. It must be the GAME bot, not your control bot. Clear it with:
      sed -i 's/^GAME_BOT_USERNAME=.*/GAME_BOT_USERNAME=/' .env
- passes_filter=True but length=None -> the prompt wording is not recognised; send me the text shown.
In the normal service log, look for "ignored a group message from ..." and "no word length found ...".

## 16. WordSeek auto-play (GAME_MODE=wordseek, the default)
Set in .env (then restart the service):

    GAME_MODE=wordseek
    GAME_BOT_USERNAME=WordSeekBot

Start a round with /new (or /new4 /new5 /new6). The account opens with a guess, reads the bot's
feedback buttons, and keeps guessing until the bot says the game is won/over/ended. Use /pause
in the control bot to stop it at any time. Check readiness FIRST with a dry run (sends nothing):

    cd /root/Game-user-pro && . .venv/bin/activate
    python diagnose.py --game --wait 180

Then type /new in the group. You should see "board readable. WOULD guess ...". If it prints
"feedback message arrived WITHOUT readable letter buttons", tell me - Pyrogram 2.0.106 is too old
to see that message type and the library has to be upgraded.
SEND_INTERVAL_SECONDS is the gap between guesses (1-3600). Telegram limits fast posting; keep it >= 3.
