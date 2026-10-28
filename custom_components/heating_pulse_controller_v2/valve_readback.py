"""Correlated, read-only ZCL readback of both Sonoff valve limits."""

import asyncio
import json
import math
import re
import uuid

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from .const import CONF_CLOSING, CONF_OPENING, CONF_TRV

CLUSTER = 0xFC11
OPEN_ATTRIBUTE = 0x600B
CLOSE_ATTRIBUTE = 0x600C


def discovery_binding(payload):
    """Derive a bridge prefix and IEEE from discovery, never room-name guesses."""
    device = payload.get("device", payload.get("dev", {}))
    ids = device.get("identifiers", device.get("ids", []))
    if isinstance(ids, str):
        ids = [ids]
    matches = [re.fullmatch(r"zigbee2mqtt_(0x[0-9a-fA-F]{16})", x) for x in ids]
    addresses = {m[1].lower() for m in matches if m}
    availability = payload.get("availability", payload.get("avty", []))
    if isinstance(availability, dict):
        availability = [availability]
    topics = [x.get("topic", x.get("t", "")) for x in availability]
    if payload.get("availability_topic", payload.get("avty_t")):
        topics.append(payload.get("availability_topic", payload.get("avty_t")))
    bases = {
        x.removesuffix("/bridge/state")
        for x in topics
        if isinstance(x, str) and x.endswith("/bridge/state")
    }
    if len(addresses) != 1 or len(bases) != 1:
        raise HomeAssistantError("Cannot identify exact Zigbee2MQTT readback binding")
    base = bases.pop()
    if not base or any(x in base for x in ["+", "#"]):
        raise HomeAssistantError("Invalid Zigbee2MQTT bridge prefix")
    return base, addresses.pop()


def decode_pair(response, network_address, endpoint):
    """Validate the actual read response, not merged MQTT device-state fields."""
    try:
        data = response["data"]
        header = data["header"]
        if (
            response["status"] != "ok"
            or data["address"] != network_address
            or data["clusterID"] != CLUSTER
            or data["endpoint"] != endpoint
            or header["commandIdentifier"] != 1
            or header["frameControl"]["frameType"] != 0
            or header["frameControl"]["direction"] != 1
        ):
            raise ValueError("Unexpected ZCL response")
        raw = data["data"]
        if raw["type"] != "Buffer" or not isinstance(raw["data"], list):
            raise ValueError("Missing ZCL response bytes")
        values = raw["data"]
        if any(type(x) is not int or not 0 <= x <= 255 for x in values):
            raise ValueError("Invalid ZCL bytes")
        offset = 5 if values[0] & 4 else 3
        if (
            values[0] & 3
            or not values[0] & 8
            or values[offset - 1] != 1
            or values[offset - 2] != header["transactionSequenceNumber"]
        ):
            raise ValueError("Invalid read-response header")
        result = {}
        while offset < len(values):
            attribute = values[offset] | values[offset + 1] << 8
            status, kind, value = values[offset + 2 : offset + 5]
            if (
                attribute not in {OPEN_ATTRIBUTE, CLOSE_ATTRIBUTE}
                or attribute in result
                or status != 0
                or kind != 0x20
                or not 0 <= value <= 100
            ):
                raise ValueError("Invalid valve read record")
            result[attribute] = value
            offset += 5
        return result[OPEN_ATTRIBUTE], result[CLOSE_ATTRIBUTE]
    except (KeyError, TypeError, ValueError, IndexError) as err:
        raise HomeAssistantError("Invalid fresh valve-limit read response") from err


class PairReader:
    def __init__(self, hass, config):
        self.hass, self.config = hass, config
        self.timeout = 12

    def _binding(self):
        registry = er.async_get(self.hass)
        opening = registry.async_get(self.config[CONF_OPENING])
        if opening is None or opening.platform != "mqtt":
            return None
        if "mqtt" not in self.hass.config.components:
            raise HomeAssistantError("MQTT valve readback is not ready")
        others = [registry.async_get(self.config[k]) for k in (CONF_CLOSING, CONF_TRV)]
        if not opening.device_id or any(
            x is None or x.platform != "mqtt" or x.device_id != opening.device_id for x in others
        ):
            raise HomeAssistantError("Valve readback entities must share one MQTT device")
        from homeassistant.components.mqtt import debug_info

        info = debug_info.info_for_device(self.hass, opening.device_id)
        try:
            item = next(x for x in info["entities"] if x["entity_id"] == opening.entity_id)
            return discovery_binding(item["discovery_data"]["payload"])
        except (KeyError, TypeError, StopIteration) as err:
            raise HomeAssistantError("Valve MQTT discovery is not ready") from err

    async def _device(self, base, ieee):
        from homeassistant.components import mqtt

        received = asyncio.Event()
        found = None

        @callback
        def receive(message):
            nonlocal found
            try:
                devices = json.loads(message.payload)
                found = next(x for x in devices if x.get("ieee_address", "").lower() == ieee)
                received.set()
            except ValueError, TypeError, AttributeError, StopIteration:
                pass

        unsubscribe = await mqtt.async_subscribe(self.hass, base + "/bridge/devices", receive)
        try:
            async with asyncio.timeout(self.timeout):
                await received.wait()
            if (
                found.get("definition", {}).get("model") != "TRVZB"
                or type(found.get("network_address")) is not int
                or not 0 <= found["network_address"] < 0xFFF8
            ):
                raise HomeAssistantError("Unsupported valve readback device/address")
            endpoints = [
                int(k)
                for k, v in found["endpoints"].items()
                if "customSonoffTrvzb" in v.get("clusters", {}).get("input", [])
            ]
            if len(endpoints) != 1 or not 1 <= endpoints[0] <= 255:
                raise HomeAssistantError("Cannot resolve Sonoff valve endpoint")
            return found["network_address"], endpoints[0]
        finally:
            unsubscribe()

    async def _read(self, base, ieee, address, endpoint):
        from homeassistant.components import mqtt

        transaction = uuid.uuid4().hex
        received = asyncio.Event()
        reply = None

        @callback
        def receive(message):
            nonlocal reply
            if message.retain:
                return
            try:
                data = json.loads(message.payload)
                if data.get("transaction") == transaction:
                    reply = data
                    received.set()
            except ValueError, AttributeError, TypeError:
                pass

        unsubscribe = await mqtt.async_subscribe(
            self.hass, base + "/bridge/response/action", receive
        )
        try:
            request = {
                "transaction": transaction,
                "action": "raw",
                "params": {
                    "ieee_address": ieee,
                    "network_address": address,
                    "dst_endpoint": endpoint,
                    "cluster_key": CLUSTER,
                    "timeout": 8000,
                    "zcl": {
                        "frame_type": 0,
                        "direction": 0,
                        "command_key": "read",
                        "payload": [{"attrId": OPEN_ATTRIBUTE}, {"attrId": CLOSE_ATTRIBUTE}],
                    },
                },
            }
            await mqtt.async_publish(
                self.hass, base + "/bridge/request/action", json.dumps(request), qos=0, retain=False
            )
            async with asyncio.timeout(self.timeout):
                await received.wait()
            return decode_pair(reply, address, endpoint)
        finally:
            unsubscribe()

    async def read(self):
        """MQTT binding requires correlated ZCL proof; no cached-state fallback."""
        try:
            binding = self._binding()
            if binding is None:
                # Direct/non-MQTT adapters provide synchronous device state.
                result = tuple(
                    float(self.hass.states.get(self.config[k]).state)
                    for k in (CONF_OPENING, CONF_CLOSING)
                )
                if any(not math.isfinite(x) or not 0 <= x <= 100 for x in result):
                    raise ValueError("Invalid valve limits")
                return result
            base, ieee = binding
            address, endpoint = await self._device(base, ieee)
            return await self._read(base, ieee, address, endpoint)
        except (TimeoutError, AttributeError, KeyError, TypeError, ValueError) as err:
            raise HomeAssistantError("Fresh valve-limit readback unavailable") from err
