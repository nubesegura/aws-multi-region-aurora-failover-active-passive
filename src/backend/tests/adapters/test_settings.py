from pathlib import Path

import pytest

from dr_demo.config.settings import DEFAULT_CA_BUNDLE, Settings, SettingsError

_BUNDLE_ENV = "DB_CA_BUNDLE_PATH"
ENV = {
    "DB_HOST": "db.example.internal",
    "DB_NAME": "drdemo",
    "DB_USER": "app_user",
    "AWS_REGION": "us-east-2",
    "ALLOWED_ORIGINS": "https://bank.example.com,https://dashboard.example.com",
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("placeholder")
    return {**ENV, _BUNDLE_ENV: str(bundle)}


def test_defaults(env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default = tmp_path / "global-bundle.pem"
    default.write_text("placeholder")
    monkeypatch.setattr("dr_demo.config.settings.DEFAULT_CA_BUNDLE", default)
    del env[_BUNDLE_ENV]
    settings = Settings.from_env(env)

    assert settings.db_port == 5432
    assert settings.log_level == "INFO"
    assert settings.ca_bundle_path == default
    assert DEFAULT_CA_BUNDLE.parts[-2:] == ("certs", "global-bundle.pem")
    assert settings.region == "us-east-2"


def test_overrides(env: dict[str, str]) -> None:
    settings = Settings.from_env({**env, "DB_PORT": "6543", "LOG_LEVEL": "debug"})

    assert (settings.db_port, settings.log_level) == (6543, "DEBUG")
    assert settings.ca_bundle_path == Path(env[_BUNDLE_ENV])


def test_missing_ca_bundle_fails_fast_with_a_clear_message(env: dict[str, str]) -> None:
    env[_BUNDLE_ENV] = env[_BUNDLE_ENV] + ".missing"

    with pytest.raises(SettingsError, match=r"fetch-rds-ca\.sh"):
        Settings.from_env(env)


def test_missing_variables_are_all_named() -> None:
    with pytest.raises(SettingsError) as caught:
        Settings.from_env({"DB_HOST": "h", "DB_NAME": ""})

    message = str(caught.value)
    for name in ("DB_NAME", "DB_USER", "AWS_REGION", "ALLOWED_ORIGINS"):
        assert name in message
    assert "DB_HOST" not in message


@pytest.mark.parametrize("port", ["0", "70000", "abc", "-1", ""])
def test_invalid_port(env: dict[str, str], port: str) -> None:
    with pytest.raises(SettingsError, match="DB_PORT"):
        Settings.from_env({**env, "DB_PORT": port})


def test_invalid_log_level(env: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match="LOG_LEVEL"):
        Settings.from_env({**env, "LOG_LEVEL": "LOUD"})


def test_allowed_origins_are_split_and_trimmed(env: dict[str, str]) -> None:
    settings = Settings.from_env(
        {
            **env,
            "ALLOWED_ORIGINS": " https://bank.example.com , https://dashboard.example.com:8443 ",
        }
    )

    assert settings.allowed_origins == (
        "https://bank.example.com",
        "https://dashboard.example.com:8443",
    )


@pytest.mark.parametrize(
    "origins",
    [
        "*",
        "http://bank.example.com",
        "bank.example.com",
        "https://",
        "https://bank.example.com/",
        "https://bank.example.com/path",
        "https://bank.example.com?x=1",
        "https://bank.example.com#top",
        "https://user@bank.example.com",
        "https://Bank.example.com",
        "https://bank.EXAMPLE.com",
        "https://bank.example.com:443",
        "https://bank.example.com:99999",
        "https://bank.example.com:abc",
        "https://bank.example.com,",
        "https://bank.example.com,,https://dashboard.example.com",
        "https://bank.example.com,http://dashboard.example.com",
        "https://bank.example.com https://dashboard.example.com",
        "https://ba nk.example.com",
        ",",
        " ",
    ],
)
def test_malformed_allowed_origins_fail_at_cold_start_without_echoing_the_value(
    env: dict[str, str], origins: str
) -> None:
    with pytest.raises(SettingsError, match="ALLOWED_ORIGINS") as caught:
        Settings.from_env({**env, "ALLOWED_ORIGINS": origins})

    assert "example.com" not in str(caught.value)


def test_the_old_single_origin_variable_is_not_accepted(env: dict[str, str]) -> None:
    del env["ALLOWED_ORIGINS"]

    with pytest.raises(SettingsError, match="ALLOWED_ORIGINS"):
        Settings.from_env({**env, "ALLOWED_ORIGIN": "https://dashboard.example.com"})
