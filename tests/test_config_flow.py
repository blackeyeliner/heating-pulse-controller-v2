from dataclasses import asdict

import pytest

from custom_components.heating_pulse_controller_v2.config_flow import (
    PulseConfigFlow,
    settings_schema,
    validate_entities,
)
from custom_components.heating_pulse_controller_v2.engine import Settings


def test_selected_entities_are_validated_without_actuation(hass, config, devices):
    assert validate_entities(hass, config) is None
    assert devices == []
    assert validate_entities(hass, {**config, "closing_value": 10}) == "inverted_limits"
    assert (
        validate_entities(hass, {**config, "radiator_sensor": "sensor.room"}) == "distinct_entities"
    )
    hass.states.async_set("sensor.radiator", "unavailable")
    assert validate_entities(hass, config) == "entity_unavailable"


def test_schema_defaults_are_valid():
    values = settings_schema({})({})
    assert values["reference_seconds"] == 10800
    assert values["reporting_verified"] is False
    Settings(**{k: values[k] for k in asdict(Settings())})


async def test_bad_tuning_does_not_create_entry(hass):
    flow = PulseConfigFlow()
    flow.hass = hass
    flow._data = {"name": "Test"}
    result = await flow.async_step_tuning({**asdict(Settings()), "cooling_fraction": 1})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_settings"}


@pytest.mark.parametrize("unit", ["°F", None])
def test_non_celsius_is_rejected(hass, config, devices, unit):
    hass.states.async_set("sensor.radiator", 20, {"unit_of_measurement": unit})
    assert validate_entities(hass, config) == "celsius_required"
