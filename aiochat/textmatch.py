"""Matching modes for message search.

SQLite's ``LIKE`` gives case-insensitive substring matches, which is the
default. Whole-word and prefix matching need word boundaries, which ``LIKE``
cannot express, so those two modes are a small Python function registered on
every connection (``db.get_db``) as ``aiochat_match(mode, query, body)``.
Queries always combine it with the ``LIKE`` prefilter, so the stricter modes
only ever narrow the substring results and stay consistent with them.

The query is matched as a literal phrase (no operators). ``word`` requires
it to be bounded by non-word characters on both sides; ``prefix`` only on
the left, so ``bud`` matches ``budget`` but not ``rebudget``.
"""

from __future__ import annotations

import re
from functools import lru_cache

MODES = ("substring", "word", "prefix")
DEFAULT_MODE = "substring"


def like_pattern(query: str) -> str:
    """``%query%`` with LIKE's wildcards escaped (use with ``ESCAPE '\\'``)."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@lru_cache(maxsize=256)
def _compile(mode: str, query: str) -> re.Pattern:
    escaped = re.escape(query)
    if mode == "word":
        pattern = rf"(?<!\w){escaped}(?!\w)"
    elif mode == "prefix":
        pattern = rf"(?<!\w){escaped}"
    else:
        pattern = escaped
    # Lookarounds rather than \b: a query that starts or ends with
    # punctuation ("c++", "@mia") would otherwise need a word character
    # next to it, which is the opposite of what a boundary should mean.
    return re.compile(pattern, re.IGNORECASE)


def matches(mode: str, query: str, body: str | None) -> bool:
    if body is None or mode not in MODES:
        return False
    return _compile(mode, query).search(body) is not None


def sql_function(mode, query, body) -> int:
    """``aiochat_match(mode, query, body)`` as seen from SQL; returns 1 or 0."""
    return 1 if matches(mode, query, body) else 0
