# Adaptive cooling

[← Algorithm](architecture.md) · [Operating guide](operation.md)

Adaptive cooling changes how far the radiator must cool before another pulse is allowed. It defaults to **off**. This file retains its original name for links from earlier release notes.

## Why adjust the threshold?

At the base setting, the controller waits until the radiator retains 30% of its peak temperature difference above the room. When the peak is low, the radiator may provide little heat near the end of that wait, even though the room is still below target.

The optional adjustment permits an earlier restart when the room is cold and its recent temperature trend shows insufficient progress. It keeps the same valve opening and still requires a heat request. It does not estimate the room's heat capacity or determine where heat is escaping.

## Calculation

At post-close peak confirmation, fit a room-temperature trend over `room_trend_window`, which defaults to 300 seconds. Use at least three real reports spanning half the window, with no excessive gaps and a sufficiently recent last report. Repeated equal measurements count; timer ticks do not. If the data is insufficient, use the base fraction.

```text
projected_deficit = target − room − max(0, room_trend) × window_seconds / 60
weight = clamp((projected_deficit − start_error) / (cooling_full_error − start_error), 0, 1)
fraction = cooling_fraction + weight × (cooling_fraction_max − cooling_fraction)
restart = room_at_peak_confirmation + fraction × (peak − room_at_peak_confirmation)
```

`room_trend` is in °C/min. A rising temperature reduces the estimated deficit. For a flat or falling temperature, the calculation uses the current deficit without projecting a further fall.

With defaults, the fraction ranges from 0.30 to 0.60. The increase begins above a projected deficit of 0.25 °C and reaches its full value at 1 °C. The restart temperature must also remain at least `cooling_min_gap`—0.5 °C by default—below the closing temperature. If a valid increase cannot be made, the base gate remains in use.

## Behaviour during a cycle

The selected threshold is fixed for the cooling episode. Later sensor noise or target changes do not move it; target changes still affect whether the room requests heat. The next confirmed peak produces a new threshold. If peak confirmation occurs while heating is off, the base fraction is used.

This means a sudden temperature drop during cooling does not immediately override the current gate. It also means the controller does not keep increasing the threshold merely because a deficit has lasted a long time.

## Diagnostics and restart

Restart temperature exposes the selected `cooling_fraction`, its `source`, and restoration status. The `room_trend` attribute is live and may differ from the value used to select that episode's threshold.

A compatible threshold can be restored independently of a complete cooling checkpoint. Startup still requires fresh feedback and verified closure. Changing algorithm settings can invalidate the stored calibration. For a one-time manual gate adjustment, see the [operating guide](operation.md#settings).
