"""HA lifecycle, fresh reports, storage and command ownership."""

import asyncio
import logging
import math
import time
from dataclasses import fields
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .actuator import SonoffActuator
from .const import (
    CONF_CLOSING,
    CONF_GUARDS,
    CONF_OPENING,
    CONF_RADIATOR,
    CONF_REPORTS,
    CONF_ROOM,
    CONF_THERMOSTAT,
    CONF_TRV,
    DOMAIN,
)
from .engine import Controller, Settings

_LOGGER = logging.getLogger(__name__)


class Runtime:
    def __init__(self, hass: HomeAssistant, entry):
        self.hass = hass
        self.entry = entry
        self.config = {**entry.data, **entry.options}
        defaults = Settings()
        settings = Settings(
            **{f.name: self.config.get(f.name, getattr(defaults, f.name)) for f in fields(Settings)}
        )
        self.engine = Controller(settings)
        self.actuator = SonoffActuator(hass, self.config)
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.enabled = False  # Explicit commissioning after every HA start/reload.
        self._owned = False
        self._closed = False
        self._lock = asyncio.Lock()
        self._unsub = []
        self._listeners = set()
        self._tasks: set[asyncio.Task] = set()
        self._rerun = False
        self._wall = time.time()
        self._monotonic = time.monotonic()
        self._sensor_ids = [self.config[CONF_ROOM], self.config[CONF_RADIATOR]]
        self._seen_report_times: dict[str, float] = {}

    def now(self) -> float:
        return self._wall + time.monotonic() - self._monotonic

    async def start(self):
        stored = await self.store.async_load()
        if stored:
            self.engine.restore(stored, self.now())
        # No initial state snapshot is passed off as a fresh report.
        watched = (
            self._sensor_ids + [self.config[CONF_THERMOSTAT]] + self.config.get(CONF_GUARDS, [])
        )
        self._unsub.extend(
            [
                async_track_state_change_event(self.hass, watched, self._state_event),
                async_track_state_report_event(self.hass, self._sensor_ids, self._report_event),
                async_track_time_interval(self.hass, self._timer, timedelta(seconds=5)),
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop_event),
            ]
        )

    @callback
    def subscribe(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _notify(self):
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _schedule(self):
        if self._closed:
            return
        self._rerun = True
        if any(not task.done() for task in self._tasks):
            return
        task = self.hass.async_create_task(self._worker())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _worker(self):
        while self._rerun and not self._closed:
            self._rerun = False
            await self._process()

    @callback
    def _state_event(self, event):
        if self._closed:
            return
        entity_id = event.data["entity_id"]
        state = event.data.get("new_state")
        old = event.data.get("old_state")
        if entity_id in self._sensor_ids:
            # MQTT force_update emits state_changed for an identical value and
            # identical attributes, rather than state_reported. Count it once.
            # Attribute-only writes still do not add a temperature sample.
            if (
                old is None
                or state is None
                or old.state != state.state
                or old.attributes == state.attributes
            ):
                self._temperature(entity_id, state)
        self._schedule()

    @callback
    def _report_event(self, event):
        if not self._closed:
            entity_id = event.data["entity_id"]
            self._temperature(entity_id, event.data["new_state"])
            self._schedule()

    def _temperature(self, entity_id, state):
        at = self.now()
        marker = state.last_reported.timestamp() if state else at
        if marker <= self._seen_report_times.get(entity_id, -math.inf):
            return
        self._seen_report_times[entity_id] = marker
        try:
            value = float(state.state)
            if state.attributes.get("unit_of_measurement") != UnitOfTemperature.CELSIUS:
                value = math.nan
        except TypeError, ValueError, AttributeError:
            value = math.nan
        report = (
            self.engine.room_report
            if entity_id == self.config[CONF_ROOM]
            else self.engine.radiator_report
        )
        report(value, at)

    @callback
    def _timer(self, _time):
        self._schedule()

    def _inputs(self):
        thermostat = self.hass.states.get(self.config[CONF_THERMOSTAT])
        try:
            target = float(thermostat.attributes["temperature"])
        except TypeError, ValueError, AttributeError, KeyError:
            target = math.nan
        return bool(thermostat and thermostat.state == "heat"), target

    def _guards_clear(self):
        return all(
            (state := self.hass.states.get(entity_id)) is not None and state.state == "off"
            for entity_id in self.config.get(CONF_GUARDS, [])
        )

    def _permitted(self):
        mode, target = self._inputs()
        room = self.engine.room
        rad = self.engine.radiator
        now = self.now()
        return (
            not self._closed
            and self.enabled
            and not self.engine.fault
            and mode
            and math.isfinite(target)
            and self._guards_clear()
            and room is not None
            and rad is not None
            and target - room.value > self.engine.settings.stop_error
            and now - room.at <= self.engine.settings.room_timeout
            and now - rad.at <= self.engine.settings.radiator_timeout
            and self.engine.opened_at is not None
            and now - self.engine.opened_at < self.engine.settings.max_open_seconds
        )

    async def _process(self):
        async with self._lock:
            if self._closed:
                return
            now = self.now()
            before = self.engine.revision
            previous = (self.engine.phase, self.engine.reason)
            mode, target = self._inputs()
            if self.enabled and not self._guards_clear():
                self.engine.trip("legacy_controller_enabled", now)
            self.engine.evaluate(now, enabled=self.enabled, heat_mode=mode, target=target)
            if self._owned:
                try:
                    if self.engine.wants_open and self.actuator.commanded_open is not True:
                        await self.actuator.open(target, self._permitted)
                        if not self._permitted():
                            self.engine.evaluate(
                                self.now(), enabled=False, heat_mode=False, target=target
                            )
                    elif not self.engine.wants_open and self.actuator.commanded_open is not False:
                        await self.actuator.close()
                except HomeAssistantError, TimeoutError:
                    _LOGGER.exception("Valve command failed for %s", self.entry.title)
                    self.engine.trip("actuator_error", self.now())
                    self.enabled = False
            if before != self.engine.revision:
                self.store.async_delay_save(self.engine.dump, 1)
            if previous != (self.engine.phase, self.engine.reason):
                _LOGGER.info("%s: %s (%s)", self.entry.title, self.engine.phase, self.engine.reason)
            self._notify()

    async def set_enabled(self, enabled: bool):
        if not enabled:
            self.enabled = False  # Inhibit queued/in-flight opens before waiting for lock.
        async with self._lock:
            if self._closed:
                raise HomeAssistantError("Controller is unloaded")
            if enabled:
                if not self.config.get(CONF_REPORTS):
                    raise HomeAssistantError(
                        "Verify temperature reporting in Configure before enabling"
                    )
                if not self._guards_clear():
                    raise HomeAssistantError("Legacy control automations must be off")
                if self.engine.fault:
                    raise HomeAssistantError("Reset the controller fault before enabling")
                now = self.now()
                if (
                    not self.engine.room
                    or not self.engine.radiator
                    or now - self.engine.room.at > self.engine.settings.room_timeout
                    or now - self.engine.radiator.at > self.engine.settings.radiator_timeout
                ):
                    raise HomeAssistantError(
                        "Waiting for fresh reports from both temperature sensors"
                    )
                for key in (CONF_TRV, CONF_OPENING, CONF_CLOSING, CONF_THERMOSTAT):
                    state = self.hass.states.get(self.config[key])
                    if state is None or state.state in ("unknown", "unavailable"):
                        raise HomeAssistantError(f"Unavailable entity: {self.config[key]}")
                self._owned = True
                try:
                    await self.actuator.close()
                except HomeAssistantError, TimeoutError:
                    self.engine.trip("actuator_error", now)
                    self._notify()
                    raise
                self.enabled = True
            else:
                # An enable awaiting its initial close may have set this again.
                self.enabled = False
                if self._owned:
                    await self.actuator.close()
        await self._process()

    async def reset_fault(self):
        if self.enabled:
            raise HomeAssistantError("Disable control before resetting a fault")
        async with self._lock:
            self.engine.reset_fault()
            self._notify()

    async def reset_reference(self):
        if self.enabled:
            raise HomeAssistantError("Disable control before resetting calibration")
        async with self._lock:
            self.engine.reset_reference()
            await self.store.async_save(self.engine.dump())
            self._notify()

    async def _stop_event(self, _event):
        await self.stop()

    async def stop(self):
        if self._closed:
            return
        self.enabled = False
        self._closed = True
        for unsubscribe in self._unsub:
            unsubscribe()
        self._unsub.clear()
        async with self._lock:
            try:
                if self._owned:
                    await self.actuator.close()
            except HomeAssistantError, TimeoutError:
                self.engine.trip("actuator_error", self.now())
                _LOGGER.exception("Final valve close failed for %s", self.entry.title)
            finally:
                self.engine.evaluate(self.now(), enabled=False, heat_mode=False, target=20)
                await self.store.async_save(self.engine.dump())
        pending = [task for task in self._tasks if task is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._notify()
        self._listeners.clear()

    def diagnostics(self):
        return {
            **self.engine.diagnostics(self.now()),
            "enabled": self.enabled,
            "commanded_open": self.actuator.commanded_open,
            "reporting_verified": self.config.get(CONF_REPORTS, False),
        }
