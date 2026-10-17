"""A restart must preserve a validated cooling gate, never an opening permission."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from test_engine import step, valid_reference
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.engine import Controller, Phase
from custom_components.heating_pulse_controller_v2.runtime import Runtime


def cooled_controller():
    c = Controller()
    valid_reference(c)
    step(c, 1, 20)
    step(c, 31, 49)
    step(c, 61, 50)
    step(c, 121, 48)
    assert c.closed_at is None
    assert c.restart_temperature == 29
    return c


def test_round_trip_preserves_gate_and_waits_for_fresh_demand():
    c = cooled_controller()
    checkpoint = c.cooling_checkpoint(125)
    d = Controller()
    assert d.restore(c.dump(), 150)
    assert not d.restore_cooling(checkpoint, 150)  # no fresh reports
    d.room_report(20, 160)
    d.radiator_report(28, 160)
    assert d.restore_cooling(checkpoint, 160)
    assert d.restart_temperature == 29 and d.peak.value == 50
    assert d._cooling_required and not d.wants_open
    d.evaluate(160, enabled=False, heat_mode=True, target=24)
    assert not d.wants_open and d.restart_temperature == 29
    d.evaluate(161, enabled=True, heat_mode=True, target=24)
    assert d.phase == Phase.HEATING  # 28 > room+3, but below preserved gate


def test_restored_gate_does_not_restore_ready_latch_for_hot_radiator():
    c = cooled_controller()
    d = Controller()
    d.restore(c.dump(), 150)
    d.room_report(20, 160)
    d.radiator_report(35, 160)
    assert d.restore_cooling(c.cooling_checkpoint(125), 160)
    d.evaluate(160, enabled=True, heat_mode=True, target=24)
    assert d.phase == Phase.COOLING and d.restart_temperature == 29


@pytest.mark.parametrize(
    "change",
    [
        {"saved_at": -1000},
        {"saved_at": 10000},
        {"saved_at": "broken"},
        {"restart_temperature": 1000},
        {"restart_temperature": float("nan")},
        {"cooling_fraction": 1},
        {"peak": {"at": 130, "value": 50}},
        {"room": {"at": 200, "value": 20}},
        {"radiator": {"at": 10000, "value": 48}},
        {"radiator": {"at": -1000, "value": 48}},
        {"peak": {"at": 61, "value": 25}},
        {"peak": None},
    ],
)
def test_corrupt_or_expired_checkpoint_is_rejected(change):
    c = cooled_controller()
    data = {**c.cooling_checkpoint(125), **change}
    d = Controller()
    d.room_report(20, 160)
    d.radiator_report(28, 160)
    assert not d.restore_cooling(data, 160)
    assert d.restart_temperature is None


def test_warming_or_old_reports_invalidate_checkpoint():
    c = cooled_controller()
    data = c.cooling_checkpoint(125)
    d = Controller()
    d.room_report(20, 120)
    d.radiator_report(28, 120)
    assert not d.restore_cooling(data, 160)
    d.room_report(20, 160)
    d.radiator_report(49, 160)
    assert not d.restore_cooling(data, 160)


@pytest.mark.parametrize("case", ["heating", "settling", "startup", "window", "fault", "stale"])
def test_only_confirmed_cooling_can_be_saved(case):
    c = cooled_controller()
    if case == "heating":
        step(c, 151, 28)
        assert c.wants_open
    elif case == "settling":
        c.closed_at = 120
    elif case == "startup":
        c.peak = None
    elif case == "window":
        c.evaluate(125, enabled=True, heat_mode=True, target=24, window_open=True)
    elif case == "fault":
        c.trip("actuator_error", 125)
    assert c.cooling_checkpoint(1000 if case == "stale" else 155) is None


async def save_clean_episode(hass, entry):
    r = Runtime(hass, entry)
    r.engine = cooled_controller()
    r.now = lambda: 125
    r._owned = True
    r.actuator.commanded_open = False
    r.store.async_save = AsyncMock()
    await r.stop()
    return deepcopy(r.store.async_save.call_args.args[0])


async def test_runtime_clean_stop_restore_is_passive_and_consumes_checkpoint(hass, entry, devices):
    saved = await save_clean_episode(hass, entry)
    assert saved["cooling"]["restart_temperature"] == 29
    r = Runtime(hass, entry)
    r.now = lambda: 160
    r.store.async_load = AsyncMock(return_value=saved)
    r.store.async_save = AsyncMock()
    count = len(devices)
    await r.start()
    assert len(devices) == count
    assert "cooling" not in r.store.async_save.call_args.args[0]
    assert r.diagnostics()["cooling_restore_status"] == "waiting_for_fresh_reports"
    hass.states.async_set("sensor.radiator", 28, {"unit_of_measurement": "°C"})
    await fresh(hass)
    assert not r.enabled and len(devices) == count
    assert r.engine.restart_temperature == 29
    assert r.diagnostics()["cooling_restore_status"] == "restored"
    await r.set_enabled(True)
    assert r.actuator.commanded_open
    await r.stop()


@pytest.mark.parametrize("case", ["window", "bindings", "settings", "valve", "late"])
async def test_runtime_rejects_changed_environment(hass, entry, devices, case):
    saved = await save_clean_episode(hass, entry)
    entry.options = {"window_cover": "cover.window"}
    saved["cooling_context"]["window_cover"] = "cover.window"
    hass.states.async_set("cover.window", "open" if case == "window" else "closed")
    if case == "bindings":
        saved["cooling_context"]["room_sensor"] = "sensor.other"
    if case == "settings":
        saved["settings"]["cooling_fraction"] = 0.4
    if case == "valve":
        hass.states.async_set("number.opening", 21)
    r = Runtime(hass, entry)
    r.now = lambda: 1000 if case == "late" else 160
    r.store.async_load = AsyncMock(return_value=saved)
    r.store.async_save = AsyncMock()
    await r.start()
    await fresh(hass)
    assert r.diagnostics()["cooling_restore_status"] in [
        "discarded",
        "incompatible",
        "valve_not_closed",
    ]
    assert r.engine.restart_temperature != 29
    await r.stop()


async def test_unknown_window_during_startup_waits_for_feedback(hass, entry, devices):
    saved = await save_clean_episode(hass, entry)
    entry.options = {"window_cover": "cover.window"}
    saved["cooling_context"]["window_cover"] = "cover.window"
    hass.states.async_set("cover.window", "unavailable")
    r = Runtime(hass, entry)
    clock = [150]
    r.now = lambda: clock[0]
    r.store.async_load = AsyncMock(return_value=saved)
    r.store.async_save = AsyncMock()
    await r.start()
    await fresh(hass)
    assert r._pending_cooling
    clock[0] = 160
    hass.states.async_set("cover.window", "closed")
    await fresh(hass)
    assert r.engine.restart_temperature == 29
    assert not r.enabled and not r.engine.wants_open
    await r.stop()


async def test_failed_final_close_does_not_save_checkpoint(hass, entry, devices):
    from homeassistant.exceptions import HomeAssistantError

    r = Runtime(hass, entry)
    r.engine = cooled_controller()
    r.now = lambda: 125
    r._owned = True
    r.actuator.commanded_open = False
    r.actuator.close = AsyncMock(side_effect=HomeAssistantError("offline"))
    r.store.async_save = AsyncMock()
    await r.stop()
    assert "cooling" not in r.store.async_save.call_args.args[0]
