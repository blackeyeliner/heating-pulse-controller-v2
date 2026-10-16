import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.heating_pulse_controller_v2.runtime import Runtime


async def fresh(hass):
    for entity in ["sensor.room", "sensor.radiator"]:
        s = hass.states.get(entity)
        hass.states.async_set(entity, s.state, s.attributes)
    await hass.async_block_till_done()


async def test_setup_is_passive_and_fresh_equal_reports_count(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    assert not r.enabled
    assert devices == []
    assert r.engine.radiator is None
    await fresh(hass)
    assert r.engine.radiator.value == 20
    assert r.engine.room.value == 20
    assert devices == []
    await r.stop()
    assert devices == []


async def test_enable_requires_new_reports_then_opens_only_minimum(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    with pytest.raises(HomeAssistantError, match="fresh"):
        await r.set_enabled(True)
    await fresh(hass)
    await r.set_enabled(True)
    await hass.async_block_till_done()
    assert r.actuator.commanded_open
    assert hass.states.get("number.opening").state == "21"
    assert hass.states.get("number.closing").state == "83"
    await r.set_enabled(False)
    assert hass.states.get("number.opening").state == "0"
    assert hass.states.get("number.closing").state == "100"
    await r.stop()


async def test_legacy_guard_prevents_enable_and_stops_live_control(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    hass.states.async_set("automation.old_controller", "on")
    with pytest.raises(HomeAssistantError, match="Legacy"):
        await r.set_enabled(True)
    hass.states.async_set("automation.old_controller", "off")
    await r.set_enabled(True)
    hass.states.async_set("automation.old_controller", "on")
    await hass.async_block_till_done()
    assert r.engine.fault == "legacy_controller_enabled"
    assert r.actuator.commanded_open is False
    await r.stop()


async def test_attribute_only_change_is_not_temperature_report(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    before = r.engine.radiator
    hass.states.async_set("sensor.radiator", 20, {"unit_of_measurement": "°C", "battery": 99})
    await hass.async_block_till_done()
    assert r.engine.radiator is before
    await r.stop()


async def test_mqtt_forced_equal_state_counts_as_fresh_temperature(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    before = r.engine.radiator
    state = hass.states.get("sensor.radiator")
    hass.states.async_set("sensor.radiator", state.state, state.attributes, force_update=True)
    await hass.async_block_till_done()
    assert r.engine.radiator is not before
    assert r.engine.radiator.value == before.value
    assert r.engine.radiator.at > before.at
    assert devices == []
    await r.stop()


async def test_disable_during_open_sequence_wins(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def blocked_mode(call):
        reached.set()
        await release.wait()

    hass.services.async_register("climate", "set_hvac_mode", blocked_mode)
    enable = asyncio.create_task(r.set_enabled(True))
    await asyncio.wait_for(reached.wait(), 3)
    disable = asyncio.create_task(r.set_enabled(False))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(enable, disable)
    await hass.async_block_till_done()
    assert not r.enabled
    assert not any(c[0] == "number" and c[2]["value"] == 21 for c in devices)
    assert r.actuator.commanded_open is False
    await r.stop()


async def test_unload_closes_and_old_events_cannot_reopen(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    await r.stop()
    count = len(devices)
    await fresh(hass)
    hass.states.async_set("climate.room", "heat", {"temperature": 30})
    await hass.async_block_till_done()
    assert len(devices) == count
    assert r.actuator.commanded_open is False
    assert not r._tasks


async def test_failed_first_close_still_attempts_second_limit(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    actual = r.actuator._limit

    async def fail_first(key, value):
        if key == "opening_entity":
            raise HomeAssistantError("device offline")
        await actual(key, value)

    r.actuator._limit = AsyncMock(side_effect=fail_first)
    with pytest.raises(HomeAssistantError):
        await r.set_enabled(True)
    assert r.engine.fault == "actuator_error"
    assert not r.enabled
    assert any(
        c[0] == "number" and c[2]["entity_id"] == "number.closing" and c[2]["value"] == 100
        for c in devices
    )
    r.actuator._limit = actual
    await r.stop()


async def test_disable_during_initial_enable_reconciliation_wins(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    reached, release = asyncio.Event(), asyncio.Event()
    actual = r.actuator.close

    async def first_close():
        reached.set()
        await release.wait()
        await actual()

    r.actuator.close = first_close
    enable = asyncio.create_task(r.set_enabled(True))
    await asyncio.wait_for(reached.wait(), 3)
    disable = asyncio.create_task(r.set_enabled(False))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(enable, disable)
    await hass.async_block_till_done()
    assert not r.enabled
    assert r.actuator.commanded_open is False
    assert not any(c[0] == "number" and c[2]["value"] == 21 for c in devices)
    await r.stop()


async def test_shutdown_command_failure_still_cleans_up(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    r.actuator.close = AsyncMock(side_effect=HomeAssistantError("offline"))
    await r.stop()
    assert r._closed and not r._unsub and not r._tasks
    assert r.engine.fault == "actuator_error"
    before = len(devices)
    await fresh(hass)
    assert len(devices) == before
