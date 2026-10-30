import random
from types import SimpleNamespace as NS

import pytest

import wordseek as ws


def keyboard(guesses, solution, per_row=None):
    """Build a fake inline keyboard exactly like the game: one row per guess, wh:<state>:<LETTER>."""
    names = {"c": "correct", "p": "present", "a": "absent"}
    rows = []
    for g in guesses:
        pat = ws.pattern(g, solution)
        rows.append([NS(text=ch.upper(), callback_data=f"wh:{names[p]}:{ch.upper()}") for ch, p in zip(g, pat)])
    return NS(inline_keyboard=rows)


@pytest.mark.parametrize(
    "guess,solution,expected",
    [
        ("crane", "crane", "ccccc"),
        ("speed", "erase", "pappa"),   # two E's: both yellow because the answer has two
        ("allee", "eagle", "ppapc"),   # second L is grey: only one L in the answer
        ("eerie", "there", "papac"),   # extra E is grey
        ("stare", "crane", "aacpc"),
        ("tale", "tell", "cacp"),
        ("aaaaa", "abcde", "caaaa"),
    ],
)
def test_pattern_matches_wordle_rules(guess, solution, expected):
    assert ws.pattern(guess, solution) == expected


def test_lists_loaded_and_consistent():
    for n in (4, 5, 6):
        a, v = ws.answers(n), set(ws.valid_guesses(n))
        assert len(a) > 1000 and set(a) <= v
        assert all(len(w) == n for w in v)
        assert ws.OPENERS[n] in v


def test_extract_and_rebuild_history_from_keyboard():
    kb = keyboard(["crane", "pilot"], "plant")
    history, length = ws.rows_from_markup(kb)
    assert length == 5
    assert history == [("crane", ws.pattern("crane", "plant")), ("pilot", ws.pattern("pilot", "plant"))]


def test_rebuild_with_bytes_callback_and_extra_buttons():
    kb = keyboard(["tale"], "tall")
    kb.inline_keyboard[0][0].callback_data = kb.inline_keyboard[0][0].callback_data.encode()
    kb.inline_keyboard.append([NS(text="Other", callback_data="leaderboard x")])
    history, length = ws.rows_from_markup(kb)
    assert length == 4 and history == [("tale", ws.pattern("tale", "tall"))]


def test_rebuild_flat_keyboard_uses_length_hint():
    kb = keyboard(["crane", "pilot"], "plant")
    flat = NS(inline_keyboard=[[b for row in kb.inline_keyboard for b in row]])
    history, length = ws.rows_from_markup(flat, length_hint=5)
    assert length == 5 and len(history) == 2


def test_no_buttons_means_no_history():
    assert ws.rows_from_markup(None) == ([], None)
    assert ws.rows_from_markup(NS(inline_keyboard=[[NS(text="x", callback_data="other")]])) == ([], None)


@pytest.mark.parametrize(
    "text,kind,data",
    [
        ("Game started! Guess the 5-letter word!", "started", 5),
        ("Game started! Guess the 4-letter word!", "started", 4),
        ("crane is not a valid 5-letter word.", "invalid", "crane"),
        ("Someone has already guessed your word. Please try another one!", "duplicate", None),
        ("Congrats! You guessed it correctly.\nCorrect Word: plant\nAdded 25 to the leaderboard.", "won", None),
        ("Game Over! The word was plant\nYou can start", "over", None),
        ("🎮 Game Ended\nCorrect Word: plant", "ended", None),
        ("There is already a game in progress in this chat. Use /end to end the current game.", "other", None),
        ("hello", "other", None),
        (None, "other", None),
    ],
)
def test_classify_text(text, kind, data):
    assert ws.classify_text(text) == (kind, data)


def test_choose_guess_is_consistent_with_history():
    secret = "plant"
    hist = [("crane", ws.pattern("crane", secret))]
    g = ws.choose_guess(5, hist, rng=random.Random(1))
    assert g and g not in {h[0] for h in hist}
    assert ws.pattern("crane", g) == hist[0][1]  # g could still be the secret


def test_choose_guess_respects_excluded_and_opener():
    assert ws.choose_guess(5, []) == ws.OPENERS[5]
    assert ws.choose_guess(5, [], excluded={ws.OPENERS[5]}) != ws.OPENERS[5]


def test_contradictory_history_returns_none():
    assert ws.choose_guess(5, [("crane", "ccccc"), ("pilot", "ccccc")]) is None


@pytest.mark.parametrize("n", [4, 5, 6])
def test_solver_wins_simulated_games(n):
    rng = random.Random(100 + n)
    for secret in rng.sample(ws.answers(n), 25):
        used = ws.simulate(secret, rng=random.Random(1))
        assert 1 <= used <= 12, (secret, used)


def test_secret_outside_common_list_still_solvable():
    secret = [w for w in ws.valid_guesses(5) if w not in set(ws.answers(5))][100]
    assert 1 <= ws.simulate(secret, rng=random.Random(1)) <= 30
