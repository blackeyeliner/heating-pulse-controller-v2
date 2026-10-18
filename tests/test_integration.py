"""Load real entity platforms/config entries in an isolated Home Assistant."""

from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components.logbook.helpers import (
    _is_state_filtered,
    async_filter_entities,
    is_sensor_continuous,
)
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.heating_pulse_controller_v2.const import DOMAIN
from custom_components.heating_pulse_controller_v2.engine import Reference, Settings


async def setup_hass(hass):
    path = Path(hass.config.config_dir)
    (path / "custom_components").symlink_to(Path(__file__).parents[1] / "custom_components")
    (path / "configuration.yaml").write_text("{}\n")
    hass.config_entries = ConfigEntries(hass, {})
    assert await async_load_base_functionality(hass)
    assert await async_setup_component(hass, "homeassistant", {})


async def test_real_setup_entities_reload_unload(hass, config, devices):
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
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert runtime.enabled is False
    assert devices == []
    created = [
        s
        for s in hass.states.async_all()
        if s.entity_id.startswith(
            ("sensor.office_pilot_", "switch.office_pilot_", "button.office_pilot_")
        )
    ]
    assert len(created) == 12
    age_id = next(s.entity_id for s in created if s.entity_id.endswith("reference_age"))
    age = hass.states.get(age_id)
    assert age.state == "unknown"
    assert age.attributes["seconds"] is None
    assert age.attributes["formatted"] is None
    assert age.attributes["unit_of_measurement"] == "min"
    assert age.attributes["device_class"] == "duration"
    runtime.now = lambda: 12000
    for seconds, expected in [
        (0, "00:00"),
        (59.9, "00:59"),
        (60, "01:00"),
        (754, "12:34"),
        (10800, "180:00"),
    ]:
        old_age = age
        runtime.engine.reference = Reference(40, 12000 - seconds, 0, [])
        runtime._notify()
        await hass.async_block_till_done()
        age = hass.states.get(age_id)
        assert float(age.state) == pytest.approx(round(seconds / 60, 2))
        assert age.attributes["formatted"] == expected
        # Exercise HA's actual historical classification and live Activity filters.
        assert is_sensor_continuous(hass, er.async_get(hass), age_id)
        assert async_filter_entities(hass, [age_id]) == []
        assert _is_state_filtered(age, old_age)
        assert age.attributes["seconds"] == pytest.approx(seconds)
    runtime.engine.reference = None
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert runtime._closed
    assert entry.runtime_data is not runtime
    assert not entry.runtime_data.enabled
    reloaded = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert reloaded._closed
    assert devices == []


async def test_real_ui_flow_and_duplicate_actuator(hass, config, devices):
    await setup_hass(hass)
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert first["step_id"] == "user"
    second = await hass.config_entries.flow.async_configure(
        first["flow_id"], {k: v for k, v in config.items() if k != "reporting_verified"}
    )
    assert second["step_id"] == "tuning"
    third = await hass.config_entries.flow.async_configure(
        second["flow_id"], {**asdict(Settings()), "reporting_verified": False}
    )
    assert third["type"] == "create_entry"
    await hass.async_block_till_done()
    entry = third["result"]
    assert entry.state == ConfigEntryState.LOADED
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    duplicate = await hass.config_entries.flow.async_configure(
        again["flow_id"], {k: v for k, v in config.items() if k != "reporting_verified"}
    )
    assert duplicate["reason"] == "already_configured"
    before = entry.runtime_data
    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["step_id"] == "init"
    saved = await hass.config_entries.options.async_configure(
        options["flow_id"],
        {
            **asdict(Settings()),
            "cooling_fraction": 0.4,
            "adaptive_cooling": True,
            "reporting_verified": False,
        },
    )
    assert saved["type"] == "create_entry"
    await hass.async_block_till_done()
    assert before._closed
    assert entry.runtime_data.engine.settings.cooling_fraction == 0.4
    assert entry.runtime_data.engine.settings.adaptive_cooling is True
    assert not entry.runtime_data.enabled
    assert devices == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_optional_window_entities_and_options_removal(hass, config, devices):
    await setup_hass(hass)
    hass.states.async_set("cover.window", "closed", {"supported_features": 15})
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Window pilot",
        data={**config, **asdict(Settings()), "window_cover": "cover.window"},
        options={},
        source="user",
        unique_id=config["trv"],
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert entry.state == ConfigEntryState.LOADED
    assert hass.states.get("sensor.window_pilot_window_status").state == "closed"
    assert hass.states.get("binary_sensor.window_pilot_window_opening_allowed").state == "on"
    assert devices == []
    options = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        options["flow_id"], {**asdict(Settings()), "reporting_verified": True}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.config["window_cover"] == ""
    assert not entry.runtime_data.enabled
    assert devices == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_real_reload_preserves_confirmed_cooling_gate(hass, config, devices, monkeypatch):
    from test_cooling_restore import cooled_controller
    from test_runtime import fresh

    from custom_components.heating_pulse_controller_v2.runtime import Runtime

    clock = [125.0]
    monkeypatch.setattr(Runtime, "now", lambda self: clock[0])
    await setup_hass(hass)
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Cooling pilot",
        data={**config, **asdict(Settings())},
        options={},
        source="user",
        unique_id=config["trv"],
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    old = entry.runtime_data
    old.engine = cooled_controller()
    old._owned = True
    old.actuator.commanded_open = False
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    new = entry.runtime_data
    assert new is not old and not new.enabled
    assert new.diagnostics()["cooling_restore_status"] == "waiting_for_fresh_reports"
    clock[0] = 160
    hass.states.async_set("sensor.radiator", 28, {"unit_of_measurement": "°C"})
    await fresh(hass)
    gate = hass.states.get("sensor.cooling_pilot_restart_temperature")
    assert float(gate.state) == 29
    assert gate.attributes["restore_status"] == "restored"
    assert not new.enabled

    # Real platform setup replaces the fixture services. Supply actual readback
    # after setup so opening still exercises the production feedback contract.
    async def number_feedback(call):
        entity_id = call.data["entity_id"]
        state = hass.states.get(entity_id)
        devices.append((call.domain, call.service, dict(call.data)))
        hass.states.async_set(entity_id, call.data["value"], state.attributes)

    hass.services.async_register("number", "set_value", number_feedback)
    await new.set_enabled(True)
    assert new.actuator.commanded_open
    await hass.config_entries.async_unload(entry.entry_id)
