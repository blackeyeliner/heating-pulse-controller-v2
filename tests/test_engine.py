"""Physical sequence and control invariants, independent of HA."""

from dataclasses import replace

import pytest

from custom_components.heating_pulse_controller_v2.engine import (
    Controller,
    Phase,
    Reference,
    Sample,
    Settings,
    slope,
)


def step(c, t, rad, room=20, target=24, enabled=True, heat=True):
    c.room_report(room, t)
    c.radiator_report(rad, t)
    c.evaluate(t, enabled=enabled, heat_mode=heat, target=target)


def calibration(c, start=0, top=40):
    step(c, start, 20)
    for seconds in range(30, 601, 30):
        value = min(top, 20 + seconds / 15)
        step(c, start + seconds, value)
        if not c.wants_open:
            return start + seconds
    raise AssertionError("no plateau")


def valid_reference(c, at=0, top=50):
    c.reference = Reference(
        top, at, at - 30, [Sample(at - 330, 20), Sample(at - 150, top - 8), Sample(at, top)]
    )
    c._update_stop()


def test_cold_flat_is_not_plateau():
    c = Controller()
    for t in range(0, 600, 30):
        step(c, t, 20)
    assert c.phase == Phase.CALIBRATING
    assert c.reference is None


def test_plateau_calibrates_and_preserves_distinct_threshold():
    c = Controller()
    t = calibration(c)
    assert c.phase == Phase.SETTLING
    assert c.reference.temperature == 40
    assert 20 < c.stop_temperature < 40
    assert c.reference.measured_at == t
    assert c.reference.knee_at < t


def test_lower_plateau_replaces_reference_and_clock():
    c = Controller()
    valid_reference(c, top=60)
    old_stop = c.stop_temperature
    t = calibration(c, start=30, top=35)
    assert c.reference.temperature == 35
    assert c.reference.measured_at == t
    assert c.stop_temperature < old_stop


def test_normal_threshold_does_not_refresh_reference_or_infer_hotter_supply():
    c = Controller()
    valid_reference(c)
    step(c, 1, 20)
    assert c.phase == Phase.HEATING
    step(c, 31, 49)
    assert c.phase == Phase.SETTLING
    assert c.reference.temperature == 50
    assert c.reference.measured_at == 0


def test_expiry_waits_for_demand_and_bypasses_old_threshold():
    c = Controller()
    valid_reference(c)
    step(c, 10801, 20, target=20)
    assert not c.wants_open
    step(c, 10831, 20)
    assert c.phase == Phase.CALIBRATING
    step(c, 10861, 49)
    assert c.wants_open
    assert c.reference.measured_at == 0


def test_no_reopen_until_post_close_peak_and_cooling():
    c = Controller()
    valid_reference(c)
    step(c, 1, 20)
    step(c, 31, 43)
    assert c.phase == Phase.SETTLING
    step(c, 31.01, 43)
    assert not c.wants_open
    step(c, 61, 46)
    step(c, 91, 48)
    step(c, 121, 47)
    assert c.phase == Phase.SETTLING
    step(c, 151, 46)
    assert c.phase == Phase.COOLING
    assert c.lag == 60
    assert c.restart_temperature == pytest.approx(28.4)
    step(c, 181, 29)
    assert not c.wants_open
    step(c, 211, 28)
    assert c.wants_open
    assert c.reference.measured_at == 0


def test_off_on_does_not_bypass_cooling():
    c = Controller()
    step(c, 0, 20)
    step(c, 60, 35, heat=False)
    assert c.phase == Phase.OFF
    step(c, 90, 39)
    assert c.phase == Phase.SETTLING
    assert not c.wants_open


def test_satisfied_room_aborts_calibration_without_reference():
    c = Controller()
    step(c, 0, 20)
    step(c, 60, 30, room=24)
    assert c.reason == "room_satisfied"
    assert c.reference is None
    assert not c.wants_open


def test_ticks_without_reports_never_establish_plateau():
    c = Controller()
    step(c, 0, 20)
    for t in range(30, 301, 30):
        step(c, t, 20 + t / 15)
    for t in range(301, 481):
        c.evaluate(t, enabled=True, heat_mode=True, target=24)
    assert c.reference is None
    assert c.fault is None
    c.evaluate(482, enabled=True, heat_mode=True, target=24)
    assert c.fault == "stale_radiator_temperature"
    assert not c.wants_open


def test_timeout_and_fault_are_latched():
    c = Controller()
    for t in range(0, 901, 30):
        step(c, t, 20)
    assert c.fault == "maximum_open_duration"
    step(c, 930, 20)
    assert not c.wants_open
    c.reset_fault()
    step(c, 960, 20)
    assert c.wants_open


def test_declining_temperature_does_not_calibrate():
    c = Controller()
    for t, value in enumerate([20, 25, 30, 35, 40, 39, 38, 37, 36, 35]):
        step(c, t * 30, value)
    assert c.reference is None


def test_storage_restores_calibration_but_never_an_open_pulse():
    c = Controller()
    calibration(c)
    d = Controller()
    assert d.restore(c.dump(), 900)
    assert d.phase == Phase.OFF
    assert not d.wants_open
    assert d.stop_temperature == c.stop_temperature
    step(d, 930, 40)
    assert d.phase == Phase.COOLING
    assert not d.wants_open


@pytest.mark.parametrize("bad", [None, [], {}, {"schema": 99}, {"schema": 1, "settings": {}}])
def test_invalid_storage_is_discarded(bad):
    c = Controller()
    assert not c.restore(bad, 100)
    assert c.reference is None


def test_settings_change_invalidates_reference():
    c = Controller()
    calibration(c)
    d = Controller(replace(Settings(), cooling_fraction=0.4))
    assert not d.restore(c.dump(), 1000)


def test_corrupt_trace_cannot_restore_a_threshold():
    c = Controller()
    calibration(c)
    stored = c.dump()
    stored["reference"]["trace"][-1]["value"] = 500
    assert not Controller().restore(stored, 1000)


def test_regression_uses_irregular_timestamps():
    assert slope([Sample(1000, 20), Sample(1010, 21), Sample(1040, 24)]) == pytest.approx(6)


def test_minute_reports_can_confirm_plateau_without_fabricated_ticks():
    c = Controller()
    for t in range(0, 841, 60):
        step(c, t, min(40, 20 + t / 15))
        if not c.wants_open:
            break
    assert c.reference.temperature == 40
    assert c.reference.knee_at == 300
    assert c.stop_temperature == pytest.approx(32)


def test_gap_discards_earlier_warming_so_flat_after_gap_is_not_calibration():
    c = Controller()
    for t in range(0, 301, 30):
        step(c, t, 20 + t / 15)
    for t in range(450, 811, 30):
        step(c, t, 40)
    assert c.reference is None
    assert c.wants_open


def test_reference_expiring_during_pulse_does_not_extend_it():
    c = Controller()
    valid_reference(c)
    step(c, 10799, 20)
    step(c, 10829, 43)
    assert not c.wants_open
    assert c.reason == "closing_threshold"
    assert c.reference.measured_at == 0


def test_lag_outside_observed_trace_fails_closed():
    c = Controller(replace(Settings(), bootstrap_lag=300))
    calibration(c)
    assert c.fault == "unusable_calibration"
    assert not c.wants_open


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), 200])
def test_invalid_temperature_closes_existing_pulse(invalid):
    c = Controller()
    step(c, 0, 20)
    step(c, 30, invalid)
    assert c.fault == "missing_temperature"
    assert not c.wants_open


def test_clock_reversal_stops_open_pulse():
    c = Controller()
    step(c, 100, 20)
    c.evaluate(99, enabled=True, heat_mode=True, target=24)
    assert c.fault == "clock_moved_backwards"
    assert not c.wants_open


@pytest.mark.parametrize(
    "changes",
    [
        {"cooling_fraction": 1},
        {"max_open_seconds": 60},
        {"bootstrap_lag": 400},
        {"stop_error": 1},
        {"min_rise": float("nan")},
    ],
)
def test_invalid_settings(changes):
    with pytest.raises(ValueError):
        replace(Settings(), **changes)
