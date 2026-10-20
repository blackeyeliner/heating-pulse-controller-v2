"""Local rotary controls, own echoes, frost updates and pulse interlocks."""

import asyncio

import pytest
from homeassistant.core import State
from test_native_climate import native


def trv(hass, mode, target):
    old = hass.states.get("climate.trv")
    hass.states.async_set("climate.trv", mode, {**old.attributes, "temperature": target})


async def settle(hass, r):
    await asyncio.sleep(0.01)
    if r.physical.task:
        await r.physical.task
    await hass.async_block_till_done()


async def test_rotation_from_off_enables_heat_and_further_changes_target(hass, entry, devices):
    r, c = await native(hass, entry)
    r.physical.debounce = 0
    await c.async_set_hvac_mode("off")
    trv(hass, "heat", 23.5)
    await settle(hass, r)
    assert r.request.mode == "heat" and r.request.target == 23.5
    assert r.enabled and c.hvac_mode == "heat"
    mark = len(devices)
    trv(hass, "heat", 24)
    await settle(hass, r)
    assert r.request.target == 24
    assert not any(
        d[1] == "set_temperature" and d[2].get("temperature") == 23.5 for d in devices[mark:]
    )
    await r.stop()


@pytest.mark.parametrize("mode", ["off", "auto"])
async def test_physical_off_or_auto_stops_heat_and_restores_firmware_off(
    hass, entry, devices, mode
):
    r, c = await native(hass, entry)
    r.physical.debounce = 0
    await c.async_set_hvac_mode("heat")
    trv(hass, mode, 21)
    await settle(hass, r)
    assert r.request.mode == "off" and not r.control_requested
    assert r._off_protection_ready and hass.states.get("climate.trv").state == "off"
    assert c.target_temperature == 10 and r.request.target == 21
    await r.stop()


async def test_own_pulses_and_frost_setpoint_updates_never_create_heat_requests(
    hass, entry, devices
):
    r, c = await native(hass, entry)
    r.physical.debounce = 0
    await c.async_set_temperature(temperature=23, hvac_mode="heat")
    await hass.async_block_till_done()
    assert r.request.mode == "heat" and r.request.target == 23
    await c.async_set_hvac_mode("off")
    trv(hass, "off", 10)  # Firmware copies frost number into occupied setpoint.
    await settle(hass, r)
    assert not r.enabled and r.request.mode == "off" and r.request.target == 23
    await c.async_set_temperature(temperature=24)
    trv(hass, "off", 24)
    await settle(hass, r)
    assert r.request.mode == "off" and r.request.target == 23
    assert r.frost_temperature == 24
    await r.stop()


async def test_ui_target_syncs_between_pulses_without_opening(hass, entry, devices):
    r, c = await native(hass, entry)
    hass.states.async_set("sensor.room", 30, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    await c.async_set_temperature(temperature=22.5, hvac_mode="heat")
    await hass.async_block_till_done()
    assert r.enabled and not r.actuator.commanded_open
    assert hass.states.get("climate.trv").state == "heat"
    assert hass.states.get("climate.trv").attributes["temperature"] == 22.5
    await c.async_set_temperature(temperature=23)
    await hass.async_block_till_done()
    assert hass.states.get("climate.trv").attributes["temperature"] == 23
    assert not r.actuator.commanded_open
    assert not any(d[0] == "number" and d[2].get("value") == 21 for d in devices)
    await r.stop()


async def test_restore_unavailable_and_status_only_are_not_gestures(hass, entry, devices):
    r, c = await native(hass, entry)
    r.physical.observe(None, State("climate.trv", "heat", {"temperature": 25}))
    r.physical.observe(
        State("climate.trv", "unavailable"), State("climate.trv", "heat", {"temperature": 25})
    )
    trv(hass, "off", 14)
    await settle(hass, r)
    assert not r.control_requested and r.request.target == 21
    await r.stop()


async def test_late_queued_rotation_cannot_enable_after_unload(hass, entry, devices):
    r, c = await native(hass, entry)
    trv(hass, "heat", 25)
    await asyncio.sleep(0)
    await r.stop()
    assert not r.enabled and not r.control_requested
    assert r.request.mode == "off" and r.request.target == 21


async def test_late_firmware_target_after_off_cannot_reenable_heat(hass, entry, devices):
    r, c = await native(hass, entry)
    r.physical.debounce = 0
    await c.async_set_hvac_mode("heat")
    trv(hass, "auto", 16)
    await settle(hass, r)
    trv(hass, "off", 16)  # Delayed schedule/setpoint report after mode handoff.
    await settle(hass, r)
    assert r.request.mode == "off" and not r.control_requested
    assert r.request.target == 21
    await r.stop()


@pytest.mark.parametrize("target", [4, 35])
async def test_physical_range_endpoints_are_accepted(hass, entry, devices, target):
    r, c = await native(hass, entry)
    r.physical.debounce = 0
    trv(hass, "heat", target)
    await settle(hass, r)
    assert r.request.mode == "heat" and r.request.target == target
    await r.stop()


async def test_off_cancels_a_pending_rotary_enable(hass, entry, devices):
    r, c = await native(hass, entry)
    r.physical.debounce = 0.05
    trv(hass, "heat", 25)
    await asyncio.sleep(0)
    trv(hass, "auto", 16)
    await settle(hass, r)
    assert r.request.mode == "off" and not r.enabled
    assert not any(d[0] == "number" and d[2].get("value") == 83 for d in devices)
    await r.stop()


async def test_physical_off_inhibits_an_inflight_open(hass, entry, devices):
    r, c = await native(hass, entry)
    r.physical.debounce = 0
    reached, release = asyncio.Event(), asyncio.Event()
    original = r.actuator._limit

    async def blocked(key, value):
        if key == "opening_entity" and value == 21 and r.request.mode == "heat":
            reached.set()
            await release.wait()
        await original(key, value)

    r.actuator._limit = blocked
    enable = asyncio.create_task(c.async_set_hvac_mode("heat"))
    await asyncio.wait_for(reached.wait(), 5)
    trv(hass, "off", 21)
    await asyncio.sleep(0)
    assert not r.enabled
    release.set()
    await enable
    await settle(hass, r)
    assert r.request.mode == "off" and not r.control_requested
    assert r._off_protection_ready and r.actuator.commanded_open is None
    await r.stop()
