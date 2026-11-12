#!/usr/bin/env python3
"""Provision confirmed demo users in the primary Cognito User Pool and verify MRR replication."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# Deployment-specific values (profile, regions, environment tier, timeout, e-mail addresses) have no
# defaults here: they come from the git-ignored ".env" next to this script (see ".env.example").
ENV_FILE_NAME = ".env"
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 8.0

CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
MAX_RETRY_ATTEMPTS = 3
MIN_PASSWORD_LENGTH = 12

REQUIRED_SETTINGS = (
    "AWS_PROFILE",
    "AWS_REGION_PRIMARY",
    "AWS_REGION_SECONDARY",
    "ENV_TYPE",
    "COGNITO_REPLICATION_TIMEOUT",
    "DEMO_BANK_EMAIL",
    "DEMO_DASHBOARD_EMAIL",
)
BANK_EMAIL_ENV = "DEMO_BANK_EMAIL"
DASHBOARD_EMAIL_ENV = "DEMO_DASHBOARD_EMAIL"
BANK_PASSWORD_ENV = "DEMO_BANK_PASSWORD"  # noqa: S105 - env var name, not a password
DASHBOARD_PASSWORD_ENV = "DEMO_DASHBOARD_PASSWORD"  # noqa: S105 - env var name, not a password
E2E_EMAIL_ENV = "DEMO_E2E_EMAIL"
E2E_PASSWORD_ENV = "DEMO_E2E_PASSWORD"  # noqa: S105 - env var name, not a password
SSM_USER_POOL_ID_TEMPLATE = "/drdemo/{env_type}/cognito/UserPoolId"
REDACTED = "[REDACTED]"

_THIRD_PARTY_LOGGERS = ("botocore", "boto3", "urllib3")
_SENSITIVE_WORDS = "password|passwd|pwd|token|secret|bearer|authorization"
_SECRET_PATTERN = re.compile(
    rf"((?:{_SENSITIVE_WORDS})\w*[\"']?\s*[:=]\s*(?:basic\s+|bearer\s+)?)[^\s,;\"'}}]+"
    r"|(bearer\s+)\S+"
    r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    re.IGNORECASE,
)


class ProvisioningError(Exception):
    """Base error for user provisioning failures; never contains plain-text credentials."""


class PasswordPolicyError(ProvisioningError):
    """Raised when a supplied demo password does not satisfy the 12-character policy."""


class ReplicationTimeoutError(ProvisioningError):
    """Raised when the replica User Pool does not confirm all users within the timeout."""


@dataclass(frozen=True)
class DemoUserSpec:
    username: str
    email_env: str
    password_env: str
    label: str


@dataclass(frozen=True)
class ProvisioningConfig:
    aws_profile: str
    primary_region: str
    replica_region: str
    env_type: str
    replication_timeout_seconds: float
    emails: Mapping[str, str]
    ca_bundle: str | None = None


@dataclass(frozen=True)
class AwsClients:
    primary_ssm: Any
    replica_ssm: Any
    primary_cognito: Any
    replica_cognito: Any


DEMO_USERS: tuple[DemoUserSpec, ...] = (
    DemoUserSpec(
        username="bank-operator",
        email_env=BANK_EMAIL_ENV,
        password_env=BANK_PASSWORD_ENV,
        label="Banking Operator",
    ),
    DemoUserSpec(
        username="dashboard-viewer",
        email_env=DASHBOARD_EMAIL_ENV,
        password_env=DASHBOARD_PASSWORD_ENV,
        label="Monitoring Dashboard Viewer",
    ),
)


E2E_USER = DemoUserSpec(
    username="e2e-runner",
    email_env=E2E_EMAIL_ENV,
    password_env=E2E_PASSWORD_ENV,
    label="End-to-end Test Runner",
)


def select_users(settings: Mapping[str, str], only: str | None = None) -> tuple[DemoUserSpec, ...]:
    """Users to provision: the two demo users, plus the optional e2e user when DEMO_E2E_EMAIL is set.

    With ``only`` the selection is limited to that one username (so the others are not touched).
    """
    users = DEMO_USERS
    if settings.get(E2E_EMAIL_ENV, "").strip():
        users = (*users, E2E_USER)
    if only is None:
        return users
    chosen = tuple(spec for spec in users if spec.username == only)
    if not chosen:
        known = ", ".join(spec.username for spec in users)
        raise ProvisioningError(f"Unknown user '{only}' for --only (available: {known}).")
    return chosen


def configure_runtime_logging(level: str = "INFO") -> logging.Logger:
    """Cap third-party AWS SDK loggers at WARNING and configure the CLI logger."""
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logger = logging.getLogger("dr_demo.provision_users")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    return logger


def redact_sensitive(text: str, secrets: Sequence[str] = ()) -> str:
    """Mask known secret values, credential key-value pairs, bearer tokens, and AWS keys."""
    sanitized = text
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, REDACTED)

    def _mask(match: re.Match[str]) -> str:
        prefix = match.group(1) or match.group(2)
        return f"{prefix}{REDACTED}" if prefix else REDACTED

    return _SECRET_PATTERN.sub(_mask, sanitized)


def load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE pairs from a local env file without using eval or shell expansion."""
    if not path.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw_val = line.partition("=")
        name = key.strip()
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if name:
            loaded[name] = val
    return loaded


def load_settings(env: Mapping[str, str] | None, env_file: Path | None) -> dict[str, str]:
    """Merge the local .env file (default: next to this script) with the process environment."""
    default_env_path = Path(__file__).resolve().parent / ENV_FILE_NAME
    file_values = load_env_file(env_file if env_file is not None else default_env_path)
    runtime_env = os.environ if env is None else env
    return {**file_values, **runtime_env}


def resolve_config(
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
    timeout_seconds: float | None = None,
    only: str | None = None,
) -> ProvisioningConfig:
    """Resolve profile, regions, tier, timeout and user e-mails from the .env file and environment."""
    merged = load_settings(env, env_file)
    users = select_users(merged, only)

    required = [
        name
        for name in REQUIRED_SETTINGS
        if name not in (BANK_EMAIL_ENV, DASHBOARD_EMAIL_ENV)
    ]
    required.extend(spec.email_env for spec in users)
    if timeout_seconds is not None:
        required.remove("COGNITO_REPLICATION_TIMEOUT")
    missing = [name for name in required if not merged.get(name, "").strip()]
    if missing:
        raise ProvisioningError(
            "Missing required settings: "
            + ", ".join(missing)
            + f". Copy .env.example to {ENV_FILE_NAME} and fill them in."
        )

    if timeout_seconds is not None:
        resolved_timeout = float(timeout_seconds)
    else:
        try:
            resolved_timeout = float(merged["COGNITO_REPLICATION_TIMEOUT"])
        except ValueError as exc:
            raise ProvisioningError(
                "COGNITO_REPLICATION_TIMEOUT must be a positive number."
            ) from exc
    if resolved_timeout <= 0:
        raise ProvisioningError("Replication timeout must be greater than zero.")

    ca_bundle: str | None = None
    raw_bundle = merged.get("AWS_CA_BUNDLE", "").strip()
    if raw_bundle:
        bundle_path = Path(raw_bundle)
        if not bundle_path.is_absolute():
            bundle_path = Path(__file__).resolve().parent / bundle_path
        if not bundle_path.is_file():
            raise ProvisioningError(f"AWS_CA_BUNDLE points to a missing file: {bundle_path}")
        ca_bundle = str(bundle_path)

    return ProvisioningConfig(
        aws_profile=merged["AWS_PROFILE"].strip(),
        primary_region=merged["AWS_REGION_PRIMARY"].strip(),
        replica_region=merged["AWS_REGION_SECONDARY"].strip(),
        env_type=merged["ENV_TYPE"].strip(),
        replication_timeout_seconds=resolved_timeout,
        emails={spec.username: merged[spec.email_env].strip() for spec in users},
        ca_bundle=ca_bundle,
    )


def validate_password_policy(password: str, username: str = "user") -> None:
    """Validate a password against the 12-character complexity policy without echoing it."""
    missing: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        missing.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if not any(ch.isupper() for ch in password):
        missing.append("at least one uppercase letter")
    if not any(ch.islower() for ch in password):
        missing.append("at least one lowercase letter")
    if not any(ch.isdigit() for ch in password):
        missing.append("at least one number")
    if not any(not ch.isalnum() and not ch.isspace() for ch in password):
        missing.append("at least one symbol")
    if missing:
        raise PasswordPolicyError(
            f"Password for '{username}' violates policy: requires " + ", ".join(missing) + "."
        )


def resolve_user_passwords(
    env: Mapping[str, str] | None = None,
    prompter: Callable[[str], str] | None = None,
    users: Sequence[DemoUserSpec] = DEMO_USERS,
) -> dict[str, str]:
    """Read passwords from environment variables or hidden prompts and validate policy."""
    source_env = os.environ if env is None else env
    prompt_fn = getpass.getpass if prompter is None else prompter
    passwords: dict[str, str] = {}
    for spec in users:
        raw = source_env.get(spec.password_env, "")
        password = raw if raw else prompt_fn(f"Password for {spec.username} ({spec.label}): ")
        validate_password_policy(password, spec.username)
        passwords[spec.username] = password
    return passwords


def build_aws_clients(config: ProvisioningConfig) -> AwsClients:
    """Create regional SSM and Cognito IDP clients with bounded timeouts and retries."""
    configure_runtime_logging()
    boto_config = Config(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        retries={"max_attempts": MAX_RETRY_ATTEMPTS, "mode": "standard"},
    )
    try:
        session = boto3.Session(profile_name=config.aws_profile)
        verify = config.ca_bundle if config.ca_bundle else True

        def make(service: str, region: str) -> Any:
            return session.client(service, region_name=region, config=boto_config, verify=verify)

        return AwsClients(
            primary_ssm=make("ssm", config.primary_region),
            replica_ssm=make("ssm", config.replica_region),
            primary_cognito=make("cognito-idp", config.primary_region),
            replica_cognito=make("cognito-idp", config.replica_region),
        )
    except (BotoCoreError, ClientError) as exc:
        raise ProvisioningError(
            f"Unable to initialize AWS session for profile '{config.aws_profile}'."
        ) from exc


def discover_user_pool_ids(
    primary_ssm: Any, replica_ssm: Any, env_type: str
) -> tuple[str, str]:
    """Read the Cognito UserPoolId from SSM in both primary and replica regions."""
    param_name = SSM_USER_POOL_ID_TEMPLATE.format(env_type=env_type)
    try:
        primary_res = primary_ssm.get_parameter(Name=param_name)
        replica_res = replica_ssm.get_parameter(Name=param_name)
    except (BotoCoreError, ClientError) as exc:
        raise ProvisioningError(
            f"Failed to read Cognito UserPoolId from SSM parameter '{param_name}'."
        ) from exc

    primary_id = str(primary_res.get("Parameter", {}).get("Value", "")).strip()
    replica_id = str(replica_res.get("Parameter", {}).get("Value", "")).strip()
    if not primary_id or not replica_id:
        raise ProvisioningError(f"SSM parameter '{param_name}' returned an empty UserPoolId.")
    return primary_id, replica_id


def provision_user_in_primary(
    cognito_primary: Any,
    user_pool_id: str,
    spec: DemoUserSpec,
    password: str,
    email: str,
) -> str:
    """Create or idempotently update a confirmed user in the primary User Pool."""
    attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    action = "created"
    try:
        try:
            cognito_primary.admin_create_user(
                UserPoolId=user_pool_id,
                Username=spec.username,
                MessageAction="SUPPRESS",
                UserAttributes=attributes,
            )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code != "UsernameExistsException":
                raise
            action = "updated"
            cognito_primary.admin_update_user_attributes(
                UserPoolId=user_pool_id,
                Username=spec.username,
                UserAttributes=attributes,
            )

        cognito_primary.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=spec.username,
            Password=password,
            Permanent=True,
        )
    except (BotoCoreError, ClientError) as exc:
        safe_detail = redact_sensitive(str(exc), secrets=(password,))
        raise ProvisioningError(
            f"Failed to provision user '{spec.username}' in primary pool '{user_pool_id}': "
            f"{safe_detail}"
        ) from exc
    return action


def verify_replica_users(
    cognito_replica: Any,
    replica_pool_id: str,
    usernames: Sequence[str],
    *,
    timeout_seconds: float,
    initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Poll the replica pool with exponential backoff until all users are CONFIRMED."""
    started = clock()
    delay = max(initial_backoff, 0.1)
    statuses: dict[str, str] = dict.fromkeys(usernames, "MISSING")

    while True:
        for username in usernames:
            if statuses.get(username) == "CONFIRMED":
                continue
            try:
                response = cognito_replica.admin_get_user(
                    UserPoolId=replica_pool_id,
                    Username=username,
                )
                statuses[username] = str(response.get("UserStatus", "UNKNOWN"))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "UserNotFoundException":
                    statuses[username] = "NOT_FOUND"
                else:
                    raise ProvisioningError(
                        f"Error polling replica pool '{replica_pool_id}' for '{username}': {code}"
                    ) from exc
            except BotoCoreError as exc:
                raise ProvisioningError(
                    f"Transport error polling replica pool '{replica_pool_id}' for '{username}'."
                ) from exc

        if all(status == "CONFIRMED" for status in statuses.values()):
            return statuses

        elapsed = clock() - started
        if elapsed >= timeout_seconds:
            pending = ", ".join(
                f"{user}={state}" for user, state in statuses.items() if state != "CONFIRMED"
            )
            raise ReplicationTimeoutError(
                f"Replica pool '{replica_pool_id}' did not confirm all users within "
                f"{timeout_seconds:.1f}s ({pending})."
            )

        sleep_for = min(delay, max(0.0, timeout_seconds - elapsed))
        sleeper(sleep_for)
        delay = min(delay * 2.0, max_backoff)


def _emit(
    stream: TextIO,
    step: str,
    status: str,
    secrets: Sequence[str] = (),
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "step": step,
        "status": status,
        **fields,
    }
    raw_json = json.dumps(payload, separators=(",", ":"))
    stream.write(redact_sensitive(raw_json, secrets=secrets) + "\n")
    stream.flush()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI options; reject any attempt to supply passwords on the command line."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    for token in raw_args:
        lowered = token.lower()
        if lowered in ("-p", "--password") or "password" in lowered:
            raise ProvisioningError(
                "Passwords must not be passed as command-line arguments; "
                f"use {BANK_PASSWORD_ENV}/{DASHBOARD_PASSWORD_ENV} or interactive prompt."
            )

    parser = argparse.ArgumentParser(
        description=(
            "Provision confirmed demo users in the primary Cognito User Pool and "
            "verify Multi-Region Replication to the standby region."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to the local env file (defaults to the .env next to this script).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Maximum seconds to poll replica User Pool for CONFIRMED status (default: COGNITO_REPLICATION_TIMEOUT).",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="USERNAME",
        help="Provision only this user (for example e2e-runner); the other users are left untouched.",
    )
    return parser.parse_args(raw_args)


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    clients: AwsClients | None = None,
    prompter: Callable[[str], str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the provisioning workflow and return 0 on success or 1 on failure."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    known_secrets: list[str] = []

    try:
        args = parse_args(argv)
        settings = load_settings(env, args.env_file)
        users = select_users(settings, args.only)
        config = resolve_config(
            env=env, env_file=args.env_file, timeout_seconds=args.timeout, only=args.only
        )
        passwords = resolve_user_passwords(env=settings, prompter=prompter, users=users)
        known_secrets.extend(passwords.values())

        _emit(
            out,
            "config_loaded",
            "ok",
            secrets=known_secrets,
            aws_profile=config.aws_profile,
            primary_region=config.primary_region,
            replica_region=config.replica_region,
            env_type=config.env_type,
        )

        active_clients = build_aws_clients(config) if clients is None else clients
        primary_pool_id, replica_pool_id = discover_user_pool_ids(
            active_clients.primary_ssm,
            active_clients.replica_ssm,
            env_type=config.env_type,
        )
        _emit(
            out,
            "pools_discovered",
            "ok",
            secrets=known_secrets,
            primary_pool_id=primary_pool_id,
            replica_pool_id=replica_pool_id,
        )

        for spec in users:
            outcome = provision_user_in_primary(
                active_clients.primary_cognito,
                primary_pool_id,
                spec,
                passwords[spec.username],
                config.emails[spec.username],
            )
            _emit(
                out,
                "user_provisioned",
                outcome,
                secrets=known_secrets,
                username=spec.username,
                email=config.emails[spec.username],
                region=config.primary_region,
            )

        replica_statuses = verify_replica_users(
            active_clients.replica_cognito,
            replica_pool_id,
            tuple(spec.username for spec in users),
            timeout_seconds=config.replication_timeout_seconds,
            clock=clock,
            sleeper=sleeper,
        )
        _emit(
            out,
            "replication_verified",
            "ok",
            secrets=known_secrets,
            replica_region=config.replica_region,
            replica_pool_id=replica_pool_id,
            users=replica_statuses,
        )
        return 0
    except ProvisioningError as exc:
        _emit(
            err,
            "provisioning_failed",
            "error",
            secrets=known_secrets,
            error_type=type(exc).__name__,
            message=redact_sensitive(str(exc), secrets=known_secrets),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
