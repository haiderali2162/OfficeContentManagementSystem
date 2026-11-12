"""App-wide search-term translation with traditional wildcards.

Every text search in the app should pass user input through
:func:`like_pattern` so the same behaviour works everywhere now and in the
future:

- ``*`` matches any run of characters (like SQL ``%``),
- ``?`` matches exactly one character (like SQL ``_``),
- a term without wildcards keeps the established "contains" behaviour,
- literal ``%`` and ``_`` typed by the user match themselves (escaped),
- the SQL side must pass ``escape="\\\\"`` to ``ilike``/``like``.
"""

from __future__ import annotations

import re
from typing import Any

_ESCAPEABLE = re.compile(r"[\\%_]")


def like_pattern(value: Any) -> str | None:
    """Translate one user search term into a SQL LIKE/ILIKE pattern.

    Returns ``None`` when the term is empty so callers can skip the filter
    exactly as they did with blank input before.
    """
    text = str(value or "").strip()
    if not text:
        return None

    def _escape(match: re.Match[str]) -> str:
        return "\\" + match.group(0)

    body = _ESCAPEABLE.sub(_escape, text)
    if "*" in text or "?" in text:
        # Wildcard term: * and ? become SQL wildcards; the pattern already
        # covers any position, so no outer % wrapping is added.
        return body.replace("*", "%").replace("?", "_")
    return f"%{body}%"


def matches_pattern(stored: Any, typed: Any) -> bool:
    """Python-side mirror of :func:`like_pattern` for candidate rows.

    SQL prefilters stay broad supersets in places (for example cadastral
    token searches validate whole tokens afterwards), so validators can use
    this to apply the same wildcard semantics a LIKE query would.
    """
    term = str(typed or "").strip()
    text = str(stored or "")
    if not term:
        return True
    if "*" in term or "?" in term:
        regex = re.escape(term).replace(r"\*", ".*").replace(r"\?", ".")
        return re.fullmatch(regex, text, flags=re.IGNORECASE) is not None
    return term.casefold() in text.casefold()
