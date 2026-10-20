"""Local TRV changes as room requests, excluding our own command feedback."""

import asyncio
import logging
import math

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)


class PhysicalControls:
    def __init__(self, runtime):
        self.runtime = runtime
        self.expected = []
        self.pending = None
        self.task = None
        self.closed = False
        self.debounce = 0.4

    def expect(self, field, value):
        """Register before a write: MQTT feedback often arrives during the call."""
        self.expected = [item for item in self.expected if item[2] >= self.runtime.now()][-15:]
        self.expected.append((field, value, self.runtime.now() + 30))

    def _echo(self, field, value):
        self.expected = [item for item in self.expected if item[2] >= self.runtime.now()]
        for i, item in enumerate(self.expected):
            if item[:2] == (field, value):
                self.expected.pop(i)
                return True
        return False

    @staticmethod
    def _target(state):
        try:
            value = state.attributes.get("temperature")
            if isinstance(value, bool):
                return None
            value = float(value)
            return value if math.isfinite(value) and 4 <= value <= 35 else None
        except AttributeError, TypeError, ValueError:
            return None

    @callback
    def observe(self, old, new):
        r = self.runtime
        if self.closed or r._closed or not r.native:
            return
        # Restoration/availability is not a physical gesture.
        if old is None or new is None or old.state not in {"off", "heat", "auto"}:
            return
        if new.state not in {"off", "heat", "auto"}:
            return
        mode_change = old.state != new.state
        target = self._target(new)
        target_change = target is not None and target != self._target(old)
        if mode_change and self._echo("mode", new.state):
            mode_change = False
        if target_change and self._echo("target", target):
            target_change = False
        # In Off firmware may replace occupied setpoint with frost/schedule
        # values. Rotation must actually leave Off before it can enable Heat;
        # the following mode report carries the latest chosen target.
        if new.state == "off":
            target_change = False
        if not mode_change and (not target_change or new.state == "auto"):
            return
        if mode_change and new.state in {"off", "auto"}:
            mode, target = "off", r.request.target
            # Stop queued/in-flight opening before waiting for any persistence.
            r.enabled = False
            r._resume_pending = False
        else:
            # Rotation out of Off explicitly enables Heat; temperature changes
            # during Heat update the room request. Native Auto is never adopted.
            if target is None:
                target = r.request.target
            mode = "heat"
        if self.pending is not None and self.pending[0] == "off" and not mode_change:
            return
        self.pending = (mode, target)
        if self.task is None or self.task.done():
            self.task = r.hass.async_create_task(self._run())

    async def _run(self):
        while self.pending is not None and not self.closed:
            if self.pending[0] != "off":
                await asyncio.sleep(self.debounce)
            if self.closed or self.runtime._closed or self.pending is None:
                return
            mode, target = self.pending
            self.pending = None
            r = self.runtime
            try:
                if mode == "off" or mode != r.request.mode or not r.control_requested:
                    await r.set_native_mode(mode, target)
                else:
                    await r.request.set(mode, target)
                    await r._process()
                    r._notify()
                _LOGGER.info("%s: physical request %s / %.1f °C", r.entry.title, mode, target)
            except HomeAssistantError, OSError:
                _LOGGER.exception("Physical thermostat request failed for %s", r.entry.title)

    async def stop(self):
        self.closed = True
        self.pending = None
        if self.task is not None and not self.task.done():
            # Let an in-flight safe close finish; runtime is already inhibited.
            await self.task
