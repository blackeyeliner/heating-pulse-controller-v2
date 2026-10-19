"""Serve the bundled thermostat wrapper without a separate HACS frontend package."""

import json
import logging
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig

from .const import DOMAIN

KEY = f"{DOMAIN}.frontend"
_LOGGER = logging.getLogger(__name__)


async def register(hass):
    if "frontend" not in hass.config.components or hass.data.get(KEY):
        return
    root = Path(__file__).parent
    hass.data[KEY] = True
    try:
        version = json.loads(await hass.async_add_executor_job((root / "manifest.json").read_text))[
            "version"
        ]
        url = f"/{DOMAIN}/thermostat-card.js"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, str(root / "www" / "thermostat-card.js"), True)]
        )
        frontend.add_extra_js_url(hass, f"{url}?v={version}")
    except Exception:
        hass.data.pop(KEY, None)
        _LOGGER.exception("Thermostat card registration failed; radiator control remains available")
