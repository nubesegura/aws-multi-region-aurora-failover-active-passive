"""Structured logs (no bodies, keys or tokens) and the thin Lambda handlers."""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from dr_demo.config import container
from dr_demo.config.container import Container, build_container
from dr_demo.config.logging import JsonFormatter
from dr_demo.config.settings import Settings, SettingsError
from services.create_transaction import handler as create_handler
from services.get_account import handler as account_handler
from services.get_consistency import handler as consistency_handler
from services.get_health import handler as health_handler
from services.get_stats import handler as stats_handler
from services.list_transactions import handler as list_handler
from tests.entrypoints.conftest import REGION, FakeContext, Wiring, get_event, post_event

ORIGINS = "https://bank.example.com,https://dashboard.example.com"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, Any]] = []
        self.setFormatter(JsonFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def captured(wiring: Wiring) -> Capture:
    handler = Capture()
    wiring.logger.addHandler(handler)
    return handler


def test_one_access_log_line_per_movement_with_id_and_type_and_nothing_sensitive(
    wiring: Wiring, context: FakeContext, captured: Capture
) -> None:
    wiring.controller.create_transaction(
        post_event({"type": "deposit", "amount": 4321.09}, {"x-api-key": "the-api-key"}), context
    )
    event = post_event(
        {"type": "withdrawal", "amount": 1234.56},
        {
            "x-api-key": "the-api-key",
            "Authorization": "Bearer token-value",
            "Idempotency-Key": "k9",
        },
    )

    wiring.controller.create_transaction(event, context)

    assert len(captured.lines) == 2
    line = captured.lines[1]
    assert line["request_id"] == "req-1"
    assert line["region"] == REGION
    assert line["route"] == "POST /transactions"
    assert line["status"] == 201
    assert line["movement_type"] == "withdrawal"
    assert line["movement_id"] == str(wiring.repository.stored[1].id)
    assert line["latency_ms"] >= 0
    dumped = json.dumps(captured.lines)
    for forbidden in ("the-api-key", "token-value", "1234.56", "4321.09", "k9", "balance"):
        assert forbidden not in dumped


def test_movement_fields_are_absent_when_nothing_was_stored(
    wiring: Wiring, context: FakeContext, captured: Capture
) -> None:
    wiring.controller.create_transaction(post_event({"type": "gift", "amount": 1}), context)

    assert "movement_id" not in captured.lines[0]
    assert captured.lines[0]["status"] == 400
    assert captured.lines[0]["error_code"] == "validation_error"


def test_error_code_is_logged_and_request_id_falls_back_to_the_context(
    wiring: Wiring, context: FakeContext, captured: Capture
) -> None:
    wiring.repository.unavailable = True
    event = get_event("/stats")
    del event["requestContext"]["requestId"]

    wiring.controller.get_stats(event, context)

    assert captured.lines[0]["status"] == 503
    assert captured.lines[0]["error_code"] == "database_unavailable"
    assert captured.lines[0]["request_id"] == "ctx-request-id"


def test_unexpected_error_is_logged_with_type_only_in_the_response_and_returns_500(
    wiring: Wiring,
    context: FakeContext,
    captured: Capture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(wiring.repository, "database_role", explode)

    result = wiring.controller.get_stats(get_event("/stats"), context)

    assert result["statusCode"] == 500
    assert [line["message"] for line in captured.lines] == ["unhandled error", "request completed"]
    assert captured.lines[0]["exception_type"] == "RuntimeError"
    assert captured.lines[1]["status"] == 500


@pytest.fixture
def installed(wiring: Wiring, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Container(settings=None, controller=wiring.controller)  # type: ignore[arg-type]
    for module in (
        create_handler,
        list_handler,
        stats_handler,
        health_handler,
        account_handler,
        consistency_handler,
    ):
        monkeypatch.setattr(module, "get_container", lambda: fake)


@pytest.mark.usefixtures("installed")
def test_each_handler_serves_its_route(context: FakeContext) -> None:
    created = create_handler.handler(post_event({"type": "deposit", "amount": 1}), context)
    responses = [
        created,
        list_handler.handler(get_event("/transactions"), context),
        stats_handler.handler(get_event("/stats"), context),
        health_handler.handler(get_event("/health"), context),
        account_handler.handler(get_event("/account"), context),
        consistency_handler.handler(get_event("/consistency"), context),
    ]

    assert [r["statusCode"] for r in responses] == [201, 200, 200, 200, 200, 200]
    assert json.loads(responses[2]["body"])["total_count"] == 1
    assert json.loads(responses[4]["body"])["balance"] == "1.00"
    assert json.loads(responses[5]["body"])["consistent"] is True


@pytest.fixture
def hermetic_aws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep boto3 away from the developer's real profile files and credentials."""
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "aws-config"
    credentials = tmp_path / "aws-credentials"
    config.write_text("")
    credentials.write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")


@pytest.mark.usefixtures("hermetic_aws")
def test_container_wires_the_graph_without_touching_the_network(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("placeholder")
    settings = Settings.from_env(
        {
            "DB_HOST": "db.example.internal",
            "DB_NAME": "drdemo",
            "DB_USER": "app_user",
            "AWS_REGION": "us-east-2",
            "ALLOWED_ORIGINS": ORIGINS,
            "DB_CA_BUNDLE_PATH": str(bundle),
        }
    )

    built = build_container(settings)

    assert built.settings is settings
    assert built.controller is not None


@pytest.mark.usefixtures("hermetic_aws")
def test_get_container_validates_settings_and_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("placeholder")
    for name in ("DB_HOST", "DB_NAME", "DB_USER", "ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    container.get_container.cache_clear()

    with pytest.raises(SettingsError):
        container.get_container()

    monkeypatch.setenv("DB_HOST", "db.example.internal")
    monkeypatch.setenv("DB_NAME", "drdemo")
    monkeypatch.setenv("DB_USER", "app_user")
    monkeypatch.setenv("ALLOWED_ORIGINS", ORIGINS)
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("DB_CA_BUNDLE_PATH", str(bundle))
    try:
        assert container.get_container() is container.get_container()
    finally:
        container.get_container.cache_clear()
