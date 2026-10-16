"""Adaptive radiator pulse control."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .runtime import Runtime

type PulseEntry = ConfigEntry[Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: PulseEntry) -> bool:
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
