"""Configuration validation (spec: thinq-connect-integration — configuration inputs)."""

from __future__ import annotations

import pytest

from airchive.config import (
    DEFAULT_DAY_TIMEZONE,
    DEFAULT_POLL_INTERVAL_SECONDS,
    ConfigError,
    load_config,
    load_dotenv,
    load_firestore_config,
)

SENTINEL_PAT = "SENTINEL-PAT-b6f3d9c1e4a24f7f8c0d1e2f3a4b5c6d"

COMPLETE = {
    "LG_THINQ_PAT": SENTINEL_PAT,
    "LG_COUNTRY_CODE": "PH",
    "LG_CLIENT_ID": "1f0b3a6e-9c7d-4b2a-8e51-0c3d5f7a9b11",
    "LG_DEVICE_ID": "device-abc-123",
    "LG_ENERGY_PROPERTY": "energyConsumption",
    "FIREBASE_PROJECT_ID": "lg-ac-telemetry",
}


def test_complete_configuration_loads_with_defaults():
    config = load_config(dict(COMPLETE))

    assert config.thinq.pat == SENTINEL_PAT
    assert config.thinq.country_code == "PH"
    assert config.firestore.project_id == "lg-ac-telemetry"
    assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
    assert config.day_timezone_name == DEFAULT_DAY_TIMEZONE
    assert config.log_level == "INFO"


def test_missing_pat_fails_startup():
    env = dict(COMPLETE)
    del env["LG_THINQ_PAT"]

    with pytest.raises(ConfigError) as excinfo:
        load_config(env)

    assert any("LG_THINQ_PAT" in problem for problem in excinfo.value.problems)


def test_missing_client_id_fails_and_does_not_generate_one():
    env = dict(COMPLETE)
    del env["LG_CLIENT_ID"]

    with pytest.raises(ConfigError) as excinfo:
        load_config(env)

    problem = next(p for p in excinfo.value.problems if "LG_CLIENT_ID" in p)
    assert "persist" in problem.lower()
    # The collector must not silently substitute an ephemeral identity.
    assert "LG_CLIENT_ID" not in env


def test_malformed_interval_fails_startup():
    with pytest.raises(ConfigError) as excinfo:
        load_config({**COMPLETE, "POLL_INTERVAL_SECONDS": "five minutes"})

    assert any("POLL_INTERVAL_SECONDS" in p for p in excinfo.value.problems)


@pytest.mark.parametrize("value", ["0", "-300"])
def test_non_positive_interval_fails_startup(value):
    with pytest.raises(ConfigError):
        load_config({**COMPLETE, "POLL_INTERVAL_SECONDS": value})


def test_every_offending_value_is_named_at_once():
    with pytest.raises(ConfigError) as excinfo:
        load_config({"LG_COUNTRY_CODE": "ph", "POLL_INTERVAL_SECONDS": "abc"})

    text = "\n".join(excinfo.value.problems)
    for name in (
        "LG_THINQ_PAT",
        "LG_CLIENT_ID",
        "LG_DEVICE_ID",
        "LG_ENERGY_PROPERTY",
        "FIREBASE_PROJECT_ID",
        "LG_COUNTRY_CODE",
        "POLL_INTERVAL_SECONDS",
    ):
        assert name in text


def test_validation_failure_never_echoes_a_secret():
    with pytest.raises(ConfigError) as excinfo:
        load_config({**COMPLETE, "LG_THINQ_PAT": "  ", "POLL_INTERVAL_SECONDS": "abc"})

    rendered = str(excinfo.value)
    assert SENTINEL_PAT not in rendered

    # A secret-valued variable is redacted even when it is the offending value.
    with pytest.raises(ConfigError) as excinfo:
        load_config({**COMPLETE, "LG_DAY_TIMEZONE": "Mars/Olympus"})
    assert SENTINEL_PAT not in str(excinfo.value)


def test_config_repr_redacts_the_token():
    config = load_config(dict(COMPLETE))
    assert SENTINEL_PAT not in repr(config)
    assert SENTINEL_PAT not in repr(config.thinq)
    assert "<redacted>" in repr(config.thinq)


def test_invalid_timezone_fails_startup():
    with pytest.raises(ConfigError) as excinfo:
        load_config({**COMPLETE, "LG_DAY_TIMEZONE": "Not/AZone"})

    assert any("LG_DAY_TIMEZONE" in p for p in excinfo.value.problems)


def test_invalid_log_level_fails_startup():
    with pytest.raises(ConfigError) as excinfo:
        load_config({**COMPLETE, "LOG_LEVEL": "CHATTY"})

    assert any("LOG_LEVEL" in p for p in excinfo.value.problems)


def test_firestore_config_does_not_require_thinq_credentials():
    config = load_firestore_config({"FIREBASE_PROJECT_ID": "lg-ac-telemetry"})
    assert config.project_id == "lg-ac-telemetry"


def test_firestore_config_requires_a_project():
    with pytest.raises(ConfigError):
        load_firestore_config({})


def test_dotenv_does_not_override_the_real_environment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nLG_COUNTRY_CODE=KR\nFIREBASE_PROJECT_ID='quoted-project'\n\n",
        encoding="utf-8",
    )
    environ = {"LG_COUNTRY_CODE": "PH"}

    load_dotenv(env_file, environ)

    assert environ["LG_COUNTRY_CODE"] == "PH"
    assert environ["FIREBASE_PROJECT_ID"] == "quoted-project"
