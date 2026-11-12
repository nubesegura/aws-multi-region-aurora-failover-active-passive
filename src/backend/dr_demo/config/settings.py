"""Runtime settings read from the environment and validated once, at cold start."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dr_demo.config.environment import get_allowed_client_ids

DEFAULT_DB_PORT = 5432
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_CA_BUNDLE = Path(__file__).resolve().parents[2] / "certs" / "global-bundle.pem"
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class SettingsError(Exception):
    """A required setting is missing or invalid. The message never contains a value."""


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    region: str
    allowed_origins: tuple[str, ...]
    log_level: str
    ca_bundle_path: Path
    allowed_client_ids: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        """Build the settings; raise ``SettingsError`` naming every missing or invalid variable."""
        missing = [
            name
            for name in ("DB_HOST", "DB_NAME", "DB_USER", "AWS_REGION", "ALLOWED_ORIGINS")
            if not env.get(name)
        ]
        if missing:
            raise SettingsError("missing environment variables: " + ", ".join(missing))
        origins = _origins(env["ALLOWED_ORIGINS"])
        port = _port(env.get("DB_PORT", str(DEFAULT_DB_PORT)))
        level = env.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
        if level not in _LOG_LEVELS:
            raise SettingsError("LOG_LEVEL must be one of " + ", ".join(sorted(_LOG_LEVELS)))
        bundle = env.get("DB_CA_BUNDLE_PATH")
        ca_bundle_path = Path(bundle) if bundle else DEFAULT_CA_BUNDLE
        if not ca_bundle_path.is_file():
            raise SettingsError(
                "RDS CA bundle not found; run scripts/fetch-rds-ca.sh before sam build "
                "or set DB_CA_BUNDLE_PATH"
            )
        return cls(
            db_host=env["DB_HOST"],
            db_port=port,
            db_name=env["DB_NAME"],
            db_user=env["DB_USER"],
            region=env["AWS_REGION"],
            allowed_origins=origins,
            log_level=level,
            ca_bundle_path=ca_bundle_path,
            allowed_client_ids=get_allowed_client_ids(env),
        )


def _port(raw: str) -> int:
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    raise SettingsError("DB_PORT must be an integer between 1 and 65535")


def _origins(raw: str) -> tuple[str, ...]:
    """Split a comma-separated list; each item must be an ``https://host[:port]`` origin."""
    origins = tuple(item.strip() for item in raw.split(","))
    if not all(_is_https_origin(item) for item in origins):
        raise SettingsError(
            "ALLOWED_ORIGINS must be a comma-separated list of lowercase https:// origins "
            "(scheme, host and optional non-default port only: no path, no trailing slash, "
            "no :443)"
        )
    return origins


def _is_https_origin(value: str) -> bool:
    if not value.isascii() or any(char.isspace() for char in value) or "@" in value:
        return False
    if value != value.lower() or value.endswith(":443"):
        return False
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not (parts.path or parts.query or parts.fragment)
        and value == f"https://{parts.netloc}"
    )
