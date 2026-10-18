"""Deterministic pulse control. No Home Assistant or third-party imports.

Times are seconds on a caller-supplied, monotonic epoch clock. Only actual
temperature reports advance the signal detector; timer ticks never add samples.
"""

import math
from collections import deque
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from statistics import median

from .window import PWM_CLOSED_SECONDS, PWM_OFF_RATIO, PWM_OPEN_SECONDS, cold_reason


class Phase(StrEnum):
    OFF = "off"
    IDLE = "idle"
    HEATING = "heating"
    CALIBRATING = "calibrating"
    SETTLING = "settling"
    COOLING = "cooling"
    FAULT = "fault"
    WINDOW_PAUSE = "window_pause"
    WINDOW_HEATING = "window_heating"
    RECOVERY_HEATING = "recovery_heating"
    RECOVERY_PAUSE = "recovery_pause"


@dataclass(frozen=True)
class Settings:
    """Provisional defaults; commissioning is explicit, never automatic."""

    reference_seconds: float = 10800
    cooling_fraction: float = 0.3
    adaptive_cooling: bool = False
    cooling_fraction_max: float = 0.6
    cooling_full_error: float = 1.0
    room_trend_window: float = 300
    cooling_min_gap: float = 0.5
    start_error: float = 0.25
    stop_error: float = 0.1
    bootstrap_lag: float = 120
    max_lag: float = 300
    trend_window: float = 180
    plateau_seconds: float = 60
    plateau_slope: float = 0.15  # degrees C / minute
    plateau_ratio: float = 0.2
    fall_tolerance: float = 0.08
    min_rise: float = 3
    min_rising_slope: float = 0.5
    max_sample_gap: float = 120
    radiator_timeout: float = 180
    room_timeout: float = 1800
    max_open_seconds: float = 1200
    peak_drop: float = 0.2
    peak_confirm_seconds: float = 60
    startup_excess: float = 3

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name == "adaptive_cooling":
                if not isinstance(value, bool):
                    raise ValueError(field.name)
                continue
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(field.name)
        if not 0 < self.cooling_fraction < 1 or not 0 < self.plateau_ratio < 1:
            raise ValueError("fractions")
        if self.stop_error >= self.start_error:
            raise ValueError("hysteresis")
        if self.adaptive_cooling and (
            not self.cooling_fraction <= self.cooling_fraction_max < 1
            or self.cooling_full_error <= self.start_error
        ):
            raise ValueError("adaptive_cooling_bounds")
        if self.bootstrap_lag > self.max_lag:
            raise ValueError("lag")
        if self.max_sample_gap > self.radiator_timeout:
            raise ValueError("sample_gap")
        if self.max_open_seconds <= self.trend_window + self.plateau_seconds:
            raise ValueError("max_open_seconds")


@dataclass(frozen=True)
class Sample:
    at: float
    value: float


def slope(samples: list[Sample]) -> float | None:
    """Least-squares slope in degrees C/minute, centred to avoid epoch error."""
    if len(samples) < 3:
        return None
    times = [s.at - samples[0].at for s in samples]
    mean_t = sum(times) / len(times)
    mean_y = sum(s.value for s in samples) / len(samples)
    denominator = sum((t - mean_t) ** 2 for t in times)
    if denominator <= 0:
        return None
    return (
        60
        * sum((t - mean_t) * (s.value - mean_y) for t, s in zip(times, samples, strict=True))
        / denominator
    )


def interpolate(trace: list[Sample], at: float) -> float | None:
    if not trace or not trace[0].at <= at <= trace[-1].at:
        return None
    for first, second in zip(trace, trace[1:], strict=False):
        if first.at <= at <= second.at:
            fraction = (at - first.at) / (second.at - first.at)
            return first.value + fraction * (second.value - first.value)
    return trace[-1].value


@dataclass
class Reference:
    temperature: float
    measured_at: float
    knee_at: float
    trace: list[Sample]


class Controller:
    """One room; state transitions have a single serial owner."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.phase = Phase.OFF
        self.reason = "not_enabled"
        self.fault: str | None = None
        self.room: Sample | None = None
        self.radiator: Sample | None = None
        self.reference: Reference | None = None
        self.lags: deque[float] = deque(maxlen=7)
        self.stop_temperature: float | None = None
        self.restart_temperature: float | None = None
        self.restart_source: str | None = None
        self.peak: Sample | None = None
        self.trend: float | None = None
        self.opened_at: float | None = None
        self.closed_at: float | None = None
        self._trace: deque[Sample] = deque(maxlen=2048)
        self._room_trace: deque[Sample] = deque(maxlen=2048)
        self.cooling_fraction_used: float | None = None
        self._candidate: list[Sample] = []
        self._max_slope = 0.0
        self._last_evaluated_report = -math.inf
        self._last_now = -math.inf
        self._learn_lag = False
        self._cooling_required = True
        self._demand = False
        self.revision = 0
        self._window_active = False
        self._window_next_open = 0.0
        self._window_deadline = 0.0
        self._recovery_reason = None
        self._recovery_next_open = 0.0
        self._recovery_deadline = 0.0
        self._retry_calibration = False
        self._pulse_exposed = False

    @property
    def wants_open(self) -> bool:
        return self.phase in (
            Phase.HEATING,
            Phase.CALIBRATING,
            Phase.WINDOW_HEATING,
            Phase.RECOVERY_HEATING,
        )

    @property
    def lag(self) -> float:
        return median(self.lags) if self.lags else self.settings.bootstrap_lag

    def room_report(self, value: float, at: float):
        previous = self.room
        self.room = self._sample(value, at, self.room)
        if self.room is None:
            self._room_trace.clear()
        elif self.room is not previous:
            if previous and at - previous.at > self.settings.max_sample_gap:
                self._room_trace.clear()
            self._room_trace.append(self.room)
            while (
                self._room_trace and self._room_trace[0].at < at - self.settings.room_trend_window
            ):
                self._room_trace.popleft()

    def room_trend(self, now: float) -> float | None:
        samples = list(self._room_trace)
        if (
            not samples
            or now - samples[-1].at > self.settings.max_sample_gap
            or samples[-1].at - samples[0].at < self.settings.room_trend_window * 0.5
        ):
            return None
        return slope(samples)

    def _cooling_fraction(self, now: float, target: float | None) -> float:
        cfg = self.settings
        trend = self.room_trend(now)
        if not cfg.adaptive_cooling or target is None or trend is None:
            return cfg.cooling_fraction
        # A short projection credits observed warming, never invents future cooling.
        deficit = target - self.room.value - max(0, trend) * cfg.room_trend_window / 60
        weight = min(
            1, max(0, (deficit - cfg.start_error) / (cfg.cooling_full_error - cfg.start_error))
        )
        return cfg.cooling_fraction + weight * (cfg.cooling_fraction_max - cfg.cooling_fraction)

    def radiator_report(self, value: float, at: float):
        previous = self.radiator
        self.radiator = self._sample(value, at, previous)
        if self.radiator is None or self.radiator is previous:
            return
        if previous and at - previous.at > self.settings.max_sample_gap:
            # A peak could have occurred in the unseen interval.
            self._learn_lag = False
        if self.phase in (Phase.HEATING, Phase.CALIBRATING):
            if previous and at - previous.at > self.settings.max_sample_gap:
                self._candidate.clear()
                self._trace.clear()
                self._max_slope = 0
            self._trace.append(self.radiator)

    @staticmethod
    def _sample(value: float, at: float, previous: Sample | None) -> Sample | None:
        if not math.isfinite(value) or not -40 <= value <= 120:
            return None
        if not math.isfinite(at):
            return None
        if previous and at <= previous.at:
            return previous
        return Sample(at, value)

    def evaluate(
        self,
        now: float,
        *,
        enabled: bool,
        heat_mode: bool,
        target: float,
        window_open: bool | None = False,
        input_error: str | None = None,
    ):
        if now < self._last_now:
            input_error = "clock_moved_backwards"
            now = self._last_now
        self._last_now = now
        fresh_report = bool(self.radiator and self.radiator.at > self._last_evaluated_report)
        if fresh_report:
            self._last_evaluated_report = self.radiator.at

        exposed = window_open is not False
        window_changed = exposed != self._window_active
        if window_changed:
            self._switch_window(now, exposed)

        if not enabled or not heat_mode:
            if self.wants_open:
                if self._recovery_reason:
                    self._close_recovery(now, "disabled" if not enabled else "thermostat_off")
                elif self._window_active:
                    self._close_window(now, "disabled" if not enabled else "thermostat_off")
                else:
                    self._close(now, "disabled" if not enabled else "thermostat_off")
            if not self._window_active:
                self._cool(now, fresh_report)
            self._demand = False
            self.phase = Phase.FAULT if self.fault else Phase.OFF
            return
        if self.fault:
            self.phase = Phase.FAULT
            return
        problem = input_error
        if not math.isfinite(target) or not 4 <= target <= 35:
            problem = problem or "invalid_target"
        elif not self.room or not self.radiator:
            problem = problem or "missing_temperature"
        elif now - self.room.at > self.settings.room_timeout:
            problem = problem or "stale_room_temperature"
        elif now - self.radiator.at > self.settings.radiator_timeout:
            problem = problem or "stale_radiator_temperature"
        elif window_open is None:
            problem = problem or "window_unavailable"

        room_fresh = bool(self.room and now - self.room.at <= self.settings.room_timeout)
        if room_fresh and math.isfinite(target):
            error = target - self.room.value
            if error > self.settings.start_error:
                self._demand = True
            elif error <= self.settings.stop_error:
                self._demand = False
        elif problem:
            # Room demand cannot be measured: preserve bounded maintenance heat
            # while heat mode remains requested; never interpret missing as satisfied.
            self._demand = True

        if problem and self._recovery_reason is None:
            self.trip(problem, now)
        if self._recovery_reason is not None:
            if self.phase == Phase.RECOVERY_HEATING or now < self._recovery_next_open or problem:
                self._recovery(now, problem)
                return
            # One complete timed cycle has ended and valid inputs returned.
            self._recovery_reason = None
            self._recovery_next_open = 0
            self.closed_at = None
            self.peak = None
            self._cooling_required = False
            self.phase = Phase.IDLE
            self.reason = "calibration_retry_ready"
            self.revision += 1

        if exposed:
            self._window(now, target, window_open, fresh_report)
            return

        if self.wants_open:
            if not self._demand:
                self._close(now, "room_satisfied")
            elif now - self.opened_at >= self.settings.max_open_seconds:
                self.trip("maximum_open_duration", now)
            elif fresh_report and self._plateau():
                self._accept_reference(now)
                self._close(now, "plateau")
                if self.stop_temperature is None:
                    self.trip("unusable_calibration", now)
            elif (
                self.phase == Phase.HEATING
                and self.stop_temperature is not None
                and self.radiator.value >= self.stop_temperature
            ):
                self._close(now, "closing_threshold")
            return  # Closing never immediately reopens within the same event.

        self._cool(now, fresh_report, target=target)
        if self._cooling_required:
            self.phase = Phase.SETTLING if self.closed_at is not None else Phase.COOLING
            return
        self.phase = Phase.IDLE
        if not self._demand:
            return
        if self.reference and self.stop_temperature is None:
            self.trip("unusable_calibration", now)
            return
        stale = (
            self._retry_calibration
            or self.reference is None
            or now - self.reference.measured_at >= self.settings.reference_seconds
        )
        # A fresh low cutoff must not produce zero-length open/close loops.
        if (
            not stale
            and self.stop_temperature is not None
            and self.radiator.value >= self.stop_temperature
        ):
            self.phase = Phase.COOLING
            return
        # Calibration must bypass a stale cutoff, including a colder current reference.
        self.phase = Phase.CALIBRATING if stale else Phase.HEATING
        self.opened_at = now
        self._pulse_exposed = False
        self.closed_at = None
        self.reason = "reference_expired" if stale else "room_demand"
        self._trace.clear()
        self._trace.append(Sample(now, self.radiator.value))
        self._candidate.clear()
        self._max_slope = 0
        self.trend = None

    def _discard_episode(self):
        """Keep calibration, but never learn from a window-contaminated curve."""
        self._trace.clear()
        self._candidate.clear()
        self._room_trace.clear()
        self._max_slope = 0
        self.trend = None
        self.peak = None
        self.opened_at = self.closed_at = None
        self.restart_temperature = None
        self.restart_source = None
        self.cooling_fraction_used = None
        self._learn_lag = False
        self._cooling_required = True
        self._demand = False

    def _switch_window(self, now: float, exposed: bool):
        """Change termination policy without restarting or closing a live pulse."""
        was_open = self.wants_open
        self._window_active = exposed
        self._trace.clear()
        self._candidate.clear()
        self._max_slope = 0
        self.trend = None
        self._learn_lag = False
        if self._recovery_reason is not None:
            if was_open:
                self.phase = Phase.RECOVERY_HEATING
                self._recovery_deadline = self.opened_at + self._recovery_limit()
            return
        if exposed and was_open:
            self._pulse_exposed = True
            self.phase = Phase.WINDOW_HEATING
            self._window_deadline = self.opened_at + min(
                PWM_OPEN_SECONDS, self.settings.max_open_seconds
            )
        elif not exposed:
            self._window_next_open = 0
            if was_open:
                stale = (
                    self._retry_calibration
                    or self.reference is None
                    or now - self.reference.measured_at >= self.settings.reference_seconds
                )
                self.phase = Phase.CALIBRATING if stale else Phase.HEATING
                self.reason = "window_closed"
        # An unconfirmed peak exposed to outside air cannot identify thermal lag
        # or a cooling gate. Preserve an already confirmed gate instead.
        if not was_open and self.closed_at is not None:
            self.closed_at = None
            self.peak = None
            self.restart_temperature = None
            self.restart_source = None
            self.cooling_fraction_used = None
            self._cooling_required = True

    def _close_window(self, now: float, reason: str):
        elapsed = max(0, now - self.opened_at)
        self._close(now, reason)
        self._window_next_open = now + max(PWM_CLOSED_SECONDS, PWM_OFF_RATIO * elapsed)
        self.phase = Phase.WINDOW_PAUSE
        self.opened_at = None

    def _window(self, now: float, target: float, window_open: bool | None, fresh_report: bool):
        reason = (
            "window_unavailable"
            if window_open is None
            else cold_reason(self.room.value, self.radiator.value)
        )
        if reason is None and not self._demand:
            reason = "window_room_satisfied"
        if self.phase == Phase.WINDOW_HEATING:
            if reason is None and now < self._window_deadline:
                return
            self._close_window(now, reason or "window_pwm_pause")
            return
        self.phase = Phase.WINDOW_PAUSE
        if reason:
            self.reason = reason
            return
        if self._window_next_open:
            if now < self._window_next_open:
                self.reason = "window_pwm_pause"
                return
            # A completed PWM pause replaces the ordinary cooling gate while
            # exposed. The next pulse still requires current room demand.
        else:
            self._cool(now, fresh_report, target=target)
            if self._cooling_required:
                self.reason = "window_cooling"
                return
        self.phase = Phase.WINDOW_HEATING
        self.reason = "window_pwm"
        self.opened_at = now
        self.closed_at = None
        self.peak = None
        self.restart_temperature = None
        self.restart_source = None
        self.cooling_fraction_used = None
        self._pulse_exposed = True
        self._window_deadline = now + min(PWM_OPEN_SECONDS, self.settings.max_open_seconds)

    def window_permits_open(self, now: float) -> bool:
        return self.phase == Phase.WINDOW_HEATING and now < self._window_deadline

    @property
    def window_deadline(self) -> float | None:
        if self.phase == Phase.RECOVERY_HEATING:
            return self._recovery_deadline
        return self._window_deadline if self.phase == Phase.WINDOW_HEATING else None

    def _plateau(self) -> bool:
        cfg = self.settings
        if len(self._trace) < 3:
            return False
        last = self._trace[-1]
        recent = [s for s in self._trace if s.at >= last.at - cfg.trend_window]
        if len(recent) < 3 or recent[-1].at - recent[0].at < cfg.trend_window * 0.5:
            self._candidate.clear()
            return False
        if any(b.at - a.at > cfg.max_sample_gap for a, b in zip(recent, recent[1:], strict=False)):
            self._candidate.clear()
            return False
        self.trend = slope(recent)
        self._max_slope = max(self._max_slope, self.trend or 0)
        rising = (
            last.value - self._trace[0].value >= cfg.min_rise
            and self._max_slope >= cfg.min_rising_slope
        )
        flat = (
            self.trend is not None
            and -cfg.fall_tolerance
            <= self.trend
            <= min(cfg.plateau_slope, self._max_slope * cfg.plateau_ratio)
            and recent[-1].value >= recent[0].value - cfg.peak_drop
        )
        if not rising or not flat:
            self._candidate.clear()
            return False
        self._candidate.append(last)
        return len(self._candidate) >= 3 and last.at - self._candidate[0].at >= cfg.plateau_seconds

    def _accept_reference(self, now: float):
        self._retry_calibration = False
        temperature = median(s.value for s in self._candidate)
        # Backtrack the near-plateau segment instead of confusing the trend-window
        # confirmation delay with thermal inertia. The onset is still an estimate.
        band = max(
            self.settings.peak_drop, self.settings.plateau_slope * self.settings.trend_window / 60
        )
        knee_at = self._candidate[0].at
        for sample in reversed(self._trace):
            if abs(sample.value - temperature) > band:
                break
            knee_at = sample.at
        self.reference = Reference(
            temperature=temperature,
            measured_at=now,
            knee_at=knee_at,
            trace=list(self._trace),
        )
        self._update_stop()
        self.revision += 1

    def _update_stop(self):
        if self.reference is None:
            self.stop_temperature = None
            return
        value = interpolate(self.reference.trace, self.reference.knee_at - self.lag)
        # Offset must land on observed warming and below the accepted reference.
        self.stop_temperature = (
            value
            if value is not None
            and self.reference.trace[0].value < value < self.reference.temperature
            else None
        )

    def _close(self, now: float, reason: str):
        self._learn_lag = (
            self.phase == Phase.HEATING
            and reason == "closing_threshold"
            and not self._pulse_exposed
        )
        self.closed_at = now
        self.peak = Sample(now, self.radiator.value) if self.radiator else None
        self._cooling_required = True
        self.restart_temperature = None
        self.restart_source = None
        self.cooling_fraction_used = None
        self.phase = Phase.SETTLING
        self.reason = reason

    def _cool(self, now: float, fresh_report: bool, *, target: float | None = None):
        if not self.room or not self.radiator:
            return
        if self.closed_at is not None:
            if not fresh_report:
                return
            if self.peak is None or self.radiator.value > self.peak.value:
                self.peak = self.radiator
            if now - self.peak.at < self.settings.peak_confirm_seconds or (
                self.peak.value - self.radiator.value < self.settings.peak_drop
                and self.peak.value - self.room.value > self.settings.peak_drop
            ):
                return
            lag = self.peak.at - self.closed_at
            if self._learn_lag and 0 < lag <= self.settings.max_lag:
                self.lags.append(lag)
                self._update_stop()
                self.revision += 1
            excess = self.peak.value - self.room.value
            fraction = self._cooling_fraction(now, target)
            # Never raise the gate into the closing threshold. The legacy base
            # gate and ordinary no-reopen check remain intact even for a low cutoff.
            if self.settings.adaptive_cooling and excess > self.settings.peak_drop:
                if self.stop_temperature is None:
                    fraction = self.settings.cooling_fraction
                else:
                    cap = (
                        self.stop_temperature - self.settings.cooling_min_gap - self.room.value
                    ) / excess
                    fraction = max(self.settings.cooling_fraction, min(fraction, cap))
            self.restart_source = "adaptive" if self.settings.adaptive_cooling else "base"
            self.cooling_fraction_used = fraction
            self.restart_temperature = (
                self.room.value + fraction * excess
                if excess > self.settings.peak_drop
                else self.room.value
            )
            self.closed_at = None
            self._learn_lag = False
        if self.restart_temperature is None:
            # Cold-start / restart gate. Does not pretend the previous pulse is known.
            self.restart_temperature = self.room.value + self.settings.startup_excess
            self.restart_source = "startup"
        if self.radiator.value <= self.restart_temperature:
            self._cooling_required = False

    def _recovery_limit(self):
        return (
            min(self.settings.max_open_seconds, PWM_OPEN_SECONDS)
            if self._window_active
            else self.settings.max_open_seconds
        )

    def _recovery_pause(self):
        return PWM_CLOSED_SECONDS if self._window_active else 4 * self.settings.max_open_seconds

    def _close_recovery(self, now: float, reason: str):
        elapsed = max(0, now - self.opened_at)
        gate, source = self.restart_temperature, self.restart_source
        self._close(now, reason)
        self.restart_temperature, self.restart_source = gate, source
        self.closed_at = None
        self.peak = None
        self._learn_lag = False
        self.opened_at = None
        self._recovery_next_open = now + max(
            self._recovery_pause(), (PWM_OFF_RATIO if self._window_active else 4) * elapsed
        )
        self.phase = Phase.RECOVERY_PAUSE
        self.revision += 1

    def _recovery(self, now: float, problem: str | None):
        if problem:
            self._recovery_reason = problem
        cold = None
        if self._window_active:
            room = (
                self.room.value
                if self.room and now - self.room.at <= self.settings.room_timeout
                else None
            )
            rad = (
                self.radiator.value
                if self.radiator and now - self.radiator.at <= self.settings.radiator_timeout
                else None
            )
            cold = cold_reason(room, rad)
        if self._recovery_reason == "window_close_failed":
            cold = None  # Failed window closure must not also remove maintenance heat.
        if self.phase == Phase.RECOVERY_HEATING:
            if now >= self._recovery_deadline or not self._demand or cold:
                self._close_recovery(now, cold or "recovery_pwm_pause")
            return
        self.phase = Phase.RECOVERY_PAUSE
        self.reason = cold or self._recovery_reason
        if now < self._recovery_next_open or not self._demand or cold:
            return
        self.phase = Phase.RECOVERY_HEATING
        self.reason = "recovery_pwm"
        self.opened_at = now
        self.closed_at = None
        self.peak = None
        self._learn_lag = False
        self._recovery_deadline = now + self._recovery_limit()
        self.revision += 1

    def recovery_permits_open(self, now: float):
        return self.phase == Phase.RECOVERY_HEATING and now < self._recovery_deadline

    def suspend_valve(self):
        """Abandon an untrustworthy pulse without declaring a critical fault."""
        self._discard_episode()
        self.phase = Phase.OFF
        self.reason = "valve_reconnecting"
        self.revision += 1

    def trip(self, reason: str, now: float):
        if reason == "actuator_error":
            if self.wants_open:
                self._close(now, reason)
            self.fault = reason
            self.reason = reason
            self.phase = Phase.FAULT
            return
        self._recovery_reason = reason
        self._retry_calibration = True
        self._trace.clear()
        self._candidate.clear()
        self.trend = None
        self._max_slope = 0
        self._learn_lag = False
        if reason == "unusable_calibration":
            self.reset_reference()
        if reason == "maximum_open_duration" or (
            self.closed_at == now and self.opened_at is not None
        ):
            self._close_recovery(now, reason)
        elif self.wants_open:
            self.phase = Phase.RECOVERY_HEATING
            self._recovery_deadline = self.opened_at + self._recovery_limit()
        else:
            self.phase = Phase.RECOVERY_PAUSE
            self._recovery_next_open = now
        self.reason = reason
        self.revision += 1

    def set_restart_gate(self, temperature: float, source: str = "manual"):
        """Set a threshold, never a measured peak or an opening permission."""
        if (
            isinstance(temperature, bool)
            or not math.isfinite(temperature)
            or not -40 <= temperature <= 120
        ):
            raise ValueError("invalid_restart_temperature")
        if source not in {"startup", "base", "adaptive", "manual"}:
            raise ValueError("invalid_restart_source")
        if self.wants_open:
            raise ValueError("restart_gate_during_heating")
        self.restart_temperature = temperature
        self.restart_source = source
        self.closed_at = None
        self.peak = None
        self.cooling_fraction_used = None
        self._learn_lag = False
        self._cooling_required = True
        self.reason = "restart_gate_set"

    def reset_fault(self):
        self.fault = None
        self.phase = Phase.OFF
        self.reason = "fault_reset"
        self._cooling_required = True

    def reset_reference(self):
        self.reference = None
        self.stop_temperature = None
        self.lags.clear()
        self.revision += 1

    def dump(self) -> dict:
        return {
            "schema": 1,
            "settings": asdict(self.settings),
            "reference": asdict(self.reference) if self.reference else None,
            "lags": list(self.lags),
            "retry_calibration": self._retry_calibration,
            "recovery": {
                "reason": self._recovery_reason,
                "resume_at": self._recovery_deadline + self._recovery_pause()
                if self.phase == Phase.RECOVERY_HEATING
                else self._recovery_next_open,
            }
            if self._recovery_reason
            else None,
        }

    def cooling_checkpoint(self, now: float) -> dict | None:
        """Snapshot confirmed cooling at clean shutdown after valve closure.

        Never persist an opening permission or an unfinished peak observation.
        """
        if (
            self.fault
            or self.wants_open
            or self._window_active
            or self.closed_at is not None
            or self.peak is None
            or self.restart_temperature is None
            or self.cooling_fraction_used is None
            or self.room is None
            or self.radiator is None
            or now - self.room.at > self.settings.room_timeout
            or now - self.radiator.at > self.settings.radiator_timeout
        ):
            return None
        return {
            "saved_at": now,
            "restart_temperature": self.restart_temperature,
            "restart_source": self.restart_source,
            "peak": asdict(self.peak),
            "cooling_fraction": self.cooling_fraction_used,
            "room": asdict(self.room),
            "radiator": asdict(self.radiator),
        }

    def restore_cooling(self, checkpoint: dict, now: float) -> bool:
        """Validate against fresh reports; restore the gate, never demand or an open pulse."""
        if (
            self.fault
            or self.wants_open
            or self._window_active
            or not self.room
            or not self.radiator
        ):
            return False
        try:
            saved = checkpoint["saved_at"]
            gate = checkpoint["restart_temperature"]
            fraction = checkpoint["cooling_fraction"]
            peak, room, rad = (Sample(**checkpoint[key]) for key in ("peak", "room", "radiator"))
            values = [
                saved,
                gate,
                fraction,
                peak.at,
                peak.value,
                room.at,
                room.value,
                rad.at,
                rad.value,
            ]
            if any(isinstance(v, bool) or not math.isfinite(v) for v in values):
                return False
            if not 0 <= now - saved <= self.settings.radiator_timeout:
                return False
            if not peak.at <= rad.at <= saved or not room.at <= saved:
                return False
            if (
                saved - rad.at > self.settings.radiator_timeout
                or saved - room.at > self.settings.room_timeout
            ):
                return False
            if not all(-40 <= v <= 120 for v in (gate, peak.value, room.value, rad.value)):
                return False
            if (
                not self.settings.cooling_fraction
                <= fraction
                <= (
                    self.settings.cooling_fraction_max
                    if self.settings.adaptive_cooling
                    else self.settings.cooling_fraction
                )
            ):
                return False
            if gate > peak.value or rad.value > peak.value:
                return False
            if self.room.at <= saved or self.radiator.at <= saved:
                return False
            if (
                now - self.room.at > self.settings.room_timeout
                or now - self.radiator.at > self.settings.radiator_timeout
            ):
                return False
            # Warming during the blind interval contradicts a closed cooling
            # episode. A changed room temperature alone does not move its frozen gate.
            if self.radiator.value > rad.value + self.settings.peak_drop:
                return False
        except KeyError, TypeError, ValueError, OverflowError:
            return False
        self._discard_episode()
        self.restart_temperature = gate
        self.restart_source = "adaptive" if self.settings.adaptive_cooling else "base"
        self.peak = peak
        self.cooling_fraction_used = fraction
        self.reason = "cooling_restored"
        # Recheck today's radiator against the gate, not an old ready-to-open latch.
        return True

    def settings_compatible(self, saved):
        if not isinstance(saved, dict):
            return False
        optional = {
            "adaptive_cooling",
            "cooling_fraction_max",
            "cooling_full_error",
            "room_trend_window",
            "cooling_min_gap",
        }
        defaults = asdict(Settings())
        values = {**{k: defaults[k] for k in optional}, **saved}
        # Operational timeout increase does not change a measured reference/gate.
        if values.get("max_open_seconds") == 900 and self.settings.max_open_seconds == 1200:
            values["max_open_seconds"] = 1200
        return values == asdict(self.settings)

    def restore(self, data: dict, now: float) -> bool:
        """Restore calibration only. Fail closed on malformed/incompatible storage."""
        self.reset_reference()
        try:
            if not isinstance(data, dict):
                return False
            if data.get("schema") != 1 or not self.settings_compatible(data.get("settings")):
                return False
            self._retry_calibration = data.get("retry_calibration", False) is True
            recovery = data.get("recovery")
            if recovery is not None:
                at, reason = recovery["resume_at"], recovery["reason"]
                if (
                    isinstance(at, bool)
                    or not math.isfinite(at)
                    or at > now + 86400
                    or not isinstance(reason, str)
                    or not reason
                ):
                    return False
                self._recovery_reason = reason
                self._recovery_next_open = at
                self._retry_calibration = True
            raw = data.get("reference")
            if raw is None:
                return False
            trace = [Sample(float(p["at"]), float(p["value"])) for p in raw["trace"]]
            ref = Reference(
                float(raw["temperature"]), float(raw["measured_at"]), float(raw["knee_at"]), trace
            )
            numbers = [ref.temperature, ref.measured_at, ref.knee_at]
            numbers += [v for s in trace for v in (s.at, s.value)]
            lags = [float(v) for v in data["lags"]]
            if not all(math.isfinite(v) for v in numbers + lags):
                return False
            if not 3 <= len(trace) <= 2048 or ref.measured_at > now:
                return False
            if not -40 <= ref.temperature <= 120 or any(not -40 <= s.value <= 120 for s in trace):
                return False
            if any(b.at <= a.at for a, b in zip(trace, trace[1:], strict=False)):
                return False
            if not trace[0].at <= ref.knee_at <= trace[-1].at <= ref.measured_at:
                return False
            if not all(0 < v <= self.settings.max_lag for v in lags):
                return False
            self.reference = ref
            self.lags.extend(lags[-7:])
            self._update_stop()
            if self.stop_temperature is None:
                self.reset_reference()
                return False
            return True
        except KeyError, TypeError, ValueError, OverflowError:
            self.reset_reference()
            return False

    def diagnostics(self, now: float) -> dict:
        return {
            "phase": str(self.phase),
            "reason": self.reason,
            "fault": self.fault,
            "recovery_reason": self._recovery_reason,
            "max_open_seconds": self.settings.max_open_seconds,
            "window_pwm_open_seconds": PWM_OPEN_SECONDS,
            "window_pwm_closed_seconds": PWM_CLOSED_SECONDS,
            "recovery_resume_at": (
                self._recovery_deadline + self._recovery_pause()
                if self.phase == Phase.RECOVERY_HEATING
                else self._recovery_next_open
            )
            if self._recovery_reason
            else None,
            "requested_open": self.wants_open,
            "reference_temperature": self.reference.temperature if self.reference else None,
            "reference_age": max(0, now - self.reference.measured_at) if self.reference else None,
            "closing_temperature": self.stop_temperature,
            "restart_temperature": self.restart_temperature,
            "restart_source": self.restart_source,
            "cooling_fraction_used": self.cooling_fraction_used,
            "room_trend": self.room_trend(now),
            "lag_seconds": self.lag,
            "trend": self.trend,
            "peak_temperature": self.peak.value if self.peak else None,
        }
