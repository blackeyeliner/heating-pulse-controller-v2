# Heating Pulse Controller v2

Adaptive radiator pulse control as a Home Assistant custom Python integration. It uses a room sensor, a radiator-surface sensor, and a fixed, mechanically reliable Sonoff TRV opening. The room's existing thermostat supplies the requested temperature and heat/off mode.

**Status:** `0.1.0b11`, tested against Home Assistant **2026.9.3 / Python 3.14**. Beta 4 adaptive calibration and ordinary cycles have been observed in the office pilot. Beta 5 corrects Reference age Activity spam. Beta 6 adds optional window-aware PWM and emergency window closure. Beta 7 preserves validated cooling episodes across clean restarts. Beta 9 switches window termination in place; beta 10 also persists the last selected cooling threshold independently of a confirmed peak. Beta 11 adds non-latching timed recovery: closed-window failure PWM uses up to 20 minutes on / at least 80 minutes off; open-window PWM uses up to 15 minutes on / at least 45 minutes off (25%) without an initial forced pause. Available as a HACS custom repository, with a separate ZIP for manual installation. Long-term comfort and consumption assessment remain ongoing.

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

Versions before `0.1.0b3` incorrectly classify the controller as a helper: existing entries appear under **Settings → Devices & services → Helpers**. Version `0.1.0b3` uses the service integration type and appears under **Integrations** after an update and HA restart. Existing room configuration and entity IDs are retained; do not delete/recreate the entry.

HACS reads `custom_components/heating_pulse_controller_v2/` directly from the repository. The separately built ZIP is for manual installation/recovery: extract its `custom_components/heating_pulse_controller_v2` directory into HA's `/config/custom_components/`, then restart HA.

Initial installation and configuration send **no valve commands**. Reloading an existing owner requests valve closure; a previously recorded emergency resumes its pending optional actions. The pilot starts disabled after every HA restart or integration reload. Calibration and a validated confirmed cooling episode can be restored, but an interrupted open pulse and the enabled state are never restored. Re-enable explicitly after checking the room. With verified reporting configured and a valid known thermostat setpoint, explicit enable can use bounded recovery PWM while temperature reports are missing; ordinary adaptive operation still requires fresh reports. Automatic restart of heating is a later operating-policy decision, not implemented in this beta.

## Actuator and ownership

This beta supports the Sonoff opening/closing-limit interface used by the pilot. It is not a generic adapter for every TRV.

- Close: opening limit `0`, then closing limit `100`.
- Open: first establish closed limits, set the physical thermostat's target and heat mode, then apply the configured opening/closing limits. Pilot defaults `21 / 83` preserve the existing calibrated setting; they do **not** mean a measured water-flow percentage.
- An optional logical valve-position helper mirrors `5` while open and `0` while closed for the existing dashboard. This beta preserves that legacy convention; it is not stem feedback.
- The runtime serialises commands, checks inhibition between service calls, and blocks enablement if any selected legacy automation is not off. If one becomes enabled during operation, it yields control by closing and inhibiting its own heating; it automatically becomes eligible again once all selected writers are off. This ownership handover is not a valve fault. It does not disable the other automation itself.
- Separate integration entries cannot share the same selected actuator. Undeclared writers and manual device changes are outside this ownership check.

Changed valve-limit commands must be reflected by non-optimistic limit entities within 15 seconds. For the pilot Sonoff/Zigbee2MQTT path, limit state follows the Zigbee write response. Already-matching values are accepted without a new report because HA can suppress identical reports. This is not a continuous heartbeat: a silent disconnect may only be discovered at the next changed command or when the valve becomes unavailable. Limit readback does not prove physical stem movement or water flow. Unload and normal HA shutdown request closure. A HA crash, power loss or Zigbee outage cannot be made fail-closed by this Python integration; the device's outage behaviour must be checked before live commissioning.

## Emergency notification and backup heating

In **Configure**, optionally select an **Emergency notification service** (for example `notify.mobile_app_<phone>`) and multiple **Backup climates**. A critical valve-control failure sends a mobile-app push and requests `heat` / **20 °C** on every selected unit. The room thermostat and its physical TRV cannot be selected as their own backup. Empty selections disable these optional actions.

The trigger is `actuator_error`: unavailable valve/control entities, failed service calls, or missing confirmation of a changed valve limit. Sensor/calibration failures continue recovery PWM and do not activate the reserve. Controllers that are deliberately disabled and do not own the valve do not monitor it for emergencies. HA itself must be running to detect and handle a failure.

**New in beta13:** a new valve failure first enters `valve_reconnecting`. No mobile alert or reserve command is issued during suspicion. Ten retry slots run at +1, +2, …, +10 minutes; each sends only closing commands and requires live non-retained MQTT closed-limit feedback from the selected Sonoff/Z2M device, as well as available HA valve entities. Cached no-op values and retained MQTT messages cannot prove reconnection. The state topic comes from the selected MQTT entity's discovery diagnostics (HA 2026.9.3); unresolved bindings fail the probe rather than guessing a topic. Firmware/stem/flow confirmation remains outside this test.

If a retry succeeds, the suspended enabled controller resumes through normal demand, ownership, fresh-input and cooling rules. An interrupted pulse is discarded without plateau/lag learning. Explicit manual off remains off. After the tenth failed attempt, the critical incident latches and the existing notification/reserve policy runs. Initial failure is not retry1. Pending suspicion and retry count/timing survive restart, while ordinary enabled state still does not; pending closed probes resume even with ordinary heating disabled. Missed slots are not replayed in a burst: downtime can extend the wall-clock grace period. Selected legacy writers pause the probes while they own the valve. A final-close failure during shutdown is persisted as suspicion, not an immediate emergency. Existing confirmed incidents remain confirmed and are not silently reset by this update.

Phase adds `valve_reconnecting`, `valve_reconnect_attempts`, `valve_reconnect_limit`, `valve_reconnect_started_at` and `valve_reconnect_next_attempt_at` attributes. Pending reconnection cannot be bypassed by Reset fault or a fresh enable request. Versions through beta12 triggered the emergency immediately; beta13 adds this confirmation period.

One incident is persisted independently of calibration. A successful push and each successfully requested backup are remembered through reload/restart. Failed actions retry once per minute without blocking valve control; failure of one unit does not prevent the others. A service acknowledgement is not proof that the phone received the push or that an AC physically heats. Successful backup commands are not continually reasserted, so later manual changes are respected. A crash between an external action and its saved acknowledgement can cause a repeat.

After checking the valve, use **Reset fault**, then explicitly enable normal control. **Backup ACs are never automatically turned off** by reset, disable, recovery, or restart: turn them off manually. A stored active emergency resumes pending backup/notification attempts after restart even though ordinary radiator control starts disabled. A new incident after reset can send a new notification. Simultaneous failures in different room entries may each notify; shared backup lists can be requested by each incident.

Phase attributes expose `emergency_active`, `emergency_notification`, `backup_selected`, `backup_requested` and `backup_failed`. A requested backup means both HA service calls completed, not physical heat confirmation. Failed alarm persistence is logged; live attempts continue, but restart deduplication then cannot be guaranteed.

## Sensors and tuning

**New in beta 5:** Reference age is a numeric duration in minutes, with total `MM:SS` retained in its `formatted` attribute and numeric seconds in `seconds`. This prevents timer ticks from appearing in both historical and live Activity. Beta 4 introduced optional adaptive cooling; see [design and migration notes](docs/next-stage.md). Adaptation defaults to off. Enable it through Configure after updating; changing algorithm settings invalidates calibration and disables control until explicitly re-enabled.

Both temperature sensors must report Celsius. Fresh reports must arrive within the configured timeouts, including when temperature stays constant. The adapter subscribes to changed states, unchanged `state_reported` events, and identical forced state updates. Attribute-only changes do not create temperature samples, and the initial state snapshot after setup does not count as a fresh report.

**Verify this with the actual sensor/integration.** A cached MQTT payload republished for an unrelated attribute is not necessarily a new physical temperature measurement. The setup checkbox records that verification; it does not perform it. Recorder history omits some unchanged reports and cannot establish this by itself.

For MQTT sensors, Home Assistant may suppress identical values before creating any state event. Enable `force_update: true` on the temperature entities used by this controller. With Zigbee2MQTT, the device-specific discovery override is:

```yaml
homeassistant:
  temperature:
    force_update: true
```

This affects HA event/history recording, not the sensor's Zigbee reporting interval. Confirm the actual reporting interval separately. Version `0.1.0b2` handles the resulting forced `state_changed` events; `0.1.0b1` does not. Freshness and plateau detection cannot work from equal measurements that HA discards. See [MQTT sensor configuration](https://www.home-assistant.io/integrations/sensor.mqtt/#force_update) and [Zigbee2MQTT discovery overrides](https://www.zigbee2mqtt.io/guide/usage/integrations/home_assistant.html#overriding-discovery-properties).

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
| Radiator / room timeout | 180 / 1800 seconds | Switch to bounded recovery PWM on stale data |
| Maximum open duration | 1200 seconds | Hard exit while HA is running |
| Peak confirmation | ≥0.2 °C decline after ≥60 seconds | Confirm the end of residual warming |
| Startup surface excess | ≤3 °C above room | Cold-start cooling gate |

A negative slope tolerance of 0.08 °C/min accommodates noise; sustained cooling is not a plateau. Tuning is editable through **Configure**. Saving options reloads the integration, closes an owned valve, disables control and invalidates calibration if algorithm settings changed.

The ordinary closing threshold comes from the observed calibration trace at estimated plateau onset minus residual delay. Several usable post-close peaks refine the delay; diagnostic pulses do not. No extrapolation is allowed: an unidentifiable threshold causes `unusable_calibration`, requiring review rather than guessed heating commands.

## Optional window control (beta 6)

Select a **Window motor** through Configure to enable this policy for one room. Leave it empty to preserve ordinary operation. Configuration/reload remains passive and Control enabled must be explicitly turned on; missing temperature reports select bounded recovery PWM. Clearing the selection removes window control.

- An open, opening, closing or partially open cover selects timed termination. If a pulse is running, keep the valve open and its original start time; close when its total opening reaches **15 minutes** (or the configured maximum-open cap, if shorter). If that time has already elapsed when the window opens, close immediately. Clear plateau evidence and suspend reference/lag learning while exposed.
- If the valve is closed when the window opens, retain ordinary room-demand hysteresis and cooling readiness. There is **no initial forced pause** merely because the window opened. The first eligible demand starts a timed pulse.
- After any timed pulse closure, wait **at least 45 minutes** while the window remains open. Then repeat only if the room still requests heat; that completed pause replaces the ordinary cooling gate in window mode. Room satisfaction, off or protection can truncate a pulse; unknown feedback switches to timed recovery. Missed pulses are not replayed; late closure extends the next pause to at least three times elapsed opening. This describes nominal maximum 25% duty for repeated timed cycles, not a rolling flow or heat budget across changes back to ordinary mode.
- No reference, plateau, residual-lag or peak learning occurs during window PWM. Unknown/unavailable window feedback selects conservative 15/45 timed recovery; it is not treated as a confirmed closed window.
- While Control enabled is on, **room ≤16 °C OR radiator ≤10 °C requests immediate `cover.close_cover`**, without first testing a heating response. PWM stops at those boundaries. Window protection also operates during a latched heating fault and with the thermostat off; it is inactive when Control enabled is off or the integration is unloaded. Each fresh, independently cold sensor can trigger closure even if the other sensor is missing. If the window command fails, maintenance PWM continues while closure retries remain bounded. Stale values are not used as current measurements.
- Do not confuse service success with physical closure. If the cover remains exposed (or unknown), closure can be retried at most once per 30 seconds while cold. Command failures are logged and exposed through **Window status**. The controller never opens the window.
- Confirmed window closure returns a running pulse to ordinary termination **without closing/reopening the valve or resetting its start time**. A fresh reference supplies its saved closing threshold; absent/expired reference uses calibration. Plateau detection collects only fresh post-closure samples. A pulse that was exposed is excluded from residual-lag learning. The original maximum-open safety limit still applies across mode switches. If closure occurs during a PWM pause, cancel the remaining timer and resume ordinary cooling and room demand; an unconfirmed window-exposed peak is discarded.
- **Window opening allowed**, a new binary sensor, is off when enabled control sees either cold threshold or lacks fresh temperature data. Add a condition requiring this sensor to be `on` to external automatic opening routines, including group commands that could reopen this window. Manual reopening while cold causes another emergency close. This interlock does not itself rewrite external automations.

The cover state is the motor's logical feedback, not an independent contact sensor. These thresholds are the pilot owner's selected operating limits, not a certification against freezing. A sensor can miss a colder part of the radiator; closure needs working hardware, HA and communication. Existing faults remain latched and are not automatically reset by a window event.

## Operation and diagnostics

Each room has nine core diagnostic sensors: phase, last decision, reference temperature/age, closing temperature, restart temperature, residual warming delay, radiator trend and last peak. It also has a control-enable switch and buttons to reset a fault or calibration.

Only inability to control the physical valve (`actuator_error`, failed service commands or unavailable valve entities) is a latched fault. Correct valve communication, then reset the fault with control off and enable again. A successful HA call is still command delivery, not physical position proof.

Calibration/ordinary maximum-open duration is now **20 minutes**. If it expires without a usable closing condition, close and count that opening as the on portion of one recovery cycle: **at least 80 minutes off**, then retry calibration on room demand. There is no additional bonus PWM pulse after the timeout. Late closure extends the pause to at least four times elapsed opening. Keep the failed curve out of reference/lag learning.

Other data/calibration errors use timed maintenance heat instead of latching off. With a closed window: up to 20 minutes on / at least 80 minutes off. With an open or unknown window: up to 15 minutes on / at least 45 minutes off; late closure extends pause to at least three times actual opening. Room satisfaction truncates/skips heat when a fresh room measurement is available. If room temperature is missing/stale, requested heat mode allows bounded maintenance PWM without inventing a room temperature. An unavailable room thermostat uses the last valid heat/off request and setpoint seen by this runtime; no initial unknown setpoint is guessed. Explicit Control off and a reported thermostat off always stop heating.

A complete timed pause remains mandatory when data returns. At the next eligible cycle, fresh valid inputs return control to a calibration attempt; otherwise PWM repeats. Plateau/reference/residual-lag learning is suspended during recovery. Recovery pauses and pending calibration retries persist through reload/restart, but an open pulse and enabled state never resume. Phase attributes expose `recovery_reason` and `recovery_resume_at` (UTC epoch seconds). Resetting calibration still requires control off.

The emergency window-close policy remains active while control is enabled, using any independently fresh cold sensor. Data failure never substitutes fabricated readings. Actual actuator failure still stops this integration, and HA/device/network outages cannot be made fail-closed by software alone.

Versioned storage retains the reference, its trace and usable delay observations. On clean shutdown/reload after successful valve closure it also records a confirmed cooling peak, frozen restart threshold/fraction, timestamp and latest sensor observations. Startup consumes that checkpoint before subscribing, so a later crash cannot resurrect a previous episode. It never restores a running pulse, room-demand latch, ready-to-open latch or enabled state.

The selected restart threshold is also stored separately with its origin (`startup`, `base`, `adaptive`, or `manual`) and matching settings/sensor/actuator/window bindings. It survives updates and restarts even without a confirmed peak, during settling/window exposure, or after a longer downtime. Restore the parameter only after fresh post-start sensor reports, known window feedback and closed TRV limits; never restore a demand/ready latch, measured peak, PWM timer or enabled state from this record. Fresh radiator temperature still has to satisfy the restored threshold. The threshold record does not expire with the 180-second sensor timeout; that timeout still applies to current sensor readings. Settings/binding changes or malformed data invalidate it.

When a complete beta 7 cooling episode validates, retain its full information. Otherwise the independent threshold can restore without inventing a peak. Restart temperature exposes `source`; `restore_status: restored_gate` identifies this threshold-only path. A missing old checkpoint is no longer a reason to replace a saved threshold with room +3 °C. Releases before beta 10 did not write the independent record.

For a one-time threshold adjustment, use the action `heating_pulse_controller_v2.set_restart_temperature` with `config_entry_id` and `temperature` (°C). It is allowed only while not heating and without a fault. This sets and persists the current gate with source `manual`; it does not change the room setpoint, force a pulse, or permanently fix the threshold. The next confirmed peak recalculates the gate normally.

Cooling restoration waits for fresh post-start reports from both sensors, a logically closed window (or no configured window), and closed TRV limits. Settings and selected sensor/actuator/window bindings must match. The checkpoint must still be within the configured radiator freshness timeout (180 seconds by default), temperatures/timestamps/fraction must be valid, and the radiator must not have warmed more than the peak-drop tolerance during the blind interval. Observed window opening discards it. Unknown window feedback waits within that same deadline. After restoration, current temperature must still satisfy the saved gate before heating can begin.

Unconfirmed peaks, interrupted heating/calibration, window PWM, faults, stale/changed/incompatible observations and failed final closure do not qualify for full episode restoration. The independent threshold still remains eligible under its own compatibility/fresh-feedback checks; only if neither record is usable does startup use room +3 °C. Versions before beta 7 saved no cooling episode; versions before beta 10 saved no independent threshold. Missing historical information cannot be reconstructed automatically. The Restart temperature sensor's `restore_status` attribute explains whether the checkpoint was restored, absent, waiting or discarded. The live loop uses bounded memory and does not query Recorder.

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python tools/build_release.py
```

The ZIP and SHA-256 file appear in `dist/`. Its contents are restricted to integration Python, JSON, service YAML and icons. Local backups, history, credentials and developer tools are excluded.

Recorded-history inspection is read-only:

```sh
uv run python tools/replay_history.py history.json \
  --radiator sensor.example_radiator_temperature \
  --valve input_number.example_valve_position
```

Input is the HA history response array or an object containing it under `data`. Include explicit UTC start/end times when fetching history. Replay checks the detector on the original open intervals; it cannot predict temperatures under different commands or estimate savings.

CI runs lint, formatting, the test suite and packaging on Linux. See [GitHub Actions](https://github.com/blackeyeliner/heating-pulse-controller-v2/actions/workflows/check.yml) for results on each published commit. Before upgrading HA, run tests against the proposed target release and inspect relevant developer API changes. After sensor relocation, valve recalibration or firmware changes, recheck reporting and discard old calibration as appropriate.
