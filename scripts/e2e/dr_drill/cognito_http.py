"""Cognito `InitiateAuth` over HTTPS (adapter), as `src/bank/app.js` does."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from dr_drill.auth import AuthError, AuthResult
from dr_drill.http_safe import open_url

COGNITO_TARGET = "AWSCognitoIdentityProviderService.InitiateAuth"
COGNITO_CONTENT_TYPE = "application/x-amz-json-1.1"
REQUEST_TIMEOUT_SECONDS = 4.0
HTTP_OK = 200
_ERROR_TYPE = re.compile(r"[A-Za-z]{1,64}")


Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]


def urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    """POST over HTTPS with the standard library; returns the status and the body."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
    try:
        with open_url(request, timeout) as response:
            return int(response.status), bytes(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _error_name(body: bytes) -> str:
    try:
        raw = json.loads(body).get("__type", "")
    except (ValueError, AttributeError):
        return "UnknownError"
    name = str(raw).rsplit("#", 1)[-1]
    return name if _ERROR_TYPE.fullmatch(name) else "UnknownError"


def _parse_result(body: bytes) -> AuthResult:
    try:
        result = json.loads(body)["AuthenticationResult"]
        return AuthResult(
            id_token=str(result["IdToken"]),
            refresh_token=str(result["RefreshToken"]) if result.get("RefreshToken") else None,
            expires_in=int(result["ExpiresIn"]),
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        raise AuthError("Cognito answered without an authentication result") from None


class HttpCognitoGateway:
    """Cognito `InitiateAuth` over HTTPS, as `src/bank/app.js` does."""

    def __init__(self, transport: Transport = urllib_transport) -> None:
        self._transport = transport

    def initiate_auth(
        self,
        region: str,
        client_id: str,
        auth_flow: str,
        parameters: Mapping[str, str],
    ) -> AuthResult:
        """Call InitiateAuth in the region; raises AuthError naming the Cognito error type."""
        url = f"https://cognito-idp.{region}.amazonaws.com/"
        headers = {"Content-Type": COGNITO_CONTENT_TYPE, "X-Amz-Target": COGNITO_TARGET}
        payload = {"AuthFlow": auth_flow, "ClientId": client_id, "AuthParameters": dict(parameters)}
        try:
            status, body = self._transport(
                url, headers, json.dumps(payload).encode("utf-8"), REQUEST_TIMEOUT_SECONDS
            )
        except (OSError, ValueError) as error:
            raise AuthError(f"InitiateAuth in {region} failed: {type(error).__name__}") from None
        if status != HTTP_OK:
            raise AuthError(f"InitiateAuth in {region} failed: {_error_name(body)} (HTTP {status})")
        return _parse_result(body)
