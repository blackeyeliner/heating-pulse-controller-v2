"""Durable one-incident alarm and optional backup heat, separate from valve control."""

import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

CONF_NOTIFY = "emergency_notify_service"
CONF_BACKUP = "backup_climates"
RETRY_SECONDS = 60
_LOGGER = logging.getLogger(__name__)


class Emergency:
    def __init__(self, hass, entry, config, now, changed):
        self.hass, self.entry, self.config = hass, entry, config
        self.now, self.changed = now, changed
        self.store = Store(hass, 1, f"{DOMAIN}.emergency.{entry.entry_id}")
        self.record = None
        self.task = None
        self.closed = False

    @property
    def active(self):
        return bool(self.record and self.record.get("active") is True)

    async def load(self):
        record = await self.store.async_load()
        try:
            valid = (
                isinstance(record, dict)
                and record.get("active") is True
                and isinstance(record["id"], str)
                and 0 < len(record["id"]) <= 64
                and not isinstance(record["started_at"], bool)
                and math.isfinite(record["started_at"])
                and record["started_at"] <= self.now()
                and isinstance(record.get("backups", {}), dict)
                and all(
                    isinstance(k, str) and v in {"requested", "failed"}
                    for k, v in record.get("backups", {}).items()
                )
                and isinstance(record.get("notified", ""), str)
            )
        except KeyError, TypeError, ValueError:
            valid = False
        if valid:
            record.setdefault("backups", {})
            record.setdefault("notified", "")
            self.record = record
        return self.active

    async def _save(self):
        try:
            await self.store.async_save(self.record)
        except OSError:
            _LOGGER.exception("Unable to persist heating emergency; continue live alarm attempts")

    async def trigger(self):
        if not self.active:
            self.record = {
                "active": True,
                "id": uuid.uuid4().hex,
                "started_at": self.now(),
                "notified": "",
                "backups": {},
            }
            await self._save()  # Record the incident before external effects.
        self.kick()

    def _complete(self):
        notifier = self.config.get(CONF_NOTIFY, "")
        return (not notifier or self.record.get("notified") == notifier) and all(
            self.record.get("backups", {}).get(e) == "requested"
            for e in self.config.get(CONF_BACKUP, [])
        )

    def kick(self):
        if (
            self.closed
            or not self.active
            or self._complete()
            or self.task is not None
            and not self.task.done()
        ):
            return
        self.task = self.hass.async_create_task(self._run())

    async def _call(self, domain, service, data):
        async with asyncio.timeout(5):
            await self.hass.services.async_call(domain, service, data, blocking=True)

    async def _run(self):
        now = self.now()
        previous = self.record.get("last_attempt")
        if isinstance(previous, (int, float)) and 0 <= now - previous < RETRY_SECONDS:
            return
        self.record["last_attempt"] = now
        await self._save()
        notifier = self.config.get(CONF_NOTIFY, "")
        if notifier and self.record.get("notified") != notifier:
            at = dt_util.as_local(
                datetime.fromtimestamp(self.record["started_at"], UTC)
            ).isoformat()
            names = []
            for entity_id in self.config.get(CONF_BACKUP, []):
                state = self.hass.states.get(entity_id)
                names.append(state.name if state else entity_id)
            backup = (
                "Будут отправлены команды резервного отопления heat / 20 °C: "
                + ", ".join(names)
                + ". Команда не подтверждает фактическую подачу тепла."
                if names
                else "Резервные кондиционеры не настроены."
            )
            try:
                domain, service = notifier.split(".", 1)
                await self._call(
                    domain,
                    service,
                    {
                        "title": "Авария водяного отопления",
                        "message": f"{self.entry.title}: потеря надёжного управления вентилем. "
                        f"Контроллер остановлен. Начало: {at}. {backup} "
                        "Требуется проверить связь и вентиль. Резерв выключается вручную.",
                        "data": {"tag": f"heating-valve-{self.entry.entry_id}"},
                    },
                )
                self.record["notified"] = notifier
                self.record.pop("notify_error", None)
            except HomeAssistantError, TimeoutError, ValueError:
                self.record["notify_error"] = "delivery_failed"
                _LOGGER.exception("Heating emergency mobile notification failed; will retry")
            await self._save()
            self.changed()
        for entity_id in self.config.get(CONF_BACKUP, []):
            if self.record["backups"].get(entity_id) == "requested":
                continue
            state = self.hass.states.get(entity_id)
            try:
                if state is None or state.state in {"unknown", "unavailable"}:
                    raise HomeAssistantError("Backup climate unavailable")
                await self._call(
                    "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "heat"}
                )
                await self._call(
                    "climate", "set_temperature", {"entity_id": entity_id, "temperature": 20}
                )
                self.record["backups"][entity_id] = "requested"
            except HomeAssistantError, TimeoutError:
                self.record["backups"][entity_id] = "failed"
                _LOGGER.exception("Backup heating command failed for %s; will retry", entity_id)
            await self._save()
            self.changed()

    async def resolve(self):
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.record:
            self.record["active"] = False
            await self._save()
        self.changed()  # Never turn backup units off automatically.

    async def stop(self):
        self.closed = True
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def diagnostics(self):
        statuses = (self.record or {}).get("backups", {})
        selected = self.config.get(CONF_BACKUP, [])
        notifier = self.config.get(CONF_NOTIFY, "")
        return {
            "emergency_active": self.active,
            "emergency_notification": (
                "not_configured"
                if not notifier
                else "sent"
                if (self.record or {}).get("notified") == notifier
                else "failed"
                if (self.record or {}).get("notify_error")
                else "pending"
                if self.active
                else "idle"
            ),
            "backup_selected": len(selected),
            "backup_requested": sum(statuses.get(e) == "requested" for e in selected),
            "backup_failed": sum(statuses.get(e) == "failed" for e in selected),
        }
