"""Configuration loading and startup validation.

Every required value is validated before any network call or write. Failure
names every offending variable at once, and never echoes the value of a secret.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Variables whose values must never appear in an error message or log record.
SECRET_VARS = frozenset({"LG_THINQ_PAT", "GOOGLE_APPLICATION_CREDENTIALS"})

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_DAY_TIMEZONE = "Asia/Manila"
DEFAULT_LOG_LEVEL = "INFO"


class ConfigError(Exception):
    """Raised when configuration is absent, empty, or structurally invalid."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        joined = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Invalid configuration:\n{joined}")


@dataclass(frozen=True)
class ThinqConfig:
    pat: str
    country_code: str
    client_id: str
    device_id: str
    energy_property: str

    def __repr__(self) -> str:  # never render the PAT
        return (
            f"ThinqConfig(country_code={self.country_code!r}, "
            f"client_id={self.client_id!r}, device_id={self.device_id!r}, "
            f"energy_property={self.energy_property!r}, pat=<redacted>)"
        )


@dataclass(frozen=True)
class FirestoreConfig:
    project_id: str


@dataclass(frozen=True)
class CollectorConfig:
    thinq: ThinqConfig
    firestore: FirestoreConfig
    poll_interval_seconds: int
    day_timezone: ZoneInfo
    day_timezone_name: str
    log_level: str


def load_dotenv(path: str | Path = ".env", environ: dict[str, str] | None = None) -> None:
    """Load `KEY=value` lines from a local .env file without overriding real env vars.

    Deliberately tiny: the deployed runtime supplies configuration through the
    environment and Secret Manager, so a dotenv dependency would only ever serve
    local development.
    """
    env = os.environ if environ is None else environ
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in env:
            env[key] = value


def _get(environ: dict[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def _require(environ: dict[str, str], name: str, problems: list[str], hint: str = "") -> str:
    value = _get(environ, name)
    if not value:
        suffix = f" {hint}" if hint else ""
        problems.append(f"{name} is required but missing or empty.{suffix}")
    return value


def _describe(name: str, value: str) -> str:
    """Render an offending value, or a redaction marker for secrets."""
    return "<redacted>" if name in SECRET_VARS else repr(value)


def load_firestore_config(environ: dict[str, str] | None = None) -> FirestoreConfig:
    """Validate only what Firestore access needs.

    `check-firestore` runs before ThinQ credentials exist, so it must not demand
    them.
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []
    project_id = _require(
        env, "FIREBASE_PROJECT_ID", problems, "Set it to the Firebase project ID."
    )
    if problems:
        raise ConfigError(problems)
    return FirestoreConfig(project_id=project_id)


def load_discovery_config(
    environ: dict[str, str] | None = None,
) -> tuple[ThinqConfig, ZoneInfo, str]:
    """Validate only what read-only discovery needs.

    `discover` is what *produces* `LG_DEVICE_ID` and `LG_ENERGY_PROPERTY`, so it
    cannot require them.
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []

    pat = _require(env, "LG_THINQ_PAT", problems, "Obtain it from the LG ThinQ developer portal.")
    country_code = _require(env, "LG_COUNTRY_CODE", problems)
    client_id = _require(
        env,
        "LG_CLIENT_ID",
        problems,
        "Generate one once and persist it; the collector never generates an ephemeral identity.",
    )
    if country_code and not _COUNTRY_RE.match(country_code):
        problems.append(
            f"LG_COUNTRY_CODE must be a two-letter uppercase ISO 3166-1 code, got "
            f"{_describe('LG_COUNTRY_CODE', country_code)}."
        )

    tz_name = _get(env, "LG_DAY_TIMEZONE") or DEFAULT_DAY_TIMEZONE
    tz: ZoneInfo | None = None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(
            f"LG_DAY_TIMEZONE must be a valid IANA timezone name, got "
            f"{_describe('LG_DAY_TIMEZONE', tz_name)}."
        )

    if problems:
        raise ConfigError(problems)

    assert tz is not None
    return (
        ThinqConfig(
            pat=pat,
            country_code=country_code,
            client_id=client_id,
            device_id=_get(env, "LG_DEVICE_ID"),
            energy_property=_get(env, "LG_ENERGY_PROPERTY"),
        ),
        tz,
        tz_name,
    )


def load_config(environ: dict[str, str] | None = None) -> CollectorConfig:
    """Validate the full collector configuration, reporting every problem at once."""
    env = os.environ if environ is None else environ
    problems: list[str] = []

    pat = _require(env, "LG_THINQ_PAT", problems, "Obtain it from the LG ThinQ developer portal.")
    country_code = _require(env, "LG_COUNTRY_CODE", problems)
    client_id = _require(
        env,
        "LG_CLIENT_ID",
        problems,
        "Generate one once (e.g. `python -c \"import uuid; print(uuid.uuid4())\"`) "
        "and persist it; the collector never generates an ephemeral identity.",
    )
    device_id = _require(env, "LG_DEVICE_ID", problems, "Take it from `airchive discover`.")
    energy_property = _require(
        env,
        "LG_ENERGY_PROPERTY",
        problems,
        "Take it from the energy profile via `airchive discover`.",
    )
    project_id = _require(env, "FIREBASE_PROJECT_ID", problems)

    if country_code and not _COUNTRY_RE.match(country_code):
        problems.append(
            f"LG_COUNTRY_CODE must be a two-letter uppercase ISO 3166-1 code, got "
            f"{_describe('LG_COUNTRY_CODE', country_code)}."
        )

    raw_interval = _get(env, "POLL_INTERVAL_SECONDS")
    interval = DEFAULT_POLL_INTERVAL_SECONDS
    if raw_interval:
        try:
            interval = int(raw_interval)
        except ValueError:
            problems.append(
                f"POLL_INTERVAL_SECONDS must be a positive integer number of seconds, got "
                f"{_describe('POLL_INTERVAL_SECONDS', raw_interval)}."
            )
        else:
            if interval <= 0:
                problems.append(
                    f"POLL_INTERVAL_SECONDS must be a positive integer number of seconds, got "
                    f"{_describe('POLL_INTERVAL_SECONDS', raw_interval)}."
                )

    tz_name = _get(env, "LG_DAY_TIMEZONE") or DEFAULT_DAY_TIMEZONE
    tz: ZoneInfo | None = None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(
            f"LG_DAY_TIMEZONE must be a valid IANA timezone name, got "
            f"{_describe('LG_DAY_TIMEZONE', tz_name)}."
        )

    log_level = (_get(env, "LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper()
    if log_level not in _LOG_LEVELS:
        problems.append(
            f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}, got "
            f"{_describe('LOG_LEVEL', log_level)}."
        )

    if problems:
        raise ConfigError(problems)

    assert tz is not None
    return CollectorConfig(
        thinq=ThinqConfig(
            pat=pat,
            country_code=country_code,
            client_id=client_id,
            device_id=device_id,
            energy_property=energy_property,
        ),
        firestore=FirestoreConfig(project_id=project_id),
        poll_interval_seconds=interval,
        day_timezone=tz,
        day_timezone_name=tz_name,
        log_level=log_level,
    )
