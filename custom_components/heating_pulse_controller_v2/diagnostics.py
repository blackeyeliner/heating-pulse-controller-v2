"""Export control diagnostics without entity IDs or credentials."""


async def async_get_config_entry_diagnostics(hass, entry):
    return entry.runtime_data.diagnostics()
