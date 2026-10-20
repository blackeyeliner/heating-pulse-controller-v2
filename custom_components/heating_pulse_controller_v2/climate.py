"""One native room thermostat, driven directly by its pulse runtime."""

import math

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_ROOM
from .entity import PulseEntity


async def async_setup_entry(hass, entry, async_add_entities):
    if entry.runtime_data.native:
        async_add_entities([PulseClimate(entry)])


class PulseClimate(PulseEntity, ClimateEntity):
    _attr_name = "Heating"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_min_temp = 4
    _attr_max_temp = 35
    _attr_target_temperature_step = 0.1
    _attr_precision = 0.1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, entry):
        super().__init__(entry, "climate")

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.runtime.request.entity_id = self.entity_id

    @property
    def current_temperature(self):
        state = self.hass.states.get(self.runtime.config[CONF_ROOM])
        try:
            value = float(state.state)
            if state.attributes.get("unit_of_measurement") != UnitOfTemperature.CELSIUS:
                return None
            return value if math.isfinite(value) else None
        except AttributeError, ValueError, TypeError:
            return None

    @property
    def target_temperature(self):
        return (
            self.runtime.frost_temperature
            if self.runtime.request.mode == "off"
            else self.runtime.request.target
        )

    @property
    def target_temperature_step(self):
        if self.runtime.request.mode == "off" and self.runtime.frost_entity:
            state = self.hass.states.get(self.runtime.frost_entity)
            if state is not None:
                return state.attributes.get("step", 0.5)
        return self._attr_target_temperature_step

    @property
    def hvac_mode(self):
        # Show persisted control intent while startup checks are pending.
        return (
            HVACMode.HEAT
            if self.runtime.control_requested and self.runtime.request.mode == "heat"
            else HVACMode.OFF
        )

    @property
    def hvac_action(self):
        r = self.runtime
        if r.engine.fault or r.reconnect.active or r.control_requested and not r.enabled:
            return None  # Do not claim a closed valve when its state is uncertain.
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if r.actuator.commanded_open is True else HVACAction.IDLE

    @property
    def extra_state_attributes(self):
        return {
            "requested_hvac_mode": self.runtime.request.mode,
            "control_enabled": self.runtime.control_requested,
            "controller_phase": self.runtime.diagnostics()["phase"],
            "heating_target_temperature": self.runtime.request.target,
            "frost_protection_temperature": self.runtime.frost_temperature,
            "frost_protection_active": self.runtime._off_protection_ready,
        }

    async def async_set_temperature(self, **kwargs):
        mode = kwargs.get("hvac_mode", self.runtime.request.mode)
        target = kwargs.get(ATTR_TEMPERATURE, self.runtime.request.target)
        self.runtime.request.validate(mode, target)
        if mode == HVACMode.OFF and ATTR_TEMPERATURE in kwargs:
            entity = self.runtime.frost_entity
            if entity is None or self.runtime.frost_temperature is None:
                raise HomeAssistantError("Frost protection temperature is unavailable")
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": entity, "value": target}, blocking=True
            )
            target = self.runtime.request.target
        if kwargs.get("hvac_mode") == HVACMode.OFF:
            self.runtime.enabled = False
            self.runtime._resume_pending = False
        await self.runtime.request.set(mode, target)
        if "hvac_mode" in kwargs:
            await self.runtime.set_enabled(
                mode == HVACMode.HEAT, frost_protection=mode == HVACMode.OFF
            )
        else:
            await self.runtime._process()
        self.runtime._notify()

    async def async_set_hvac_mode(self, hvac_mode):
        self.runtime.request.validate(hvac_mode, self.runtime.request.target)
        if hvac_mode == HVACMode.OFF:
            self.runtime._resume_pending = False
            self.runtime.enabled = False  # Inhibit in-flight opens before persistence.
        await self.runtime.request.set(hvac_mode, self.runtime.request.target)
        await self.runtime.set_enabled(
            hvac_mode == HVACMode.HEAT, frost_protection=hvac_mode == HVACMode.OFF
        )
        self.runtime._notify()
