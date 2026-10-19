"""Persist the native room request independently of radiator calibration."""

import math

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN

CONF_NATIVE = "native_thermostat"


class RoomRequest:
    def __init__(self, hass, entry):
        self.store = Store(hass, 1, f"{DOMAIN}.thermostat.{entry.entry_id}")
        self.mode = "off"
        self.target = 21.0
        self.entity_id = None

    @staticmethod
    def validate(mode, target):
        if mode not in {"heat", "off"}:
            raise HomeAssistantError("Only heat and off are supported")
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise HomeAssistantError("Temperature must be a number")
        if not math.isfinite(target) or not 4 <= target <= 35:
            raise HomeAssistantError("Temperature must be between 4 and 35 °C")

    async def load(self):
        data = await self.store.async_load()
        if not isinstance(data, dict):
            return
        try:
            self.validate(data["mode"], data["target"])
        except KeyError, HomeAssistantError:
            return
        self.mode, self.target = data["mode"], float(data["target"])

    async def set(self, mode, target):
        self.validate(mode, target)
        self.mode, self.target = mode, float(target)
        await self.store.async_save({"mode": self.mode, "target": self.target})
