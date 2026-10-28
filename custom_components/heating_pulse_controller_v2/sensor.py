"""Small diagnostic entities; no recorder-heavy rolling traces."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory

from .const import CONF_WINDOW
from .entity import PulseEntity

SENSORS = {
    "valve_position": ("Valve position", "%"),
    "phase": ("Phase", None),
    "reason": ("Last decision", None),
    "reference_temperature": ("Reference temperature", "°C"),
    "reference_age": ("Reference age", "min"),
    "closing_temperature": ("Closing temperature", "°C"),
    "restart_temperature": ("Restart temperature", "°C"),
    "lag_seconds": ("Residual warming delay", "s"),
    "trend": ("Radiator trend", "°C/min"),
    "peak_temperature": ("Last peak", "°C"),
    "window_status": ("Window status", None),
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        PulseSensor(entry, key)
        for key in SENSORS
        if (key != "window_status" or entry.runtime_data.config.get(CONF_WINDOW))
        and (key != "valve_position" or entry.runtime_data.native)
    )


class PulseSensor(PulseEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, key):
        super().__init__(entry, key)
        self.key = key
        self._attr_name, self._attr_native_unit_of_measurement = SENSORS[key]
        if key == "reference_age":
            self._attr_device_class = SensorDeviceClass.DURATION
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self):
        value = self.runtime.diagnostics()[self.key]
        if self.key == "reference_age" and value is not None:
            return round(max(0, value) / 60, 2)
        return round(value, 2) if isinstance(value, float) else value

    @property
    def extra_state_attributes(self):
        diagnostics = self.runtime.diagnostics()
        if self.key == "valve_position":
            return {
                "source": "trv_running_state"
                if self.runtime._off_protection_ready
                else "pulse_command",
                "estimated_opening_percent": diagnostics["estimated_valve_opening"],
                "position_measured": False,
            }
        if self.key == "phase":
            return {
                key: diagnostics[key]
                for key in (
                    "valve_pair_verified_at",
                    "valve_pair_readback",
                    "valve_pair_corrections",
                    "recovery_reason",
                    "recovery_resume_at",
                    "max_open_seconds",
                    "window_pwm_open_seconds",
                    "window_pwm_closed_seconds",
                    "valve_reconnecting",
                    "valve_reconnect_attempts",
                    "valve_reconnect_limit",
                    "valve_reconnect_started_at",
                    "valve_reconnect_next_attempt_at",
                    "emergency_active",
                    "emergency_notification",
                    "backup_selected",
                    "backup_requested",
                    "backup_failed",
                )
            }
        if self.key == "reference_age":
            age = diagnostics["reference_age"]
            formatted = None
            if age is not None:
                minutes, seconds = divmod(max(0, int(age)), 60)
                formatted = f"{minutes:02d}:{seconds:02d}"
            return {"seconds": age, "formatted": formatted}
        if self.key == "restart_temperature":
            return {
                "cooling_fraction": diagnostics["cooling_fraction_used"],
                "room_trend": diagnostics["room_trend"],
                "restore_status": diagnostics["cooling_restore_status"],
                "source": diagnostics["restart_source"],
            }
        return None
