import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

import pytest

from dr_drill import cognito_http, http_safe
from dr_drill.auth import PASSWORD_FLOW, AuthError, AuthResult
from dr_drill.cognito_http import HttpCognitoGateway
from tests.fakes import FakeOpener

PRIMARY = "us-east-2"
PASSWORD = "Fake-Password-123!"
ID_TOKEN = "fake-id-token-value"
REFRESH_TOKEN = "fake-refresh-token-value"


class FakeTransport:
    def __init__(self, status: int, body: Any = None, error: Exception | None = None) -> None:
        self.status = status
        self.body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.error = error
        self.requests: list[tuple[str, Mapping[str, str], bytes]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append((url, headers, body))
        if self.error:
            raise self.error
        return self.status, self.body


def call(gateway: HttpCognitoGateway) -> AuthResult:
    return gateway.initiate_auth(
        PRIMARY, "client", PASSWORD_FLOW, {"USERNAME": "u", "PASSWORD": PASSWORD}
    )


def test_http_gateway_builds_the_request_and_parses_the_answer() -> None:
    transport = FakeTransport(
        200,
        {
            "AuthenticationResult": {
                "IdToken": ID_TOKEN,
                "RefreshToken": REFRESH_TOKEN,
                "ExpiresIn": 900,
            }
        },
    )

    result = call(HttpCognitoGateway(transport))

    url, headers, body = transport.requests[0]
    assert url == "https://cognito-idp.us-east-2.amazonaws.com/"
    assert headers["X-Amz-Target"] == cognito_http.COGNITO_TARGET
    assert json.loads(body)["AuthFlow"] == PASSWORD_FLOW
    assert result == AuthResult(ID_TOKEN, REFRESH_TOKEN, 900)


def test_http_gateway_accepts_an_answer_without_refresh_token() -> None:
    transport = FakeTransport(
        200, {"AuthenticationResult": {"IdToken": ID_TOKEN, "ExpiresIn": 900}}
    )

    assert call(HttpCognitoGateway(transport)).refresh_token is None


@pytest.mark.parametrize(
    "body",
    [
        {"ChallengeName": "NEW_PASSWORD_REQUIRED"},
        b"not json",
        {"AuthenticationResult": {"IdToken": "x"}},
    ],
)
def test_http_gateway_rejects_answers_without_a_result(body: Any) -> None:
    with pytest.raises(AuthError, match="without an authentication result"):
        call(HttpCognitoGateway(FakeTransport(200, body)))


def test_http_gateway_error_names_the_type_and_no_secret() -> None:
    transport = FakeTransport(400, {"__type": "NotAuthorizedException", "message": PASSWORD})

    with pytest.raises(AuthError) as raised:
        call(HttpCognitoGateway(transport))

    assert "NotAuthorizedException" in str(raised.value)
    assert PASSWORD not in str(raised.value)


@pytest.mark.parametrize("body", [b"not json", b"[]", {"__type": "not a type!"}])
def test_http_gateway_unknown_error_body(body: Any) -> None:
    with pytest.raises(AuthError, match="UnknownError"):
        call(HttpCognitoGateway(FakeTransport(500, body)))


def test_http_gateway_transport_failure_hides_the_message() -> None:
    transport = FakeTransport(0, error=OSError(PASSWORD))

    with pytest.raises(AuthError) as raised:
        call(HttpCognitoGateway(transport))

    assert "OSError" in str(raised.value)
    assert PASSWORD not in str(raised.value)


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def test_urllib_transport_posts_and_returns_status_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float) -> FakeResponse:
        seen["url"], seen["data"], seen["timeout"] = request.full_url, request.data, timeout
        return FakeResponse()

    monkeypatch.setattr(http_safe, "_OPENER", FakeOpener(fake_urlopen))

    status, body = cognito_http.urllib_transport("https://example.invalid/", {"A": "b"}, b"x", 3.0)

    assert (status, body) == (200, b"{}")
    assert seen == {"url": "https://example.invalid/", "data": b"x", "timeout": 3.0}


def test_urllib_transport_returns_http_errors_as_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(http_safe, "_OPENER", FakeOpener(fake_urlopen))

    assert cognito_http.urllib_transport("https://example.invalid/", {}, b"", 1.0)[0] == 400


def test_urllib_transport_refuses_plain_http() -> None:
    with pytest.raises(ValueError, match="https"):
        cognito_http.urllib_transport("http://example.invalid/", {}, b"", 1.0)
