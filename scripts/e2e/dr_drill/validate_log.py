"""Reads the log of the failover step "validate the service" to learn whether the probe deposit ran.

`scripts/dr/40-validate.sh --probe` logs `step 3/3: probe movement (deposit of 0.01)` and then one
of: `HTTP 201 processed_region=...` (the 0.01 deposit was posted), `skipped (...)` (nothing was
posted: no token, or no `--probe`) or a warning with an HTTP error. Only a 4xx answer or the 503
"cannot write" (fenced region) prove nothing was posted (REJECTED); a gateway error (502, 504) or
status 000 may hide a committed deposit, so it stays UNKNOWN like anything else. A log without the
header proves nothing. A rejected probe fails the step, so the log is read also when the run failed.

Pure text logic: the log may carry the `job<TAB>step<TAB>` prefix of `gh run view --log`, a
timestamp and ANSI colours; every marker is searched as a substring.
"""

from __future__ import annotations

import re
from enum import Enum

VALIDATE_STEP = "Group 3 - validate the service on the custom domain"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROBE_HEADER = "step 3/3: probe movement"
_DEPOSITED = re.compile(r"HTTP 201 processed_region=")
_REJECTED = re.compile(r"probe returned HTTP 4\d\d\b|HTTP 503 .*cannot write")
_SKIPPED = re.compile(r"skipped \((?:no COGNITO_ID_TOKEN|add --probe)")


class ProbeDeposit(Enum):
    """What the validate step log says about the 0.01 probe deposit of one failover."""

    DEPOSITED = "deposited"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


def classify_probe_deposit(log: str | None) -> ProbeDeposit:
    """Classify the log of the validate step; UNKNOWN when it is missing, cut or not recognised."""
    if not log:
        return ProbeDeposit.UNKNOWN
    lines = [_ANSI.sub("", line) for line in log.splitlines()]
    starts = [index for index, line in enumerate(lines) if PROBE_HEADER in line]
    if not starts:
        return ProbeDeposit.UNKNOWN
    for line in lines[starts[0] + 1 :]:
        if _DEPOSITED.search(line):
            return ProbeDeposit.DEPOSITED
        if _REJECTED.search(line):
            return ProbeDeposit.REJECTED
        if _SKIPPED.search(line):
            return ProbeDeposit.SKIPPED
    return ProbeDeposit.UNKNOWN
