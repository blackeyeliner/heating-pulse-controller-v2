# Adaptive cooling and Reference age (beta 4–5)

Implemented on 2026-10-16 for beta 4, following the first baseline calibration and ordinary pulse on beta 3. Adaptive hardware commissioning began on 2026-10-16; calibration, ordinary cycles, lower-plateau replacement and scheduled recalibration have been observed in the office pilot. Comfort tuning and consumption assessment remain ongoing. Adaptive cooling is experimental and defaults to off, including for existing config entries.

## Adaptive cooling

The fixed base fraction waits until the radiator retains 30% of its peak temperature excess over the room. With a low supply reference, the final part of that wait delivers little heat. An optional higher restart threshold trades deeper cooling for shorter intervals when the room is still below target and making insufficient progress.

At post-close peak confirmation, fit a timestamp-based room-temperature trend over `room_trend_window` (default 300 seconds). Require at least three real reports spanning at least half the window, no gap over `max_sample_gap`, and a latest report no older than that limit. Identical fresh reports count; timer ticks and duplicate timestamps do not. Invalid/gapped histories are discarded. Insufficient trend evidence falls back to the fixed base fraction.

Use the same window as a short projection horizon:

```text
projected_deficit = target - room - max(0, room_trend) * window_seconds / 60
weight = clamp((projected_deficit - start_error) / (cooling_full_error - start_error), 0, 1)
fraction = cooling_fraction + weight * (cooling_fraction_max - cooling_fraction)
restart = room_at_peak_confirmation + fraction * (peak - room_at_peak_confirmation)
```

A rising room receives credit for its observed progress. A flat/falling room receives no such credit; we do not extrapolate a temperature fall. This projection is a control heuristic, not a room thermal model or a guarantee of future temperature.

Provisional development defaults are base fraction 0.3, maximum 0.6, full adaptive increase at a projected deficit of 1 °C, and a 300-second trend/projection window. At the default start error of 0.25 °C, no increase is applied at or below that deficit. The increase ramps continuously between these points. These values are provisional commissioning settings, not established optimal tuning.

Cap any adaptive increase so the restart temperature remains at least `cooling_min_gap` (default 0.5 °C) below the closing threshold. If that cannot be achieved above the existing base gate, retain the base gate and its existing no-reopen-at/above-closing-threshold guard. Without a usable closing threshold, do not apply an adaptive increase.

Freeze the selected threshold for the entire cooling episode, using room temperature at peak confirmation. Later room noise or setpoint changes do not move that gate; setpoint changes still affect heat demand immediately. Adaptation is recalculated at the next confirmed peak. This deliberately does not provide an immediate override of a cooling episode if the room suddenly gets colder midway through it.

Peak confirmation, heat demand, sensor freshness, reference lifetime, plateau detection, fixed actuator opening and early-closing compensation remain separate controls. No time-based forced reopen is introduced. If peak confirmation occurs with heating off, selection uses the base fraction; turning heating off after selection does not change the frozen gate. Startup after restart still uses the original cold-start gate, with control disabled.

The `Restart temperature` sensor exposes the selected fraction as the `cooling_fraction` attribute and the current room trend in °C/min as `room_trend`. The latter is live, not the frozen trend at selection. Changing algorithm options reloads the integration and invalidates calibration under the existing policy. Old beta 1–3 calibration settings are accepted with the new defaults while adaptation remains disabled.

## Reference age

Beta 5 retains the existing entity and unique ID but exposes a numeric duration in **minutes**, with `device_class: duration` and suggested display precision 2. The `formatted` attribute holds total minutes/seconds (`12:34`, `180:00`), floored to whole seconds. The `seconds` attribute and controller diagnostics retain raw numeric seconds. Before calibration, state and both attributes are unknown/None. Internal three-hour expiry remains in seconds; this is only a diagnostic representation change.

Beta 4 used a plain MM:SS text state, causing Activity to treat every timer tick as an event. Numeric duration makes Home Assistant filter the sensor from historical and live Activity, including explicitly selected device/entity views. Any consumer parsing the beta 4 state as MM:SS must use `formatted`; numeric calculations should use `seconds` or account for the state now being minutes. Updates retain the existing runtime cadence.

### Activity / Logbook filtering

The beta 4 text state caused timer spam. The initial workaround below suppressed the general feed only and is insufficient on its own for explicitly selected Activity views. Beta 5 numeric duration resolves that classification issue; the host filter can remain but is not required for numeric duration:

```yaml
logbook:
  exclude:
    entities:
      - sensor.your_room_reference_age
```

Merge this into an existing `logbook` section rather than creating duplicate YAML keys. This host configuration requires an HA restart; reloading the pulse integration alone does not apply it. The filter affects the general Activity feed, not stored Recorder history or the live value. An explicitly entity-filtered Logbook query can still return this sensor's entries; verify the general feed when checking this exclusion. No numeric device/state class is assigned to the MM:SS text as a workaround.

### Recorder exclusion for the timer

On 2026-10-17 the user confirmed that Reference age history is unnecessary. To stop persisting new timer ticks, add the same entity to `recorder.exclude.entities` as well as Logbook exclusion. Live state and algorithm remain available; existing history is not purged. Recorder exclusion alone does not suppress an explicitly selected live Activity stream for a text sensor. Beta 5 numeric duration supplies that missing live filter. Verify historical queries and live streaming separately.

```yaml
recorder:
  exclude:
    entities:
      - sensor.your_room_reference_age
```

## Validation and later rollout

Local verification on 2026-10-16: all 66 tests passed against HA 2026.9.3 with simulated device services; Ruff lint/format and git whitespace checks passed. Five warnings originate in HA/dependency deprecations. Those local checks did not issue live actuator commands; hardware commissioning followed separately.

Tests cover earlier reopening with outstanding demand, peak-confirmation gating, room progress/deficit weighting, sparse/stale trend fallback, a frozen episode threshold, satisfied-demand blocking, the gap below the closing threshold, legacy calibration restore and option validation. Real HA platform tests check unknown age, minute/hour boundaries, numeric attributes, options reload and unchanged entity count.

The baseline pilot verified calibration, a roughly six-minute ordinary pulse, automatic threshold closure and post-close lag learning. Control was turned off before beta 4 publication. The office was subsequently updated/restarted, adaptive cooling enabled through Configure, and control explicitly enabled after fresh reports. The cold-start cooling gate preceded calibration; adaptation selected its first threshold at post-close peak confirmation. For future installations, follow the same setup sequence. No reduction in water consumption is claimed from these tests.

## Known diagnostic issue after restart

`Last decision` initially reads `not_enabled`. Enabling control while the radiator is still above its startup cooling gate changes the phase to cooling but leaves this reason text unchanged until the next heat/open/close decision. It does not indicate the live control switch is off. Inspect Control enabled and Phase for current status. A future diagnostic change should distinguish the current waiting condition from the last action reason; no controller behaviour change is implemented for this issue yet.

Calibration and lag survive restart; the interrupted cooling episode does not. Startup uses room temperature plus `startup_excess` (default 3 °C), not the prior adaptive restart gate. Control remains explicitly disabled until fresh reports and an enable action after every restart/reload.
