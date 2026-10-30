"""WordSeek (Wordle-style) logic: feedback parsing, scoring, candidate filtering, guess choice.

Facts taken from the game's open-source code (MIT, binamralamsal/WordSeek):
* Any plain text message that is exactly one 4/5/6-letter word is a guess.
* After a wrong guess the bot posts ONE message showing EVERY guess of the game so far.
  Each letter is an inline button whose callback data is  ``wh:<state>:<LETTER>`` with
  state in {correct, present, absent}. So colours are read from callback data, not from
  the button colour.
* Answers are drawn from the "common" lists; accepted guesses come from the "all" lists.
* Scoring is the standard Wordle algorithm (greens first, then yellows by remaining count).

No Telegram code lives here, so everything is unit-testable.
"""
from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DATA_DIR = Path(__file__).resolve().parent / "data"
LENGTHS = (4, 5, 6)
STATE_CHAR = {"correct": "c", "present": "p", "absent": "a"}

# Best first guesses (highest-entropy words that are also possible answers; verified valid guesses).
OPENERS = {4: 'tale', 5: 'raise', 6: 'satire'}

History = List[Tuple[str, str]]  # [(guess, pattern like "capac")]


# ---------------------------------------------------------------- word lists
_cache: Dict[Tuple[str, int], List[str]] = {}


def _load(kind: str, length: int) -> List[str]:
    key = (kind, length)
    if key not in _cache:
        name = {4: "four", 5: "five", 6: "six"}[length]
        with open(DATA_DIR / f"{kind}-{name}.json", "r", encoding="utf-8") as fh:
            words = [w.lower() for w in json.load(fh) if isinstance(w, str)]
        _cache[key] = [w for w in words if len(w) == length and w.isalpha()]
    return _cache[key]


def answers(length: int) -> List[str]:
    """Words the game can pick as the secret."""
    return _load("common", length)


def valid_guesses(length: int) -> List[str]:
    """Words the game accepts as guesses."""
    return _load("all", length)


# ------------------------------------------------------------------- scoring
def pattern(guess: str, solution: str) -> str:
    """Wordle feedback as a string of c(orrect) / p(resent) / a(bsent). Matches the game's code."""
    n = len(guess)
    result = ["a"] * n
    remaining = Counter(solution)
    for i in range(n):
        if guess[i] == solution[i]:
            result[i] = "c"
            remaining[guess[i]] -= 1
    for i in range(n):
        if result[i] == "a" and remaining[guess[i]] > 0:
            result[i] = "p"
            remaining[guess[i]] -= 1
    return "".join(result)


def filter_candidates(candidates: Iterable[str], history: History) -> List[str]:
    return [w for w in candidates if all(pattern(g, w) == p for g, p in history)]


# ----------------------------------------------------------- feedback parsing
_BUTTON_RE = re.compile(r"^wh:(correct|present|absent):([A-Za-z])$")


def _callback_of(button: Any) -> Optional[str]:
    data = getattr(button, "callback_data", None)
    if isinstance(data, bytes):
        try:
            data = data.decode()
        except UnicodeDecodeError:
            return None
    return data if isinstance(data, str) else None


def extract_letters(reply_markup: Any) -> List[Tuple[str, str]]:
    """All (letter, state_char) pairs found in a message's inline keyboard, in order."""
    out: List[Tuple[str, str]] = []
    rows = getattr(reply_markup, "inline_keyboard", None) or []
    for row in rows:
        for button in row:
            m = _BUTTON_RE.match(_callback_of(button) or "")
            if m:
                out.append((m.group(2).lower(), STATE_CHAR[m.group(1)]))
    return out


def rows_from_markup(reply_markup: Any, length_hint: Optional[int] = None) -> Tuple[History, Optional[int]]:
    """Rebuild the guess history from a feedback message. Returns (history, word_length).

    Keyboard rows that all have the same size 4/5/6 define the length; otherwise the
    hint is used to cut the flat letter list into guesses.
    """
    sizes = []
    for row in getattr(reply_markup, "inline_keyboard", None) or []:
        n = sum(1 for b in row if _BUTTON_RE.match(_callback_of(b) or ""))
        if n:
            sizes.append(n)
    letters = extract_letters(reply_markup)
    if not letters:
        return [], None
    length: Optional[int] = None
    if sizes and len(set(sizes)) == 1 and sizes[0] in LENGTHS:
        length = sizes[0]
    elif length_hint in LENGTHS:
        length = length_hint
    if length is None or len(letters) % length:
        return [], length
    history: History = []
    for i in range(0, len(letters), length):
        chunk = letters[i:i + length]
        history.append(("".join(c[0] for c in chunk), "".join(c[1] for c in chunk)))
    return history, length


# --------------------------------------------------------- bot text messages
_STARTED = re.compile(r"Game started!?\s*Guess the\s*(\d)\s*-?\s*letter word", re.I)
_INVALID = re.compile(r"\b([A-Za-z]{4,6})\s+is not a valid\s+(\d)\s*-?\s*letter word", re.I)
_DUPLICATE = re.compile(r"already guessed your word", re.I)
_WON = re.compile(r"guessed it correctly", re.I)
_OVER = re.compile(r"Game Over!", re.I)
_ENDED = re.compile(r"Game Ended", re.I)


def classify_text(text: Optional[str]) -> Tuple[str, Any]:
    """Return (kind, data) for a message from the game bot.

    kinds: started(length) | invalid(word) | duplicate | won | over | ended | other
    """
    if not text:
        return "other", None
    m = _STARTED.search(text)
    if m and int(m.group(1)) in LENGTHS:
        return "started", int(m.group(1))
    m = _INVALID.search(text)
    if m:
        return "invalid", m.group(1).lower()
    if _DUPLICATE.search(text):
        return "duplicate", None
    if _WON.search(text):
        return "won", None
    if _OVER.search(text):
        return "over", None
    if _ENDED.search(text):
        return "ended", None
    return "other", None


# -------------------------------------------------------------------- solver
def _letter_scores(words: Sequence[str]) -> Dict[str, int]:
    scores: Counter = Counter()
    for w in words:
        scores.update(set(w))
    return scores


def _entropy(guess: str, targets: Sequence[str]) -> float:
    buckets: Counter = Counter(pattern(guess, t) for t in targets)
    total = len(targets)
    return -sum((c / total) * math.log2(c / total) for c in buckets.values())


def choose_guess(
    length: int,
    history: History,
    excluded: Iterable[str] = (),
    rng: Optional[random.Random] = None,
    max_pool: int = 250,
    max_targets: int = 500,
) -> Optional[str]:
    """Best next guess given everything known, or None if the history is contradictory."""
    rng = rng or random.Random()
    excluded = set(excluded) | {g for g, _ in history}
    cands = [w for w in filter_candidates(answers(length), history) if w not in excluded]
    if not cands:  # the secret might not be in the "common" list
        cands = [w for w in filter_candidates(valid_guesses(length), history) if w not in excluded]
    if not cands:
        return None
    if not history:
        opener = OPENERS.get(length)
        if opener and opener not in excluded:
            return opener
    if len(cands) <= 2:
        return cands[0]

    targets = cands if len(cands) <= max_targets else rng.sample(cands, max_targets)
    scores = _letter_scores(cands)
    pool = sorted(cands, key=lambda w: -sum(scores[c] for c in set(w)))[:max_pool]
    best, best_val = pool[0], -1.0
    for w in pool:
        val = _entropy(w, targets) + (0.01 if w in cands else 0.0)
        if val > best_val:
            best, best_val = w, val
    return best


def simulate(solution: str, max_guesses: int = 30, rng: Optional[random.Random] = None) -> int:
    """Play a whole game against a known solution; returns number of guesses used (0 = failed)."""
    length = len(solution)
    history: History = []
    for turn in range(1, max_guesses + 1):
        guess = choose_guess(length, history, rng=rng)
        if guess is None:
            return 0
        if guess == solution:
            return turn
        history.append((guess, pattern(guess, solution)))
    return 0
