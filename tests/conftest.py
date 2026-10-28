"""Run adapter tests against real HA core with a local fake actuator."""

from types import SimpleNamespace

import pytest
from homeassistant import loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant

from custom_components.heating_pulse_controller_v2.const import (
    CONF_CLOSE_VALUE,
    CONF_CLOSING,
    CONF_GUARDS,
    CONF_OPEN_VALUE,
    CONF_OPENING,
    CONF_OUTPUT,
    CONF_RADIATOR,
    CONF_REPORTS,
    CONF_ROOM,
    CONF_THERMOSTAT,
    CONF_TRV,
)


@pytest.fixture
async def hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    assert await async_load_base_functionality(hass)
    yield hass
    await hass.async_stop(force=True)


@pytest.fixture
def config():
    return {
        "name": "Test room",
        CONF_ROOM: "sensor.room",
        CONF_RADIATOR: "sensor.radiator",
        CONF_THERMOSTAT: "climate.room",
        CONF_TRV: "climate.trv",
        CONF_OPENING: "number.opening",
        CONF_CLOSING: "number.closing",
        CONF_OPEN_VALUE: 21,
        CONF_CLOSE_VALUE: 83,
        CONF_OUTPUT: "input_number.valve",
        CONF_GUARDS: ["automation.old_controller"],
        CONF_REPORTS: True,
    }


@pytest.fixture
def entry(config):
    return SimpleNamespace(entry_id="test_room", title="Test room", data=config, options={})


@pytest.fixture
def devices(hass, config):
    hass.states.async_set("number.frost", 10, {"min": 4, "max": 35, "step": 0.5})
    hass.states.async_set(
        "climate.room", "heat", {"temperature": 24, "hvac_modes": ["off", "heat"]}
    )
    hass.states.async_set(
        "climate.trv",
        "off",
        {"temperature": 14, "hvac_modes": ["off", "heat"], "hvac_action": "idle"},
    )
    for key, value in [("number.opening", 0), ("number.closing", 100)]:
        hass.states.async_set(key, value, {"min": 0, "max": 100, "step": 1})
    hass.states.async_set("input_number.valve", 0)
    hass.states.async_set("automation.old_controller", "off")
    hass.states.async_set("sensor.room", 20, {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.radiator", 20, {"unit_of_measurement": "°C"})
    calls = []

    async def service(call):
        calls.append((call.domain, call.service, dict(call.data)))
        entity_id = call.data["entity_id"]
        state = hass.states.get(entity_id)
        if call.service == "set_value":
            hass.states.async_set(entity_id, call.data["value"], state.attributes)
        elif call.service == "set_hvac_mode":
            hass.states.async_set(entity_id, call.data["hvac_mode"], state.attributes)
        elif call.service == "set_temperature":
            hass.states.async_set(
                entity_id,
                state.state,
                {**state.attributes, "temperature": call.data["temperature"]},
            )

    for domain, action in [
        ("number", "set_value"),
        ("input_number", "set_value"),
        ("climate", "set_temperature"),
        ("climate", "set_hvac_mode"),
    ]:
        hass.services.async_register(domain, action, service)
    return calls
