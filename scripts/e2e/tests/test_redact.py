import json
import threading
import uuid
from datetime import UTC, datetime

import pytest

from dr_drill.probe import Observation, round_ms
from dr_drill.redact import OutputGuard, RefusedOutput

FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.c2lnbmF0dXJlLWZha2U"
GUARD = OutputGuard(secrets=["Fake-Password-123!"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("login with Fake-Password-123! failed", "known secret value"),
        (f"Authorization: Bearer {FAKE_JWT}", "JWT"),
        ("token aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbb.cccccccccccccccccccc", "JWT"),
        ("key 0123456789abcdef0123456789abcdef", "hexadecimal"),
        ("key " + "Ab9_" * 12, "opaque"),
        ("account 123456789012 caller", "12-digit"),
        ("role arn:aws:iam::role/fake", "ARN"),
        ("owner someone@example.com", "e-mail"),
        ("secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "opaque"),
        ("session IQoJb3JpZ2luX2VjEXAMPLE+abc/def+ghi/jklmnopqrstuvwxyz0123456789==", "opaque"),
        ("key AKIAABCDEFGHIJKLMNOP", "opaque"),
        ("temporary ASIAABCDEFGHIJKLMNOP", "opaque"),
        ("account 1234-5678-9012 caller", "12-digit"),
        ("account 1234 5678 9012 caller", "12-digit"),
        ("account 123456789012.", "12-digit"),
        ('{"account": "123456789012"}', "12-digit"),
        ("uuid 3f2b8c1e-9d4a-4c7e-8b6a-1a2b3c4d5e6f account 123456789012", "12-digit"),
        ('"latency_ms": 523.123456789012', "12-digit"),
        ("value 123456789012.5 ms", "12-digit"),
        ("1.123456789012", "12-digit"),
        ("1.2.123456789012", "12-digit"),
        ("v2.123456789012.1", "12-digit"),
        ("10.0.0.1.123456789012", "12-digit"),
        ("1.5 123456789012.0", "12-digit"),
        ("x 123456789012.5.6", "12-digit"),
        ("12345678-1234-1234-1234-123456789012", "12-digit"),
        ("123456789012-1234-1234-1234-123456789012", "12-digit"),
        ("123456789012-3f2b8c1e-9d4a-4c7e-8b6a-1a2b3c4d5e6f", "12-digit"),
        ("a3f2b8c1e-9d4a-4c7e-8b6a-123456789012", "12-digit"),
        ("3f2b8c1e-9d4a-3c7e-8b6a-123456789012", "12-digit"),
        ("3f2b8c1e-9d4a-4c7e-cb6a-123456789012", "12-digit"),
        ("role ARN:AWS:iam::role/fake", "ARN"),
        ("role Arn:aws-cn:iam::role/fake", "ARN"),
    ],
)
def test_unsafe_text_is_refused(text: str, expected: str) -> None:
    reason = GUARD.reason(text)

    assert reason is not None
    assert expected in reason


@pytest.mark.parametrize(
    "text",
    [
        "us-east-2",
        "deposit 0.01 balance 1234.56",
        "idempotency 3f2b8c1e-9d4a-4c7e-8b6a-1a2b3c4d5e6f",
        "failover to-secondary took 312.5 s",
        "cognito-idp.us-west-2.amazonaws.com",
        "stk-useast1-drdemo-edge-prod UPDATE_COMPLETE",
        "1234567890123 epoch ms",
        "request 3f2b8c1e-9d4a-4c7e-8b6a-123456789012 ok",
        "key 3F2B8C1E-9D4A-4C7E-8B6A-123456789012",
        "reports/e2e/2026-11-11T10-00-00Z/run.log",
        "written to reports/e2e/2026-11-11T10-00-00Z/timeline.json ok",
        "path=reports/e2e/2026-11-11T10-00-00Z/report.md",
        "fnc-uswest2-drdemo-create-transaction-prod",
        "stk-useast2-drdemo-api-prod",
        "",
    ],
)
def test_ordinary_text_passes(text: str) -> None:
    assert GUARD.reason(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "token mfrggzdfmztwq2lknnwg23tpobyxe43uovwgy3bamjrw",
        "token " + "a1b2c3d4e5f6g7h8i9j0" * 3,
        "token AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGhIjKlMnOp",
        "path/reports/" + "k" * 41 + "/run.log",
    ],
)
def test_a_long_lowercase_word_is_refused_as_opaque(text: str) -> None:
    reason = GUARD.reason(text)

    assert reason is not None
    assert "opaque" in reason


@pytest.mark.parametrize(
    "text",
    [
        "name " + "-".join(["x" * 32, "z" * 32]),
        "stk-useast2-drdemo-" + "x" * 32 + "-prod-extra",
        "q" * 32 + "-" + "9" * 30,
    ],
)
def test_words_up_to_32_characters_still_pass(text: str) -> None:
    assert GUARD.reason(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "token mfrggzdfmztwq2lknnwg23tpob-yxe43uovwgy3bamjrw-mfrggzdfm",
        "token AbCdEfGhIjKlMnOpQrSt-AbCdEfGhIjKlMnOpQrSt",
        "token Ab9_" + "Ab9_" * 3 + " " + "Ab9_" * 3,
    ],
)
def test_accepted_residual_the_heuristic_is_a_second_line_only(text: str) -> None:
    """Documents the limit: unregistered hyphen-split or letters-only tokens are not caught."""
    assert GUARD.reason(text) is None


def test_the_same_residual_is_refused_once_the_value_is_registered() -> None:
    secret = "mfrggzdfmztwq2lknnwg23tpob-yxe43uovwgy3bamjrw-mfrggzdfm"
    guard = OutputGuard()
    assert guard.reason(f"value {secret}") is None

    guard.register(secret)

    assert guard.reason(f"value {secret}") is not None


def test_concurrent_registrations_are_all_kept() -> None:
    guard = OutputGuard()
    values = [f"tok{number}x" for number in range(200)]

    def register_all(chunk: list[str]) -> None:
        for value in chunk:
            guard.register(value)

    threads = [threading.Thread(target=register_all, args=(values[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(guard.reason(value) is not None for value in values)


def test_empty_known_secret_does_not_refuse_everything() -> None:
    assert OutputGuard(secrets=[""]).reason("anything") is None


def test_ensure_names_field_and_never_the_value() -> None:
    with pytest.raises(RefusedOutput) as raised:
        GUARD.ensure("result", "pwd Fake-Password-123!")

    assert "result" in str(raised.value)
    assert "Fake-Password-123!" not in str(raised.value)
    assert raised.value.field == "result"


def test_ensure_accepts_safe_text() -> None:
    GUARD.ensure("result", "ok")


def test_a_token_registered_at_run_time_is_refused_verbatim() -> None:
    guard = OutputGuard()
    assert guard.reason("short-token") is None

    guard.register("short-token")
    guard.register("short-token")
    guard.register("")

    assert guard.reason("value short-token end") is not None
    assert guard.reason("ordinary text") is None


@pytest.mark.parametrize(
    "value",
    [523.123456789012, 52.123456789012, 1e-12, 123456.7891234567, 1.5e9, 9.87654321012e7, 0.0],
)
def test_rounded_milliseconds_never_carry_a_twelve_digit_run(value: float) -> None:
    payload = json.dumps({"milliseconds": round_ms(value), "latency_ms": round_ms(value)})

    assert GUARD.reason(payload) is None


def test_observation_latency_is_rounded_at_the_source() -> None:
    moment = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    observation = Observation(
        "write", "w", 1, moment, moment, True, 200, 523.123456789012, key=str(uuid.uuid4())
    )

    assert observation.to_data()["latency_ms"] == 523.123
    assert GUARD.reason(json.dumps(observation.to_data())) is None


def test_generated_idempotency_keys_are_never_refused() -> None:
    assert all(GUARD.reason(f"key {uuid.uuid4()}") is None for _ in range(2000))
