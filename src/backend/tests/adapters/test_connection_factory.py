"""Criterion 4.4: the password is the IAM token, TLS is verified, nothing secret is logged."""

import logging
import socket
import ssl
from pathlib import Path
from typing import Any

import pytest
from pg8000.exceptions import InterfaceError

from dr_demo.adapters.outbound.postgres_repository import PgConnectionFactory, _open_socket
from dr_demo.domain.errors import DatabaseUnavailable
from tests.adapters.fake_db import FakeConnection

_TOKEN = "iam-token-value-must-not-leak"
_BUNDLE = Path("/var/task/certs/global-bundle.pem")


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _factory(
    connect: Any, contexts: list[Path] | None = None, sock: FakeSocket | None = None
) -> PgConnectionFactory:
    def context(path: Path) -> ssl.SSLContext:
        if contexts is not None:
            contexts.append(path)
        return ssl.create_default_context()

    return PgConnectionFactory(
        host="db.example.internal",
        port=5432,
        database="drdemo",
        user="app_user",
        ca_bundle_path=_BUNDLE,
        token_provider=lambda: _TOKEN,
        connect=connect,
        ssl_context_factory=context,
        open_socket=lambda _host, _port: sock or FakeSocket(),  # type: ignore[arg-type,return-value]
    )


def test_connects_with_token_as_password_verifying_tls_and_a_statement_timeout() -> None:
    received: dict[str, Any] = {}
    connection = FakeConnection()
    paths: list[Path] = []
    sock = FakeSocket()

    def connect(**kwargs: Any) -> FakeConnection:
        received.update(kwargs)
        return connection

    assert _factory(connect, paths, sock)() is connection

    assert received["password"] == _TOKEN
    assert (received["host"], received["port"]) == ("db.example.internal", 5432)
    assert (received["database"], received["user"]) == ("drdemo", "app_user")
    assert received["sock"] is sock
    assert received["startup_params"] == {"statement_timeout": "5000"}
    assert received["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert received["ssl_context"].check_hostname is True
    assert paths == [_BUNDLE]
    assert not sock.closed


def test_tls_context_is_built_once() -> None:
    paths: list[Path] = []
    factory = _factory(lambda **_kwargs: FakeConnection(), paths)

    factory()
    factory()

    assert paths == [_BUNDLE]


@pytest.mark.parametrize(
    "error", [InterfaceError("refused"), OSError("unreachable"), TimeoutError()]
)
def test_connection_errors_are_unavailable_close_the_socket_and_log_no_token(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    def connect(**_kwargs: Any) -> FakeConnection:
        raise error

    sock = FakeSocket()
    logger = logging.getLogger("dr_demo.postgres")
    logger.addHandler(caplog.handler)
    try:
        with (
            caplog.at_level(logging.DEBUG, logger="dr_demo.postgres"),
            pytest.raises(DatabaseUnavailable),
        ):
            _factory(connect, sock=sock)()
    finally:
        logger.removeHandler(caplog.handler)

    assert sock.closed
    assert "database connection failed" in caplog.text
    assert _TOKEN not in caplog.text
    assert "db.example.internal" not in caplog.text


def test_missing_ca_bundle_is_unavailable() -> None:
    factory = PgConnectionFactory(
        host="h",
        port=5432,
        database="d",
        user="u",
        ca_bundle_path=Path("/nonexistent/bundle.pem"),
        token_provider=lambda: _TOKEN,
        connect=lambda **_kwargs: FakeConnection(),
        open_socket=lambda _host, _port: FakeSocket(),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(DatabaseUnavailable):
        factory()


def test_open_socket_connects_with_keepalive_and_a_backstop_timeout() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        sock = _open_socket("127.0.0.1", server.getsockname()[1])

        with sock:
            assert sock.gettimeout() == 8
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
