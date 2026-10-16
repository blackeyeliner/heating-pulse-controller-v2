"""Small diagnostic entities; no recorder-heavy rolling traces."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory

from .entity import PulseEntity

SENSORS = {
    "phase": ("Phase", None),
    "reason": ("Last decision", None),
    "reference_temperature": ("Reference temperature", "°C"),
    "reference_age": ("Reference age", None),
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
        if self.key == "reference_age" and value is not None:
            minutes, seconds = divmod(max(0, int(value)), 60)
            return f"{minutes:02d}:{seconds:02d}"
        return round(value, 2) if isinstance(value, float) else value

    @property
    def extra_state_attributes(self):
        diagnostics = self.runtime.diagnostics()
        if self.key == "reference_age":
            return {"seconds": diagnostics["reference_age"]}
        if self.key == "restart_temperature":
            return {
                "cooling_fraction": diagnostics["cooling_fraction_used"],
                "room_trend": diagnostics["room_trend"],
            }
        return None
