"""Game-source clients. Each normalises its platform's payload to `Game`.

The pieces shared between them live here: a common error base, so a sync that
walks several platforms can catch one platform failing without naming every
platform's exception type, and the PGN header reader they both fall back to.
"""

from __future__ import annotations

import io

import chess.pgn


class PlatformError(Exception):
    """A game source could not be used.

    Each client raises its own subclass, so a message always names the platform
    that produced it.
    """


def eco_from_pgn(pgn: str) -> str | None:
    """The opening code from a PGN's headers, when it carries one."""
    headers = chess.pgn.read_headers(io.StringIO(pgn))
    if headers is None:
        return None
    code = headers.get("ECO")
    return code or None
