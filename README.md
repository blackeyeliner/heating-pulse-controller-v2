# Heating Pulse Controller v2

Adaptive radiator pulse control as a Home Assistant custom Python integration. It uses a room sensor, a radiator-surface sensor, and a fixed, mechanically reliable Sonoff TRV opening. The room's existing thermostat supplies the requested temperature and heat/off mode.

**Status:** `0.1.0b1`, implemented and locally tested against Home Assistant **2026.9.3 / Python 3.14**. Not installed in production. Available as a HACS custom repository, with a separate ZIP for manual installation. This is an initial pilot, not a commissioned heating system.

## Control behaviour

1. Open only at the configured minimum operating point when the room needs heat and the radiator has cooled sufficiently.
2. With no reference, or a reference at least three hours old, observe a calibration pulse until a sustained plateau is confirmed. Room satisfaction, off commands, missing data and the maximum open duration can stop it earlier.
3. In ordinary pulses, close at the learned, lag-compensated temperature. Continue checking for a lower plateau during every pulse; accepting one replaces the reference and its timestamp.
4. After closure, observe residual warming and then cooling. The next pulse needs both room demand and a radiator below `room + cooling_fraction × (peak − room)`. Freeze this cooling threshold for that episode.

The plateau reference describes radiator surface temperature, not measured inlet-water temperature. No hotter-supply inference is made from the initial slope. Fan operation is independent. Longer pulses are not used to compensate for a cold room. Surface temperature alone cannot establish water consumption or heat delivered per litre.

The core is in [engine.py](custom_components/heating_pulse_controller_v2/engine.py), with no Home Assistant imports or third-party numerical dependencies. The HA runtime handles subscriptions, commands, storage and lifecycle. [Validation notes](docs/validation.md) describe what has and has not been demonstrated.

## Installation through HACS

Custom repository: [blackeyeliner/heating-pulse-controller-v2](https://github.com/blackeyeliner/heating-pulse-controller-v2).

[Releases and installation downloads](https://github.com/blackeyeliner/heating-pulse-controller-v2/releases). This integration is distributed through a custom repository, not the HACS default catalogue.

1. Add its GitHub URL to HACS **Custom repositories**, category **Integration**.
2. Download **Heating Pulse Controller v2**, then restart Home Assistant. If using the initial beta release, enable prerelease versions in HACS.
3. In **Settings → Devices & services → Add integration**, select **Heating Pulse Controller v2**.
4. Select the existing room thermostat, physical TRV, both temperature sensors, and Sonoff opening/closing-limit number entities. Select every legacy automation that can write to the valve or synchronise the thermostats.
5. Review tuning and sensor-reporting semantics before turning on the new **Control enabled** switch.

HACS reads `custom_components/heating_pulse_controller_v2/` directly from the repository. The separately built ZIP is for manual installation/recovery: extract its `custom_components/heating_pulse_controller_v2` directory into HA's `/config/custom_components/`, then restart HA.

Installation and configuration send **no valve commands**. The pilot starts disabled after every HA restart or integration reload. Calibration can be restored, but an interrupted open pulse and the enabled state are never restored. Re-enable explicitly after checking the room. Automatic restart of heating is a later operating-policy decision, not implemented in this beta.

## Actuator and ownership

This beta supports the Sonoff opening/closing-limit interface used by the pilot. It is not a generic adapter for every TRV.

- Close: opening limit `0`, then closing limit `100`.
- Open: first establish closed limits, set the physical thermostat's target and heat mode, then apply the configured opening/closing limits. Pilot defaults `21 / 83` preserve the existing calibrated setting; they do **not** mean a measured water-flow percentage.
- An optional logical valve-position helper mirrors `5` while open and `0` while closed for the existing dashboard. This beta preserves that legacy convention; it is not stem feedback.
- The runtime serialises commands, checks inhibition between service calls, and blocks enablement if any selected legacy automation is not off. If one becomes enabled during operation, it closes and latches a fault. It does not disable the other automation itself.
- Separate integration entries cannot share the same selected actuator. Undeclared writers and manual device changes are outside this ownership check.

A successful service call confirms delivery through HA, not physical stem position. Unload and normal HA shutdown request closure. A HA crash, power loss or Zigbee outage cannot be made fail-closed by this Python integration; the device's outage behaviour must be checked before live commissioning.

## Sensors and tuning

Both temperature sensors must report Celsius. Fresh reports must arrive within the configured timeouts, including when temperature stays constant. The adapter subscribes to both changed states and unchanged `state_reported` events. Attribute-only changes do not create temperature samples, and the initial state snapshot after setup does not count as a fresh report.

**Verify this with the actual sensor/integration.** A cached MQTT payload republished for an unrelated attribute is not necessarily a new physical temperature measurement. The setup checkbox records that verification; it does not perform it. Recorder history omits some unchanged reports and cannot establish this by itself.

Defaults are provisional commissioning values:

| Setting | Default | Purpose |
|---|---:|---|
| Reference lifetime | 3 hours | Calibrate on the next eligible heat request |
| Start / stop room error | 0.25 / 0.10 °C | Room-demand hysteresis |
| Remaining surface temperature excess | 30% | Cooling gate |
| Initial / maximum usable lag | 120 / 300 seconds | Early-closing compensation |
| Trend window | 180 seconds | Regression over actual timestamps |
| Plateau confirmation | ≥60 seconds and ≥3 qualifying samples | Reject isolated flat readings |
| Plateau slope / relative slope | ≤0.15 °C/min / ≤20% | Both conditions must hold |
| Prior warming / rising slope | ≥3 °C / ≥0.5 °C/min | Reject flat cold starts |
| Maximum gap in a usable curve | 120 seconds | Discard interrupted curve evidence |
| Radiator / room timeout | 180 / 1800 seconds | Close and latch a fault on stale data |
| Maximum open duration | 900 seconds | Hard exit while HA is running |
| Peak confirmation | ≥0.2 °C decline after ≥60 seconds | Confirm the end of residual warming |
| Startup surface excess | ≤3 °C above room | Cold-start cooling gate |

A negative slope tolerance of 0.08 °C/min accommodates noise; sustained cooling is not a plateau. Tuning is editable through **Configure**. Saving options reloads the integration, closes an owned valve, disables control and invalidates calibration if algorithm settings changed.

The ordinary closing threshold comes from the observed calibration trace at estimated plateau onset minus residual delay. Several usable post-close peaks refine the delay; diagnostic pulses do not. No extrapolation is allowed: an unidentifiable threshold causes `unusable_calibration`, requiring review rather than guessed heating commands.

## Operation and diagnostics

Each room has nine diagnostic sensors: phase, last decision, reference temperature/age, closing temperature, restart temperature, residual warming delay, radiator trend and last peak. It also has a control-enable switch and buttons to reset a fault or calibration.

Faults are latched. Turn off control, correct the reason (such as stale data or a legacy writer), reset the fault, then enable again. Resetting calibration also requires control to be off. The diagnostic download contains controller values without selected entity IDs or credentials.

Versioned storage retains the reference, its trace and usable delay observations. It does not store an in-progress open pulse. The live loop uses bounded memory and does not query Recorder.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python tools/build_release.py
```

The ZIP and SHA-256 file appear in `dist/`. Its contents are restricted to integration Python, JSON and icons. Local backups, history, credentials and developer tools are excluded.

Recorded-history inspection is read-only:

```sh
uv run python tools/replay_history.py history.json \
  --radiator sensor.example_radiator_temperature \
  --valve input_number.example_valve_position
```

Input is the HA history response array or an object containing it under `data`. Include explicit UTC start/end times when fetching history. Replay checks the detector on the original open intervals; it cannot predict temperatures under different commands or estimate savings.

CI runs lint, formatting, the test suite and packaging on Linux. See [GitHub Actions](https://github.com/blackeyeliner/heating-pulse-controller-v2/actions/workflows/check.yml) for results on each published commit. Before upgrading HA, run tests against the proposed target release and inspect relevant developer API changes. After sensor relocation, valve recalibration or firmware changes, recheck reporting and discard old calibration as appropriate.
