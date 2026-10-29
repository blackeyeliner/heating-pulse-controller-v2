"""Fresh correlated ZCL proof, independent of optimistic MQTT state."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.heating_pulse_controller_v2.valve_readback import (
    PairReader,
    ReadbackTimeout,
    decode_pair,
    discovery_binding,
)


def response(transaction="test", opening=21, closing=83):
    return {
        "transaction": transaction,
        "status": "ok",
        "data": {
            "address": 50467,
            "clusterID": 64529,
            "endpoint": 1,
            "header": {
                "commandIdentifier": 1,
                "transactionSequenceNumber": 82,
                "frameControl": {"frameType": 0, "direction": 1},
            },
            "data": {
                "type": "Buffer",
                "data": [24, 82, 1, 11, 96, 0, 32, opening, 12, 96, 0, 32, closing],
            },
        },
    }


def test_discovery_handles_custom_base_and_abbreviations_without_room_name_guess():
    payload = {
        "dev": {"ids": ["zigbee2mqtt_0xd44867fffe212209"]},
        "avty": [{"t": "house/z2m/bridge/state"}, {"t": "house/z2m/room/device/availability"}],
    }
    assert discovery_binding(payload) == ("house/z2m", "0xd44867fffe212209")
    with pytest.raises(HomeAssistantError):
        discovery_binding({**payload, "avty": [{"t": "house/+/bridge/state"}]})


@pytest.mark.parametrize(
    "change",
    [
        "status",
        "address",
        "cluster",
        "endpoint",
        "header",
        "sequence",
        "missing",
        "error_record",
        "duplicate",
        "type",
        "range",
    ],
)
def test_invalid_or_incomplete_device_responses_cannot_confirm_a_pair(change):
    x = response()
    d = x["data"]
    raw = d["data"]["data"]
    if change == "status":
        x["status"] = "error"
    elif change == "address":
        d["address"] = 1
    elif change == "cluster":
        d["clusterID"] = 6
    elif change == "endpoint":
        d["endpoint"] = 2
    elif change == "header":
        d["header"]["commandIdentifier"] = 0
    elif change == "sequence":
        raw[1] = 0
    elif change == "missing":
        del raw[-5:]
    elif change == "error_record":
        raw[5] = 1
    elif change == "duplicate":
        raw[8:10] = [11, 96]
    elif change == "type":
        raw[6] = 33
    elif change == "range":
        raw[7] = 101
    with pytest.raises(HomeAssistantError):
        decode_pair(x, 50467, 1)


def test_valid_pair_comes_from_response_bytes_not_cached_state():
    assert decode_pair(response(), 50467, 1) == (21, 83)
    assert decode_pair(response(opening=0, closing=100), 50467, 1) == (0, 100)


async def test_transaction_and_retained_filters_and_cleanup(hass, config, monkeypatch):
    from homeassistant.components import mqtt

    callback = None
    unsub = Mock()
    reader = PairReader(hass, config)
    reader.timeout = 0.02

    async def subscribe(_hass, topic, receive):
        nonlocal callback
        assert topic == "custom/base/bridge/response/action"
        callback = receive
        return unsub

    async def publish(_hass, topic, payload, **kwargs):
        assert topic == "custom/base/bridge/request/action" and kwargs["retain"] is False
        req = json.loads(payload)
        assert req["action"] == "raw" and req["params"]["zcl"]["command_key"] == "read"
        assert req["params"]["zcl"]["payload"] == [{"attrId": 0x600B}, {"attrId": 0x600C}]
        callback(SimpleNamespace(payload=json.dumps(response("wrong")), retain=False))
        callback(SimpleNamespace(payload=json.dumps(response(req["transaction"])), retain=True))
        callback(SimpleNamespace(payload="bad", retain=False))
        callback(SimpleNamespace(payload=json.dumps(response(req["transaction"])), retain=False))

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    assert await reader._read("custom/base", "0xd44867fffe212209", 50467, 1) == (21, 83)
    assert not reader._pending
    unsub.assert_not_called()
    await reader.stop()
    unsub.assert_called_once()


async def test_unrelated_and_retained_only_responses_time_out(hass, config, monkeypatch):
    from homeassistant.components import mqtt

    callback = None
    unsub = Mock()
    reader = PairReader(hass, config)
    reader.timeout = 0.01

    async def subscribe(_hass, topic, receive):
        nonlocal callback
        callback = receive
        return unsub

    async def publish(_hass, topic, payload, **kwargs):
        req = json.loads(payload)
        callback(SimpleNamespace(payload=json.dumps(response(req["transaction"])), retain=True))
        callback(SimpleNamespace(payload=json.dumps(response("other")), retain=False))

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    with pytest.raises(ReadbackTimeout, match="transaction="):
        await reader._read("base", "0xd44867fffe212209", 50467, 1)
    assert not reader._pending
    await reader.stop()
    unsub.assert_called_once()


async def test_device_lookup_uses_selected_ieee_and_sonoff_endpoint(hass, config, monkeypatch):
    from homeassistant.components import mqtt

    unsub = Mock()
    reader = PairReader(hass, config)

    async def subscribe(_hass, topic, receive):
        devices = [
            {
                "ieee_address": "0xd44867fffe212209",
                "network_address": 50467,
                "definition": {"model": "TRVZB"},
                "endpoints": {"1": {"clusters": {"input": ["customSonoffTrvzb"]}}},
            }
        ]
        receive(SimpleNamespace(payload=json.dumps(devices), retain=True))
        return unsub

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    assert await reader._device("base", "0xd44867fffe212209") == (50467, 1)
    unsub.assert_called_once()


async def test_mqtt_not_ready_cannot_fall_back_to_cached_values(hass, config, devices):
    from homeassistant.helpers import entity_registry as er
    from test_integration import setup_hass

    await setup_hass(hass)
    opening = er.async_get(hass).async_get_or_create("number", "mqtt", "opening")
    from custom_components.heating_pulse_controller_v2.const import CONF_OPENING

    reader = PairReader(hass, {**config, CONF_OPENING: opening.entity_id})
    with pytest.raises(HomeAssistantError, match="not ready"):
        await reader.read()


async def test_fast_reply_after_deferred_broker_subscription_and_repeated_reads(
    hass, config, monkeypatch
):
    """Reproduce HA's queued SUBSCRIBE and an immediate Z2M read response."""
    import asyncio

    from homeassistant.components import mqtt

    queued = {}
    active = {}
    unsubscribed = []
    subscriptions = []
    requests = []
    reader = PairReader(hass, config)
    reader.timeout = 0.05
    monkeypatch.setattr(reader, "_binding", lambda: ("base", "0xd44867fffe212209"))
    device = {
        "ieee_address": "0xd44867fffe212209",
        "network_address": 50467,
        "definition": {"model": "TRVZB"},
        "endpoints": {"1": {"clusters": {"input": ["customSonoffTrvzb"]}}},
    }

    def activate():
        batch = dict(queued)
        queued.clear()
        active.update(batch)
        if cb := batch.get("base/bridge/devices"):
            cb(SimpleNamespace(payload=json.dumps([device]), retain=True))

    async def subscribe(_hass, topic, receive):
        subscriptions.append(topic)
        queued[topic] = receive
        asyncio.get_running_loop().call_later(0.001, activate)

        def unsubscribe():
            unsubscribed.append(topic)
            active.pop(topic, None)
            queued.pop(topic, None)

        return unsubscribe

    async def publish(_hass, topic, payload, **kwargs):
        req = json.loads(payload)
        requests.append(req)
        # With beta25 ordering the response listener is only queued here,
        # so this real-time reply is lost even though Z2M reports success.
        if cb := active.get("base/bridge/response/action"):
            pair = (21, 83) if len(requests) == 1 else (0, 100)
            cb(
                SimpleNamespace(
                    payload=json.dumps(response(req["transaction"], *pair)), retain=False
                )
            )

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    assert await reader.read() == (21, 83)
    assert await reader.read() == (0, 100)
    assert requests[0]["transaction"] != requests[1]["transaction"]
    assert subscriptions.count("base/bridge/response/action") == 1
    assert "base/bridge/response/action" not in unsubscribed
    assert not reader._pending
    await reader.stop()
    assert unsubscribed.count("base/bridge/response/action") == 1
    assert not active and not queued


async def test_cancelled_read_and_late_reply_cannot_complete_next_transaction(
    hass, config, monkeypatch
):
    import asyncio

    from homeassistant.components import mqtt

    receive = None
    requests = []
    reader = PairReader(hass, config)
    unsubscribe = Mock()

    async def subscribe(_hass, topic, cb):
        nonlocal receive
        receive = cb
        return unsubscribe

    async def publish(_hass, topic, payload, **kwargs):
        req = json.loads(payload)
        requests.append(req)
        if len(requests) == 2:
            receive(
                SimpleNamespace(
                    payload=json.dumps(response(requests[0]["transaction"])), retain=False
                )
            )
            receive(
                SimpleNamespace(
                    payload=json.dumps(response(req["transaction"], 0, 100)), retain=False
                )
            )

    monkeypatch.setattr(mqtt, "async_subscribe", subscribe)
    monkeypatch.setattr(mqtt, "async_publish", publish)
    first = asyncio.create_task(reader._read("base", "0xd44867fffe212209", 50467, 1))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not reader._pending
    assert await reader._read("base", "0xd44867fffe212209", 50467, 1) == (0, 100)
    await reader.stop()
    unsubscribe.assert_called_once()
