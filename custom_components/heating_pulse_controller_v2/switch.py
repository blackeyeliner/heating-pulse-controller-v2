"""Explicit actuation enablement; starts off on every load."""

from homeassistant.components.switch import SwitchEntity

from .entity import PulseEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PulseSwitch(entry, "control")])


class PulseSwitch(PulseEntity, SwitchEntity):
    _attr_name = "Control enabled"
    _attr_icon = "mdi:radiator"

    @property
    def is_on(self):
        return self.runtime.control_requested

    async def async_turn_on(self, **kwargs):
        await self.runtime.set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.runtime.set_enabled(False)
