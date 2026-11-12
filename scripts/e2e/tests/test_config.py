from pathlib import Path

import pytest

from dr_drill.config import SettingsError, load_settings, parse_env_file

PASSWORD = "Fake-Password-123!"
BASE = {
    "E2E_USERNAME": "runner@example.invalid",
    "E2E_PASSWORD": PASSWORD,
    "API_DOMAIN_NAME": "api.example.invalid",
    "GITHUB_REPOSITORY": "example/repo",
    "AWS_PROFILE": "profile",
    "AWS_REGION_PRIMARY": "us-east-2",
    "AWS_REGION_SECONDARY": "us-west-2",
    "AWS_REGION_GLOBAL": "us-east-1",
    "ENV_TYPE": "prod",
}


def write_env(folder: Path, values: dict[str, str]) -> None:
    text = "\n".join(f"{key}={value}" for key, value in values.items())
    (folder / ".env").write_bytes(text.encode())


def test_loads_file_with_defaults(tmp_path: Path) -> None:
    write_env(tmp_path, BASE)

    settings = load_settings(tmp_path, {})

    assert settings.region_primary == "us-east-2"
    assert settings.probe_rate_seconds == 1.0
    assert settings.phase_steady_seconds == 60
    assert settings.ca_bundle is None
    assert settings.secret_values() == ("runner@example.invalid", PASSWORD)


def test_process_environment_wins_over_file(tmp_path: Path) -> None:
    write_env(tmp_path, BASE)

    settings = load_settings(tmp_path, {"AWS_PROFILE": "other", "UNRELATED": "x"})

    assert settings.aws_profile == "other"


def test_environment_alone_is_enough(tmp_path: Path) -> None:
    assert load_settings(tmp_path, BASE).env_type == "prod"


def test_missing_keys_are_all_named_without_values(tmp_path: Path) -> None:
    write_env(tmp_path, {"E2E_PASSWORD": PASSWORD, "AWS_PROFILE": " "})

    with pytest.raises(SettingsError) as raised:
        load_settings(tmp_path, {})

    message = str(raised.value)
    assert "E2E_USERNAME" in message
    assert "AWS_PROFILE" in message
    assert "E2E_PASSWORD" not in message
    assert PASSWORD not in message


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PROBE_RATE_SECONDS", "fast"),
        ("PROBE_RATE_SECONDS", "0"),
        ("PROBE_RATE_SECONDS", "nan"),
        ("PROBE_RATE_SECONDS", "inf"),
        ("PHASE_STEADY_SECONDS", "1.5"),
        ("PHASE_STEADY_SECONDS", "-3"),
    ],
)
def test_bad_numbers_are_rejected(tmp_path: Path, key: str, value: str) -> None:
    with pytest.raises(SettingsError, match=key):
        load_settings(tmp_path, {**BASE, key: value})


def test_numbers_are_parsed(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path, {**BASE, "PROBE_RATE_SECONDS": "0.5", "PHASE_STEADY_SECONDS": "90"}
    )

    assert settings.probe_rate_seconds == 0.5
    assert settings.phase_steady_seconds == 90


def test_relative_ca_bundle_is_resolved_next_to_the_runner(tmp_path: Path) -> None:
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "ca.pem").write_bytes(b"pem")

    settings = load_settings(tmp_path, {**BASE, "AWS_CA_BUNDLE": "certs/ca.pem"})

    assert settings.ca_bundle == (tmp_path / "certs" / "ca.pem").resolve()


def test_absolute_ca_bundle_is_kept(tmp_path: Path) -> None:
    pem = tmp_path / "ca.pem"
    pem.write_bytes(b"pem")

    assert load_settings(tmp_path, {**BASE, "AWS_CA_BUNDLE": str(pem)}).ca_bundle == pem


def test_missing_ca_bundle_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="AWS_CA_BUNDLE"):
        load_settings(tmp_path, {**BASE, "AWS_CA_BUNDLE": "nowhere.pem"})


def test_repr_and_errors_hide_the_credentials(tmp_path: Path) -> None:
    settings = load_settings(tmp_path, BASE)

    assert PASSWORD not in repr(settings)
    assert "runner@example.invalid" not in repr(settings)
    with pytest.raises(SettingsError) as raised:
        load_settings(tmp_path, {**BASE, "PROBE_RATE_SECONDS": PASSWORD})
    assert PASSWORD not in str(raised.value)


def test_parse_env_file_handles_quotes_comments_and_export() -> None:
    text = "# comment\n\nA=1\nB='two words'\nC=\"x=y\"\nexport D=4\nE=$HOME\nF=\n"

    assert parse_env_file(text) == {
        "A": "1",
        "B": "two words",
        "C": "x=y",
        "D": "4",
        "E": "$HOME",
        "F": "",
    }


def test_parse_env_file_rejects_a_line_without_equals() -> None:
    with pytest.raises(SettingsError, match="line 2"):
        parse_env_file("A=1\nnot a setting\n")


def test_an_empty_process_variable_does_not_override_the_file(tmp_path: Path) -> None:
    write_env(tmp_path, {**BASE, "PROBE_RATE_SECONDS": "2"})

    settings = load_settings(tmp_path, {"PROBE_RATE_SECONDS": "", "AWS_PROFILE": "  "})

    assert settings.probe_rate_seconds == 2.0
    assert settings.aws_profile == "profile"
