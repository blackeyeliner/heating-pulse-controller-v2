"""Pair drift, bounded correction, cancellation and reconnection integration."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_native_climate import native
from test_reconnect import running


async def test_late_zero_during_pulse_is_corrected_without_resetting_clock(hass, entry, devices):
    r, clock = await running(hass, entry)
    opened = r.engine.opened_at
    before = len(devices)
    state = hass.states.get("number.opening")
    hass.states.async_set("number.opening", 0, state.attributes)
    await hass.async_block_till_done()
    assert r.actuator.commanded_open is True
    assert r.actuator.last_pair == (21, 83) and r.actuator.pair_corrections == 1
    assert r.engine.opened_at == opened and not r.reconnect.active
    assert len(devices) > before
    assert len(r.engine._trace) <= 1  # No pre-correction curve remains to teach lag/reference.
    await r.stop()


async def test_stale_cache_does_not_rewrite_a_device_that_reads_correctly(hass, entry, devices):
    r, clock = await running(hass, entry)
    original = r.actuator.reader.read
    r.actuator.reader.read = AsyncMock(return_value=(21, 83))
    count = len(devices)
    state = hass.states.get("number.opening")
    hass.states.async_set("number.opening", 0, state.attributes)
    await hass.async_block_till_done()
    assert len(devices) == count and r.actuator.pair_corrections == 0
    calls = r.actuator.reader.read.await_count
    await r._process()
    assert r.actuator.reader.read.await_count == calls
    clock[0] += 60
    await r._process()
    assert r.actuator.reader.read.await_count == calls + 1
    r.actuator.reader.read = original
    await r.stop()


async def test_failed_final_open_readback_closes_and_enters_grace_not_emergency(
    hass, entry, devices
):
    r, clock = await running(hass, entry)
    original = r.actuator.reader.read

    async def stuck():
        pair = await original()
        return (0, 83) if pair == (21, 83) else pair

    r.actuator.reader.read = stuck
    state = hass.states.get("number.opening")
    hass.states.async_set("number.opening", 0, state.attributes)
    await hass.async_block_till_done()
    assert r.reconnect.active and r.reconnect.record["attempts"] == 0
    assert not r.emergency.active and r.engine.fault is None
    assert (
        hass.states.get("number.opening").state == "0"
        and hass.states.get("number.closing").state == "100"
    )
    await r.stop()


async def test_manual_off_during_readback_cannot_reopen(hass, entry, devices):
    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("heat")
    original = r.actuator.reader.read
    entered = asyncio.Event()
    release = asyncio.Event()
    first = True

    async def blocked():
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
            return (0, 83)
        return await original()

    r.actuator.reader.read = blocked
    state = hass.states.get("number.opening")
    hass.states.async_set("number.opening", 0, state.attributes)
    await asyncio.wait_for(entered.wait(), 2)
    count = len(devices)
    off = asyncio.create_task(c.async_set_hvac_mode("off"))
    await asyncio.sleep(0)
    release.set()
    await off
    await hass.async_block_till_done()
    assert r.request.mode == "off" and r._off_protection_ready
    assert not any(d[0] == "number" and d[2].get("value") == 83 for d in devices[count:])
    await r.stop()


async def test_firmware_frost_pair_is_not_rewritten_by_owned_closed_monitor(hass, entry, devices):
    r, c = await native(hass, entry)
    await c.async_set_hvac_mode("off")
    mark = len(devices)
    r.actuator.reader.read = AsyncMock(side_effect=AssertionError("Off handoff must stay released"))
    await r._process()
    assert len(devices) == mark and r.actuator.commanded_open is None and r._off_protection_ready
    await r.stop()


async def test_final_open_read_rejects_pair_even_after_both_cached_acks(hass, config, devices):
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.heating_pulse_controller_v2.actuator import SonoffActuator

    actuator = SonoffActuator(hass, config)
    original = actuator.reader.read

    async def reverted():
        pair = await original()
        return (0, 83) if pair == (21, 83) else pair

    actuator.reader.read = reverted
    with pytest.raises(HomeAssistantError):
        await actuator.open(24, lambda: True)
    assert actuator.commanded_open is False
    assert actuator.last_pair == (0, 100)
    assert hass.states.get("input_number.valve").state == "0"
    assert not any(
        domain == "input_number" and data.get("value") == 5 for domain, _, data in devices
    )
