"""Reading ThinQ payloads (spec: thinq-connect-integration — discovery, energy property)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from airchive.config import ConfigError
from airchive.thinq.payloads import (
    extract_energy_reading,
    parse_device_list,
    supported_energy_properties,
)
from airchive.thinq.validation import check_energy_property_supported

DEVICE_LIST = [
    {
        "deviceId": "ac-device-1",
        "deviceInfo": {
            "deviceType": "DEVICE_AIR_CONDITIONER",
            "modelName": "PC18SQ.NSD",
            "alias": "Bedroom AC",
            "reportable": True,
        },
    },
    {
        "deviceId": "wash-device-2",
        "deviceInfo": {
            "deviceType": "DEVICE_WASHER",
            "modelName": "FV1450S3B",
            "alias": "Washer",
        },
    },
]


def test_device_list_identifies_air_conditioner_candidates():
    devices = parse_device_list(DEVICE_LIST)

    assert [d.device_id for d in devices] == ["ac-device-1", "wash-device-2"]
    ac = devices[0]
    assert ac.is_air_conditioner
    assert ac.alias == "Bedroom AC"
    assert ac.model_name == "PC18SQ.NSD"
    assert ac.device_type == "DEVICE_AIR_CONDITIONER"
    assert not devices[1].is_air_conditioner


def test_device_list_of_an_unexpected_shape_yields_nothing_rather_than_guessing():
    assert parse_device_list(None) == []
    assert parse_device_list({"response": []}) == []
    assert parse_device_list([{"noDeviceId": 1}]) == []


def test_supported_energy_properties_comes_from_the_profile():
    profile = {"result": {"property": ["energyConsumption", "powerConsumption"]}}
    assert supported_energy_properties(profile) == ["energyConsumption", "powerConsumption"]


def test_supported_energy_properties_of_a_device_without_energy():
    assert supported_energy_properties(None) == []
    assert supported_energy_properties({}) == []
    assert supported_energy_properties({"result": {}}) == []


def test_unsupported_energy_property_fails_startup_naming_what_is_supported():
    with pytest.raises(ConfigError) as excinfo:
        check_energy_property_supported(
            "energyConsumption", ["powerConsumption", "instantPower"]
        )

    message = str(excinfo.value)
    assert "energyConsumption" in message
    assert "instantPower" in message
    assert "powerConsumption" in message


def test_supported_energy_property_passes_startup():
    check_energy_property_supported("energyConsumption", ["energyConsumption"])


def test_a_device_with_no_energy_properties_fails_startup_explicitly():
    with pytest.raises(ConfigError) as excinfo:
        check_energy_property_supported("energyConsumption", [])

    assert "no properties" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"unit": "kWh", "energyData": [{"date": "20260820", "value": "8.751"}]},
        {"result": {"unit": "kWh", "energyData": [{"date": "2026-08-20", "value": "8.751"}]}},
        [{"date": "20260820", "value": 8.751, "unit": "kWh"}],
        {"unit": "kWh", "value": "8.751", "date": "20260820"},
    ],
)
def test_energy_reading_is_extracted_from_plausible_shapes(payload):
    reading = extract_energy_reading(payload, "energyConsumption", day_label="2026-08-20")

    assert reading is not None
    assert reading.value == Decimal("8.751")
    assert reading.unit == "kWh"
    assert reading.decimal_places == 3


def test_the_requested_day_is_picked_out_of_a_multi_day_response():
    payload = {
        "unit": "kWh",
        "energyData": [
            {"date": "20260819", "value": "8.751"},
            {"date": "20260820", "value": "0.021"},
        ],
    }

    reading = extract_energy_reading(payload, "energyConsumption", day_label="2026-08-19")
    assert reading is not None and reading.value == Decimal("8.751")

    reading = extract_energy_reading(payload, "energyConsumption", day_label="2026-08-20")
    assert reading is not None and reading.value == Decimal("0.021")


def test_the_property_name_itself_can_carry_the_value():
    payload = {"result": {"energyConsumption": "1.250", "unit": "kWh"}}

    reading = extract_energy_reading(payload, "energyConsumption")
    assert reading is not None
    assert reading.value == Decimal("1.250")


def test_no_numeric_reading_returns_none_rather_than_zero():
    assert extract_energy_reading(None, "energyConsumption") is None
    assert extract_energy_reading({}, "energyConsumption") is None
    assert extract_energy_reading({"message": "no data"}, "energyConsumption") is None
    assert extract_energy_reading({"value": "n/a"}, "energyConsumption") is None
    assert extract_energy_reading({"energyData": []}, "energyConsumption") is None


def test_values_never_route_through_binary_float():
    reading = extract_energy_reading({"value": "8.751"}, "energyConsumption")
    assert reading is not None
    # Decimal("8.751") - Decimal("8.732") is exact; the float path is not.
    assert reading.value - Decimal("8.732") == Decimal("0.019")


def test_a_missing_unit_is_reported_as_missing_not_assumed():
    reading = extract_energy_reading({"value": "1.5"}, "energyConsumption")
    assert reading is not None
    assert reading.unit is None
