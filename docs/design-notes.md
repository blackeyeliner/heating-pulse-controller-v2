# Design notes

[← README](../README.md) · [Algorithm](architecture.md)

The design addresses high supply pressure, changing water temperature, different radiator sizes and valves with limited positioning accuracy. Recorded heating cycles inform the control rules and regression tests.

## Minimum opening under a high-pressure supply

The design assumes that a radiator can heat adequately at the smallest reliable valve opening. A wider opening admits more water, but the radiator has a finite rate of heat transfer to its surroundings. Beyond a useful flow, increasing circulation can mainly send hotter water back into the return rather than produce a comparable increase in room heating.

That makes pulse timing a useful control variable. The valve opens to a checked operating point, closes while the radiator can continue delivering heat, and reopens when cooling and room demand justify another pulse. The minimum setting is specific to the installation; it is not a universal optimum for every radiator or hydraulic system.

There is also a lower mechanical limit. Smaller movements become unreliable and can lose the expected closed position, allowing water to pass after an apparent closure. The controller therefore avoids trying to regulate flow through increasingly small motor movements.

The intended benefit is more useful heat extracted from the admitted water. Its magnitude cannot be calculated from valve-open time alone: that requires flow and the temperature difference between supply and return, neither of which is currently measured.

## Room measurement and radiator inertia

A sensor inside the valve measures its immediate surroundings, close to the radiator, pipework and wall. During a heating cycle, the difference from the occupied part of the room changes. A fixed offset cannot fully compensate for that changing bias, so the heating request uses an external room sensor.

Even with an accurate room reading, closure happens before all the heat in the radiator reaches the room. The surface may continue warming after water stops entering, then release heat through a long cooling period. Waiting until the room reaches its target can leave more residual heat than is needed.

A surface sensor makes that response observable. The controller learns usable post-close delays and selects a cooling threshold for each radiator. This avoids imposing the same pause on a large radiator and a smaller radiator that cools much faster. It does not require radiator mass or water volume as configuration inputs, although surface temperature remains an indirect indication of stored heat.

## Learning the cutoff

Supply conditions vary enough that a fixed surface-temperature cutoff can become either premature or unreachable. Calibration uses a needed heating pulse to observe a sustained plateau after warming. An accepted trace supplies both a current surface reference and the basis for a cutoff adjusted for residual warming.

The decision combines several checks: prior temperature rise, absolute and relative slope, report timing, confirmation duration and a valid compensated cutoff. A flat cold radiator does not qualify, and an unsuitable new candidate does not erase a previous valid calibration.

Calibration is due after three hours. A lower plateau can replace the reference during an ordinary pulse; hotter supply waits for scheduled recalibration. The initial slope is not used to infer supply temperature because it also depends on the radiator, sensor position and starting conditions.

Pipe-mounted inlet and outlet sensors were considered, but their response during short pulses was a concern. The current implementation uses surface measurements and does not present its reference as measured water temperature.

## Adjusting the interval between pulses

Deep cooling gives the radiator time to release heat, but waiting too long can leave a cold room receiving little useful output. Adaptive cooling balances that interval using the gap to the room target and the room's recent temperature trend.

At peak confirmation, the controller estimates the remaining deficit after allowing for observed warming. A room already making progress needs less intervention. A larger deficit with little warming raises the radiator restart threshold, so the next pulse can begin before as much cooling has occurred.

The adjustment stays within configured bounds and below the closing threshold. It is fixed for the episode, preventing noise from moving the gate continuously. Insufficient room history falls back to the base threshold. A persistent error does not accumulate into ever more aggressive heating, and the room must still request heat when the radiator becomes ready.

An open door makes this especially relevant: a room can feed heat into adjacent spaces without reaching its own target. Wind and ventilation change those losses again. Door-aware behaviour and heating budgets remain future work; the current heuristic is not a model of heat transfer between rooms.

## Physical controls and firmware protection

The TRV remains a usable physical control. Rotation changes the native room target and can enable Heat from Off; firmware mode feedback supports switching it off. MQTT processing distinguishes those actions from retained messages and echoes of the integration's own commands. Without that distinction, synchronising the display could inadvertently create another heating request.

Off also needs an explicit handover. Leaving the valve in Heat with a forced zero opening would prevent its own frost protection from working. The integration instead confirms closure and firmware Off, then permits the checked opening with a full closing limit so firmware can heat on frost demand.

The normal heating target remains saved separately from the frost threshold. During firmware heating, the card shows Antifreeze activity while its mode selector stays Off. This preserves the distinction between an ordinary room request and protection initiated by the valve. Firmware protection still depends on the TRV's temperature source and functioning hardware.

## Windows and cold-room protection

Cold airflow can flatten or reverse the radiator curve even while hot water is entering it. With window feedback, the controller suspends thermal learning and applies up to 15 minutes open followed by at least 45 minutes closed. Opening or closing a window changes the termination rule of an active pulse without resetting the original start time.

If an open configured window coincides with a fresh room reading at or below 16 °C, or a radiator reading at or below 10 °C, the enabled controller requests closure. The cover's reported state is the available feedback; it does not provide independent mechanical confirmation.

Without a bound window entity, the controller cannot reliably distinguish ventilation from other causes of an unusual temperature curve.

## Failures found during operation

### Equal readings were being lost

Plateau detection requires repeated measurements at roughly the same temperature. Equal MQTT sensor values were being discarded before reaching the controller, leaving too little fresh evidence to confirm a plateau.

The integration now handles identical fresh updates. Setup also requires verifying the sensor's actual reporting behaviour; Zigbee2MQTT devices can use `force_update` in their Home Assistant discovery configuration to pass equal readings through to HA.

### Commanded opening differed from device configuration

One recorded pulse showed a logical opening while the device's opening limit had returned to zero. That led to reading both valve limits back after commands and during heating, with correction if the configured pair drifts.

The new checks exposed a subscription race: a fast response could arrive before a temporary listener was ready. The resulting diagnostic timeout could interrupt an otherwise valid heating pulse. The listener now stays subscribed, and isolated missed periodic replies are tolerated only when recent matching confirmation is available. A regression test reproduces the subscription race.

These reads establish the valve's configured limits. They still cannot confirm stem movement or hot-water delivery; a later pulse remained cold despite correct readback.

### Brief infrastructure outages triggered emergency handling

Maintenance interruptions should not immediately start backup heating. Suspected valve-control failures therefore receive ten reconnection attempts, one minute apart. Successful confirmation restores normal operation; a sustained failure disables only the affected room and can trigger a phone notification and backup climates at 20 °C.

Sensor and calibration problems have a different recovery path. They use bounded fallback heating rather than indefinitely disabling a room whose valve can still be controlled. Recovery progress persists through restart, and optional backup requests are tracked individually so one failed unit does not block the others. Backup equipment is stopped manually after an incident.

## Custom integration and lifecycle

The earlier version depended on automations, templates and helpers. As control, protection and recovery paths grew, ownership of valve commands and restoration after updates became harder to follow.

The custom Python component now owns the native thermostat, configuration flow, diagnostics, physical input handling and persistence. It supplies the thermostat card and is distributed through HACS. Existing valve writers can be declared as guards so the integration yields control if one becomes active.

Mode, target and enabled intent survive compatible restarts. Startup reconciles the valve and waits for fresh inputs; it never resumes an interrupted open pulse. Stored calibration and cooling thresholds have their own compatibility checks, while confirmed incidents remain blocked until reset.

The thermal engine is separate from Home Assistant and device transport. Tests can exercise its decisions with timestamped curves, while integration tests cover event delivery, storage and lifecycle behaviour. [Testing and observations](validation.md) describes the checks and remaining limitations.
