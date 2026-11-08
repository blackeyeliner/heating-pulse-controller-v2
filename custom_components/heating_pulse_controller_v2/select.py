"""Per-room maximum duty cycle for ordinary pulses."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.util import slugify

from .entity import PulseEntity

CHOICES = {"No limit": 100, "50%": 50, "33%": 33, "25%": 25}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PulseDutySelect(entry, "max_duty")])


class PulseDutySelect(PulseEntity, SelectEntity):
    _attr_name = "Maximum duty cycle"
    _attr_icon = "mdi:percent-circle-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(CHOICES)

    def __init__(self, entry, key):
        super().__init__(entry, key)
        # Registered IDs follow the room, not a renamed device name.
        self.entity_id = (
            f"select.{slugify(entry.title)}_heating_pulse_controller_v2_maximum_duty_cycle"
        )

    @property
    def current_option(self):
        value = self.runtime.engine.settings.max_duty_percent
        # A custom value from the options form has no dropdown entry.
        return next((label for label, v in CHOICES.items() if v == value), None)

    async def async_select_option(self, option: str):
        await self.runtime.set_max_duty(CHOICES[option])
