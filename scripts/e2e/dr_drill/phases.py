"""Phase names and verdicts shared by the drill, the metrics and the report.

A leaf module: it imports nothing from the package, so the pure calculation
and rendering modules do not depend on the orchestrator.
"""

from __future__ import annotations

PHASE_BASELINE = "baseline"
PHASE_FENCE = "fence"
PHASE_TO_SECONDARY = "to-secondary"
PHASE_STEADY_SECONDARY = "steady-secondary"
PHASE_TO_PRIMARY = "to-primary"
PHASE_STEADY_PRIMARY = "steady-primary"
PHASE_CONSISTENCY = "consistency"

VERDICT_PASS = "PASS"  # noqa: S105 - a verdict, not a password
VERDICT_FAIL = "FAIL"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_ABORTED = "ABORTED"

LAG_SETTLE_SECONDS = 120.0
