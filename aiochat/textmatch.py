"""Turning a user's search text into an FTS5 MATCH expression.

Message bodies are indexed in the ``messages_fts`` virtual table (see
schema.sql), tokenized by ``unicode61``: a token is a run of letters and
digits, case and accents folded. Searching is by word, not by substring:
every word of the query must occur in the message, in any order.

* ``word`` (default): each query word must match a whole token, so
  ``note`` does not find ``notes``.
* ``prefix``: each query word may match the start of a token, so
  ``note stand`` finds ``standup notes``.

The user never writes FTS5 syntax. Each whitespace-separated word is
quoted, which makes FTS5 tokenize it with the same rules as the indexed
text: ``c++`` becomes the token ``c``, ``budget-review`` the phrase
"budget review", and a ``"`` inside a word is harmless. Quoted terms
separated by spaces are an implicit AND.
"""

from __future__ import annotations

MODES = ("word", "prefix")
DEFAULT_MODE = "word"


def like_pattern(query: str) -> str:
    """``%query%`` with LIKE's wildcards escaped (use with ``ESCAPE '\\'``).

    Still used for substring filters on short fields such as contact names.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def words(query: str) -> list[str]:
    """The searchable words of a query: whitespace-separated, and containing
    at least one letter or digit (``_`` or ``%`` alone would match nothing)."""
    return [w for w in query.split() if any(ch.isalnum() for ch in w)]


def fts_query(query: str, mode: str = DEFAULT_MODE) -> str | None:
    """An FTS5 MATCH expression for ``query``, or None if it has no words."""
    terms = words(query)
    if not terms:
        return None
    suffix = "*" if mode == "prefix" else ""
    return " ".join(f'"{term.replace(chr(34), chr(34) * 2)}"{suffix}' for term in terms)
