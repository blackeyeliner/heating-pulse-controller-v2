"""Deterministic pulse control. No Home Assistant or third-party imports.

Times are seconds on a caller-supplied, monotonic epoch clock. Only actual
temperature reports advance the signal detector; timer ticks never add samples.
"""

import math
from collections import deque
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from statistics import median


class Phase(StrEnum):
    OFF = "off"
    IDLE = "idle"
    HEATING = "heating"
    CALIBRATING = "calibrating"
    SETTLING = "settling"
    COOLING = "cooling"
    FAULT = "fault"


@dataclass(frozen=True)
class Settings:
    """Provisional defaults; commissioning is explicit, never automatic."""

    reference_seconds: float = 10800
    cooling_fraction: float = 0.3
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
    max_open_seconds: float = 900
    peak_drop: float = 0.2
    peak_confirm_seconds: float = 60
    startup_excess: float = 3

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(field.name)
        if not 0 < self.cooling_fraction < 1 or not 0 < self.plateau_ratio < 1:
            raise ValueError("fractions")
        if self.stop_error >= self.start_error:
            raise ValueError("hysteresis")
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
        self.peak: Sample | None = None
        self.trend: float | None = None
        self.opened_at: float | None = None
        self.closed_at: float | None = None
        self._trace: deque[Sample] = deque(maxlen=2048)
        self._candidate: list[Sample] = []
        self._max_slope = 0.0
        self._last_evaluated_report = -math.inf
        self._last_now = -math.inf
        self._learn_lag = False
        self._cooling_required = True
        self._demand = False
        self.revision = 0

    @property
    def wants_open(self) -> bool:
        return self.phase in (Phase.HEATING, Phase.CALIBRATING)

    @property
    def lag(self) -> float:
        return median(self.lags) if self.lags else self.settings.bootstrap_lag

    def room_report(self, value: float, at: float):
        self.room = self._sample(value, at, self.room)

    def radiator_report(self, value: float, at: float):
        previous = self.radiator
        self.radiator = self._sample(value, at, previous)
        if self.radiator is None or self.radiator is previous:
            return
        if previous and at - previous.at > self.settings.max_sample_gap:
            # A peak could have occurred in the unseen interval.
            self._learn_lag = False
        if self.wants_open:
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

    def evaluate(self, now: float, *, enabled: bool, heat_mode: bool, target: float):
        if now < self._last_now:
            self.trip("clock_moved_backwards", self._last_now)
            return
        self._last_now = now
        fresh_report = bool(self.radiator and self.radiator.at > self._last_evaluated_report)
        if fresh_report:
            self._last_evaluated_report = self.radiator.at

        if not enabled or not heat_mode:
            if self.wants_open:
                self._close(now, "disabled" if not enabled else "thermostat_off")
            self._cool(now, fresh_report)
            self._demand = False
            self.phase = Phase.FAULT if self.fault else Phase.OFF
            return
        if self.fault:
            self.phase = Phase.FAULT
            return
        if not math.isfinite(target) or not 4 <= target <= 35:
            self.trip("invalid_target", now)
            return
        if not self.room or not self.radiator:
            self.trip("missing_temperature", now)
            return
        if now - self.room.at > self.settings.room_timeout:
            self.trip("stale_room_temperature", now)
            return
        if now - self.radiator.at > self.settings.radiator_timeout:
            self.trip("stale_radiator_temperature", now)
            return

        error = target - self.room.value
        if error > self.settings.start_error:
            self._demand = True
        elif error <= self.settings.stop_error:
            self._demand = False

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

        self._cool(now, fresh_report)
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
            self.reference is None
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
        self.closed_at = None
        self.reason = "reference_expired" if stale else "room_demand"
        self._trace.clear()
        self._trace.append(Sample(now, self.radiator.value))
        self._candidate.clear()
        self._max_slope = 0
        self.trend = None

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
        self._learn_lag = self.phase == Phase.HEATING and reason == "closing_threshold"
        self.closed_at = now
        self.peak = Sample(now, self.radiator.value) if self.radiator else None
        self._cooling_required = True
        self.restart_temperature = None
        self.phase = Phase.SETTLING
        self.reason = reason

    def _cool(self, now: float, fresh_report: bool):
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
            self.restart_temperature = (
                self.room.value + self.settings.cooling_fraction * excess
                if excess > self.settings.peak_drop
                else self.room.value
            )
            self.closed_at = None
            self._learn_lag = False
        if self.restart_temperature is None:
            # Cold-start / restart gate. Does not pretend the previous pulse is known.
            self.restart_temperature = self.room.value + self.settings.startup_excess
        if self.radiator.value <= self.restart_temperature:
            self._cooling_required = False

    def trip(self, reason: str, now: float):
        if self.wants_open:
            self._close(now, reason)
        self.fault = reason
        self.reason = reason
        self.phase = Phase.FAULT

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
        }

    def restore(self, data: dict, now: float) -> bool:
        """Restore calibration only. Fail closed on malformed/incompatible storage."""
        self.reset_reference()
        try:
            if not isinstance(data, dict):
                return False
            if data.get("schema") != 1 or data.get("settings") != asdict(self.settings):
                return False
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
            "requested_open": self.wants_open,
            "reference_temperature": self.reference.temperature if self.reference else None,
            "reference_age": max(0, now - self.reference.measured_at) if self.reference else None,
            "closing_temperature": self.stop_temperature,
            "restart_temperature": self.restart_temperature,
            "lag_seconds": self.lag,
            "trend": self.trend,
            "peak_temperature": self.peak.value if self.peak else None,
        }
