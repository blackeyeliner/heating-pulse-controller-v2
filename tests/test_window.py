"""Window interruption, PWM budget, emergency boundaries and HA commands."""

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_engine import valid_reference
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.engine import Controller, Phase
from custom_components.heating_pulse_controller_v2.runtime import Runtime
from custom_components.heating_pulse_controller_v2.window import cold_reason, is_window_open


def tick(c, at, *, window=True, room=20, rad=25, enabled=True, heat=True):
    c.room_report(room, at)
    c.radiator_report(rad, at)
    c.evaluate(at, enabled=enabled, heat_mode=heat, target=24, window_open=window)


def test_pwm_waits_first_and_never_learns_from_heating_or_falling_surface():
    c = Controller()
    valid_reference(c)
    before = c.dump()
    for at in range(0, 9001, 10):
        tick(c, at, rad=35 - (at % 3000) / 300)
        assert c.wants_open == (at % 3000 >= 2400)
        assert not c.fault
    assert c.dump() == before
    assert c.trend is None and c.peak is None and not c._trace


def test_delayed_callback_extends_pause_instead_of_catching_up():
    c = Controller()
    tick(c, 0)
    tick(c, 2400)
    tick(c, 3020)
    assert not c.wants_open
    tick(c, 5420)
    assert not c.wants_open
    tick(c, 5500)
    assert c.wants_open


def test_open_window_cancels_calibration_and_close_discards_contaminated_curve():
    c = Controller()
    tick(c, 0, window=False, rad=20)
    assert c.phase == Phase.CALIBRATING
    tick(c, 30, rad=30)
    assert not c.wants_open and c.reference is None
    tick(c, 2430, rad=25)
    assert c.phase == Phase.WINDOW_HEATING
    tick(c, 2440, window=False, rad=20)
    assert not c.wants_open and not c._trace and c.peak is None
    tick(c, 2445, window=False, rad=20)
    assert c.phase == Phase.CALIBRATING
    assert c._trace[0].at == 2445


@pytest.mark.parametrize(
    "room,rad,reason",
    [(16, 25, "window_room_cold"), (20, 10, "window_radiator_cold"), (15, 9, "window_room_cold")],
)
def test_cold_boundaries_stop_pwm(room, rad, reason):
    c = Controller()
    tick(c, 0)
    tick(c, 2400)
    assert c.wants_open
    tick(c, 2410, room=room, rad=rad)
    assert not c.wants_open and c.reason == reason
    assert c.reference is None and not c.fault


@pytest.mark.parametrize(
    "kwargs", [{"enabled": False}, {"heat": False}, {"room": 24}, {"window": None}]
)
def test_pwm_inhibitions(kwargs):
    c = Controller()
    tick(c, 0)
    tick(c, 2400)
    tick(c, 2410, **kwargs)
    assert not c.wants_open


def test_closed_window_cold_room_uses_normal_heating():
    c = Controller()
    tick(c, 0, window=False, room=15, rad=9)
    assert c.phase == Phase.CALIBRATING


def test_window_interruption_does_not_update_lag():
    c = Controller()
    valid_reference(c)
    tick(c, 0, window=False, rad=20)
    tick(c, 30, window=False, rad=49)
    assert c._learn_lag
    before = c.dump()
    tick(c, 60, rad=50)
    tick(c, 120, rad=30)
    tick(c, 180, window=False, rad=25)
    tick(c, 240, window=False, rad=24)
    assert c.dump() == before


@pytest.mark.parametrize(
    "state,pos,expected",
    [
        ("closed", 0, False),
        ("closed", 5, True),
        ("open", 0, True),
        ("opening", 0, True),
        ("closing", 0, True),
        ("unavailable", 0, None),
        ("unknown", 0, None),
        (None, None, None),
    ],
)
def test_motor_state_is_not_assumed_closed(state, pos, expected):
    assert is_window_open(state, pos) is expected


@pytest.mark.parametrize("room,rad", [(16, 20), (20, 10)])
async def test_runtime_emergency_close_is_immediate_and_recovers_to_normal(
    hass, entry, devices, room, rad
):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "open", {"current_position": 30})
    calls = []

    async def close(call):
        calls.append(dict(call.data))
        hass.states.async_set("cover.window", "closed", {"current_position": 0})

    hass.services.async_register("cover", "close_cover", close)
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    assert not r.engine.wants_open and not calls
    hass.states.async_set("sensor.room", room, {"unit_of_measurement": "°C"}, force_update=True)
    hass.states.async_set("sensor.radiator", rad, {"unit_of_measurement": "°C"}, force_update=True)
    await hass.async_block_till_done()
    assert calls == [{"entity_id": "cover.window"}]
    assert r._window_open() is False
    assert not r.engine.fault
    await r._process()
    if rad > room + r.engine.settings.startup_excess:
        assert r.engine.phase == Phase.COOLING
        hass.states.async_set("sensor.radiator", room, {"unit_of_measurement": "°C"})
        await hass.async_block_till_done()
    assert r.engine.phase == Phase.CALIBRATING
    await r.stop()


async def test_window_protection_works_during_heating_fault_but_not_when_disabled(
    hass, entry, devices
):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "open")
    calls = []

    async def close(call):
        calls.append(call)
        raise HomeAssistantError("offline")

    hass.services.async_register("cover", "close_cover", close)
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    r.engine.trip("maximum_open_duration", r.now())
    hass.states.async_set("sensor.room", 16, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert r.engine.fault == "maximum_open_duration"
    assert r.diagnostics()["window_status"] == "window_close_failed"
    await r._process()
    assert len(calls) == 1  # No command storm on the 5-second timer.
    await r.set_enabled(False)
    r._window_close_last = -float("inf")
    await r._process()
    assert len(calls) == 1
    await r.stop()


async def test_opening_window_during_normal_open_sequence_prevents_valve_open(hass, entry, devices):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "closed", {"current_position": 0})
    reached, release = asyncio.Event(), asyncio.Event()

    async def block(call):
        reached.set()
        await release.wait()

    hass.services.async_register("climate", "set_hvac_mode", block)
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    enabling = asyncio.create_task(r.set_enabled(True))
    await asyncio.wait_for(reached.wait(), 3)
    hass.states.async_set("cover.window", "open", {"current_position": 30})
    release.set()
    await enabling
    await hass.async_block_till_done()
    assert not any(c[0] == "number" and c[2]["value"] == 21 for c in devices)
    assert r.actuator.commanded_open is False
    assert r.engine.phase == Phase.WINDOW_PAUSE
    await r.stop()


def test_missing_one_temperature_does_not_hide_other_cold_sensor():
    assert cold_reason(None, 9) == "window_radiator_cold"
    assert cold_reason(15, None) == "window_room_cold"
    assert cold_reason(None, None) is None


async def test_live_pwm_closes_by_deadline_and_unavailable_cover_inhibits(hass, entry, devices):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "open")
    r = Runtime(hass, entry)
    clock = [1000.0]
    r.now = lambda: clock[0]
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    assert not r.actuator.commanded_open
    assert r.window_opening_allowed()
    for _ in range(40):
        clock[0] += 60
        await fresh(hass)
        await r._process()
    assert r.actuator.commanded_open
    assert r._window_deadline_cancel is not None
    for _ in range(9):
        clock[0] += 60
        await fresh(hass)
        await r._process()
        assert r.actuator.commanded_open
    clock[0] += 60
    await fresh(hass)
    await r._process()
    assert r.actuator.commanded_open is False
    assert r._window_deadline_cancel is None
    for _ in range(40):
        clock[0] += 60
        await fresh(hass)
        await r._process()
    assert r.actuator.commanded_open
    hass.states.async_set("cover.window", "unavailable")
    await hass.async_block_till_done()
    assert r.actuator.commanded_open is False
    assert r.engine.reason == "window_unavailable"
    await r.stop()


async def test_disabled_setup_never_closes_cold_window_and_interlock_blocks_cold(
    hass, entry, devices
):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "closed")
    r = Runtime(hass, entry)
    await r.start()
    hass.states.async_set("sensor.room", 16, {"unit_of_measurement": "°C"})
    await fresh(hass)
    assert devices == []
    assert r.window_opening_allowed()
    await r.set_enabled(True)
    assert not r.window_opening_allowed()
    hass.states.async_set("sensor.room", 16.01, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert r.window_opening_allowed()
    hass.states.async_set("sensor.radiator", "unavailable")
    await hass.async_block_till_done()
    assert not r.window_opening_allowed()
    await r.stop()
