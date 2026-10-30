"""Game prompt parsing: extract the requested word length from a message."""
from __future__ import annotations

import re
from typing import Optional

MIN_LENGTH = 2
MAX_LENGTH = 20

_N = r"(\d{1,3})"
_UNIT = r"(?:letter|character|char)s?"

# Each pattern captures the length in group 1.
_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # "5-letter word", "6 letter word", "7 letters words"
        rf"\b{_N}\s*-?\s*{_UNIT}\s*-?\s*words?\b",
        # "word with 7 letters", "word having 8 letters", "word containing 6 characters"
        rf"\bwords?\s+(?:with|having|containing|of)\s+(?:exactly\s+)?{_N}\s*{_UNIT}\b",
        # "word of length 9", "word length: 9", "word length is 9"
        rf"\bwords?\s+(?:of\s+)?length\s*(?:is|=|:|-)?\s*{_N}\b",
        # "Length: 5", "length = 5"
        rf"\blength\s*[:=]\s*{_N}\b",
        # "Find a 5 letter ...", "Send me a 6-letter ..."
        rf"\b(?:find|guess|send|give|type|make|form)\s+(?:me\s+)?(?:a|an|the|any)?\s*{_N}\s*-?\s*{_UNIT}\b",
    )
)


def extract_length(text: Optional[str]) -> Optional[int]:
    """Return the requested word length, or None if the text is not a length prompt.

    When several length phrases appear, the earliest one in the text wins.
    Lengths outside MIN_LENGTH..MAX_LENGTH are ignored.
    """
    if not text:
        return None
    best: Optional[tuple] = None  # (position, length)
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            length = int(match.group(1))
            if not (MIN_LENGTH <= length <= MAX_LENGTH):
                continue
            if best is None or match.start() < best[0]:
                best = (match.start(), length)
            break  # first valid hit per pattern is enough
    return best[1] if best else None
