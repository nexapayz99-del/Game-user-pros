import random

import pytest

import words
from prompts import extract_length


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Find a 5-letter word", 5),
        ("Find 6 letter word", 6),
        ("Word with 7 letters", 7),
        ("Word having 8 letters", 8),
        ("Word of length 9", 9),
        ("Length: 5", 5),
        ("find a 5-LETTER WORD!", 5),
        ("🎮 Round 3\nFind a 10 letter word starting with any letter", 10),
        ("Send me a 4 letter word", 4),
        ("word length: 12", 12),
        ("Word containing 6 characters", 6),
    ],
)
def test_supported_formats(text, expected):
    assert extract_length(text) == expected


@pytest.mark.parametrize(
    "text",
    ["hello everyone", "good morning", "", None, "I have 5 apples", "Find a 1 letter word",
     "Find a 99 letter word", "length is long", "Round 5 starts now"],
)
def test_unrelated_or_out_of_range_ignored(text):
    assert extract_length(text) is None


def test_every_supported_length_has_words():
    for n in range(3, 14):
        assert words.words_for_length(n), f"no words of length {n}"


def test_choose_word_exact_length():
    rng = random.Random(1)
    for n in range(3, 14):
        for _ in range(20):
            w = words.choose_word(n, rng=rng)
            assert w is not None and len(w) == n and w.isalpha() and w == w.lower()


def test_no_word_for_unsupported_length_returns_none(monkeypatch):
    # isolate from any system dictionary (e.g. the VPS has /usr/share/dict installed)
    monkeypatch.setattr(words, "_system_by_len", {})
    monkeypatch.setattr(words, "_system_loaded", True)
    monkeypatch.setattr(words, "_builtin_by_len", {k: v for k, v in words._builtin_by_len.items() if k != 19})
    assert words.choose_word(19) is None


def test_avoid_repeats():
    builtin = words._builtin_by_len[5]
    avoid = set(builtin[:-1])
    assert words.choose_word(5, avoid=avoid) == builtin[-1]


def test_recent_words_ring():
    r = words.RecentWords(size=2)
    for w in ("a", "b", "c"):
        r.add(w)
    assert "a" not in r and "b" in r and "c" in r
