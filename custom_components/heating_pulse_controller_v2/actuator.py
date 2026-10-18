"""Sequenced Sonoff limits. All calls are owned by one runtime lock."""

import asyncio
import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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

    async def _call(self, domain: str, service: str, data: dict):
        async with asyncio.timeout(5):
            await self.hass.services.async_call(domain, service, data, blocking=True)

    async def _limit(self, key: str, value: float):
        await self._call(
            "number",
            "set_value",
            {
                "entity_id": self.config[key],
                "value": value,
            },
        )

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
