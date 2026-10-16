"""Load real entity platforms/config entries in an isolated Home Assistant."""

from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

import pytest
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
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
    assert "unit_of_measurement" not in age.attributes
    runtime.now = lambda: 12000
    for seconds, expected in [
        (0, "00:00"),
        (59.9, "00:59"),
        (60, "01:00"),
        (754, "12:34"),
        (10800, "180:00"),
    ]:
        runtime.engine.reference = Reference(40, 12000 - seconds, 0, [])
        runtime._notify()
        await hass.async_block_till_done()
        age = hass.states.get(age_id)
        assert age.state == expected
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
