# Validation of 0.1.0b2

Date: 2026-10-16. Development environment: Python 3.14.7, Home Assistant 2026.9.3, matching the pilot's installed HA Core version. Tests use a temporary HA config directory and fake actuator services; none issue production commands.

**Local result:** 52 tests passed; lint and formatting checks passed. The beta 1 coverage baseline was 88% overall and 91% for the core. Five deprecation warnings originate in HA/aiohttp/backoff dependencies. The ZIP build and contents checks passed. GitHub CI results are recorded in the repository Actions tab; live hardware commissioning has not run.

## Automated coverage

- Pure state transitions: cold startup, ordinary threshold closure, lower-plateau replacement and age reset, three-hour expiry at cycle start, demand cancellation, cooling gates and off/on continuity.
- Curve handling: timestamp-based regression, reports every minute, absent/invalid reports, interrupted curves, declining traces, unusable lag compensation and bounds.
- Persistence: valid calibration restores without open state; malformed/incompatible data is discarded.
- Real HA event dispatch: unchanged reports and identical forced state updates count, attribute-only writes do not, setup waits for fresh readings.
- Actuator sequencing and ownership: legacy guards, command failure, disable during initial reconciliation or an open command, closure on unload, and no stale callbacks after unload.
- Actual HA config entry/platform setup: UI config flow, duplicate actuator rejection, all twelve entities, options reload, integration reload and unload.
- Packaging: deterministic installation ZIP containing only integration files; English translation and manifest metadata checks.

These are adapter tests against installed HA code, not just mocked HA imports. Hardware service delivery is simulated, so RF reliability, firmware interpretation and physical closure still need the office pilot.

## Recorded office history

The read-only replay covered 2026-10-13 10:27 UTC through 2026-10-16 10:27 UTC: 1,628 radiator history entries and ten logical open intervals. Original history and detailed replay output remain under local-only storage, outside the installation package.

The detector accepted **no complete calibration** in these recorded intervals. This is a limitation, not evidence of successful live plateau identification:

- Two cold-start pulses lasted about 579 and 612 seconds. Surface temperature was still approaching its peak when the old controller closed; the records contain insufficient sustained open-valve plateau evidence under the provisional settings.
- Other intervals include a hot restart with insufficient fresh warming, sustained falling temperature, and gaps in recorded state changes. Several old cycles have only milliseconds between logical close and reopen; logical position alone does not prove a hydraulic interruption.
- The last 111-second pulse contained only one changed radiator sample while logically open. Its observed surface peak was about 119 seconds after closure, consistent with using two minutes as an initial *proposal*, but insufficient to establish a reliable universal lag.
- The near-saturated long pulses show post-close maxima about 32–35 seconds later; this illustrates why lag is learned from ordinary short pulses rather than diagnostic plateaus. Sampling and late closure also affect that observation.

Recorder state changes do not include every fresh unchanged sensor report. Replay does not insert synthetic equal readings, use post-close flattening as an open-valve plateau, or extrapolate the thermal trajectory after a different closing command. Synthetic curves exercise successful calibration; successful real calibration remains to be demonstrated.

## Commissioning still required

1. Install the published HACS custom repository/release only for the office pilot.
2. Verify that the actual sensor feed distinguishes fresh equal measurements from cached updates, and measure reporting cadence while flat.
3. Select initial tuning explicitly. The README defaults are a proposed starting set, not approved production parameters.
4. Verify Sonoff command behaviour and the operating procedure after HA/Zigbee outages.
5. Enable the controller while observing one complete calibration, ordinary pulse and cooling cycle. Inspect the estimated knee, lag and closing threshold against the live curve.
6. Check comfort and available consumption measurements over subsequent cycles. No water-saving percentage is claimed.

The beta intentionally requires explicit re-enabling after every restart/reload. Production unattended restart policy is still a separate decision.

## Live reporting check and beta 2 fix

On 2026-10-16, both selected office sensors had temperature reporting configured for 10 s minimum / 60 s maximum. Zigbee2MQTT logs showed repeated equal temperatures at approximately one-minute intervals. HA did not advance the corresponding state timestamps for these identical MQTT sensor values. Inspection of the installed HA 2026.9.3 MQTT code confirmed that unchanged values are discarded unless `force_update` is enabled.

Beta 2 accepts the identical forced `state_changed` events generated with that setting; beta 1 only accepted ordinary value changes and `state_reported` events. A regression test exercises the real HA state machine with `force_update=True`. Attribute-only events remain excluded. This fixes the transport prerequisite for plateau detection; it does not replace live heating-cycle validation.
