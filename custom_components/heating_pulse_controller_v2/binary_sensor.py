"""Interlock for external automatic window-opening routines."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory

from .const import CONF_WINDOW
from .entity import PulseEntity


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data.config.get(CONF_WINDOW):
        async_add_entities([WindowOpeningAllowed(entry)])


class WindowOpeningAllowed(PulseEntity, BinarySensorEntity):
    _attr_name = "Window opening allowed"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry):
        super().__init__(entry, "window_opening_allowed")

    @property
    def is_on(self):
        return self.runtime.window_opening_allowed()
