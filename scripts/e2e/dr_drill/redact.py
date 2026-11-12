"""Output guard: the single place that decides whether a text may be written, printed or stored."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable

_JWT_PREFIXED = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_JWT_SHAPE = re.compile(r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")
_HEX_TOKEN = re.compile(r"[0-9A-Fa-f]{32,}")
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_.+/=-]{40,}")
_WORD_SEPARATORS = re.compile(r"[-_./=+]")
_MAX_WORD = 32
_PLAIN_WORD = re.compile(r"[a-z0-9]{1,32}|\d+(?:T\d+)?Z?")
_DIGITS = re.compile(r"\d+(?:T\d+)?Z?")
_ACCESS_KEY_ID = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")
_ACCOUNT_ID = re.compile(r"(?<!\d)(?:\d{12}|\d{4}[ -]\d{4}[ -]\d{4})(?!\d)")
_UUID_V4 = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)
_ARN = re.compile(r"arn:aws", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def _is_opaque(run: str) -> bool:
    """A long run is opaque unless it is made of short lowercase words (names, paths, run folders).

    Names such as `fnc-uswest2-drdemo-create-transaction-prod` and paths such as
    `reports/e2e/2026-11-11T10-00-00Z/run.log` have that word structure; a base64
    or AWS key alphabet secret mixes upper case, lower case and digits inside its words, and a
    base32 or base36 secret is one long word with no separator. No legitimate name here has a
    word longer than `_MAX_WORD`, so such a word is opaque even when it is all lowercase.

    Limit (accepted): this heuristic is a SECOND line of defence. A token made of short
    lowercase or digit segments joined by hyphens looks like a resource name and passes, and so
    does a secret split by a space or a backslash. The primary defence is `OutputGuard.register`:
    every module that reads a secret must register its value, and registered values are refused
    verbatim wherever they appear.
    """
    words = [word for word in _WORD_SEPARATORS.split(run) if word]
    if any(len(word) > _MAX_WORD and not _DIGITS.fullmatch(word) for word in words):
        return True
    if all(_PLAIN_WORD.fullmatch(word) for word in words):
        return False
    return (
        any(char.isupper() for char in run)
        and any(char.islower() for char in run)
        and any(char.isdigit() for char in run)
    )


def _has_opaque_token(text: str) -> bool:
    return any(_is_opaque(match.group()) for match in _OPAQUE_TOKEN.finditer(text))


class RefusedOutput(Exception):  # noqa: N818 - the name is part of the runner's vocabulary
    """A field was refused by the guard; the message names the field and reason, never the value."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"output refused: {field} was not written because {reason}")
        self.field = field
        self.reason = reason


class OutputGuard:
    """Decides whether a text is safe to write; known secret values are passed at construction."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(value for value in secrets if value)
        self._lock = threading.Lock()

    def register(self, value: str) -> None:
        """Add a secret learned at run time (a token); from now on it is refused verbatim.

        Thread-safe: probe threads register fresh tokens concurrently.
        """
        with self._lock:
            if value and value not in self._secrets:
                self._secrets = (*self._secrets, value)

    def reason(self, text: str) -> str | None:
        """Why the text must not be written, or None when it is safe.

        A 12-digit run not adjacent to other digits is refused whatever surrounds it (dots
        included); numbers that are serialized must be rounded at the source so that no float
        carries such a run. The only exception is a standalone, well-formed UUID v4 (the probe
        idempotency keys), which is replaced before the account check. Accepted residual: a
        deliberately built v4 whose last group is an account id passes; every other shape is
        refused.
        """
        if any(secret in text for secret in self._secrets):
            return "it contains a known secret value"
        if _JWT_PREFIXED.search(text) or _JWT_SHAPE.search(text):
            return "it contains a token with a JWT shape"
        if _HEX_TOKEN.search(text):
            return "it contains a long hexadecimal token"
        if _has_opaque_token(text) or _ACCESS_KEY_ID.search(text):
            return "it contains a long opaque token"
        if _ACCOUNT_ID.search(_UUID_V4.sub("uuid", text)):
            return "it contains a 12-digit number (possible AWS account id)"
        if _ARN.search(text):
            return "it contains an ARN"
        if _EMAIL.search(text):
            return "it contains an e-mail address"
        return None

    def ensure(self, field: str, text: str) -> None:
        """Raise RefusedOutput naming the field when the text is not safe."""
        reason = self.reason(text)
        if reason is not None:
            raise RefusedOutput(field, reason)
