"""The documentation has to keep matching the code.

Closed sets documented in prose rot silently. These assertions fail the build
instead — an operator reading about a status that no longer exists, or missing
one that does, is worse than no table at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from airchive.observation.model import IntervalStatus, QualityFlag
from airchive.thinq.failures import FailureClass

REPO = Path(__file__).resolve().parents[1]
OPERATIONS = REPO / "docs" / "operations.md"
SETUP = REPO / "docs" / "setup.md"
ENV_EXAMPLE = REPO / ".env.example"


@pytest.fixture(scope="module")
def operations() -> str:
    return OPERATIONS.read_text(encoding="utf-8")


def _documented_terms(text: str) -> set[str]:
    """Every SCREAMING_CASE term rendered in a markdown table cell."""
    return set(re.findall(r"\|\s*`([A-Z][A-Z_]+)`\s*\|", text))


def test_every_interval_status_is_documented(operations):
    documented = _documented_terms(operations)

    for status in IntervalStatus:
        assert str(status) in documented, f"{status} is implemented but undocumented"


def test_every_quality_flag_is_documented(operations):
    documented = _documented_terms(operations)

    for flag in QualityFlag:
        assert str(flag) in documented, f"{flag} is implemented but undocumented"


def test_every_failure_class_is_documented(operations):
    documented = _documented_terms(operations)

    for failure_class in FailureClass:
        assert str(failure_class) in documented, (
            f"{failure_class} is implemented but undocumented"
        )


def test_no_documented_status_or_flag_has_been_removed_from_the_code(operations):
    """The reverse direction: prose must not promise a vocabulary that is gone."""
    known = (
        {str(s) for s in IntervalStatus}
        | {str(f) for f in QualityFlag}
        | {str(c) for c in FailureClass}
    )
    # Terms that appear in the quality/failure tables specifically.
    section = operations.split("## The quality model", 1)[1].split("## Idempotency", 1)[0]
    documented = _documented_terms(section)

    unknown = documented - known
    assert not unknown, f"documented but not implemented: {sorted(unknown)}"


def test_every_environment_variable_is_documented(operations):
    declared = set(re.findall(r"^([A-Z_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    declared |= {"GOOGLE_APPLICATION_CREDENTIALS"}  # commented out on purpose

    for name in declared:
        assert f"`{name}`" in operations, f"{name} is configurable but undocumented"


def test_the_documentation_covers_what_the_operations_spec_requires(operations):
    for topic in (
        "## Configuration",
        "## Running it",
        "## Deployment",
        "## The stored data model",
        "## Interval and delta semantics",
        "## Day rollover and reconciliation",
        "## The quality model",
        "## Idempotency",
        "## Inspection commands",
        "## Collector health",
        "## Rate limiting",
        "## Credential hygiene",
        "## Known device and API limitations",
        "## Storage growth",
    ):
        assert topic in operations, f"missing section: {topic}"

    setup = SETUP.read_text(encoding="utf-8")
    for topic in ("Firebase project", "Firestore", "Application Default Credentials", "PAT"):
        assert topic in setup, f"setup guide does not cover: {topic}"


def test_the_permanence_of_the_database_region_is_stated_before_it_is_chosen():
    setup = SETUP.read_text(encoding="utf-8")
    choice = setup.index("Choose a location")
    warning = setup.index("The database location is permanent")

    # The warning has to land with the choice, not in a footnote afterwards.
    assert warning > choice
    assert warning - choice < 400
    assert warning < setup.index("## 2.")


def test_storage_growth_and_archival_are_documented(operations):
    growth = operations.split("## Storage growth", 1)[1]

    assert "a year" in growth
    assert "never deleted" in growth or "never be deleted" in growth
    assert "BigQuery" in growth or "GCS" in growth


def test_mqtt_is_recorded_as_a_future_enhancement_only(operations):
    future = operations.split("## Future enhancements", 1)[1]
    assert "MQTT" in future
    assert "not" in future.split("MQTT", 1)[1][:400].lower()


def test_the_implementation_does_not_depend_on_mqtt():
    """MQTT is a future enhancement, so nothing may import or call it."""
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lowered = stripped.lower()
            if "mqtt" in lowered or "push_subscribe" in lowered or "event_subscribe" in lowered:
                offenders.append(f"{path.relative_to(REPO)}: {stripped}")

    assert not offenders, f"MQTT is referenced by the implementation: {offenders}"


def test_the_collector_never_issues_a_control_command():
    """Read-only means read-only: no control call may exist in the source."""
    offenders = []
    for path in (REPO / "src").rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "async_post_device_control" in stripped or "post_device_control" in stripped:
                offenders.append(f"{path.relative_to(REPO)}: {stripped}")

    assert not offenders, f"a control command appears in the source: {offenders}"


def test_the_env_template_carries_no_real_secret():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", text, re.M))

    assert values, "the template declares no variables"
    for name, value in values.items():
        assert value.startswith("replace-with-") or value in {
            "PH",
            "lg-ac-telemetry",
            "300",
            "Asia/Manila",
            "INFO",
        }, f"{name} looks like it holds a real value: {value!r}"
