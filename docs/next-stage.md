# Beta 4: adaptive cooling and readable reference age

Implemented on 2026-10-16 for beta 4, following the first baseline calibration and ordinary pulse on beta 3. Adaptive hardware commissioning remains pending. Adaptive cooling is experimental and defaults to off, including for existing config entries.

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

The existing Reference age entity and unique ID are retained. Its state becomes a text duration, `MM:SS`, with total minutes (for example `12:34` or `180:00`), floored to whole seconds. It has no numeric unit or state class. Before calibration the state remains unknown.

The numeric age remains in the entity's `seconds` attribute and in downloaded controller diagnostics. Automations that previously parsed the entity state as a number must instead use `state_attr('sensor.…_reference_age', 'seconds')`. No additional entity or dashboard modification is needed. Internal three-hour expiry remains in seconds. Display updates follow the existing runtime update cadence, normally five seconds, not a new per-second timer.

## Validation and later rollout

Local verification on 2026-10-16: all 66 tests passed against HA 2026.9.3 with simulated device services; Ruff lint/format and git whitespace checks passed. Five warnings originate in HA/dependency deprecations. No live actuator commands or deployment were performed.

Tests cover earlier reopening with outstanding demand, peak-confirmation gating, room progress/deficit weighting, sparse/stale trend fallback, a frozen episode threshold, satisfied-demand blocking, the gap below the closing threshold, legacy calibration restore and option validation. Real HA platform tests check unknown age, minute/hour boundaries, numeric attributes, options reload and unchanged entity count.

The baseline pilot verified calibration, a roughly six-minute ordinary pulse, automatic threshold closure and post-close lag learning. Control was turned off before beta 4 publication. Update and restart HA, enable adaptive cooling through Configure, wait for fresh temperature reports, then explicitly re-enable control for supervised commissioning. Expect the cold-start cooling gate before a new calibration; adaptation first selects a threshold at post-close peak confirmation. No reduction in water consumption is claimed from these tests.
