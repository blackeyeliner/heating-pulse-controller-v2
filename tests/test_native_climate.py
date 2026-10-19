"""Native thermostat, persistent requests/control and safe legacy adoption."""

from dataclasses import asdict
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_integration import setup_hass
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.climate import PulseClimate
from custom_components.heating_pulse_controller_v2.const import DOMAIN
from custom_components.heating_pulse_controller_v2.engine import Settings
from custom_components.heating_pulse_controller_v2.runtime import Runtime
from custom_components.heating_pulse_controller_v2.thermostat import CONF_NATIVE


async def native(hass, entry):
    entry.data = {k: v for k, v in entry.data.items() if k not in ["thermostat", "output_helper"]}
    r = Runtime(hass, entry)
    entry.runtime_data = r
    await r.start()
    await fresh(hass)
    c = PulseClimate(entry)
    c.hass = hass
    return r, c


async def test_fresh_native_is_independent_passive_and_standard_climate(hass, entry, devices):
    r, c = await native(hass, entry)
    assert r.native and not r.enabled and not devices
    assert c.hvac_mode == "off" and c.hvac_action == "off"
    assert c.current_temperature == 20 and c.target_temperature == 21
    await c.async_set_temperature(temperature=23)
    assert c.target_temperature == 23 and not devices
    await c.async_set_hvac_mode("heat")
    assert r.enabled and c.hvac_mode == "heat" and c.hvac_action == "heating"
    assert r.actuator.commanded_open
    await c.async_set_hvac_mode("off")
    assert not r.enabled and c.hvac_action == "off" and not r.actuator.commanded_open
    await r.stop()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 3, 36, True, "23"])
async def test_invalid_setpoint_never_moves_or_changes_request(hass, entry, devices, value):
    r, c = await native(hass, entry)
    with pytest.raises(HomeAssistantError):
        await c.async_set_temperature(temperature=value)
    assert c.target_temperature == 21 and not devices
    await r.stop()


async def test_state_is_action_not_heat_mode_and_unknown_on_reconnect(hass, entry, devices):
    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("heat")
    await r.actuator.close()
    assert c.hvac_mode == "heat" and c.hvac_action == "idle"
    await r._suspect_valve()
    assert c.hvac_action is None
    await r.stop()


async def test_native_restart_waits_for_reports_then_auto_resumes(hass, entry, devices):
    r, c = await native(hass, entry)
    await c.async_set_temperature(temperature=23.4)
    await c.async_set_hvac_mode("heat")
    await r.stop()
    count = len(devices)
    d = Runtime(hass, entry)
    entry.runtime_data = d
    await d.start()
    assert d.control_requested and not d.enabled and len(devices) == count
    assert d.request.target == 23.4 and d.request.mode == "heat"
    await d._process()
    assert not d.enabled and len(devices) == count
    await fresh(hass)
    assert d.enabled and d.actuator.commanded_open
    await d.stop()


async def test_native_manual_off_survives_restart(hass, entry, devices):
    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("heat")
    await c.async_set_hvac_mode("off")
    await r.stop()
    count = len(devices)
    d = Runtime(hass, entry)
    await d.start()
    await fresh(hass)
    assert not d.control_requested and not d.enabled and d.request.mode == "off"
    assert len(devices) == count
    await d.stop()


async def test_resume_waits_for_guards_and_manual_off_wins(hass, entry, devices):
    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("heat")
    await r.stop()
    d = Runtime(hass, entry)
    await d.start()
    hass.states.async_set("automation.old_controller", "on")
    await fresh(hass)
    assert d.control_requested and not d.enabled
    await d.set_enabled(False)
    hass.states.async_set("automation.old_controller", "off")
    await fresh(hass)
    assert not d.enabled and not d.control_requested
    await d.stop()


async def test_corrupt_request_and_control_state_do_not_start_heat(hass, entry, devices):
    entry.data[CONF_NATIVE] = True
    r = Runtime(hass, entry)
    r.request.store.async_load = AsyncMock(return_value={"mode": "heat", "target": float("nan")})
    r._control_store.async_load = AsyncMock(return_value={"enabled": True, "context": {}})
    await r.start()
    await fresh(hass)
    assert r.request.mode == "off" and not r.control_requested and not devices
    await r.stop()


async def test_native_entity_setup_and_actual_services_reload(hass, config, devices):
    await setup_hass(hass)
    data = {k: v for k, v in config.items() if k not in ["thermostat", "output_helper"]}
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Native room",
        data={**data, **asdict(Settings())},
        options={},
        source="user",
        unique_id=data["trv"],
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    climate_id = registry.async_get_entity_id("climate", DOMAIN, f"{entry.entry_id}_climate")
    sensor_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_valve_position")
    assert climate_id and sensor_id and hass.states.get(climate_id).state == "off"
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": climate_id, "temperature": 24.1}, blocking=True
    )
    assert hass.states.get(climate_id).attributes["temperature"] == 24.1
    assert devices == []
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(climate_id).attributes["temperature"] == 24.1
    assert not entry.runtime_data.enabled
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("preserve", [False, True])
async def test_adoption_captures_request_reloads_and_removes_helper_dependency(
    hass, config, devices, preserve
):
    await setup_hass(hass)
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Adopt room",
        data={**config, **asdict(Settings())},
        options={},
        source="user",
        unique_id=config["trv"],
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    hass.states.async_set(
        "climate.room", "heat", {"temperature": 22.7, "hvac_modes": ["heat", "off"]}
    )
    await hass.services.async_call(
        DOMAIN,
        "adopt_thermostat",
        {"config_entry_id": entry.entry_id, "preserve_history_helper": preserve},
        blocking=True,
    )
    await hass.async_block_till_done()
    r = entry.runtime_data
    assert r.native and r.request.target == 22.7 and r.request.mode == "heat"
    assert ("output_helper" in r.config) == preserve
    assert not r.control_requested
    hass.states.async_remove("climate.room")
    assert r._inputs() == (True, 22.7)
    assert devices == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_auto_resume_uses_bounded_pwm_if_temperature_reports_never_return(
    hass, entry, devices
):
    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("heat")
    await r.stop()
    d = Runtime(hass, entry)
    clock = [d.now()]
    d.now = lambda: clock[0]
    await d.start()
    await d._process()
    assert not d.enabled and d.control_requested
    clock[0] += d.engine.settings.radiator_timeout + 1
    await d._process()
    assert d.enabled and d.engine._recovery_reason == "missing_temperature"
    assert d.actuator.commanded_open
    await d.stop()


async def test_manual_off_during_startup_close_wins(hass, entry, devices):
    import asyncio

    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("heat")
    await r.stop()
    d = Runtime(hass, entry)
    await d.start()
    reached = asyncio.Event()
    release = asyncio.Event()
    actual = d.actuator.close

    async def blocked():
        reached.set()
        await release.wait()
        await actual()

    d.actuator.close = blocked
    # Emit reports without waiting for the resulting worker to finish.
    for entity in ["sensor.room", "sensor.radiator"]:
        s = hass.states.get(entity)
        hass.states.async_set(entity, s.state, s.attributes, force_update=True)
    await asyncio.wait_for(reached.wait(), 3)
    count = len(devices)
    off = asyncio.create_task(d.set_enabled(False))
    await asyncio.sleep(0)
    release.set()
    await off
    await hass.async_block_till_done()
    assert not d.control_requested and not d.enabled and not d.actuator.commanded_open
    assert not any(x[0] == "number" and x[2]["value"] == 21 for x in devices[count:])
    await d.stop()


async def test_setup_flow_requires_no_existing_thermostat(hass, config, devices):
    await setup_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    data = {
        k: v
        for k, v in config.items()
        if k not in ["thermostat", "output_helper", "reporting_verified"]
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["step_id"] == "tuning"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**asdict(Settings()), "reporting_verified": True}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.data[CONF_NATIVE] and entry.runtime_data.native
    assert not devices and not entry.runtime_data.control_requested
    await hass.config_entries.async_unload(entry.entry_id)
