"""Dictionary used to answer length prompts.

Words are bucketed by their *actual* length, so a bucket can never contain a
word of the wrong size. A built-in list of common words is always available.
If a system word list exists (Ubuntu: ``apt install wamerican``) or
``WORDLIST_PATH`` points to a file (one word per line), it is used as a
fallback for lengths the built-in list cannot satisfy or has exhausted.
"""
from __future__ import annotations

import os
import random
import re
from collections import deque
from typing import Collection, Dict, List, Optional, Tuple

_BUILTIN = """
cat dog sun map pen cup hat bag box key sky sea fox owl bee ant car bus jam rug net pot fan gem ink jar lid
mud nut oak pie rat sip tub web yak zip van wax toy tea bed cow day eye fig gas hen ice joy kit leg man nap
pig rib sad tin urn vet win yes zoo arm bat cap dam elf fur gum hip jet lip mat oil pan rod sit tap wig
tree book lamp fish bird door wind rain snow moon star cake ring ship road hill lake farm gold iron jump
kind lion milk nest park rock sand tent wolf yarn zone bell coin dust echo flag gift hope idea jade kite
lava mist note oven pond quiz rose silk tide vase wave yard zero atom bark cave dive east fame glow herb
apple table chair house water music green light plant river stone bread cloud dream earth flame grape
heart juice knife lemon mouse night ocean piano queen robot smile tiger uncle video whale youth zebra
beach candy dance eagle fruit globe honey input jelly koala lunch magic noble olive pearl quiet radio
sugar toast unity voice wheat xerox yield zesty anchor brave cabin delta eight fairy giant happy ivory
garden window planet orange purple silver yellow spring summer winter forest island bridge castle dragon
flower guitar jungle market pencil rabbit school travel wizard basket candle desert engine friend hammer
jacket kitten ladder magnet nature oyster parrot rocket sunset tomato valley walnut yogurt zipper bottle
cherry doctor eleven finger golden harbor insect jigsaw kidney lizard monkey nickel orchard pepper
balance biscuit captain chicken diamond elegant factory gallery harvest imagine journey kitchen lantern
machine natural octopus painter quality rainbow sunrise teacher variety weather blanket cabinet dolphin
explore forward giraffe holiday inspect justice kingdom library monster network outside penguin quarter
respect station thunder uniform village whisper younger zealous anytime brother cartoon distant evening
absolute backpack calendar daylight elephant festival graphite hospital internet jukebox keyboard language
mountain notebook obstacle painting question reaction sandwich treasure umbrella vacation waterfall
airplane birthday champion discover envelope football goldfish hardware identity jellyfish kangaroo
magazine nightfall optimism platform quickest research strength triangle universe vitamins wildlife
yearbook zucchini bicycles chestnut dinosaur electron fireworks gladiator hairbrush instance
adventure beautiful chocolate dangerous education fireplace generator happiness important knowledge
landscape magnetic nightmare operation telephone universal vegetable wonderful yesterday alphabet
butterfly crocodile dandelion elevation fantastic geography hurricane invention kilometer lightning
marathons notebooks orchestra passenger questions raspberry saturdays tradition umbrellas volleyball
waterfalls blueberry classroom delicious emergency favorites grapefruit hospitals
basketball dictionary earthquake friendship helicopter incredible journalist lighthouse motorcycle
playground restaurant reflection strawberry television wilderness government electrical photograph
adventures blacksmith broadcast chameleon excitement fascinated headphones imaginable landmarks
grandfather grandmother imagination electricity environment mathematics opportunity temperature
celebration butterflies comfortable difference everything flashlight generations headquarters
refrigerator encyclopedia photographer playgrounds spectacular thunderstorm adventurous
communication conversation determination entertainment extraordinary generosity international
neighborhood independent professional transportation understanding
""".split()

_SYSTEM_DICT_PATHS = (
    "/usr/share/dict/american-english",
    "/usr/share/dict/words",
)
_ALPHA = re.compile(r"^[a-z]+$")

_builtin_by_len: Dict[int, List[str]] = {}
_system_by_len: Dict[int, List[str]] = {}
_system_loaded = False


def _bucket(words, into: Dict[int, List[str]]) -> None:
    seen = set()
    for w in words:
        w = w.strip().lower()
        if w and _ALPHA.match(w) and w not in seen:
            seen.add(w)
            into.setdefault(len(w), []).append(w)


def reload() -> None:
    """(Re)build the built-in buckets and forget the system dictionary cache."""
    global _system_loaded
    _builtin_by_len.clear()
    _system_by_len.clear()
    _bucket(_BUILTIN, _builtin_by_len)
    _system_loaded = False


def _load_system_words() -> None:
    global _system_loaded
    if _system_loaded:
        return
    _system_loaded = True
    paths = list(_SYSTEM_DICT_PATHS)
    extra = os.getenv("WORDLIST_PATH", "").strip()
    if extra:
        paths.insert(0, extra)
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                _bucket(fh, _system_by_len)
        except OSError:
            continue


def available_lengths() -> List[int]:
    _load_system_words()
    return sorted(set(_builtin_by_len) | set(_system_by_len))


def words_for_length(length: int) -> Tuple[str, ...]:
    _load_system_words()
    merged = list(_builtin_by_len.get(length, ()))
    known = set(merged)
    merged.extend(w for w in _system_by_len.get(length, ()) if w not in known)
    return tuple(merged)


def choose_word(
    length: int,
    avoid: Optional[Collection[str]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Pick a word of exactly ``length`` letters, or None if none exists.

    Preference order: unused built-in word, unused system-dictionary word,
    then (only if everything was used) any built-in word.
    """
    rng = rng or random
    avoid = set(avoid or ())

    candidates = [w for w in _builtin_by_len.get(length, ()) if w not in avoid]
    if not candidates:
        _load_system_words()
        candidates = [w for w in _system_by_len.get(length, ()) if w not in avoid]
    if not candidates:
        candidates = list(_builtin_by_len.get(length, ()))
    if not candidates:
        return None

    word = rng.choice(candidates)
    # Defensive validation: never return a word of the wrong size.
    return word if len(word) == length else None


class RecentWords:
    """Remembers the last N words sent so the same word is not repeated."""

    def __init__(self, size: int = 300):
        self._items: deque = deque(maxlen=size)

    def add(self, word: str) -> None:
        self._items.append(word)

    def __contains__(self, word: str) -> bool:
        return word in self._items

    def as_set(self) -> set:
        return set(self._items)


reload()
