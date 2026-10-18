"""Timeouts/data failures must keep bounded maintenance heat, not latch off."""

from dataclasses import replace
from unittest.mock import AsyncMock

from test_engine import step, valid_reference
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.engine import Controller, Phase, Settings
from custom_components.heating_pulse_controller_v2.runtime import Runtime


def test_long_calibration_can_finish_after_old_fifteen_minute_limit():
    c = Controller()
    for at in range(0, 1201, 30):
        rad = min(43, 20 + at / 40)
        step(c, at, rad)
        if at == 900:
            assert c.phase == Phase.CALIBRATING
        if not c.wants_open:
            break
    assert 900 < at < 1200 and c.reason == "plateau" and c.fault is None


def test_repeated_missing_data_is_twenty_on_eighty_off_without_learning():
    c = Controller()
    for at in range(0, 18001, 60):
        c.evaluate(at, enabled=True, heat_mode=True, target=24)
        assert c.wants_open == (at % 6000 < 1200)
        assert c.fault is None and c.reference is None
        assert c.peak is None and not c.lags


def test_data_recovery_does_not_bypass_pause_then_recalibrates():
    c = Controller()
    step(c, 0, 20)
    step(c, 100, float("nan"))
    assert c.phase == Phase.RECOVERY_HEATING
    step(c, 1200, 30)
    assert c.phase == Phase.RECOVERY_PAUSE
    step(c, 3000, 20)
    assert not c.wants_open
    step(c, 6000, 20)
    assert c.phase == Phase.CALIBRATING and c.fault is None


def test_timeout_pause_survives_restart_without_reference():
    c = Controller()
    for at in range(0, 1201, 60):
        step(c, at, 20)
    stored = c.dump()
    d = Controller()
    d.restore(stored, 1500)
    assert d._recovery_next_open == 6000
    step(d, 5999, 20)
    assert not d.wants_open
    step(d, 6000, 20)
    assert d.phase == Phase.CALIBRATING


def test_shutdown_during_recovery_heating_preserves_remaining_pause():
    c = Controller()
    c.evaluate(0, enabled=True, heat_mode=True, target=24)
    c.evaluate(300, enabled=False, heat_mode=False, target=24)
    d = Controller()
    d.restore(c.dump(), 350)
    assert d._recovery_next_open == 5100
    step(d, 5099, 20)
    assert not d.wants_open
    step(d, 5100, 20)
    assert d.phase == Phase.CALIBRATING


def test_window_changes_do_not_cancel_timeout_backoff():
    c = Controller()
    for at in range(0, 1201, 60):
        step(c, at, 20)
    for at, opened in [(1300, True), (1400, False)]:
        c.room_report(20, at)
        c.radiator_report(25, at)
        c.evaluate(at, enabled=True, heat_mode=True, target=24, window_open=opened)
        assert not c.wants_open and c._recovery_next_open == 6000


def test_missing_sensors_with_open_window_use_fifteen_forty_five_and_manual_off_wins():
    c = Controller()
    for at in range(0, 3601, 60):
        c.evaluate(at, enabled=True, heat_mode=True, target=24, window_open=True)
        assert c.wants_open == (at % 3600 < 900)
    c.evaluate(3610, enabled=False, heat_mode=True, target=24, window_open=True)
    assert not c.wants_open and c.phase == Phase.OFF


def test_only_actuator_error_is_latched():
    c = Controller()
    step(c, 0, 20)
    c.trip("actuator_error", 30)
    step(c, 6000, 20)
    assert c.fault == "actuator_error" and not c.wants_open


def test_timeout_increase_preserves_reference_but_other_changes_do_not():
    c = Controller(Settings(max_open_seconds=900))
    valid_reference(c)
    d = Controller()
    assert d.restore(c.dump(), 100)
    assert d.reference.temperature == 50
    other = Controller(replace(Settings(), cooling_fraction=0.4))
    assert not other.restore(c.dump(), 100)


async def test_runtime_sensor_outage_keeps_timed_heat_and_actual_deadline(hass, entry, devices):
    r = Runtime(hass, entry)
    clock = [1000.0]
    r.now = lambda: clock[0]
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    clock[0] += 60
    hass.states.async_set("sensor.radiator", "unavailable")
    hass.states.async_set("sensor.room", "unavailable")
    await hass.async_block_till_done()
    assert r.engine.phase == Phase.RECOVERY_HEATING and r.actuator.commanded_open
    assert r.engine.fault is None and r.enabled
    assert r._window_deadline_cancel is not None
    clock[0] = 2200
    await r._process()
    assert not r.actuator.commanded_open and r.engine._recovery_next_open == 7000
    clock[0] = 7000
    await r._process()
    assert r.actuator.commanded_open and r.engine.fault is None
    await r.stop()


async def test_runtime_thermostat_outage_uses_last_target_but_off_is_respected(
    hass, entry, devices
):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    hass.states.async_set("climate.room", "unavailable")
    await hass.async_block_till_done()
    assert r.engine._recovery_reason == "thermostat_unavailable"
    assert r.actuator.commanded_open and r.enabled
    hass.states.async_set("climate.room", "off", {"temperature": 24})
    await hass.async_block_till_done()
    assert not r.actuator.commanded_open
    await r.stop()


async def test_runtime_unavailable_valve_is_the_critical_fault(hass, entry, devices):
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    hass.states.async_set("number.opening", "unavailable")
    await r._process()
    assert r.engine.fault == "actuator_error" and not r.enabled
    await r.stop()


async def test_runtime_clean_reload_retains_timeout_without_confirmed_peak(hass, entry, devices):
    r = Runtime(hass, entry)
    r.now = lambda: 1200
    for at in range(0, 1201, 60):
        step(r.engine, at, 20)
    r.store.async_save = AsyncMock()
    await r.stop()
    saved = r.store.async_save.call_args.args[0]
    d = Runtime(hass, entry)
    d.now = lambda: 1500
    d.store.async_load = AsyncMock(return_value=saved)
    d.store.async_save = AsyncMock()
    await d.start()
    await fresh(hass)
    await d.set_enabled(True)
    assert d.engine.phase == Phase.RECOVERY_PAUSE and not d.actuator.commanded_open
    await d.stop()
