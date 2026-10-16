"""Small diagnostic entities; no recorder-heavy rolling traces."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory

from .entity import PulseEntity

SENSORS = {
    "phase": ("Phase", None),
    "reason": ("Last decision", None),
    "reference_temperature": ("Reference temperature", "°C"),
    "reference_age": ("Reference age", "s"),
    "closing_temperature": ("Closing temperature", "°C"),
    "restart_temperature": ("Restart temperature", "°C"),
    "lag_seconds": ("Residual warming delay", "s"),
    "trend": ("Radiator trend", "°C/min"),
    "peak_temperature": ("Last peak", "°C"),
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(PulseSensor(entry, key) for key in SENSORS)


class PulseSensor(PulseEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, key):
        super().__init__(entry, key)
        self.key = key
        self._attr_name, self._attr_native_unit_of_measurement = SENSORS[key]

    @property
    def native_value(self):
        value = self.runtime.diagnostics()[self.key]
        return round(value, 2) if isinstance(value, float) else value
