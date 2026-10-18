"""Persist a valve outage suspicion before promoting it to an emergency."""

import logging
import math

from homeassistant.helpers.storage import Store

from .const import CONF_CLOSING, CONF_OPENING, CONF_TRV, DOMAIN

INTERVAL = 60
ATTEMPTS = 10
_LOGGER = logging.getLogger(__name__)


class Reconnect:
    def __init__(self, hass, entry, config, now):
        self.store = Store(hass, 1, f"{DOMAIN}.reconnect.{entry.entry_id}")
        self.context = {k: config[k] for k in (CONF_TRV, CONF_OPENING, CONF_CLOSING)}
        self.now = now
        self.record = None

    @property
    def active(self):
        return self.record is not None

    async def load(self):
        r = await self.store.async_load()
        try:
            valid = (
                isinstance(r, dict)
                and r["context"] == self.context
                and type(r["attempts"]) is int
                and 0 <= r["attempts"] <= ATTEMPTS
                and all(
                    type(r[k]) in (int, float) and math.isfinite(r[k])
                    for k in ("started_at", "next_attempt_at")
                )
                and r["started_at"] <= self.now()
                and r["started_at"] < r["next_attempt_at"]
            )
        except KeyError, TypeError, ValueError:
            valid = False
        if valid:
            self.record = r

    async def _save(self):
        try:
            await self.store.async_save(self.record)
        except OSError:
            _LOGGER.exception("Unable to persist valve reconnection; keep live retry limits")

    async def begin(self):
        if not self.active:
            now = self.now()
            self.record = {
                "context": self.context,
                "started_at": now,
                "next_attempt_at": now + INTERVAL,
                "attempts": 0,
            }
            await self._save()

    @property
    def due(self):
        return self.active and self.now() >= self.record["next_attempt_at"]

    async def attempt(self):
        # Checkpoint before awaiting commands. No burst of catch-up attempts
        # after downtime, and no extra attempts on repeated sensor events.
        self.record["attempts"] = min(ATTEMPTS, self.record["attempts"] + 1)
        self.record["next_attempt_at"] = self.now() + INTERVAL
        await self._save()

    @property
    def exhausted(self):
        return self.active and self.record["attempts"] >= ATTEMPTS

    async def clear(self):
        self.record = None
        await self._save()

    def diagnostics(self):
        return {
            "valve_reconnecting": self.active,
            "valve_reconnect_attempts": self.record["attempts"] if self.active else 0,
            "valve_reconnect_limit": ATTEMPTS,
            "valve_reconnect_started_at": self.record["started_at"] if self.active else None,
            "valve_reconnect_next_attempt_at": (
                self.record["next_attempt_at"] if self.active else None
            ),
        }
