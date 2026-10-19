"""Critical valve failures alert once and request optional independent backup heat."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.config_flow import validate_emergency
from custom_components.heating_pulse_controller_v2.emergency import CONF_BACKUP, CONF_NOTIFY
from custom_components.heating_pulse_controller_v2.runtime import Runtime


async def configured(hass, entry, backups=("climate.backup_a", "climate.backup_b")):
    entry.options = {CONF_NOTIFY: "notify.mobile_app_test", CONF_BACKUP: list(backups)}
    pushes = []

    async def notify(call):
        pushes.append(dict(call.data))

    hass.services.async_register("notify", "mobile_app_test", notify)
    for entity_id in backups:
        hass.states.async_set(entity_id, "off", {"hvac_modes": ["off", "heat"], "temperature": 23})
    return pushes


async def critical(r, hass):
    r.engine.trip("actuator_error", r.now())
    await r._process()
    await hass.async_block_till_done()


async def expire_reconnect(r, hass):
    clock = [r.now()]
    r.now = lambda: clock[0]
    for _ in range(10):
        clock[0] += 60
        await r._process()
        await hass.async_block_till_done()


async def test_setup_is_passive_and_critical_fault_alerts_and_heats_backups_once(
    hass, entry, devices
):
    pushes = await configured(hass, entry)
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    assert devices == [] and pushes == []
    await r.set_enabled(True)
    await critical(r, hass)
    assert len(pushes) == 1 and "вентилем" in pushes[0]["message"]
    for e in entry.options[CONF_BACKUP]:
        assert hass.states.get(e).state == "heat"
        assert hass.states.get(e).attributes["temperature"] == 20
    count = len(devices)
    for _ in range(3):
        await r._process()
    await hass.async_block_till_done()
    assert len(pushes) == 1 and len(devices) == count
    assert r.diagnostics()["backup_requested"] == 2
    await r.stop()


async def test_data_recovery_does_not_trigger_emergency(hass, entry, devices):
    pushes = await configured(hass, entry)
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    hass.states.async_set("sensor.radiator", "unavailable")
    await hass.async_block_till_done()
    assert r.engine._recovery_reason == "missing_temperature"
    assert pushes == [] and not r.emergency.active
    assert hass.states.get("climate.backup_a").state == "off"
    await r.stop()


async def test_partial_backup_failure_retries_only_failed_head_and_respects_manual_override(
    hass, entry, devices
):
    pushes = await configured(hass, entry)
    hass.states.async_set("climate.backup_b", "unavailable")
    r = Runtime(hass, entry)
    clock = [1000.0]
    r.now = lambda: clock[0]
    await r.start()
    await fresh(hass)
    await critical(r, hass)
    assert r.diagnostics()["backup_requested"] == 1
    assert r.diagnostics()["backup_failed"] == 1
    hass.states.async_set("climate.backup_a", "off", {"temperature": 25})
    hass.states.async_set("climate.backup_b", "off", {"temperature": 22})
    await r._process()
    await hass.async_block_till_done()
    assert hass.states.get("climate.backup_b").state == "off"
    clock[0] += 60
    await r._process()
    await hass.async_block_till_done()
    assert hass.states.get("climate.backup_b").state == "heat"
    assert hass.states.get("climate.backup_a").state == "off"
    assert hass.states.get("climate.backup_a").attributes["temperature"] == 25
    assert len(pushes) == 1
    await r.stop()


async def test_failed_notification_does_not_block_backup_and_retries_without_spam(
    hass, entry, devices
):
    await configured(hass, entry)
    calls = []

    async def failing(call):
        calls.append(call)
        if len(calls) == 1:
            raise HomeAssistantError("push offline")

    hass.services.async_register("notify", "mobile_app_test", failing)
    r = Runtime(hass, entry)
    clock = [1000.0]
    r.now = lambda: clock[0]
    await r.start()
    await fresh(hass)
    await critical(r, hass)
    assert len(calls) == 1 and r.diagnostics()["backup_requested"] == 2
    await r._process()
    await hass.async_block_till_done()
    assert len(calls) == 1
    clock[0] += 60
    await r._process()
    await hass.async_block_till_done()
    assert len(calls) == 2 and r.diagnostics()["emergency_notification"] == "sent"
    await r.stop()


async def test_incident_survives_reload_passively_without_duplicate_push_or_commands(
    hass, entry, devices
):
    pushes = await configured(hass, entry)
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    await critical(r, hass)
    await r.stop()
    count = len(devices)
    d = Runtime(hass, entry)
    await d.start()
    await hass.async_block_till_done()
    assert d.engine.fault == "actuator_error" and not d.enabled
    assert len(pushes) == 1 and len(devices) == count
    await d.reset_fault()
    assert not d.emergency.active and not d.engine.fault
    assert hass.states.get("climate.backup_a").state == "heat"
    assert hass.states.get("climate.backup_a").attributes["temperature"] == 20
    await d.stop()


async def test_configured_notification_only_never_changes_climates(hass, entry, devices):
    pushes = await configured(hass, entry, backups=())
    r = Runtime(hass, entry)
    await r.start()
    await critical(r, hass)
    assert len(pushes) == 1 and devices == []
    await r.stop()


async def test_failed_explicit_enable_also_triggers_alarm(hass, entry, devices):
    pushes = await configured(hass, entry)
    r = Runtime(hass, entry)
    await r.start()
    hass.states.async_set("number.opening", "unavailable")
    with pytest.raises(HomeAssistantError):
        await r.set_enabled(True)
    await hass.async_block_till_done()
    assert not pushes and r.reconnect.active and r.engine.fault is None
    await expire_reconnect(r, hass)
    assert len(pushes) == 1 and r.diagnostics()["backup_requested"] == 2
    await r.stop()


async def test_missing_feedback_after_published_command_is_a_real_valve_fault(hass, entry, devices):
    pushes = await configured(hass, entry)

    async def no_feedback(call):
        pass  # Service accepts publication but number state never changes.

    hass.services.async_register("number", "set_value", no_feedback)
    r = Runtime(hass, entry)
    r.actuator.feedback_timeout = 0.01
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)
    await hass.async_block_till_done()
    assert r.reconnect.active and r.engine.fault is None and not pushes
    await expire_reconnect(r, hass)
    assert r.engine.fault == "actuator_error" and not r.enabled
    assert len(pushes) == 1 and r.diagnostics()["backup_requested"] == 2
    await r.stop()


async def test_optimistic_feedback_is_not_accepted(hass, entry, devices):
    await configured(hass, entry, backups=())
    hass.states.async_set("number.opening", 0, {"assumed_state": True})
    r = Runtime(hass, entry)
    await r.start()
    await fresh(hass)
    with pytest.raises(HomeAssistantError):
        await r.set_enabled(True)
    assert r.reconnect.active and r.engine.fault is None
    await r.stop()


async def test_configuration_rejects_primary_device_and_unsupported_targets(hass, entry, devices):
    from test_integration import setup_hass

    await setup_hass(hass)
    await configured(hass, entry)
    assert validate_emergency(hass, {**entry.data, **entry.options}) is None
    assert (
        validate_emergency(hass, {**entry.data, CONF_BACKUP: ["climate.trv"]})
        == "backup_is_primary"
    )
    assert validate_emergency(hass, {CONF_NOTIFY: "notify.missing"}) == "invalid_emergency_notify"
    hass.states.async_set("climate.cool_only", "off", {"hvac_modes": ["off", "cool"]})
    assert validate_emergency(hass, {CONF_BACKUP: ["climate.cool_only"]}) == "unsupported_backup"


async def test_corrupt_incident_does_not_actuate_on_setup(hass, entry, devices):
    pushes = await configured(hass, entry)
    r = Runtime(hass, entry)
    r.emergency.store.async_load = AsyncMock(
        return_value={"active": True, "id": [], "backups": None}
    )
    await r.start()
    await hass.async_block_till_done()
    assert not r.emergency.active and not devices and not pushes
    await r.stop()


async def test_missing_close_feedback_alarms_even_on_explicit_disable(hass, entry, devices):
    pushes = await configured(hass, entry)
    r = Runtime(hass, entry)
    r.actuator.feedback_timeout = 0.01
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)

    async def no_feedback(call):
        pass

    hass.services.async_register("number", "set_value", no_feedback)
    with pytest.raises(HomeAssistantError):
        await r.set_enabled(False)
    await hass.async_block_till_done()
    assert r.reconnect.active and r.engine.fault is None and not pushes
    await expire_reconnect(r, hass)
    assert r.engine.fault == "actuator_error" and not r.enabled
    assert len(pushes) == 1 and r.diagnostics()["backup_requested"] == 2
    await r.stop()


async def test_failed_notification_pending_incident_retries_after_restart(hass, entry, devices):
    await configured(hass, entry)
    attempts = []

    async def notify(call):
        attempts.append(call)
        if len(attempts) == 1:
            raise HomeAssistantError("temporary push failure")

    hass.services.async_register("notify", "mobile_app_test", notify)
    r = Runtime(hass, entry)
    r.now = lambda: 1000
    await r.start()
    await critical(r, hass)
    await r.stop()
    count = len(devices)
    d = Runtime(hass, entry)
    d.now = lambda: 2000
    await d.start()
    await hass.async_block_till_done()
    assert len(attempts) == 2
    assert d.diagnostics()["emergency_notification"] == "sent"
    assert len(devices) == count
    await d.stop()
