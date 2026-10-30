import pytest

import config as cfgmod
from config import ConfigError, parse_admin_ids, validate


def test_valid_config(cfg):
    assert cfg.api_id == 123456
    assert cfg.target_group_id == -1001234567890
    assert cfg.admin_ids == frozenset({111, 222})
    assert cfg.send_interval == 5.0
    assert cfg.bot_id == 123456789
    assert cfg.game_bot_username == ""


def test_multiple_admin_ids_and_whitespace():
    assert parse_admin_ids("1,2, 3 ,,4;5") == frozenset({1, 2, 3, 4, 5})


@pytest.mark.parametrize("bad", ["", "abc", "1,x", "-5", "0", " , "])
def test_bad_admin_ids(bad):
    with pytest.raises(ValueError):
        parse_admin_ids(bad)


@pytest.mark.parametrize("name", cfgmod.REQUIRED_VARS)
def test_missing_required(env, name):
    env.pop(name)
    with pytest.raises(ConfigError) as err:
        validate(env)
    assert any(name in p for p in err.value.problems)


@pytest.mark.parametrize(
    "name,value",
    [
        ("API_ID", "12ab"), ("API_ID", "-4"), ("API_HASH", "short"), ("BOT_TOKEN", "nocolon"),
        ("USER_SESSION", "has space " * 10), ("MONGO_URI", "http://x"), ("TARGET_GROUP_ID", "abc"),
        ("TARGET_GROUP_ID", "0"), ("ADMIN_IDS", "x"), ("SEND_INTERVAL_SECONDS", "fast"),
        ("SEND_INTERVAL_SECONDS", "0"), ("LOG_LEVEL", "LOUD"), ("GAME_BOT_USERNAME", "a b"),
        ("MONGO_DB", "bad name!"),
    ],
)
def test_invalid_values(env, name, value):
    env[name] = value
    with pytest.raises(ConfigError) as err:
        validate(env)
    assert any(name in p for p in err.value.problems)


def test_placeholder_rejected(env):
    env["BOT_TOKEN"] = "YOUR_BOT_TOKEN"
    with pytest.raises(ConfigError):
        validate(env)


def test_errors_never_contain_secret_values(env):
    env["API_HASH"] = "SUPERSECRETVALUE"
    env["BOT_TOKEN"] = "bad-SECRETTOKEN"
    with pytest.raises(ConfigError) as err:
        validate(env)
    text = str(err.value)
    assert "SUPERSECRETVALUE" not in text and "SECRETTOKEN" not in text


def test_summary_and_repr_hide_secrets(cfg, env):
    blob = repr(cfg) + str(cfg.summary())
    for key in ("API_HASH", "BOT_TOKEN", "USER_SESSION", "MONGO_URI"):
        assert env[key] not in blob
    assert "p%40ssw0rd" not in blob


def test_secrets_include_mongo_password(cfg):
    assert "p%40ssw0rd" in cfg.secrets and "p@ssw0rd" in cfg.secrets


def test_username_normalised(env):
    env["GAME_BOT_USERNAME"] = "@MyGame_Bot"
    assert validate(env).game_bot_username == "mygame_bot"


def test_defaults(env):
    for k in ("MONGO_DB", "SEND_INTERVAL_SECONDS", "LOG_LEVEL"):
        env.pop(k)
    c = validate(env)
    assert (c.mongo_db, c.send_interval, c.log_level) == ("game_user", 5.0, "INFO")


def test_old_key_target_group_is_accepted(env):
    env["TARGET_GROUP"] = env.pop("TARGET_GROUP_ID")
    assert validate(env).target_group_id == -1001234567890
