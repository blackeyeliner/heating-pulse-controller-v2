"""Ten spaced valve retries suppress infrastructure-restart false alarms."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_emergency import configured
from test_runtime import fresh

from custom_components.heating_pulse_controller_v2.runtime import Runtime


async def running(hass, entry):
    clock = [1000.0]
    r = Runtime(hass, entry)
    r.now = lambda: clock[0]
    await r.start()
    await fresh(hass)
    await r.set_enabled(True)

    # The runtime tests isolate the transport. Raw MQTT freshness has separate
    # adapter coverage below; close failures remain observable here.
    async def probe():
        await r.actuator.close()

    r.actuator.probe = AsyncMock(side_effect=probe)
    return r, clock


async def failed_close(r):
    r.actuator.close = AsyncMock(side_effect=HomeAssistantError("transport restarting"))
    await r._suspect_valve()


async def test_ten_minutes_ten_failed_probes_then_one_emergency(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    await failed_close(r)
    for n in range(1, 11):
        clock[0] = 1000 + n * 60 - 0.01
        await r._process()
        assert r.actuator.close.await_count == n - 1
        assert not pushes and not r.emergency.active and r.engine.fault is None
        clock[0] += 0.01
        await r._process()
        await hass.async_block_till_done()
        assert r.actuator.close.await_count == n
        if n < 10:
            assert r.reconnect.active and r.enabled and not pushes
            assert r.diagnostics()["phase"] == "valve_reconnecting"
            assert not r.engine.wants_open
    assert r.engine.fault == "actuator_error" and not r.enabled
    assert len(pushes) == 1 and r.diagnostics()["backup_requested"] == 2
    count = len(devices)
    await r._process()
    await hass.async_block_till_done()
    assert len(pushes) == 1 and len(devices) == count
    await r.stop()


async def test_service_recovery_automatically_resumes_enabled_control(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    actual = r.actuator.close
    await failed_close(r)
    clock[0] += 60
    await r._process()
    r.actuator.close = actual
    clock[0] += 60
    await fresh(hass)
    await r._process()
    assert not r.reconnect.active and not r.engine.fault and r.enabled
    assert r.actuator.commanded_open and not pushes
    await r.stop()


async def test_manual_off_during_reconnect_does_not_resume_heating(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    actual = r.actuator.close
    await failed_close(r)
    with pytest.raises(HomeAssistantError):
        await r.set_enabled(False)
    assert not r.enabled and r.reconnect.active
    r.actuator.close = actual
    clock[0] += 60
    await fresh(hass)
    await r._process()
    assert not r.reconnect.active and not r.enabled
    assert not r.actuator.commanded_open and not pushes
    await r.stop()


async def test_repeated_events_and_downtime_do_not_burst_or_reset_attempts(hass, entry, devices):
    await configured(hass, entry)
    r, clock = await running(hass, entry)
    await failed_close(r)
    for _ in range(20):
        await r._process()
        await r._suspect_valve()
    assert r.reconnect.record["started_at"] == 1000
    assert r.actuator.close.await_count == 0
    clock[0] += 300
    await r._process()
    assert r.actuator.close.await_count == 1
    assert r.reconnect.record["attempts"] == 1
    for _ in range(20):
        await r._process()
    assert r.actuator.close.await_count == 1
    await r.stop()


async def test_suspicion_survives_real_store_reload_but_enabled_does_not(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    await failed_close(r)
    clock[0] += 60
    await r._process()
    await r.stop()
    d = Runtime(hass, entry)
    d.now = lambda: clock[0]
    await d.start()

    async def probe():
        await d.actuator.close()

    d.actuator.probe = AsyncMock(side_effect=probe)
    assert d.reconnect.active and not d.enabled and not d.emergency.active
    assert d.reconnect.record["attempts"] == 1
    count = len(devices)
    await d._process()
    assert len(devices) == count
    clock[0] += 60
    await fresh(hass)
    await d._process()
    assert not d.reconnect.active and not d.enabled and not d.actuator.commanded_open
    assert not pushes
    await d.stop()


async def test_unavailable_entity_is_not_recovered_by_successful_cached_close(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    hass.states.async_set("climate.trv", "unavailable")
    await r._process()
    clock[0] += 60
    await r._process()
    assert r.reconnect.active and r.reconnect.record["attempts"] == 1
    assert r.engine.fault is None and not pushes
    hass.states.async_set("climate.trv", "off", {"hvac_modes": ["off", "heat"]})
    clock[0] += 60
    await fresh(hass)
    await r._process()
    assert not r.reconnect.active and r.enabled and not r.engine.fault
    await r.stop()


async def test_reappearing_states_do_not_bypass_failed_command_probe(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    await failed_close(r)
    clock[0] += 60
    await r._process()
    assert r.reconnect.active and not pushes
    await r.stop()


async def test_legacy_writer_blocks_probe_and_normal_open(hass, entry, devices):
    await configured(hass, entry)
    r, clock = await running(hass, entry)
    await failed_close(r)
    hass.states.async_set("automation.old_controller", "on")
    clock[0] += 60
    await r._process()
    assert r.actuator.close.await_count == 0
    assert not r._permitted()
    hass.states.async_set("automation.old_controller", "off")
    await r._process()
    assert r.actuator.close.await_count == 1
    await r.stop()


async def test_tenth_probe_can_succeed_without_emergency(hass, entry, devices):
    pushes = await configured(hass, entry)
    r, clock = await running(hass, entry)
    actual = r.actuator.close
    await failed_close(r)
    for _ in range(9):
        clock[0] += 60
        await r._process()
    r.actuator.close = actual
    clock[0] += 60
    await fresh(hass)
    await r._process()
    assert not r.reconnect.active and r.enabled and not pushes
    await r.stop()


@pytest.mark.parametrize("reply", ["fresh", "retained", "none", "malformed", "open"])
async def test_probe_requires_live_closed_mqtt_feedback(hass, entry, devices, reply):
    from types import SimpleNamespace

    from custom_components.heating_pulse_controller_v2.actuator import SonoffActuator

    a = SonoffActuator(hass, entry.data)
    a.feedback_timeout = 0.01
    called = []

    def unsub():
        called.append(True)

    async def subscribe(receive):
        async def close():
            if reply == "none":
                return
            payload = (
                "bad json"
                if reply == "malformed"
                else '{"valve_opening_degree":21,"valve_closing_degree":83}'
                if reply == "open"
                else '{"valve_opening_degree":0,"valve_closing_degree":100}'
            )
            receive(SimpleNamespace(payload=payload, retain=reply == "retained"))

        a.close = AsyncMock(side_effect=close)
        return unsub

    a._subscribe_probe = AsyncMock(side_effect=subscribe)
    if reply == "fresh":
        await a.probe()
    else:
        with pytest.raises(HomeAssistantError, match="No live MQTT"):
            await a.probe()
    assert called == [True]


async def test_probe_unsubscribes_on_failed_close(hass, entry, devices):
    from unittest.mock import Mock

    from custom_components.heating_pulse_controller_v2.actuator import SonoffActuator

    a = SonoffActuator(hass, entry.data)
    unsub = Mock()
    a._subscribe_probe = AsyncMock(return_value=unsub)
    a.close = AsyncMock(side_effect=HomeAssistantError("transport offline"))
    with pytest.raises(HomeAssistantError):
        await a.probe()
    unsub.assert_called_once()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"attempts": True},
        {"attempts": 11},
        {"started_at": float("nan")},
        {"started_at": 2000},
        {"context": {}},
        {"next_attempt_at": 900},
    ],
)
async def test_invalid_pending_state_never_starts_commands(hass, entry, devices, bad):
    from custom_components.heating_pulse_controller_v2.reconnect import Reconnect

    c = Reconnect(hass, entry, entry.data, lambda: 1000)
    valid = {"context": c.context, "attempts": 0, "started_at": 900, "next_attempt_at": 960}
    value = None if bad is None else {**valid, **bad}
    if bad == {}:
        value = {}
    c.store.async_load = AsyncMock(return_value=value)
    await c.load()
    assert not c.active and not devices


@pytest.mark.parametrize(
    "payload,topic",
    [
        ({"state_topic": "zigbee2mqtt/Valve"}, "zigbee2mqtt/Valve"),
        ({"stat_t": "~/state", "~": "custom/Valve"}, "custom/Valve/state"),
        ({}, None),
        ({"state_topic": "zigbee2mqtt/+"}, None),
    ],
)
async def test_probe_topic_comes_from_selected_entity_discovery(
    hass, entry, devices, monkeypatch, payload, topic
):
    import sys
    from types import ModuleType, SimpleNamespace
    from unittest.mock import Mock

    from homeassistant.helpers import entity_registry as er

    from custom_components.heating_pulse_controller_v2.actuator import SonoffActuator

    mqtt = ModuleType("homeassistant.components.mqtt")
    unsub = Mock()
    mqtt.async_subscribe = AsyncMock(return_value=unsub)
    mqtt.debug_info = SimpleNamespace(
        info_for_device=Mock(
            return_value={
                "entities": [
                    {"entity_id": "number.opening", "discovery_data": {"payload": payload}}
                ]
            }
        )
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.mqtt", mqtt)
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get=lambda entity: SimpleNamespace(
                entity_id=entity, platform="mqtt", device_id="selected_device"
            )
        ),
    )
    a = SonoffActuator(hass, entry.data)
    receive = Mock()
    if topic:
        assert await a._subscribe_probe(receive) is unsub
        mqtt.async_subscribe.assert_awaited_once_with(hass, topic, receive, qos=0)
    else:
        with pytest.raises(HomeAssistantError):
            await a._subscribe_probe(receive)
        mqtt.async_subscribe.assert_not_called()


async def test_pending_store_failure_keeps_live_spacing(hass, entry, devices):
    r, clock = await running(hass, entry)
    r.reconnect.store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    await failed_close(r)
    clock[0] += 60
    await r._process()
    await r._process()
    assert r.reconnect.record["attempts"] == 1
    assert r.actuator.close.await_count == 1
    await r.stop()


async def test_hung_transport_probe_is_bounded_and_keeps_retry_spacing(
    hass, entry, devices, monkeypatch
):
    import asyncio

    import custom_components.heating_pulse_controller_v2.runtime as runtime

    monkeypatch.setattr(runtime, "VALVE_PROBE_TIMEOUT", 0.01)
    r, clock = await running(hass, entry)
    await failed_close(r)

    async def hung():
        await asyncio.Event().wait()

    r.actuator.probe = AsyncMock(side_effect=hung)
    clock[0] += 60
    await r._process()
    assert r.reconnect.active and r.reconnect.record["attempts"] == 1
    await r._process()
    assert r.actuator.probe.await_count == 1
    await r.stop()
