"""Settings of the runner: the git-ignored `.env` file merged under the process environment."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ENV_FILE_NAME = ".env"
REQUIRED_KEYS = (
    "E2E_USERNAME",
    "E2E_PASSWORD",
    "API_DOMAIN_NAME",
    "GITHUB_REPOSITORY",
    "AWS_PROFILE",
    "AWS_REGION_PRIMARY",
    "AWS_REGION_SECONDARY",
    "AWS_REGION_GLOBAL",
    "ENV_TYPE",
)
OPTIONAL_KEYS = ("AWS_CA_BUNDLE", "PROBE_RATE_SECONDS", "PHASE_STEADY_SECONDS")
_KNOWN_KEYS = frozenset((*REQUIRED_KEYS, *OPTIONAL_KEYS))
DEFAULT_PROBE_RATE_SECONDS = 1.0
DEFAULT_PHASE_STEADY_SECONDS = 60


class SettingsError(Exception):
    """The settings are missing or invalid. Messages name keys, never values."""


@dataclass(frozen=True)
class Settings:
    """Validated settings; credentials are hidden from `repr` and exposed via `secret_values`."""

    username: str = field(repr=False)
    password: str = field(repr=False)
    api_domain_name: str
    github_repository: str
    aws_profile: str
    region_primary: str
    region_secondary: str
    region_global: str
    env_type: str
    ca_bundle: Path | None = None
    probe_rate_seconds: float = DEFAULT_PROBE_RATE_SECONDS
    phase_steady_seconds: int = DEFAULT_PHASE_STEADY_SECONDS

    def secret_values(self) -> tuple[str, ...]:
        """Values the output guard must never let through."""
        return (self.username, self.password)


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines: no shell expansion, comments ignored, quotes stripped."""
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise SettingsError(f"the settings file has an invalid line (line {number})")
        values[key.strip()] = _strip_quotes(value.strip())
    return values


def _positive_number(key: str, text: str, kind: type[float] | type[int]) -> float:
    try:
        number = kind(text)
    except ValueError:
        raise SettingsError(f"{key} must be a positive number") from None
    if not math.isfinite(number) or number <= 0:
        raise SettingsError(f"{key} must be a positive number")
    return number


def _resolve_ca_bundle(runner_dir: Path, text: str) -> Path:
    path = Path(text)
    resolved = path if path.is_absolute() else (runner_dir / path).resolve()
    if not resolved.is_file():
        raise SettingsError("AWS_CA_BUNDLE does not point to an existing file")
    return resolved


def load_settings(
    runner_dir: Path,
    environ: Mapping[str, str],
    env_file: Path | None = None,
) -> Settings:
    """Read the settings file (default: `.env` in the runner folder) under the environment."""
    source = env_file if env_file is not None else runner_dir / ENV_FILE_NAME
    merged = parse_env_file(source.read_text(encoding="utf-8")) if source.is_file() else {}
    merged.update(
        {key: value for key, value in environ.items() if key in _KNOWN_KEYS and value.strip()}
    )
    missing = [key for key in REQUIRED_KEYS if not merged.get(key, "").strip()]
    if missing:
        raise SettingsError("missing required settings: " + ", ".join(missing))
    rate = _positive_number(
        "PROBE_RATE_SECONDS",
        merged.get("PROBE_RATE_SECONDS") or str(DEFAULT_PROBE_RATE_SECONDS),
        float,
    )
    steady = _positive_number(
        "PHASE_STEADY_SECONDS",
        merged.get("PHASE_STEADY_SECONDS") or str(DEFAULT_PHASE_STEADY_SECONDS),
        int,
    )
    bundle_text = merged.get("AWS_CA_BUNDLE", "").strip()
    return Settings(
        username=merged["E2E_USERNAME"],
        password=merged["E2E_PASSWORD"],
        api_domain_name=merged["API_DOMAIN_NAME"],
        github_repository=merged["GITHUB_REPOSITORY"],
        aws_profile=merged["AWS_PROFILE"],
        region_primary=merged["AWS_REGION_PRIMARY"],
        region_secondary=merged["AWS_REGION_SECONDARY"],
        region_global=merged["AWS_REGION_GLOBAL"],
        env_type=merged["ENV_TYPE"],
        ca_bundle=_resolve_ca_bundle(runner_dir, bundle_text) if bundle_text else None,
        probe_rate_seconds=rate,
        phase_steady_seconds=int(steady),
    )
