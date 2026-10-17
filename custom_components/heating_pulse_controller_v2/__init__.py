"""Adaptive radiator pulse control."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, PLATFORMS
from .runtime import Runtime

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
