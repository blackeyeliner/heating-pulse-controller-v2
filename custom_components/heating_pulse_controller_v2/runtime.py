"""HA lifecycle, fresh reports, storage and command ownership."""

import asyncio
import logging
import math
import time
from dataclasses import asdict, fields
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .actuator import SonoffActuator
from .const import (
    CONF_CLOSE_VALUE,
    CONF_CLOSING,
    CONF_GUARDS,
    CONF_OPEN_VALUE,
    CONF_OPENING,
    CONF_RADIATOR,
    CONF_REPORTS,
    CONF_ROOM,
    CONF_THERMOSTAT,
    CONF_TRV,
    CONF_WINDOW,
    DOMAIN,
)
from .engine import Controller, Settings
from .window import cold_reason, is_window_open

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
        self._window_close_last = -math.inf
        self._window_close_error = None
        self._window_close_reason = None
        self._window_deadline_cancel = None
        self._pending_cooling = None
        self._restart_gate = None
        self._pending_gate = None
        self._started_at = self.now()
        self._cooling_restore_status = "not_saved"

    def _capture_gate(self):
        if self._pending_gate is not None or self.engine.fault:
            return
        gate = self.engine.restart_temperature
        source = self.engine.restart_source
        if gate is not None and source is not None:
            if self._restart_gate is None or (gate, source) != (
                self._restart_gate["temperature"],
                self._restart_gate["source"],
            ):
                self._restart_gate = {"temperature": gate, "source": source, "saved_at": self.now()}

    def _storage_data(self):
        self._capture_gate()
        stored = self.engine.dump()
        if self._restart_gate is not None:
            stored["restart_gate"] = self._restart_gate
            stored["restart_gate_context"] = self._cooling_context()
        return stored

    def _valid_gate(self, record):
        try:
            value, saved, source = record["temperature"], record["saved_at"], record["source"]
            return (
                not isinstance(value, bool)
                and math.isfinite(value)
                and -40 <= value <= 120
                and not isinstance(saved, bool)
                and math.isfinite(saved)
                and saved <= self.now()
                and source in {"startup", "base", "adaptive", "manual"}
            )
        except KeyError, TypeError, ValueError, OverflowError:
            return False

    def _restore_gate_if_ready(self):
        if self._pending_gate is None:
            return
        if self._cooling_restore_status == "restored":
            self._pending_gate = None
            return
        now = self.now()
        if self.engine.fault or self.engine.wants_open:
            return
        if self._window_open() is None:
            return
        for sample, timeout in (
            (self.engine.room, self.engine.settings.room_timeout),
            (self.engine.radiator, self.engine.settings.radiator_timeout),
        ):
            if sample is None or sample.at < self._started_at or now - sample.at > timeout:
                return
        for key, expected in ((CONF_OPENING, 0), (CONF_CLOSING, 100)):
            state = self.hass.states.get(self.config[key])
            try:
                if state is None or float(state.state) != expected:
                    return
            except ValueError, TypeError:
                return
        record = self._pending_gate
        self._pending_gate = None
        self._pending_cooling = None
        self.engine.set_restart_gate(record["temperature"], record["source"])
        self._cooling_restore_status = "restored_gate"

    async def set_restart_temperature(self, temperature):
        async with self._lock:
            if self._closed or self.engine.wants_open or self.engine.fault:
                raise HomeAssistantError(
                    "Set restart temperature only while not heating and without a fault"
                )
            self.engine.set_restart_gate(temperature)
            self._pending_gate = self._pending_cooling = None
            self._cooling_restore_status = "manual"
            await self.store.async_save(self._storage_data())
            self._notify()
        await self._process()

    def _cooling_context(self):
        return {
            key: self.config.get(key)
            for key in (
                CONF_ROOM,
                CONF_RADIATOR,
                CONF_THERMOSTAT,
                CONF_TRV,
                CONF_OPENING,
                CONF_CLOSING,
                CONF_OPEN_VALUE,
                CONF_CLOSE_VALUE,
                CONF_WINDOW,
            )
        }

    def now(self) -> float:
        return self._wall + time.monotonic() - self._monotonic

    async def start(self):
        self._started_at = self.now()
        stored = await self.store.async_load()
        if stored:
            restored = self.engine.restore(stored, self.now())
            if isinstance(stored, dict) and stored.get("restart_gate") is not None:
                if (
                    stored.get("settings") == asdict(self.engine.settings)
                    and stored.get("restart_gate_context") == self._cooling_context()
                    and self._valid_gate(stored["restart_gate"])
                ):
                    self._restart_gate = stored["restart_gate"]
                    self._pending_gate = self._restart_gate
                    self._cooling_restore_status = "waiting_for_fresh_reports"
                else:
                    self._cooling_restore_status = "incompatible"
            if isinstance(stored, dict) and stored.get("cooling") is not None:
                if restored and stored.get("cooling_context") == self._cooling_context():
                    self._pending_cooling = stored["cooling"]
                    self._cooling_restore_status = "waiting_for_fresh_reports"
                else:
                    self._cooling_restore_status = "incompatible"
                # Consume the clean-stop checkpoint before subscribing. A crash
                # in this session must never resurrect the previous episode.
                await self.store.async_save(self._storage_data())
        # No initial state snapshot is passed off as a fresh report.
        watched = (
            self._sensor_ids + [self.config[CONF_THERMOSTAT]] + self.config.get(CONF_GUARDS, [])
        )
        if self.config.get(CONF_WINDOW):
            watched.append(self.config[CONF_WINDOW])
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

    def _window_open(self):
        if not self.config.get(CONF_WINDOW):
            return False
        state = self.hass.states.get(self.config[CONF_WINDOW])
        return is_window_open(
            state.state if state else None,
            state.attributes.get("current_position") if state else None,
        )

    def _window_cold(self):
        now = self.now()
        room, rad = self.engine.room, self.engine.radiator
        return cold_reason(
            room.value if room and now - room.at <= self.engine.settings.room_timeout else None,
            rad.value if rad and now - rad.at <= self.engine.settings.radiator_timeout else None,
        )

    def _arm_window_deadline(self):
        if self._window_deadline_cancel:
            self._window_deadline_cancel()
            self._window_deadline_cancel = None
        if self.engine.window_deadline is not None and not self._closed:
            self._window_deadline_cancel = async_call_later(
                self.hass, max(0, self.engine.window_deadline - self.now()), self._timer
            )

    def window_opening_allowed(self):
        if not self.enabled:
            return True
        now = self.now()
        room, rad = self.engine.room, self.engine.radiator
        return bool(
            room
            and rad
            and now - room.at <= self.engine.settings.room_timeout
            and now - rad.at <= self.engine.settings.radiator_timeout
            and not self._window_cold()
        )

    async def _protect_window(self):
        """Close immediately at either threshold, without a trial heating phase.

        Remains effective during a heating fault, while Control enabled is on.
        A successful service call is not physical confirmation. Retry boundedly
        while the cover still reports exposed; never issue an open command.
        """
        exposed = self._window_open()
        if exposed is False:
            self._window_close_last = -math.inf
            self._window_close_error = None
            self._window_close_reason = None
            return
        reason = self._window_cold()
        if not self.enabled or self._closed or not self.config.get(CONF_WINDOW) or not reason:
            return
        self._window_close_reason = reason
        now = self.now()
        if now - self._window_close_last < 30:
            return
        self._window_close_last = now
        try:
            async with asyncio.timeout(5):
                await self.hass.services.async_call(
                    "cover", "close_cover", {"entity_id": self.config[CONF_WINDOW]}, blocking=True
                )
            self._window_close_error = None
            _LOGGER.warning("%s: emergency window close (%s)", self.entry.title, reason)
        except HomeAssistantError, TimeoutError:
            self._window_close_error = "window_close_failed"
            _LOGGER.exception("Emergency window close failed for %s", self.entry.title)

    def _permitted(self):
        mode, target = self._inputs()
        room = self.engine.room
        rad = self.engine.radiator
        now = self.now()
        # Cover feedback can change between awaited actuator service calls.
        # Reconcile the policy inside the same serial owner before deciding
        # whether that in-flight opening is still permitted.
        window = self._window_open()
        if (window is not False) != self.engine._window_active:
            self.engine.evaluate(
                now, enabled=self.enabled, heat_mode=mode, target=target, window_open=window
            )
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
            and (
                self._window_open() is False
                and not self.engine._window_active
                or self._window_open() is True
                and self.engine.window_permits_open(now)
                and not self._window_cold()
            )
        )

    def _restore_cooling_if_ready(self):
        checkpoint = self._pending_cooling
        if checkpoint is None:
            return
        now = self.now()
        try:
            age = now - checkpoint["saved_at"]
            valid_age = math.isfinite(age) and 0 <= age <= self.engine.settings.radiator_timeout
        except TypeError, KeyError:
            valid_age = False
        if not valid_age or self._window_open() is True or self.engine.fault:
            self._pending_cooling = None
            self._cooling_restore_status = "discarded"
            return
        if self._window_open() is None or not self.engine.room or not self.engine.radiator:
            return
        for key, expected in ((CONF_OPENING, 0), (CONF_CLOSING, 100)):
            state = self.hass.states.get(self.config[key])
            if state is None or state.state in ("unknown", "unavailable"):
                return
            try:
                closed = float(state.state) == expected
            except TypeError, ValueError:
                closed = False
            if not closed:
                self._pending_cooling = None
                self._cooling_restore_status = "valve_not_closed"
                return
        self._pending_cooling = None
        # Unknown window feedback at startup may have selected the waiting mode;
        # reconcile confirmed closure before applying the saved episode.
        self.engine.evaluate(now, enabled=False, heat_mode=False, target=20, window_open=False)
        self._cooling_restore_status = (
            "restored" if self.engine.restore_cooling(checkpoint, now) else "discarded"
        )

    async def _process(self):
        async with self._lock:
            if self._closed:
                return
            now = self.now()
            before = self.engine.revision
            previous = (self.engine.phase, self.engine.reason)
            self._restore_cooling_if_ready()
            self._restore_gate_if_ready()
            gate_before = self._restart_gate
            mode, target = self._inputs()
            if self.enabled and not self._guards_clear():
                self.engine.trip("legacy_controller_enabled", now)
            self.engine.evaluate(
                self.now(),
                enabled=self.enabled,
                heat_mode=mode,
                target=target,
                window_open=self._window_open(),
            )
            await self._protect_window()
            if self._owned:
                try:
                    if self.engine.wants_open and self.actuator.commanded_open is not True:
                        await self.actuator.open(target, self._permitted)
                        if not self._permitted():
                            self.engine.evaluate(
                                self.now(),
                                enabled=False,
                                heat_mode=False,
                                target=target,
                                window_open=self._window_open(),
                            )
                    elif not self.engine.wants_open and self.actuator.commanded_open is not False:
                        await self.actuator.close()
                except HomeAssistantError, TimeoutError:
                    _LOGGER.exception("Valve command failed for %s", self.entry.title)
                    self.engine.trip("actuator_error", self.now())
                    self.enabled = False
            self._capture_gate()
            if before != self.engine.revision or gate_before != self._restart_gate:
                self.store.async_delay_save(self._storage_data, 1)
            if previous != (self.engine.phase, self.engine.reason):
                _LOGGER.info("%s: %s (%s)", self.entry.title, self.engine.phase, self.engine.reason)
            self._arm_window_deadline()
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
            await self.store.async_save(self._storage_data())
            self._notify()

    async def _stop_event(self, _event):
        await self.stop()

    async def stop(self):
        if self._closed:
            return
        self.enabled = False
        self._closed = True
        if self._window_deadline_cancel:
            self._window_deadline_cancel()
            self._window_deadline_cancel = None
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
                self.engine.evaluate(
                    self.now(),
                    enabled=False,
                    heat_mode=False,
                    target=20,
                    window_open=self._window_open(),
                )
                stored = self._storage_data()
                if (
                    self._owned
                    and self.actuator.commanded_open is False
                    and self._window_open() is False
                ):
                    checkpoint = self.engine.cooling_checkpoint(self.now())
                    if checkpoint:
                        stored["cooling"] = checkpoint
                        stored["cooling_context"] = self._cooling_context()
                await self.store.async_save(stored)
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
            "cooling_restore_status": self._cooling_restore_status,
            "window_status": (
                self._window_close_error
                or self._window_close_reason
                or (
                    "unknown"
                    if self._window_open() is None
                    else "open"
                    if self._window_open()
                    else "closed"
                )
            )
            if self.config.get(CONF_WINDOW)
            else "not_configured",
        }
