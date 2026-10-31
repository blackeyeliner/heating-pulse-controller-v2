# Operating guide

[← README](../README.md) · [Installation](installation.md) · [Algorithm](architecture.md)

## Heat, Off and Control enabled

Use the native thermostat to set the room's heating mode and target. **Control enabled** determines whether the integration may operate; it starts off for a new entry and is remembered across compatible restarts once enabled.

| Thermostat mode | Behaviour |
|---|---|
| **Heat** | Request heating toward the saved room target. Each pulse still depends on demand, radiator cooling and the controller's checks. |
| **Off** | Stop ordinary pulses and hand eligible frost protection to the physical TRV. Show the frost threshold while retaining the normal heating target separately. |

Changing the displayed temperature through HA while Off changes the frost threshold. The saved Heat target is available in `heating_target_temperature` and returns when Heat is selected. If the frost reading is unavailable, the integration shows it as unknown.

### Physical controls

Turning the valve's dial from Off selects Heat and changes the room target. The supported range is 4–35 °C. On the tested firmware, the button sequence in Heat can switch it Off; a press in Off may only wake the screen and sends no separate button event.

The integration follows the firmware's reported mode and target changes. It filters retained MQTT snapshots and its own command feedback so these are not interpreted as new physical input.

### Frost protection

When entering Off, the integration first closes the valve and confirms its firmware mode. It then permits the checked opening setting with a full closing limit, allowing firmware to open on its own frost demand. An opening limit of zero would prevent that protection from heating.

During reported frost heating, the card shows **Antifreeze** in red on the active arc, status and current room temperature. The mode selector remains Off. Selecting Heat returns control to the room thermostat.

Firmware protection uses the TRV's temperature source, which may differ substantially from the external room sensor. It is not a guarantee that the whole room stays at the displayed frost threshold.

## Diagnostics

| Entity | Meaning |
|---|---|
| Phase | Current state: for example, idle, calibrating, heating, cooling or recovery_pause. |
| Last decision | Reason for the last action. It may not describe the current waiting condition. |
| Reference temperature | Surface temperature accepted during calibration. |
| Reference age | Age in minutes. Attributes also provide `formatted` MM:SS and numeric `seconds`. |
| Closing temperature | Cutoff used for ordinary heating pulses. |
| Restart temperature | Radiator temperature below which another pulse may become eligible. Attributes show its source and restoration status. |
| Radiator trend | Estimated temperature slope in °C/min. Plateau detection also checks the preceding rise and confirmation interval. |
| Residual warming delay | Learned delay between closure and the end of surface warming, using suitable observations. |
| Valve position | Logical 0 / 5 display: closed/open command, or unknown when unconfirmed. |

The valve graph retains the earlier controller's 0 / 5 convention. It is not measured valve travel. In frost mode it follows reported firmware activity. Its `estimated_opening_percent` attribute is also an estimate; `position_measured` remains false.

Phase attributes include the latest confirmed limit pair and time, consecutive missed reads, total periodic read timeouts, corrections, reconnection progress and emergency state. Read counters apply to the current runtime and can reset after a restart.

## Settings

Change settings after inspecting complete heating and cooling cycles. A long pulse can mean slow heating, an unsuitable plateau criterion, missing reports or no hot water; those cases need different responses.

| Setting | Software default | Purpose |
|---|---:|---|
| Reference lifetime | 3 hours | Recalibrate on the next eligible request. |
| Start / stop room error | 0.25 / 0.10 °C | Hysteresis for room demand. |
| Base cooling fraction | 0.30 | Remaining surface-temperature excess above the room at restart. |
| Adaptive cooling | Off | Allow a bounded earlier restart based on room deficit and progress. |
| Initial / maximum usable lag | 120 / 300 seconds | Compensation for warming after closure. |
| Trend window | 180 seconds | Fit the slope using actual report timestamps. |
| Plateau confirmation | At least 60 seconds | Require sustained flattening across fresh reports. |
| Plateau absolute / relative slope | 0.15 °C/min / 20% of maximum slope | Both limits must hold. |
| Prior warming / rising slope | 3 °C / 0.5 °C/min | Prevent a cold flat trace from qualifying. |
| Maximum sample gap | 120 seconds | Reject interrupted curve evidence. |
| Radiator / room timeout | 180 / 1800 seconds | Enter bounded recovery when readings are stale. |
| Maximum opening | 1200 seconds | Limit each opening while the runtime is functioning. |
| Peak confirmation | 0.2 °C decline after at least 60 seconds | Confirm the end of residual warming. |
| Startup surface excess | 3 °C above room | Initial cooling threshold if no stored gate can be used. |

Maximum opening is configurable per entry. Saved options take precedence over the software defaults. The [adaptive cooling reference](next-stage.md) gives the threshold calculation and its bounds.

For a one-time adjustment of the current restart threshold, call `heating_pulse_controller_v2.set_restart_temperature` with `config_entry_id` and `temperature`. This is allowed while not heating and without a fault. The value is saved as a manual threshold; the next confirmed peak recalculates it. It does not change the room target or force a pulse.

Changing algorithm options reloads the entry and may invalidate incompatible calibration. Restored settings still require current sensor readings and valve checks before heating.

## Recovery and emergency response

### Temperature and calibration problems

Reaching the opening limit closes the valve and starts a recovery pause at least four times the actual open duration. With the default 20-minute opening limit, the pause is at least 80 minutes. When the pause ends, the controller retries calibration if the room still needs heat.

Other non-actuator input failures use bounded maintenance pulses. With a closed window, the opening limit comes from the room's configured maximum, followed by at least four times that duration closed. With an open or unknown configured window, the policy is up to 15 minutes open and at least 45 minutes closed; late closure extends the pause. Fresh room satisfaction and explicit Off still stop heating.

Returning data does not cancel a recovery pause already in progress. Learning is suspended during fallback heating.

### Valve-control failure

A failed command or unconfirmed valve state first enters reconnection handling:

1. Attempt closure and suspend new openings.
2. Make ten attempts, one minute apart, requiring fresh closed feedback. A successful attempt allows normal operation to resume.
3. If all attempts fail, record a critical incident and disable the affected room controller.
4. If configured, send a phone notification and request every selected backup climate in **Heat at 20 °C**.

The notification service and backup climates are optional Configure fields. Failed actions retry independently. Successful requests are remembered, so the controller does not keep overriding later manual changes. Other radiator controllers continue operating.

After checking the valve, use **Reset fault** and explicitly enable control. **Stop backup climates manually**: recovery, reset and restart do not turn them off. Acknowledged service calls show that HA accepted the request; they cannot confirm phone delivery or physical heat output.

Recovery and incident progress survive restart. Downtime may extend the reconnection period because missed attempts are not replayed in a burst. Multiple affected rooms can each notify and request the same backup equipment.

## Other valve controllers and outages

All selected legacy automation guards must be off. If one is enabled, this integration closes and yields control until it is off again. That is an ownership handover, not a valve fault. Writers that were not declared are outside this check.

On normal shutdown or unload, the integration requests closure when it owns the valve. A stopped HA process cannot issue that command or enforce an opening deadline. Check the actual device behaviour during power and communication outages as part of commissioning.

## Troubleshooting

| Symptom | Check |
|---|---|
| Room below target but no pulse | Demand hysteresis, Phase, restart threshold, reporting freshness, automation guards and recovery timer. |
| Last decision says `not_enabled` while control is on | Current Phase and Control enabled. The decision text can remain from an earlier action. |
| Repeated calibration timeouts | Full temperature trace, equal-value reporting, gaps and physical warming. |
| Reported opening but cold radiator | Limit readback and physical supply/valve behaviour. Confirmed configuration alone does not establish flow. |
| Missing custom card after an update | Refresh the browser tab after HA has restarted. |
| Too much Reference age history | Exclude that entity from Recorder if its history is unnecessary. Live state and attributes remain available. |

Include a complete cycle and its timestamps when [reporting a problem](../.github/ISSUE_TEMPLATE/bug_report.yml).
