import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Optional: lets the suite run where Pyrogram is vendored/stubbed (CI sandboxes).
for extra in filter(None, os.environ.get("EXTRA_PYTHONPATH", "").split(os.pathsep)):
    sys.path.insert(0, extra)

import pytest

import config as cfgmod

GOOD_ENV = {
    "API_ID": "123456",
    "API_HASH": "0123456789abcdef0123456789abcdef",
    "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "USER_SESSION": "S" * 120,
    "MONGO_URI": "mongodb+srv://user:p%40ssw0rd@cluster.example.mongodb.net/",
    "MONGO_DB": "game_user_test",
    "TARGET_GROUP_ID": "-1001234567890",
    "ADMIN_IDS": "111, 222",
    "GAME_BOT_USERNAME": "",
    "SEND_INTERVAL_SECONDS": "5",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def env():
    return dict(GOOD_ENV)


@pytest.fixture
def cfg(env):
    return cfgmod.validate(env)


def run(coro):
    """Run a coroutine on a fresh loop (no pytest plugins required)."""
    return asyncio.run(coro)


class FakeUser:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


class FakeChat:
    def __init__(self, cid):
        self.id = cid


class FakeMessage:
    """Duck-typed pyrogram Message."""

    def __init__(self, text, uid=111, username=None, chat_id=None, mid=1, fail_reply=0, fail_with_markup=False, reply_markup=None):
        self.text = text
        self.caption = None
        self.reply_markup = reply_markup
        self.from_user = FakeUser(uid, username) if uid is not None else None
        self.chat = FakeChat(chat_id if chat_id is not None else uid)
        self.id = mid
        self.replies = []
        self._fail_reply = fail_reply
        self._fail_with_markup = fail_with_markup

    async def reply_text(self, text, **kwargs):
        if self._fail_with_markup and "reply_markup" in kwargs:
            raise RuntimeError("keyboard rejected")
        if self._fail_reply > 0:
            self._fail_reply -= 1
            raise RuntimeError("reply failed")
        self.replies.append((text, kwargs))
        return self


class FakeQuery:
    def __init__(self, data, uid=111):
        self.data = data
        self.from_user = FakeUser(uid)
        self.message = FakeMessage("", uid=uid)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeUserClient:
    """Stands in for the authorized user Pyrogram client."""

    def __init__(self):
        self.sent = []
        self.joined = []
        self.left = []

    async def get_me(self):
        return type("Me", (), {"id": 999, "username": "owner_acc"})()

    async def get_chat(self, ref):
        return type("Chat", (), {"id": ref if isinstance(ref, int) else -100777, "title": "Test Group"})()

    async def join_chat(self, ref):
        self.joined.append(ref)
        return type("Chat", (), {"id": -100555, "title": "Joined"})()

    async def leave_chat(self, cid):
        self.left.append(cid)
