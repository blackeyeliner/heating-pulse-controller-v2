"""Configure an existing room thermostat, sensors and Sonoff limit entities."""

import math
from dataclasses import fields

import voluptuous as vol
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import ConfigFlow, OptionsFlowWithReload
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CLOSE_VALUE,
    CONF_CLOSING,
    CONF_GUARDS,
    CONF_OPEN_VALUE,
    CONF_OPENING,
    CONF_OUTPUT,
    CONF_RADIATOR,
    CONF_REPORTS,
    CONF_ROOM,
    CONF_THERMOSTAT,
    CONF_TRV,
    CONF_WINDOW,
    DOMAIN,
)
from .emergency import CONF_BACKUP, CONF_NOTIFY
from .engine import Settings


def entity(domain, **kwargs):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, **kwargs))


def number(low, high, step=1):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=low, max=high, step=step, mode=selector.NumberSelectorMode.BOX
        )
    )


ENTITY_SCHEMA = vol.Schema(
    {
        vol.Required("name"): selector.TextSelector(),
        vol.Required(CONF_THERMOSTAT): entity("climate"),
        vol.Required(CONF_TRV): entity("climate"),
        vol.Required(CONF_ROOM): entity("sensor", device_class="temperature"),
        vol.Required(CONF_RADIATOR): entity("sensor", device_class="temperature"),
        vol.Required(CONF_OPENING): entity("number"),
        vol.Required(CONF_CLOSING): entity("number"),
        vol.Required(CONF_OPEN_VALUE, default=21): number(1, 100),
        vol.Required(CONF_CLOSE_VALUE, default=83): number(0, 99),
        vol.Optional(CONF_OUTPUT): entity("input_number"),
        vol.Optional(CONF_GUARDS, default=[]): entity("automation", multiple=True),
    }
)


def settings_schema(values, hass=None):
    defaults = Settings()
    schema = {}
    for field in fields(Settings):
        name = field.name
        default = values.get(name, getattr(defaults, name))
        schema[vol.Required(name, default=default)] = (
            selector.BooleanSelector()
            if isinstance(getattr(defaults, name), bool)
            else number(0.01, 86400, 0.01)
        )
    schema[vol.Required(CONF_REPORTS, default=values.get(CONF_REPORTS, False))] = (
        selector.BooleanSelector()
    )
    schema[vol.Optional(CONF_WINDOW, description={"suggested_value": values.get(CONF_WINDOW)})] = (
        entity("cover")
    )
    schema[
        vol.Optional(CONF_BACKUP, description={"suggested_value": values.get(CONF_BACKUP, [])})
    ] = entity("climate", multiple=True)
    notify_choices = [""]
    if hass:
        notify_choices += [
            "notify." + name
            for name in hass.services.async_services().get("notify", {})
            if name.startswith("mobile_app_")
        ]
    schema[
        vol.Optional(CONF_NOTIFY, description={"suggested_value": values.get(CONF_NOTIFY, "")})
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=notify_choices, custom_value=True, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )
    return vol.Schema(schema)


def validate_window(hass, data):
    if not data.get(CONF_WINDOW):
        return None
    state = hass.states.get(data[CONF_WINDOW])
    if state is None or state.state in ("unknown", "unavailable"):
        return "entity_unavailable"
    if not state.attributes.get("supported_features", 0) & CoverEntityFeature.CLOSE:
        return "unsupported_window"
    return None


def validate_emergency(hass, data):
    notifier = data.get(CONF_NOTIFY, "")
    if notifier:
        parts = notifier.split(".")
        if len(parts) != 2 or parts[0] != "notify" or not hass.services.has_service(*parts):
            return "invalid_emergency_notify"
    forbidden = {data.get(CONF_THERMOSTAT), data.get(CONF_TRV)}
    for entity_id in data.get(CONF_BACKUP, []):
        state = hass.states.get(entity_id)
        if entity_id in forbidden:
            return "backup_is_primary"
        if state is None:
            return "entity_unavailable"
        modes = state.attributes.get("hvac_modes", [])
        if modes and "heat" not in modes:
            return "unsupported_backup"
        if not state.attributes.get("min_temp", 4) <= 20 <= state.attributes.get("max_temp", 35):
            return "unsupported_backup"
    return None


def validate_settings(data):
    return Settings(**{f.name: data[f.name] for f in fields(Settings)})


def validate_entities(hass, data):
    """Read-only validation. Configuring a controller never moves its valve."""
    ids = [
        data[key]
        for key in (CONF_THERMOSTAT, CONF_TRV, CONF_ROOM, CONF_RADIATOR, CONF_OPENING, CONF_CLOSING)
    ]
    if len(set(ids)) != len(ids):
        return "distinct_entities"
    for key in (CONF_ROOM, CONF_RADIATOR):
        state = hass.states.get(data[key])
        if state is None or state.state in ("unknown", "unavailable"):
            return "entity_unavailable"
        try:
            if not math.isfinite(float(state.state)):
                return "invalid_temperature"
        except ValueError:
            return "invalid_temperature"
        if state.attributes.get("unit_of_measurement") != UnitOfTemperature.CELSIUS:
            return "celsius_required"
    for key in (CONF_THERMOSTAT, CONF_TRV):
        state = hass.states.get(data[key])
        if state is None or state.state in ("unknown", "unavailable"):
            return "entity_unavailable"
        if not {"heat", "off"} <= set(state.attributes.get("hvac_modes", [])):
            return "unsupported_climate"
    for key, value in (
        (CONF_OPENING, data[CONF_OPEN_VALUE]),
        (CONF_CLOSING, data[CONF_CLOSE_VALUE]),
    ):
        state = hass.states.get(data[key])
        if state is None or state.state in ("unknown", "unavailable"):
            return "entity_unavailable"
        if state.attributes.get("assumed_state"):
            return "optimistic_valve_limits"
        low, high = state.attributes.get("min", 0), state.attributes.get("max", 100)
        if not low <= 0 <= value <= 100 <= high:
            return "unsupported_limits"
    if 100 - data[CONF_CLOSE_VALUE] > data[CONF_OPEN_VALUE]:
        return "inverted_limits"
    for entity_id in data.get(CONF_GUARDS, []) + (
        [data[CONF_OUTPUT]] if data.get(CONF_OUTPUT) else []
    ):
        if hass.states.get(entity_id) is None:
            return "entity_unavailable"
    return None


class PulseConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            if error := validate_entities(self.hass, user_input):
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_input[CONF_TRV])
                self._abort_if_unique_id_configured()
                # Reject partially overlapping actuators too.
                for entry in self._async_current_entries():
                    if {entry.data.get(CONF_OPENING), entry.data.get(CONF_CLOSING)} & {
                        user_input[CONF_OPENING],
                        user_input[CONF_CLOSING],
                    }:
                        return self.async_abort(reason="already_configured")
                self._data = user_input
                return await self.async_step_tuning()
        return self.async_show_form(step_id="user", data_schema=ENTITY_SCHEMA, errors=errors)

    async def async_step_tuning(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                validate_settings(user_input)
            except ValueError, TypeError:
                errors["base"] = "invalid_settings"
            else:
                if error := (
                    validate_window(self.hass, user_input)
                    or validate_emergency(self.hass, {**self._data, **user_input})
                ):
                    return self.async_show_form(
                        step_id="tuning",
                        data_schema=settings_schema(user_input, self.hass),
                        errors={"base": error},
                    )
                return self.async_create_entry(
                    title=self._data["name"], data={**self._data, **user_input}
                )
        return self.async_show_form(
            step_id="tuning",
            data_schema=settings_schema(user_input or {}, self.hass),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PulseOptionsFlow()


class PulseOptionsFlow(OptionsFlowWithReload):
    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                validate_settings(user_input)
            except ValueError, TypeError:
                errors["base"] = "invalid_settings"
            else:
                if error := (
                    validate_window(self.hass, user_input)
                    or validate_emergency(
                        self.hass,
                        {**self.config_entry.data, **self.config_entry.options, **user_input},
                    )
                ):
                    return self.async_show_form(
                        step_id="init",
                        data_schema=settings_schema(user_input, self.hass),
                        errors={"base": error},
                    )
                return self.async_create_entry(
                    data={
                        **user_input,
                        CONF_WINDOW: user_input.get(CONF_WINDOW, ""),
                        CONF_NOTIFY: user_input.get(CONF_NOTIFY, ""),
                        CONF_BACKUP: user_input.get(CONF_BACKUP, []),
                    }
                )
        values = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="init", data_schema=settings_schema(values, self.hass), errors=errors
        )
