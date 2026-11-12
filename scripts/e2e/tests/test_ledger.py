from datetime import UTC, datetime

import pytest

from dr_drill.api import FieldValue, Sample
from dr_drill.ledger import (
    ConsistencyReading,
    LedgerReading,
    LedgerReport,
    read_account,
    read_consistency,
    verify_ledger,
)
from dr_drill.probe import Abandoned, Acknowledged, Pending

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
BASELINE = LedgerReading(movement_count=10, balance_cents=10_000)


def acked(count: int) -> list[Acknowledged]:
    """Alternating movements, deposit first: five of them net +1 cent."""
    kinds = ["deposit", "withdrawal"]
    return [Acknowledged(f"w{n}", f"k{n}", kinds[n % 2], None, 201) for n in range(count)]


def good(consistent: bool | None = True, truncated: bool = False) -> ConsistencyReading:
    return ConsistencyReading(consistent, truncated, 1, 1, 0, True, True, {"us-east-2": 3})


def verify(
    stored_count: int | None,
    stored_balance: int | None,
    count: int = 5,
    abandoned: list[Abandoned] | None = None,
    pending: list[Pending] | None = None,
    consistency: ConsistencyReading | None = None,
    deposits: int = 2,
    uncertain: int = 0,
) -> LedgerReport:
    final = (
        None
        if stored_count is None or stored_balance is None
        else LedgerReading(stored_count, stored_balance)
    )
    return verify_ledger(
        BASELINE,
        final,
        consistency if consistency is not None else good(),
        acked(count),
        abandoned or [],
        pending or [],
        deposits,
        uncertain,
    )


# Five acknowledged movements net +1 cent, two failover deposits +2: balance 10_003, count 17.
FULL_COUNT = 17
FULL_BALANCE = 10_003


def test_zero_loss() -> None:
    report = verify(FULL_COUNT, FULL_BALANCE)

    assert report.rpo_status == "zero_loss"
    assert (report.rpo_lost_min, report.rpo_lost_max) == (0, 0)
    assert report.band == 0
    assert report.acked_expected == FULL_COUNT
    assert report.balance_ok is True
    assert report.problems == ()
    assert report.verified


def test_one_lost_acknowledged_write_is_a_definite_loss() -> None:
    report = verify(FULL_COUNT - 1, FULL_BALANCE - 1)

    assert report.rpo_status == "loss"
    assert (report.rpo_lost_min, report.rpo_lost_max) == (1, 1)
    assert any("1 acknowledged writes are missing" in problem for problem in report.problems)


def test_a_surplus_is_reported_separately_and_is_not_a_loss() -> None:
    report = verify(FULL_COUNT + 2, FULL_BALANCE + 2)

    assert report.rpo_status == "zero_loss"
    assert report.unexpected_extra == 2
    assert any("not accounted for" in problem for problem in report.problems)


def test_a_truncated_consistency_answer_is_not_verified_never_zero_loss() -> None:
    report = verify(FULL_COUNT, FULL_BALANCE, consistency=good(consistent=None, truncated=True))

    assert report.rpo_status == "not_verified"
    assert report.rpo_lost_min is None
    assert not report.verified
    assert report.notes


def test_missing_final_readings_are_not_verified() -> None:
    assert verify(None, None).rpo_status == "not_verified"


def test_an_abandoned_movement_that_did_commit_is_neither_loss_nor_extra() -> None:
    abandoned = [Abandoned("w6", "k6", "deposit", 3, 422, "permanent")]

    report = verify(FULL_COUNT + 1, FULL_BALANCE + 1, abandoned=abandoned)

    assert report.band == 1
    assert report.rpo_status == "zero_loss"
    assert report.unexpected_extra == 0
    assert report.balance_ok is True
    assert report.problems == ()


def test_an_abandoned_movement_that_did_not_commit_is_not_verifiable() -> None:
    abandoned = [Abandoned("w6", "k6", "withdrawal", 600, 503, "max_outage")]

    report = verify(FULL_COUNT, FULL_BALANCE, abandoned=abandoned)

    assert report.rpo_status == "not_verifiable"
    assert (report.rpo_lost_min, report.rpo_lost_max) == (0, 1)
    assert report.balance_ok is True
    assert report.problems == ()
    assert report.notes


def test_a_pending_movement_at_stop_is_part_of_the_band() -> None:
    pending = [Pending("w6", "k6", "deposit", 2)]

    committed = verify(FULL_COUNT + 1, FULL_BALANCE + 1, pending=pending)
    not_committed = verify(FULL_COUNT, FULL_BALANCE, pending=pending)

    assert committed.rpo_status == "zero_loss"
    assert not_committed.rpo_status == "not_verifiable"


def test_a_lost_acked_write_hidden_by_an_abandoned_commit_is_never_zero_loss() -> None:
    abandoned = [Abandoned("w6", "k6", "deposit", 3, 422, "permanent")]

    report = verify(FULL_COUNT, FULL_BALANCE, abandoned=abandoned)

    assert report.rpo_status == "not_verifiable"
    assert report.rpo_status != "zero_loss"


def test_a_loss_beyond_the_band_is_definite() -> None:
    abandoned = [Abandoned("w6", "k6", "deposit", 3, 422, "permanent")]

    report = verify(FULL_COUNT - 2, FULL_BALANCE - 2, abandoned=abandoned)

    assert report.rpo_status == "loss"
    assert (report.rpo_lost_min, report.rpo_lost_max) == (2, 3)


def test_a_surplus_beyond_the_band_is_unexpected() -> None:
    abandoned = [Abandoned("w6", "k6", "deposit", 3, 422, "permanent")]

    report = verify(FULL_COUNT + 3, FULL_BALANCE + 3, abandoned=abandoned)

    assert report.unexpected_extra == 2


def test_a_balance_outside_the_band_is_a_problem() -> None:
    report = verify(FULL_COUNT, FULL_BALANCE + 5)

    assert report.balance_ok is False
    assert any("balance" in problem for problem in report.problems)


def test_the_balance_band_follows_the_kind_of_the_band_movements() -> None:
    abandoned = [Abandoned("w6", "k6", "withdrawal", 3, 422, "permanent")]

    report = verify(FULL_COUNT + 1, FULL_BALANCE - 1, abandoned=abandoned)

    assert (report.balance_expected_min, report.balance_expected_max) == (
        FULL_BALANCE - 1,
        FULL_BALANCE,
    )
    assert report.balance_ok is True


def test_uncertain_failover_deposits_widen_the_band() -> None:
    report = verify(FULL_COUNT - 1, FULL_BALANCE - 1, deposits=1, uncertain=1)

    assert report.band == 1
    assert report.rpo_status == "not_verifiable"
    assert report.balance_ok is True


def test_fewer_failover_deposits_lower_the_expectation() -> None:
    report = verify(FULL_COUNT - 2, FULL_BALANCE - 2, deposits=0)

    assert report.acked_expected == FULL_COUNT - 2
    assert report.rpo_status == "zero_loss"


@pytest.mark.parametrize(
    ("consistency", "fragment"),
    [
        (ConsistencyReading(False, False, 1, 2, 0, True, False, {}), "inconsistency"),
        (ConsistencyReading(True, False, 1, 1, 3, True, True, {}), "duplicate"),
        (ConsistencyReading(True, False, 1, 1, 0, False, True, {}), "chain"),
    ],
)
def test_an_inconsistent_ledger_is_a_problem(
    consistency: ConsistencyReading, fragment: str
) -> None:
    report = verify(FULL_COUNT, FULL_BALANCE, consistency=consistency)

    assert any(fragment in problem for problem in report.problems)


def test_the_report_converts_to_json_ready_data() -> None:
    data = verify(FULL_COUNT, FULL_BALANCE).to_data()

    assert data["rpo_status"] == "zero_loss"
    assert data["consistency"]["counts_by_region"] == {"us-east-2": 3}
    assert data["problems"] == []
    assert verify(None, None, consistency=good()).to_data()["stored_count"] is None


def _sample(status: int, fields: dict[str, FieldValue], malformed: bool = False) -> Sample:
    return Sample(NOW, NOW, status, 1.0, body_fields=fields, malformed=malformed)


def test_read_account() -> None:
    sample = _sample(200, {"balance_cents": 5, "movement_count": 3})

    assert read_account(sample) == LedgerReading(3, 5)
    assert read_account(_sample(200, {"balance_cents": 5})) is None
    assert read_account(_sample(503, {"balance_cents": 5, "movement_count": 3})) is None


def test_read_consistency() -> None:
    fields: dict[str, FieldValue] = {
        "consistent": True,
        "truncated": False,
        "replayed_balance_cents": 11,
        "stored_balance_cents": 11,
        "duplicate_keys": 0,
        "chain_ok": True,
        "balances_match": True,
        "counts_by_region": {"us-east-2": 5},
    }

    reading = read_consistency(_sample(200, fields))

    assert reading is not None
    assert reading.consistent is True
    assert reading.counts_by_region == {"us-east-2": 5}
    assert read_consistency(_sample(500, fields)) is None


def test_a_truncated_answer_reads_as_not_claiming_anything() -> None:
    reading = read_consistency(_sample(200, {"truncated": True}))

    assert reading is not None
    assert reading.consistent is None
    assert reading.truncated is True
    assert reading.counts_by_region == {}


def test_ledger_report_round_trips_through_its_data() -> None:
    consistency = ConsistencyReading(True, False, 501, 501, 0, True, True, {"us-east-2": 5})
    report = verify_ledger(
        LedgerReading(10, 100),
        LedgerReading(11, 101),
        consistency,
        [Acknowledged("w0001", "k1", "deposit", None, 201)],
        [],
        [],
    )
    assert LedgerReport.from_data(report.to_data()) == report
    bare = verify_ledger(LedgerReading(10, 100), None, None, [], [], [])
    assert LedgerReport.from_data(bare.to_data()) == bare


def test_ledger_report_from_data_rejects_missing_or_mistyped_fields() -> None:
    data = verify_ledger(LedgerReading(10, 100), None, None, [], [], []).to_data()
    with pytest.raises(ValueError, match="lacks the field band"):
        LedgerReport.from_data({key: value for key, value in data.items() if key != "band"})
    with pytest.raises(ValueError, match="band is not an integer"):
        LedgerReport.from_data({**data, "band": "2"})
    with pytest.raises(ValueError, match="rpo_status is not a string"):
        LedgerReport.from_data({**data, "rpo_status": 1})
    with pytest.raises(ValueError, match="problems is not a list of strings"):
        LedgerReport.from_data({**data, "problems": "abc"})
    with pytest.raises(ValueError, match="notes is not a list of strings"):
        LedgerReport.from_data({**data, "notes": [1]})
    with pytest.raises(ValueError, match="stored_count is not an integer"):
        LedgerReport.from_data({**data, "stored_count": "5"})
    with pytest.raises(ValueError, match="balance_ok is not a boolean"):
        LedgerReport.from_data({**data, "balance_ok": "yes"})
    with pytest.raises(ValueError, match="consistency is not an object"):
        LedgerReport.from_data({**data, "consistency": []})
    assert LedgerReport.from_data({**data, "problems": None, "notes": None}).problems == ()


def test_consistency_reading_from_data_ignores_mistyped_fields() -> None:
    reading = ConsistencyReading.from_data(
        {"consistent": "yes", "truncated": 1, "counts_by_region": {"a": 1, "b": "x", "c": True}}
    )
    assert reading == ConsistencyReading(None, False, None, None, None, None, None, {"a": 1})
    assert ConsistencyReading.from_data({"counts_by_region": []}).counts_by_region == {}
