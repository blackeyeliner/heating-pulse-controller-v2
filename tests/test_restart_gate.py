"""Persist a threshold independently of peaks, pulses and opening permission."""

from copy import deepcopy
from dataclasses import asdict
from types import MappingProxyType
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from test_integration import setup_hass
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.const import DOMAIN
from custom_components.heating_pulse_controller_v2.engine import Settings
from custom_components.heating_pulse_controller_v2.runtime import Runtime


def saved_gate(r, source="startup"):
    data = r.engine.dump()
    data["restart_gate"] = {"temperature": 29, "source": source, "saved_at": 100}
    data["restart_gate_context"] = r._cooling_context()
    return data


@pytest.mark.parametrize("source", ["startup", "base", "adaptive", "manual"])
async def test_gate_without_peak_survives_long_restart_passively(hass, entry, devices, source):
    r = Runtime(hass, entry)
    r.now = lambda: 10000
    r.store.async_load = AsyncMock(return_value=saved_gate(r, source))
    r.store.async_save = AsyncMock()
    await r.start()
    assert not devices and r.engine.restart_temperature is None
    hass.states.async_set("sensor.radiator", 28, {"unit_of_measurement": "°C"})
    await fresh(hass)
    assert r.engine.restart_temperature == 29
    assert r.engine.restart_source == source
    assert r.engine.peak is None and not r.engine.wants_open
    assert r.diagnostics()["cooling_restore_status"] == "restored_gate"
    await r.set_enabled(True)
    assert r.actuator.commanded_open
    await r.stop()
    assert r.store.async_save.call_args.args[0]["restart_gate"]["temperature"] == 29


@pytest.mark.parametrize("case", ["settings", "binding", "nan", "future", "source"])
async def test_invalid_gate_is_not_applied(hass, entry, devices, case):
    r = Runtime(hass, entry)
    r.now = lambda: 150
    saved = saved_gate(r)
    if case == "settings":
        saved["settings"]["startup_excess"] = 4
    elif case == "binding":
        saved["restart_gate_context"]["radiator_sensor"] = "sensor.other"
    elif case == "nan":
        saved["restart_gate"]["temperature"] = float("nan")
    elif case == "future":
        saved["restart_gate"]["saved_at"] = 151
    else:
        saved["restart_gate"]["source"] = "fake_peak"
    r.store.async_load = AsyncMock(return_value=saved)
    r.store.async_save = AsyncMock()
    await r.start()
    await fresh(hass)
    assert r.engine.restart_temperature != 29 and not devices
    await r.stop()


async def test_pending_gate_is_not_overwritten_by_startup_feedback(hass, entry, devices):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "unknown")
    r = Runtime(hass, entry)
    r.now = lambda: 150
    r.store.async_load = AsyncMock(return_value=saved_gate(r))
    r.store.async_save = AsyncMock()
    await r.start()
    await fresh(hass)
    assert r._storage_data()["restart_gate"]["temperature"] == 29
    hass.states.async_set("cover.window", "open")
    await hass.async_block_till_done()
    assert r.engine.restart_temperature == 29
    assert not r.engine.wants_open
    await r.stop()


async def test_manual_gate_persists_and_next_peak_can_replace_it(hass, entry, devices):
    r = Runtime(hass, entry)
    r.store.async_save = AsyncMock()
    await r.start()
    await fresh(hass)
    await r.set_restart_temperature(29)
    assert r.engine.restart_temperature == 29 and r.engine.restart_source == "manual"
    saved = deepcopy(r.store.async_save.call_args.args[0])
    assert saved["restart_gate"]["temperature"] == 29 and not devices
    await r.set_enabled(True)
    with pytest.raises(HomeAssistantError, match="not heating"):
        await r.set_restart_temperature(30)
    r.engine._close(r.now(), "closing_threshold")
    r.engine.peak = r.engine.radiator
    at = r.now() + 61
    r.engine.room_report(20, at)
    r.engine.radiator_report(19, at)
    r.engine._cool(at, True, target=24)
    assert r.engine.restart_source == "base" and r.engine.restart_temperature != 29
    await r.stop()


async def test_real_action_and_storage_reload_restore_manual_gate(hass, config, devices):
    await setup_hass(hass)
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Office pilot",
        data={**config, **asdict(Settings())},
        options={},
        source="user",
        unique_id=config["trv"],
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)
    await fresh(hass)
    await hass.services.async_call(
        DOMAIN,
        "set_restart_temperature",
        {"config_entry_id": entry.entry_id, "temperature": 29},
        blocking=True,
    )
    assert entry.runtime_data.engine.restart_temperature == 29
    assert not devices
    assert await hass.config_entries.async_reload(entry.entry_id)
    await fresh(hass)
    r = entry.runtime_data
    assert r.engine.restart_temperature == 29 and r.engine.peak is None
    assert r.engine.restart_source == "manual"
    assert not r.enabled and not devices
    # Repeat reload to ensure a consumed episode cannot erase the durable gate.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await fresh(hass)
    assert entry.runtime_data.engine.restart_temperature == 29
    await hass.config_entries.async_unload(entry.entry_id)
