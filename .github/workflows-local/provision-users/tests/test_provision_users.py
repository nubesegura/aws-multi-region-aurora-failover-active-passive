"""Unit tests for the multi-region Cognito demo user provisioning CLI tool."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

# Ensure the `provision-users` folder is importable during tests.
WORKFLOWS_LOCAL_DIR = Path(__file__).resolve().parents[1]
if str(WORKFLOWS_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(WORKFLOWS_LOCAL_DIR))

import provision_users as pu

VALID_BANK_PW = "BankDemo#2026! Safe1".replace(" ", "")
VALID_DASH_PW = "DashDemo#2026! Safe2".replace(" ", "")
POOL_ID = "us-east-2_DemoPool123"
NL = chr(10)
EMAILS = {
    "bank-operator": "bank-operator@example.internal",
    "dashboard-viewer": "dashboard-viewer@example.internal",
}
SETTINGS = {
    "AWS_PROFILE": "test-profile",
    "AWS_REGION_PRIMARY": "us-east-2",
    "AWS_REGION_SECONDARY": "us-west-2",
    "ENV_TYPE": "prod",
    "COGNITO_REPLICATION_TIMEOUT": "60",
    "DEMO_BANK_EMAIL": EMAILS["bank-operator"],
    "DEMO_DASHBOARD_EMAIL": EMAILS["dashboard-viewer"],
}


def _client_error(
    code: str, message: str = "simulated error", operation: str = "Op"
) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class FakeSsmClient:
    def __init__(self, pool_id: str = POOL_ID, fail_code: str | None = None) -> None:
        self.pool_id = pool_id
        self.fail_code = fail_code
        self.calls: list[str] = []

    def get_parameter(self, *, Name: str) -> dict[str, Any]:
        self.calls.append(Name)
        if self.fail_code is not None:
            raise _client_error(self.fail_code, "parameter error", "GetParameter")
        return {"Parameter": {"Name": Name, "Value": self.pool_id}}


class FakeCognitoPrimaryClient:
    def __init__(
        self,
        existing_users: set[str] | None = None,
        fail_on_create: str | None = None,
        fail_message: str = "aws error",
    ) -> None:
        self.existing_users = set(existing_users or ())
        self.fail_on_create = fail_on_create
        self.fail_message = fail_message
        self.created: list[dict[str, Any]] = []
        self.updated_attrs: list[dict[str, Any]] = []
        self.passwords_set: list[dict[str, Any]] = []

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        username = kwargs["Username"]
        if self.fail_on_create is not None:
            raise _client_error(self.fail_on_create, self.fail_message, "AdminCreateUser")
        if username in self.existing_users:
            raise _client_error("UsernameExistsException", "User already exists", "AdminCreateUser")
        self.created.append(kwargs)
        return {"User": {"Username": username, "UserStatus": "FORCE_CHANGE_PASSWORD"}}

    def admin_update_user_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.updated_attrs.append(kwargs)
        return {}

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]:
        self.passwords_set.append(kwargs)
        return {}


class FakeCognitoReplicaClient:
    """Simulates eventual consistency in us-west-2 across AdminGetUser calls."""

    def __init__(self, schedules: dict[str, list[str | None]]) -> None:
        # Each list entry is None (UserNotFoundException) or a UserStatus string ("CONFIRMED", etc.)
        self.schedules = {k: list(v) for k, v in schedules.items()}
        self.calls: list[tuple[str, str]] = []

    def admin_get_user(self, *, UserPoolId: str, Username: str) -> dict[str, Any]:
        self.calls.append((UserPoolId, Username))
        outcomes = self.schedules.get(Username, ["CONFIRMED"])
        status = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if status is None:
            raise _client_error("UserNotFoundException", "User does not exist", "AdminGetUser")
        if status == "ERROR":
            raise _client_error("InternalErrorException", "Service error", "AdminGetUser")
        return {"Username": Username, "UserStatus": status}


def test_load_env_file_and_resolve_config_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    lines = ["# Comment line"] + [f"{k}={v}" for k, v in SETTINGS.items()]
    lines[1] = 'AWS_PROFILE="custom-admin-profile"'
    env_file.write_text(NL.join(lines) + NL, encoding="utf-8")

    cfg_from_file = pu.resolve_config(env={}, env_file=env_file)
    assert cfg_from_file.aws_profile == "custom-admin-profile"
    assert cfg_from_file.primary_region == "us-east-2"
    assert cfg_from_file.replica_region == "us-west-2"
    assert cfg_from_file.env_type == "prod"
    assert cfg_from_file.replication_timeout_seconds == 60.0
    assert cfg_from_file.emails == EMAILS

    # Explicit environment variables override the .env values.
    cfg_override = pu.resolve_config(
        env={"AWS_PROFILE": "override-profile", "COGNITO_REPLICATION_TIMEOUT": "45"},
        env_file=env_file,
    )
    assert cfg_override.aws_profile == "override-profile"
    assert cfg_override.replication_timeout_seconds == 45.0


def test_resolve_config_has_no_defaults_and_names_missing_settings(tmp_path: Path) -> None:
    with pytest.raises(pu.ProvisioningError) as excinfo:
        pu.resolve_config(env={"AWS_PROFILE": "x"}, env_file=tmp_path / "missing.env")
    message = str(excinfo.value)
    assert "AWS_REGION_PRIMARY" in message
    assert "DEMO_BANK_EMAIL" in message
    assert "AWS_PROFILE" not in message.split("settings:")[1].split(".")[0]


def test_discover_user_pool_ids_from_ssm_in_both_regions() -> None:
    primary_ssm = FakeSsmClient(pool_id=POOL_ID)
    replica_ssm = FakeSsmClient(pool_id=POOL_ID)

    primary_id, replica_id = pu.discover_user_pool_ids(primary_ssm, replica_ssm, env_type="prod")

    assert primary_id == POOL_ID
    assert replica_id == POOL_ID
    assert primary_ssm.calls == ["/drdemo/prod/cognito/UserPoolId"]
    assert replica_ssm.calls == ["/drdemo/prod/cognito/UserPoolId"]


def test_discover_user_pool_ids_raises_provisioning_error_on_ssm_failure() -> None:
    primary_ssm = FakeSsmClient(fail_code="ParameterNotFound")
    replica_ssm = FakeSsmClient(pool_id=POOL_ID)

    with pytest.raises(pu.ProvisioningError, match="UserPoolId"):
        pu.discover_user_pool_ids(primary_ssm, replica_ssm, env_type="prod")


@pytest.mark.parametrize(
    "bad_password",
    [
        "Short#1a",  # < 12 chars
        "alllowercase#1234",  # no uppercase
        "ALLUPPERCASE#1234",  # no lowercase
        "NoNumbersHere#abc",  # no digits
        "NoSymbolsHere1234",  # no symbols
    ],
)
def test_validate_password_policy_rejects_weak_passwords_without_echoing_secret(
    bad_password: str,
) -> None:
    with pytest.raises(pu.PasswordPolicyError) as exc_info:
        pu.validate_password_policy(bad_password, "bank-operator")

    message = str(exc_info.value)
    assert "bank-operator" in message
    assert bad_password not in message


def test_resolve_user_passwords_reads_env_or_prompts_via_getpass() -> None:
    env_passwords = pu.resolve_user_passwords(
        env={
            "DEMO_BANK_PASSWORD": VALID_BANK_PW,
            "DEMO_DASHBOARD_PASSWORD": VALID_DASH_PW,
        }
    )
    assert env_passwords == {
        "bank-operator": VALID_BANK_PW,
        "dashboard-viewer": VALID_DASH_PW,
    }

    prompts: list[str] = []
    answers = iter([VALID_BANK_PW, VALID_DASH_PW])

    def fake_prompter(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    prompted = pu.resolve_user_passwords(env={}, prompter=fake_prompter)
    assert len(prompts) == 2
    assert prompted["bank-operator"] == VALID_BANK_PW
    assert prompted["dashboard-viewer"] == VALID_DASH_PW


def test_cli_rejects_password_arguments() -> None:
    with pytest.raises(pu.ProvisioningError, match="command-line"):
        pu.parse_args(["--password", VALID_BANK_PW])


def test_provision_users_creates_confirmed_users_in_primary_only() -> None:
    primary = FakeCognitoPrimaryClient()
    for spec, pw in zip(pu.DEMO_USERS, (VALID_BANK_PW, VALID_DASH_PW), strict=True):
        outcome = pu.provision_user_in_primary(primary, POOL_ID, spec, pw, EMAILS[spec.username])
        assert outcome == "created"

    assert len(primary.created) == 2
    assert primary.created[0]["Username"] == "bank-operator"
    assert primary.created[0]["MessageAction"] == "SUPPRESS"
    assert {
        "Name": "email",
        "Value": "bank-operator@example.internal",
    } in primary.created[0]["UserAttributes"]
    assert {"Name": "email_verified", "Value": "true"} in primary.created[0]["UserAttributes"]

    assert primary.created[1]["Username"] == "dashboard-viewer"
    assert {
        "Name": "email",
        "Value": "dashboard-viewer@example.internal",
    } in primary.created[1]["UserAttributes"]

    assert len(primary.passwords_set) == 2
    assert primary.passwords_set[0]["Permanent"] is True
    assert primary.passwords_set[1]["Permanent"] is True
    assert primary.updated_attrs == []


def test_provision_user_is_idempotent_when_user_already_exists() -> None:
    primary = FakeCognitoPrimaryClient(existing_users={"bank-operator", "dashboard-viewer"})
    for spec, pw in zip(pu.DEMO_USERS, (VALID_BANK_PW, VALID_DASH_PW), strict=True):
        outcome = pu.provision_user_in_primary(primary, POOL_ID, spec, pw, EMAILS[spec.username])
        assert outcome == "updated"

    assert primary.created == []
    assert len(primary.updated_attrs) == 2
    assert primary.updated_attrs[0]["Username"] == "bank-operator"
    assert {"Name": "email_verified", "Value": "true"} in primary.updated_attrs[0]["UserAttributes"]
    assert len(primary.passwords_set) == 2
    assert primary.passwords_set[0]["Permanent"] is True


def test_verify_replica_users_polls_with_exponential_backoff_until_confirmed() -> None:
    replica = FakeCognitoReplicaClient(
        {
            "bank-operator": [None, "FORCE_CHANGE_PASSWORD", "CONFIRMED"],
            "dashboard-viewer": [None, "CONFIRMED"],
        }
    )
    slept: list[float] = []
    now = [0.0]

    def fake_clock() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    statuses = pu.verify_replica_users(
        replica,
        POOL_ID,
        ("bank-operator", "dashboard-viewer"),
        timeout_seconds=60.0,
        initial_backoff=1.0,
        max_backoff=8.0,
        clock=fake_clock,
        sleeper=fake_sleep,
    )

    assert statuses == {"bank-operator": "CONFIRMED", "dashboard-viewer": "CONFIRMED"}
    assert slept == [1.0, 2.0]


def test_verify_replica_users_raises_timeout_error_when_not_confirmed_in_time() -> None:
    replica = FakeCognitoReplicaClient(
        {
            "bank-operator": ["CONFIRMED"],
            "dashboard-viewer": [None],
        }
    )
    now = [0.0]

    def fake_clock() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(pu.ReplicationTimeoutError, match="dashboard-viewer"):
        pu.verify_replica_users(
            replica,
            POOL_ID,
            ("bank-operator", "dashboard-viewer"),
            timeout_seconds=5.0,
            initial_backoff=2.0,
            max_backoff=4.0,
            clock=fake_clock,
            sleeper=fake_sleep,
        )


def test_main_end_to_end_succeeds_and_never_logs_passwords(tmp_path: Path) -> None:
    prod_env = tmp_path / ".env"
    prod_env.write_text(NL.join(f"{k}={v}" for k, v in SETTINGS.items()), encoding="utf-8")

    primary_ssm = FakeSsmClient(pool_id=POOL_ID)
    replica_ssm = FakeSsmClient(pool_id=POOL_ID)
    primary_cognito = FakeCognitoPrimaryClient(existing_users={"bank-operator"})
    replica_cognito = FakeCognitoReplicaClient(
        {
            "bank-operator": [None, "CONFIRMED"],
            "dashboard-viewer": ["CONFIRMED"],
        }
    )
    out = io.StringIO()
    err = io.StringIO()

    exit_code = pu.main(
        argv=["--env-file", str(prod_env), "--timeout", "15"],
        env={
            "DEMO_BANK_PASSWORD": VALID_BANK_PW,
            "DEMO_DASHBOARD_PASSWORD": VALID_DASH_PW,
        },
        clients=pu.AwsClients(
            primary_ssm=primary_ssm,
            replica_ssm=replica_ssm,
            primary_cognito=primary_cognito,
            replica_cognito=replica_cognito,
        ),
        sleeper=lambda _: None,
        stdout=out,
        stderr=err,
    )

    assert exit_code == 0
    combined_output = out.getvalue() + err.getvalue()
    assert VALID_BANK_PW not in combined_output
    assert VALID_DASH_PW not in combined_output
    lines = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert any(item.get("step") == "replication_verified" for item in lines)


def test_main_returns_nonzero_and_redacts_secrets_on_error(tmp_path: Path) -> None:
    primary_ssm = FakeSsmClient(pool_id=POOL_ID)
    replica_ssm = FakeSsmClient(pool_id=POOL_ID)
    primary_cognito = FakeCognitoPrimaryClient(
        fail_on_create="InvalidPasswordException",
        fail_message=f"Rejected password={VALID_BANK_PW}",
    )
    replica_cognito = FakeCognitoReplicaClient({})
    out = io.StringIO()
    err = io.StringIO()

    exit_code = pu.main(
        argv=["--env-file", str(tmp_path / "missing.env")],
        env={
            **SETTINGS,
            "DEMO_BANK_PASSWORD": VALID_BANK_PW,
            "DEMO_DASHBOARD_PASSWORD": VALID_DASH_PW,
        },
        clients=pu.AwsClients(
            primary_ssm=primary_ssm,
            replica_ssm=replica_ssm,
            primary_cognito=primary_cognito,
            replica_cognito=replica_cognito,
        ),
        stdout=out,
        stderr=err,
    )

    assert exit_code == 1
    combined = out.getvalue() + err.getvalue()
    assert VALID_BANK_PW not in combined
    assert VALID_DASH_PW not in combined
    assert "[REDACTED]" in combined


def test_passwords_are_read_from_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        NL.join(
            [
                f"DEMO_BANK_PASSWORD={VALID_BANK_PW}",
                f"DEMO_DASHBOARD_PASSWORD={VALID_DASH_PW}",
            ]
        ),
        encoding="utf-8",
    )
    settings = pu.load_settings({}, env_file)
    assert pu.resolve_user_passwords(env=settings) == {
        "bank-operator": VALID_BANK_PW,
        "dashboard-viewer": VALID_DASH_PW,
    }


def test_optional_ca_bundle_is_resolved_next_to_the_script_and_must_exist(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("x", encoding="utf-8")
    cfg = pu.resolve_config(
        env={**SETTINGS, "AWS_CA_BUNDLE": str(bundle)}, env_file=tmp_path / "none.env"
    )
    assert cfg.ca_bundle == str(bundle)
    assert pu.resolve_config(env=SETTINGS, env_file=tmp_path / "none.env").ca_bundle is None
    with pytest.raises(pu.ProvisioningError):
        pu.resolve_config(
            env={**SETTINGS, "AWS_CA_BUNDLE": str(tmp_path / "missing.pem")},
            env_file=tmp_path / "none.env",
        )


E2E_PW = "E2eRunner#2026!Safe3"
E2E_SETTINGS = {**SETTINGS, "DEMO_E2E_EMAIL": "e2e-runner@example.internal", "DEMO_E2E_PASSWORD": E2E_PW}


def test_the_e2e_user_is_optional_and_only_selects_that_user() -> None:
    assert [u.username for u in pu.select_users(SETTINGS)] == ["bank-operator", "dashboard-viewer"]
    assert [u.username for u in pu.select_users(E2E_SETTINGS)][-1] == "e2e-runner"
    assert [u.username for u in pu.select_users(E2E_SETTINGS, "e2e-runner")] == ["e2e-runner"]
    with pytest.raises(pu.ProvisioningError):
        pu.select_users(SETTINGS, "e2e-runner")


def test_only_provisions_the_e2e_user_and_leaves_the_others_untouched(tmp_path: Path) -> None:
    primary_cognito = FakeCognitoPrimaryClient()
    replica_cognito = FakeCognitoReplicaClient({"e2e-runner": ["CONFIRMED"]})
    out = io.StringIO()
    err = io.StringIO()
    only_e2e = {key: value for key, value in E2E_SETTINGS.items() if "BANK" not in key and "DASHBOARD" not in key}

    exit_code = pu.main(
        argv=["--env-file", str(tmp_path / "none.env"), "--only", "e2e-runner"],
        env=only_e2e,
        clients=pu.AwsClients(
            primary_ssm=FakeSsmClient(),
            replica_ssm=FakeSsmClient(),
            primary_cognito=primary_cognito,
            replica_cognito=replica_cognito,
        ),
        sleeper=lambda _: None,
        stdout=out,
        stderr=err,
    )

    assert exit_code == 0, err.getvalue()
    assert [item["Username"] for item in primary_cognito.created] == ["e2e-runner"]
    assert E2E_PW not in out.getvalue() + err.getvalue()
