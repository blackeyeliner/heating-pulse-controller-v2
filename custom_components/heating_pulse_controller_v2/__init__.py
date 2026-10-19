"""Adaptive radiator pulse control."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, PLATFORMS
from .frontend import register as register_frontend
from .runtime import Runtime
from .thermostat import CONF_NATIVE

type PulseEntry = ConfigEntry[Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: PulseEntry) -> bool:
    if not hass.services.has_service(DOMAIN, "set_restart_temperature"):

        async def set_restart_temperature(call: ServiceCall):
            selected = hass.config_entries.async_get_entry(call.data["config_entry_id"])
            if (
                selected is None
                or selected.domain != DOMAIN
                or not hasattr(selected, "runtime_data")
            ):
                raise HomeAssistantError("Heating controller entry is not loaded")
            await selected.runtime_data.set_restart_temperature(call.data["temperature"])

        hass.services.async_register(
            DOMAIN,
            "set_restart_temperature",
            set_restart_temperature,
            schema=vol.Schema(
                {
                    vol.Required("config_entry_id"): str,
                    vol.Required("temperature"): vol.All(
                        vol.Coerce(float), vol.Range(min=-40, max=120)
                    ),
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, "adopt_thermostat"):

        async def adopt_thermostat(call: ServiceCall):
            selected = hass.config_entries.async_get_entry(call.data["config_entry_id"])
            if (
                selected is None
                or selected.domain != DOMAIN
                or not hasattr(selected, "runtime_data")
            ):
                raise HomeAssistantError("Heating controller entry is not loaded")
            runtime = selected.runtime_data
            if runtime.control_requested or runtime.native:
                raise HomeAssistantError("Disable the legacy-input controller before adoption")
            state = hass.states.get(runtime.config.get("thermostat", ""))
            if state is None or state.state not in {"heat", "off"}:
                raise HomeAssistantError("Legacy thermostat is unavailable")
            await runtime.request.set(state.state, state.attributes.get("temperature"))
            options = dict(selected.options)
            data = {**selected.data, CONF_NATIVE: True}
            if not call.data["preserve_history_helper"]:
                data.pop("output_helper", None)
                options.pop("output_helper", None)
            hass.config_entries.async_update_entry(selected, data=data, options=options)
            await hass.config_entries.async_reload(selected.entry_id)

        hass.services.async_register(
            DOMAIN,
            "adopt_thermostat",
            adopt_thermostat,
            schema=vol.Schema(
                {
                    vol.Required("config_entry_id"): str,
                    vol.Optional("preserve_history_helper", default=False): bool,
                }
            ),
        )
    await register_frontend(hass)
    runtime = Runtime(hass, entry)
    entry.runtime_data = runtime
    try:
        await runtime.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await runtime.stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PulseEntry) -> bool:
    # Inhibit writes and close before removing platforms/listeners.
    await entry.runtime_data.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
