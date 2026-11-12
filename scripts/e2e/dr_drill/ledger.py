"""Ledger verification for the measured RPO: what was acknowledged against what is stored.

Pure logic over the readings of `GET /account` and `GET /consistency`. A movement that was not
acknowledged may still have committed (a timeout or a 5xx can hide a committed write), so the
abandoned and pending movements of the write probe form an uncertainty band:

    band = len(abandoned) + len(pending) + uncertain failover probe deposits (0 or 1 each)
    acked_expected = baseline + acknowledged + failover probe deposits proven to have run
    stored may legitimately lie in [acked_expected, acked_expected + band]
    rpo_lost_writes in [max(0, acked_expected - stored), max(0, acked_expected + band - stored)]

A loss is stated as definite only when `stored < acked_expected`; it is `not verifiable` when the
band is greater than 0 and the range still contains zero; a surplus larger than the band is
reported separately as `unexpected_extra`. The balance is checked against the same band, because a
band movement that did commit leaves the balance off by 0.01 (deposits and withdrawals alternate).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from dr_drill.api import Sample
from dr_drill.probe import Abandoned, Acknowledged, Pending

FAILOVER_DEPOSIT_CENTS = 1
DEPOSIT = "deposit"

RPO_ZERO_LOSS = "zero_loss"
RPO_LOSS = "loss"
RPO_NOT_VERIFIABLE = "not_verifiable"
RPO_NOT_VERIFIED = "not_verified"


@dataclass(frozen=True)
class LedgerReading:
    """The account as `GET /account` reports it."""

    movement_count: int
    balance_cents: int


@dataclass(frozen=True)
class ConsistencyReading:
    """The answer of `GET /consistency`; `consistent` is None when nothing was claimed."""

    consistent: bool | None
    truncated: bool
    replayed_balance_cents: int | None
    stored_balance_cents: int | None
    duplicate_keys: int | None
    chain_ok: bool | None
    balances_match: bool | None
    counts_by_region: dict[str, int]

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> ConsistencyReading:
        """Rebuild a reading from `to_data`; a missing or mistyped field becomes None."""
        regions = data.get("counts_by_region")
        return cls(
            consistent=_opt_bool(data.get("consistent")),
            truncated=data.get("truncated") is True,
            replayed_balance_cents=_opt_int(data.get("replayed_balance_cents")),
            stored_balance_cents=_opt_int(data.get("stored_balance_cents")),
            duplicate_keys=_opt_int(data.get("duplicate_keys")),
            chain_ok=_opt_bool(data.get("chain_ok")),
            balances_match=_opt_bool(data.get("balances_match")),
            counts_by_region={
                str(region): count
                for region, count in (regions.items() if isinstance(regions, dict) else ())
                if isinstance(count, int) and not isinstance(count, bool)
            },
        )

    def to_data(self) -> dict[str, Any]:
        """JSON-ready dictionary."""
        return {
            "consistent": self.consistent,
            "truncated": self.truncated,
            "replayed_balance_cents": self.replayed_balance_cents,
            "stored_balance_cents": self.stored_balance_cents,
            "duplicate_keys": self.duplicate_keys,
            "chain_ok": self.chain_ok,
            "balances_match": self.balances_match,
            "counts_by_region": dict(self.counts_by_region),
        }


@dataclass(frozen=True)
class LedgerReport:
    """Result of the verification; `problems` is empty when nothing is wrong."""

    rpo_status: str
    rpo_lost_min: int | None
    rpo_lost_max: int | None
    band: int
    baseline_count: int
    acked_expected: int
    stored_count: int | None
    unexpected_extra: int
    balance_expected_min: int
    balance_expected_max: int
    stored_balance_cents: int | None
    balance_ok: bool | None
    consistency: ConsistencyReading | None
    problems: tuple[str, ...] = field(default=())
    notes: tuple[str, ...] = field(default=())

    @property
    def verified(self) -> bool:
        """False when the count could not be checked (truncated or missing answers)."""
        return self.rpo_status != RPO_NOT_VERIFIED

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> LedgerReport:
        """Rebuild a report from `to_data` (the `ledger` snapshot of a timeline).

        Raises ValueError when a field is missing (required ones) or has the wrong type
        (optional ones may be absent or None, never mistyped).
        """
        consistency = data.get("consistency")
        if consistency is not None and not isinstance(consistency, dict):
            raise ValueError("ledger data field consistency is not an object")
        try:
            return cls(
                rpo_status=_req_str(data, "rpo_status"),
                rpo_lost_min=_nullable_int(data, "rpo_lost_min"),
                rpo_lost_max=_nullable_int(data, "rpo_lost_max"),
                band=_req_int(data, "band"),
                baseline_count=_req_int(data, "baseline_count"),
                acked_expected=_req_int(data, "acked_expected"),
                stored_count=_nullable_int(data, "stored_count"),
                unexpected_extra=_req_int(data, "unexpected_extra"),
                balance_expected_min=_req_int(data, "balance_expected_min"),
                balance_expected_max=_req_int(data, "balance_expected_max"),
                stored_balance_cents=_nullable_int(data, "stored_balance_cents"),
                balance_ok=_nullable_bool(data, "balance_ok"),
                consistency=(
                    ConsistencyReading.from_data(consistency) if consistency is not None else None
                ),
                problems=_texts(data, "problems"),
                notes=_texts(data, "notes"),
            )
        except KeyError as error:
            raise ValueError(f"ledger data lacks the field {error.args[0]}") from error

    def to_data(self) -> dict[str, Any]:
        """JSON-ready dictionary for the timeline and the report."""
        return {
            "rpo_status": self.rpo_status,
            "rpo_lost_min": self.rpo_lost_min,
            "rpo_lost_max": self.rpo_lost_max,
            "band": self.band,
            "baseline_count": self.baseline_count,
            "acked_expected": self.acked_expected,
            "stored_count": self.stored_count,
            "unexpected_extra": self.unexpected_extra,
            "balance_expected_min": self.balance_expected_min,
            "balance_expected_max": self.balance_expected_max,
            "stored_balance_cents": self.stored_balance_cents,
            "balance_ok": self.balance_ok,
            "consistency": self.consistency.to_data() if self.consistency else None,
            "problems": list(self.problems),
            "notes": list(self.notes),
        }


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _req_int(data: dict[str, Any], name: str) -> int:
    value = _opt_int(data[name])
    if value is None:
        raise ValueError(f"ledger data field {name} is not an integer")
    return value


def _nullable_int(data: dict[str, Any], name: str) -> int | None:
    value = data.get(name)
    if value is not None and _opt_int(value) is None:
        raise ValueError(f"ledger data field {name} is not an integer")
    return value


def _nullable_bool(data: dict[str, Any], name: str) -> bool | None:
    value = data.get(name)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"ledger data field {name} is not a boolean")
    return value


def _texts(data: dict[str, Any], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"ledger data field {name} is not a list of strings")
    return tuple(value)


def _req_str(data: dict[str, Any], name: str) -> str:
    value = data[name]
    if not isinstance(value, str):
        raise ValueError(f"ledger data field {name} is not a string")
    return value


def _int(sample: Sample, name: str) -> int | None:
    value = sample.body_fields.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(sample: Sample, name: str) -> bool | None:
    value = sample.body_fields.get(name)
    return value if isinstance(value, bool) else None


def read_account(sample: Sample) -> LedgerReading | None:
    """The account reading, or None when the answer is not a usable `GET /account`."""
    count = _int(sample, "movement_count")
    balance = _int(sample, "balance_cents")
    if not sample.ok or count is None or balance is None:
        return None
    return LedgerReading(count, balance)


def read_consistency(sample: Sample) -> ConsistencyReading | None:
    """The consistency reading, or None when the answer is not a usable `GET /consistency`."""
    if not sample.ok:
        return None
    regions = sample.body_fields.get("counts_by_region")
    return ConsistencyReading(
        consistent=_flag(sample, "consistent"),
        truncated=_flag(sample, "truncated") is True,
        replayed_balance_cents=_int(sample, "replayed_balance_cents"),
        stored_balance_cents=_int(sample, "stored_balance_cents"),
        duplicate_keys=_int(sample, "duplicate_keys"),
        chain_ok=_flag(sample, "chain_ok"),
        balances_match=_flag(sample, "balances_match"),
        counts_by_region=dict(regions) if isinstance(regions, dict) else {},
    )


def _delta(kind: str) -> int:
    return 1 if kind == DEPOSIT else -1


def _consistency_problems(consistency: ConsistencyReading) -> list[str]:
    problems = []
    if consistency.consistent is False:
        problems.append("the ledger check reports an inconsistency")
    if consistency.duplicate_keys:
        problems.append(f"{consistency.duplicate_keys} duplicate idempotency keys")
    if consistency.chain_ok is False:
        problems.append("the balance chain of the ledger is broken")
    return problems


def _rpo(acked_expected: int, band: int, stored: int) -> tuple[str, int, int]:
    low = max(0, acked_expected - stored)
    high = max(0, acked_expected + band - stored)
    if low > 0:
        return RPO_LOSS, low, high
    if high > 0:
        return RPO_NOT_VERIFIABLE, low, high
    return RPO_ZERO_LOSS, low, high


def verify_ledger(
    baseline: LedgerReading,
    final: LedgerReading | None,
    consistency: ConsistencyReading | None,
    acked: Sequence[Acknowledged],
    abandoned: Sequence[Abandoned],
    pending: Sequence[Pending],
    failover_deposits: int = 0,
    uncertain_deposits: int = 0,
) -> LedgerReport:
    """Compare the acknowledged writes with the stored ledger, within the uncertainty band.

    `failover_deposits` are failover probe deposits proven to have run by the log of the validate
    step (`dr_drill.validate_log`). `uncertain_deposits` are those whose log was missing, cut or
    not recognised: the 0.01 probe deposit of a failover happens only when `40-validate.sh --probe`
    has a token, and a successful step does not say so. They widen the band like an abandoned
    movement. A failover whose log proves no deposit adds nothing to either count.
    """
    band_items: list[Abandoned | Pending] = [*abandoned, *pending]
    band_kinds = [item.kind for item in band_items]
    band = len(band_kinds) + uncertain_deposits
    acked_expected = baseline.movement_count + len(acked) + failover_deposits
    base_delta = (
        sum(_delta(item.kind) for item in acked) + failover_deposits * FAILOVER_DEPOSIT_CENTS
    )
    deltas = [_delta(kind) for kind in band_kinds] + [FAILOVER_DEPOSIT_CENTS] * uncertain_deposits
    low_balance = baseline.balance_cents + base_delta + sum(min(0, delta) for delta in deltas)
    high_balance = baseline.balance_cents + base_delta + sum(max(0, delta) for delta in deltas)
    problems: list[str] = [] if consistency is None else _consistency_problems(consistency)
    notes: list[str] = []
    unverified = (
        final is None
        or consistency is None
        or consistency.truncated
        or consistency.consistent is None
    )
    stored = None if final is None else final.movement_count
    balance = None if final is None else final.balance_cents
    balance_ok = None if balance is None else low_balance <= balance <= high_balance
    if balance_ok is False:
        problems.append("the stored balance is outside the expected range")
    status = RPO_NOT_VERIFIED
    lost_min: int | None = None
    lost_max: int | None = None
    extra = 0
    if unverified or stored is None:
        notes.append("the movement count could not be verified (missing or truncated answer)")
    else:
        status, lost_min, lost_max = _rpo(acked_expected, band, stored)
        extra = max(0, stored - (acked_expected + band))
        if status == RPO_LOSS:
            problems.append(f"{lost_min} acknowledged writes are missing from the ledger")
        if status == RPO_NOT_VERIFIABLE:
            notes.append("the uncertainty band hides whether an acknowledged write was lost")
        if extra:
            problems.append(f"{extra} movements in the ledger are not accounted for")
    return LedgerReport(
        rpo_status=status,
        rpo_lost_min=lost_min,
        rpo_lost_max=lost_max,
        band=band,
        baseline_count=baseline.movement_count,
        acked_expected=acked_expected,
        stored_count=stored,
        unexpected_extra=extra,
        balance_expected_min=low_balance,
        balance_expected_max=high_balance,
        stored_balance_cents=balance,
        balance_ok=balance_ok,
        consistency=consistency,
        problems=tuple(problems),
        notes=tuple(notes),
    )
