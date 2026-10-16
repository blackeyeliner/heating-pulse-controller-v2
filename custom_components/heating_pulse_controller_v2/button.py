"""Explicit recovery and recalibration controls."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .entity import PulseEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            PulseButton(entry, "reset_fault", "Reset fault"),
            PulseButton(entry, "reset_reference", "Reset calibration"),
        ]
    )


class PulseButton(PulseEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry, key, name):
        super().__init__(entry, key)
        self.key = key
        self._attr_name = name

    async def async_press(self):
        await getattr(self.runtime, self.key)()
