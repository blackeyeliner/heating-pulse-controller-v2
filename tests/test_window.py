"""Window interruption, PWM budget, emergency boundaries and HA commands."""

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_engine import valid_reference
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.engine import Controller, Phase, Settings
from custom_components.heating_pulse_controller_v2.runtime import Runtime
from custom_components.heating_pulse_controller_v2.window import cold_reason, is_window_open


def tick(c, at, *, window=True, room=20, rad=25, enabled=True, heat=True):
    c.room_report(room, at)
    c.radiator_report(rad, at)
    c.evaluate(at, enabled=enabled, heat_mode=heat, target=24, window_open=window)


def test_pwm_starts_when_ready_and_never_learns_from_exposed_surface():
    c = Controller()
    valid_reference(c)
    before = c.dump()
    for at in range(0, 10801, 10):
        tick(c, at, rad=20)
        assert c.wants_open == (at % 3600 < 900)
        assert not c.fault
    assert c.dump() == before
    assert c.trend is None and not c._trace


def test_delayed_callback_extends_pause_instead_of_catching_up():
    c = Controller()
    tick(c, 0, rad=20)
    tick(c, 920)
    assert not c.wants_open
    tick(c, 3620)
    assert not c.wants_open
    tick(c, 3680)
    assert c.wants_open


def test_open_window_preserves_live_calibration_then_close_restarts_clean_trace():
    c = Controller()
    tick(c, 0, window=False, rad=20)
    assert c.phase == Phase.CALIBRATING
    tick(c, 30, rad=30)
    assert c.wants_open and c.reference is None and c.opened_at == 0
    assert c.window_deadline == 900
    assert not c._trace
    tick(c, 100, window=False, rad=30)
    assert c.phase == Phase.CALIBRATING and c.opened_at == 0
    assert not c._trace
    tick(c, 110, window=False, rad=31)
    assert c._trace[0].at == 110


@pytest.mark.parametrize("opened_for,expected", [(100, True), (900, False), (1000, False)])
def test_window_open_uses_existing_pulse_origin(opened_for, expected):
    c = Controller()
    tick(c, 0, window=False, rad=20)
    tick(c, opened_for, rad=25)
    assert c.wants_open is expected
    if expected:
        assert c.opened_at == 0 and c.window_deadline == 900
    else:
        assert c._window_next_open == opened_for + max(2700, 3 * opened_for)


def test_opening_window_while_closed_preserves_confirmed_cooling_gate():
    c = Controller()
    tick(c, 0, window=False, rad=25)
    gate = c.restart_temperature
    tick(c, 30, rad=24)
    assert not c.wants_open and c.restart_temperature == gate
    tick(c, 60, rad=gate)
    assert c.wants_open and c.opened_at == 60


def test_closing_window_mid_pwm_uses_saved_cutoff_without_extra_close():
    c = Controller()
    valid_reference(c)
    tick(c, 0, rad=20)
    tick(c, 120, window=False, rad=30)
    assert c.phase == Phase.HEATING and c.opened_at == 0
    assert c.window_deadline is None
    tick(c, 150, window=False, rad=49)
    assert not c.wants_open and c.reason == "closing_threshold"
    assert not c._learn_lag


def test_closing_window_mid_pause_cancels_timer_and_uses_normal_cooling():
    c = Controller()
    tick(c, 0, rad=20)
    tick(c, 900, rad=30)
    assert not c.wants_open
    tick(c, 1000, window=False, rad=25)
    assert not c.wants_open and c._window_next_open == 0
    tick(c, 1030, window=False, rad=20)
    assert c.phase == Phase.CALIBRATING


def test_window_pwm_repetition_requires_latched_room_demand():
    c = Controller()
    tick(c, 0, rad=20)
    tick(c, 900, rad=30)
    tick(c, 3600, room=24, rad=25)
    assert not c.wants_open
    tick(c, 3630, room=23.8, rad=25)
    assert not c.wants_open  # start hysteresis is shared with normal heating
    tick(c, 3660, room=23.7, rad=25)
    assert c.wants_open


@pytest.mark.parametrize(
    "room,rad,reason",
    [(16, 25, "window_room_cold"), (20, 10, "window_radiator_cold"), (15, 9, "window_room_cold")],
)
def test_cold_boundaries_stop_pwm(room, rad, reason):
    c = Controller()
    tick(c, 0, rad=20)
    assert c.wants_open
    tick(c, 10, room=room, rad=rad)
    assert not c.wants_open and c.reason == reason
    assert c.reference is None and not c.fault


@pytest.mark.parametrize("kwargs", [{"enabled": False}, {"heat": False}, {"room": 24}])
def test_pwm_inhibitions(kwargs):
    c = Controller()
    tick(c, 0, rad=20)
    tick(c, 10, **kwargs)
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
    hass.states.async_set("sensor.radiator", 25, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    await r.set_enabled(True)
    assert not r.engine.wants_open and not calls
    hass.states.async_set("sensor.room", room, {"unit_of_measurement": "°C"}, force_update=True)
    hass.states.async_set("sensor.radiator", rad, {"unit_of_measurement": "°C"}, force_update=True)
    await hass.async_block_till_done()
    assert calls == [{"entity_id": "cover.window"}]
    assert r._window_open() is False
    assert not r.engine.fault
    await r._process()
    if r.engine.phase == Phase.COOLING:
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
    r.engine.trip("actuator_error", r.now())
    hass.states.async_set("sensor.room", 16, {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert r.engine.fault == "actuator_error"
    assert r.diagnostics()["window_status"] == "window_close_failed"
    await r._process()
    assert len(calls) == 1  # No command storm on the 5-second timer.
    await r.set_enabled(False)
    r._window_close_last = -float("inf")
    await r._process()
    assert len(calls) == 1
    await r.stop()


@pytest.mark.parametrize("initial_open", [False, True])
async def test_window_change_during_open_sequence_preserves_opening(
    hass, entry, devices, initial_open
):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set(
        "cover.window",
        "open" if initial_open else "closed",
        {"current_position": 30 if initial_open else 0},
    )
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
    hass.states.async_set(
        "cover.window",
        "closed" if initial_open else "open",
        {"current_position": 0 if initial_open else 30},
    )
    release.set()
    await enabling
    await hass.async_block_till_done()
    assert any(c[0] == "number" and c[2]["value"] == 21 for c in devices)
    assert r.actuator.commanded_open is True
    assert r.engine.phase == (Phase.CALIBRATING if initial_open else Phase.WINDOW_HEATING)
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
    assert r.actuator.commanded_open
    assert r.window_opening_allowed()
    assert r._window_deadline_cancel is not None
    for _ in range(14):
        clock[0] += 60
        await fresh(hass)
        await r._process()
        assert r.actuator.commanded_open
    clock[0] += 60
    await fresh(hass)
    await r._process()
    assert r.actuator.commanded_open is False
    assert r._window_deadline_cancel is None
    for _ in range(45):
        clock[0] += 60
        await fresh(hass)
        await r._process()
    assert r.actuator.commanded_open
    hass.states.async_set("cover.window", "unavailable")
    await hass.async_block_till_done()
    assert r.actuator.commanded_open is True
    assert r.engine._recovery_reason == "window_unavailable"
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


def test_plateau_learning_resumes_only_from_post_closure_samples():
    c = Controller()
    valid_reference(c, top=60)
    tick(c, 0, rad=20)
    tick(c, 60, rad=50)
    assert c.reference.temperature == 60 and not c._trace
    tick(c, 90, window=False, rad=20)
    assert c.wants_open and not c._trace
    for at in range(120, 691, 30):
        tick(c, at, window=False, rad=min(35, 20 + (at - 120) / 15))
        if not c.wants_open:
            break
    assert c.reason == "plateau" and c.reference.temperature == 35
    assert all(s.at >= 120 for s in c.reference.trace)
    assert not c.lags


def test_window_toggling_does_not_reset_open_duration_or_maximum_limit():
    c = Controller()
    tick(c, 0, rad=20)
    tick(c, 890, window=False, rad=25)
    assert c.wants_open and c.opened_at == 0
    tick(c, 899, rad=25)
    assert c.wants_open and c.window_deadline == 900
    tick(c, 900, rad=25)
    assert not c.wants_open


async def test_runtime_window_transitions_keep_valve_open_and_cancel_pwm_deadline(
    hass, entry, devices
):
    entry.options = {"window_cover": "cover.window"}
    hass.states.async_set("cover.window", "closed")
    r = Runtime(hass, entry)
    clock = [1000.0]
    r.now = lambda: clock[0]
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    original_open = r.engine.opened_at
    calls_before = len(devices)
    clock[0] += 60
    hass.states.async_set("cover.window", "open")
    await hass.async_block_till_done()
    assert r.engine.phase == Phase.WINDOW_HEATING
    assert r.engine.window_deadline == original_open + 900
    assert r.actuator.commanded_open and len(devices) == calls_before
    assert r._window_deadline_cancel is not None
    clock[0] += 60
    hass.states.async_set("cover.window", "closed")
    await hass.async_block_till_done()
    assert r.engine.phase == Phase.CALIBRATING
    assert r.engine.opened_at == original_open
    assert r.actuator.commanded_open and len(devices) == calls_before
    assert r._window_deadline_cancel is None
    await r.stop()


@pytest.mark.parametrize("close_window", [False, True])
def test_original_maximum_open_limit_survives_window_changes(close_window):
    c = Controller(Settings(max_open_seconds=420))
    tick(c, 0, window=False, rad=20)
    tick(c, 300, rad=25)
    assert c.window_deadline == 420
    tick(c, 419, window=not close_window, rad=25)
    assert c.wants_open
    tick(c, 420, window=not close_window, rad=25)
    assert not c.wants_open
    assert c.fault is None
    if close_window:
        assert c._recovery_reason == "maximum_open_duration"
