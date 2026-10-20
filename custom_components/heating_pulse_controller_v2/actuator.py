"""Sequenced Sonoff limits. All calls are owned by one runtime lock."""

import asyncio
import json
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    CONF_CLOSE_VALUE,
    CONF_CLOSING,
    CONF_OPEN_VALUE,
    CONF_OPENING,
    CONF_OUTPUT,
    CONF_TRV,
)

_LOGGER = logging.getLogger(__name__)


class SonoffActuator:
    def __init__(self, hass: HomeAssistant, config: dict):
        self.hass = hass
        self.config = config
        self.commanded_open: bool | None = None
        self.feedback_timeout = 15
        self.physical_controls = None

    async def _call(self, domain: str, service: str, data: dict):
        if self.physical_controls is not None and data.get("entity_id") == self.config[CONF_TRV]:
            state = (
                self.physical_controls.baseline
                if self.physical_controls.mqtt and self.physical_controls.baseline is not None
                else self.hass.states.get(self.config[CONF_TRV])
            )
            if (
                service == "set_hvac_mode"
                and state is not None
                and state.state != data["hvac_mode"]
            ):
                self.physical_controls.expect("mode", data["hvac_mode"])
            elif (
                service == "set_temperature"
                and state is not None
                and state.attributes.get("temperature") != data["temperature"]
            ):
                self.physical_controls.expect("target", data["temperature"])
        async with asyncio.timeout(5):
            await self.hass.services.async_call(domain, service, data, blocking=True)

    async def _limit(self, key: str, value: float):
        entity_id = self.config[key]
        current = self.hass.states.get(entity_id)
        if current and current.attributes.get("assumed_state"):
            raise HomeAssistantError("Optimistic valve limits cannot confirm control delivery")
        received = asyncio.Event()

        @callback
        def check(_event=None):
            state = self.hass.states.get(entity_id)
            try:
                matches = (
                    state is not None
                    and not state.attributes.get("assumed_state")
                    and float(state.state) == value
                )
            except TypeError, ValueError:
                matches = False
            if matches:
                received.set()

        unsubscribe = async_track_state_change_event(self.hass, [entity_id], check)
        try:
            await self._call("number", "set_value", {"entity_id": entity_id, "value": value})
            check()
            async with asyncio.timeout(self.feedback_timeout):
                await received.wait()
        except TimeoutError as err:
            raise HomeAssistantError(
                "Valve limit feedback did not confirm the requested value"
            ) from err
        finally:
            unsubscribe()

    async def _subscribe_probe(self, receive):
        """Use the existing MQTT discovery state topic; never guess a device topic."""
        try:
            from homeassistant.components import mqtt
            from homeassistant.components.mqtt import debug_info
        except ImportError as err:
            raise HomeAssistantError("MQTT integration is unavailable for valve probe") from err
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(self.hass).async_get(self.config[CONF_OPENING])
        if entry is None or entry.platform != "mqtt" or not entry.device_id:
            raise HomeAssistantError("Cannot find MQTT valve feedback binding")
        try:
            info = debug_info.info_for_device(self.hass, entry.device_id)
            item = next(e for e in info["entities"] if e["entity_id"] == entry.entity_id)
            payload = item["discovery_data"]["payload"]
            topic = payload.get("state_topic", payload.get("stat_t"))
            if isinstance(topic, str) and topic.startswith("~/"):
                topic = payload["~"] + topic[1:]
            if not isinstance(topic, str) or not topic or "+" in topic or "#" in topic:
                raise ValueError("Missing exact MQTT state topic")
        except (AttributeError, KeyError, TypeError, ValueError, StopIteration) as err:
            raise HomeAssistantError("Cannot identify MQTT valve feedback topic") from err
        return await mqtt.async_subscribe(self.hass, topic, receive, qos=0)

    async def probe(self):
        """Confirm a live non-retained MQTT reply to safe closing commands.

        Cached no-op HA number states alone must not end a reconnect episode.
        Sonoff/Z2M publishes numeric state after awaiting the Zigbee write.
        """
        received = asyncio.Event()

        @callback
        def receive(message):
            if message.retain:
                return
            try:
                data = json.loads(message.payload)
                if (
                    float(data["valve_opening_degree"]) == 0
                    and float(data["valve_closing_degree"]) == 100
                ):
                    received.set()
            except ValueError, TypeError, KeyError:
                pass

        unsubscribe = await self._subscribe_probe(receive)
        try:
            await self.close()
            async with asyncio.timeout(self.feedback_timeout):
                await received.wait()
        except TimeoutError as err:
            raise HomeAssistantError("No live MQTT valve reply to reconnect probe") from err
        finally:
            unsubscribe()

    async def _mirror(self, value: int):
        if self.config.get(CONF_OUTPUT):
            try:
                await self._call(
                    "input_number",
                    "set_value",
                    {
                        "entity_id": self.config[CONF_OUTPUT],
                        "value": value,
                    },
                )
            except HomeAssistantError, TimeoutError:
                _LOGGER.warning("Valve history helper update failed; valve delivery is unaffected")

    async def close(self):
        """Try both closing commands even if one fails. Never claim physical proof."""
        errors = []
        for key, value in [(CONF_OPENING, 0), (CONF_CLOSING, 100)]:
            try:
                await self._limit(key, value)
            except (HomeAssistantError, TimeoutError) as err:
                errors.append(err)
            if key == CONF_OPENING:
                await asyncio.sleep(0.3)
        if errors:
            self.commanded_open = None
            # Best-effort extra stop through the physical thermostat.
            try:
                await self._call(
                    "climate",
                    "set_hvac_mode",
                    {
                        "entity_id": self.config[CONF_TRV],
                        "hvac_mode": "off",
                    },
                )
            except HomeAssistantError, TimeoutError:
                _LOGGER.exception("TRV fallback stop also failed")
            raise HomeAssistantError("Unable to confirm delivery of close commands") from errors[0]
        self.commanded_open = False
        await self._mirror(0)

    async def frost_protection(self):
        """Hand off to firmware Off protection without a zero opening limit."""
        await self.close()
        confirmed = asyncio.Event()

        @callback
        def check(_event=None):
            state = self.hass.states.get(self.config[CONF_TRV])
            if state is not None and state.state == "off":
                confirmed.set()

        unsubscribe = async_track_state_change_event(self.hass, [self.config[CONF_TRV]], check)
        try:
            await self._call(
                "climate",
                "set_hvac_mode",
                {"entity_id": self.config[CONF_TRV], "hvac_mode": "off"},
            )
            check()
            async with asyncio.timeout(self.feedback_timeout):
                await confirmed.wait()
        finally:
            unsubscribe()
        await self._limit(CONF_OPENING, self.config[CONF_OPEN_VALUE])
        # Firmware may now open for frost protection; do not claim physical closure.
        self.commanded_open = None

    async def sync_heat(self, target, permitted):
        """Keep local display usable even between pulses, with closed limits."""
        for field, service, data in [
            ("temperature", "set_temperature", {"temperature": target}),
            ("state", "set_hvac_mode", {"hvac_mode": "heat"}),
        ]:
            if not permitted():
                return
            state = self.hass.states.get(self.config[CONF_TRV])
            current = (
                state.state
                if field == "state" and state
                else state.attributes.get(field)
                if state
                else None
            )
            desired = "heat" if field == "state" else target
            if current != desired:
                await self._call("climate", service, {"entity_id": self.config[CONF_TRV], **data})

    async def open(self, target: float, permitted: Callable[[], bool]):
        """Recheck inhibition between awaits so a queued off always wins."""
        if not permitted():
            return
        try:
            # Establish closed limits before touching physical thermostat mode.
            await self.close()
            commands = [
                (
                    "climate",
                    "set_temperature",
                    {"entity_id": self.config[CONF_TRV], "temperature": target},
                ),
                (
                    "climate",
                    "set_hvac_mode",
                    {"entity_id": self.config[CONF_TRV], "hvac_mode": "heat"},
                ),
                (
                    "number",
                    "set_value",
                    {"entity_id": self.config[CONF_OPENING], "value": self.config[CONF_OPEN_VALUE]},
                ),
                (
                    "number",
                    "set_value",
                    {
                        "entity_id": self.config[CONF_CLOSING],
                        "value": self.config[CONF_CLOSE_VALUE],
                    },
                ),
            ]
            for domain, service, data in commands:
                if not permitted():
                    await self.close()
                    return
                if domain == "number":
                    key = (
                        CONF_OPENING
                        if data["entity_id"] == self.config[CONF_OPENING]
                        else CONF_CLOSING
                    )
                    await self._limit(key, data["value"])
                else:
                    await self._call(domain, service, data)
                if domain == "number" and data["entity_id"] == self.config[CONF_OPENING]:
                    await asyncio.sleep(0.3)
            if not permitted():
                await self.close()
                return
            self.commanded_open = True
            await self._mirror(5)
        except HomeAssistantError, TimeoutError, asyncio.CancelledError:
            await self.close()
            raise
