## Beta 12 validation

177 software tests cover the existing controller and optional emergency notification/backup path. Focused tests exercise real HA state events, changed-limit feedback timeout, explicit-disable closure failure, partial backup failure/retry, mobile retry, real persistent incident reload and manual recovery without stopping ACs. Fake actuator services are registered after real platform setup where required so readback is actually exercised. Local Python 3.14 combined-suite interpreter-finalization instability remains separate from test outcomes; require a clean Linux CI exit before installation. Production delivery and installed revision are recorded in the vault deployment entry.

Changed limit readback on the pilot uses the installed Zigbee2MQTT converter's awaited Zigbee write response. This does not provide continuous heartbeat monitoring or physical stem/heat/flow confirmation. A cached unchanged limit cannot detect a silent failure by itself. No real fault or reserve activation is injected on production.

# Validation

## Current result: beta 11, 2026-10-18

Version `0.1.0b11`: **164 test cases** against HA 2026.9.3 / Python 3.14. The local combined run then exited with SIGSEGV during Python interpreter-finalization garbage collection (Homebrew Python 3.14.7), after pytest reported all cases passed. Focused engine/runtime/window and new recovery suites exited successfully. The full local process has the same interpreter-finalization crash recorded on beta 10. Treat the combined local process result as failed; require a clean Linux CI run before deployment. The faulthandler trace points to managed-dictionary deallocation during finalization, not to a failing controller assertion. Cause remains unproven. Lint, format and deterministic packaging passed locally. Beta 7 also passed both published GitHub CI runs and production `ha core check`; beta 8 deployment results are recorded separately. Five dependency deprecation warnings remain. The historical coverage percentages below are beta 1 measurements, not a current coverage report.

Beta 11 adds bounded non-latching recovery after calibration/data failures. The 20-minute timeout closes into at least 80 minutes off before calibration retry; missing data repeat timed maintenance heat. Window policy is corrected to 15 minutes on / at least 45 minutes off (25%). Fifteen recovery cases cover calibration completion after 15 minutes, repeated outages, pause/retry persistence, data return, explicit off, cached thermostat requests, actual HA actuator deadlines and critical valve failure. Only actuator control failures latch a fault; selected legacy writers inhibit via ownership handover.

Beta 9 supersedes the beta 8 initial-pause behaviour. Window transitions preserve live valve openings and their original timestamp, switch between ten-minute timed and ordinary termination, and impose forty minutes closed only after timed closure. Tests verify both in-place transitions, first-pulse normal readiness, demand hysteresis, late exposure, fresh-only plateau learning and actual HA actuator call continuity. The dated vault record contains the completed beta 9 validation and activation result.

Beta 10 adds independent threshold persistence without a confirmed peak, including startup/manual sources and long downtime. Twelve beta 10 cases cover compatibility/corruption, unknown feedback, no actuator commands during restore, the actual set_restart_temperature action and two successive real HA storage reloads. This parameter does not expire at the radiator timeout; live reports must still be fresh and closed valve limits must be confirmed. Full beta 7 episode reconstruction keeps its stricter validation.

The suite now also covers adaptive cooling, numeric Reference age, window PWM and cold-closure handling, opening interlocks, and confirmed cooling checkpoint persistence. A real HA config-entry reload using its actual Store restores a 29 °C gate and permits fresh room demand at radiator 28 °C after explicit enable. Invalid/old/warming observations, changed settings or bindings, unknown feedback, open windows and failed final closure reject or defer restoration as appropriate. Hardware services remain simulated in these tests.

Production beta 7 was installed through HACS and enabled after fresh reports at 09:55:20 Europe/Prague. Only Anton's Office is migrated. Confirmed hardware observations across the pilot include baseline calibration, ordinary threshold closure, residual peak, adaptive cycles, lower-plateau replacement, scheduled recalibration and interruption of an ordinary pulse by actual window feedback (09:50:51 on beta 6). Numeric Reference age advances live without new timer Recorder rows or selected-device/entity Activity events.

Still pending: a complete physical window PWM cycle, emergency closure at the configured cold thresholds, the first actual production cooling-checkpoint restore, and outage/physical closure assessment. Automated HA reload coverage is not evidence that production has already restored a checkpoint. The first beta 7 upgrade had none to restore because beta 6 did not save one and the preceding pulse was window-interrupted. No production sensor values were fabricated for testing.

Overnight 00:00–08:11 on October 17, target 23.5 °C: room 23.19–23.53 °C (mean 23.358 °C), seven pulses and approximately 70 minutes commanded open (14.3%). This does not establish sustained 24 °C control, measured flow, heat consumption or monetary savings.

The authoritative dated deployment record and operating handoff are in the vault's `projects/home-assistant/subprojects/heating-pulse-controller-v2.md`. The sections below are historical results at their stated release dates; their then-pending steps do not override the current result above.

## Historical beta 2 baseline

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

## Beta 4

Adaptive cooling adds bounded room-trend/deficit-based selection of the frozen cooling threshold, disabled by default. Reference age changes to MM:SS text with a numeric seconds attribute. The expanded 66-test suite covers these changes and legacy calibration compatibility. Real hardware observations on beta 3 verified calibration and one ordinary pulse through post-close peak confirmation; beta 4 adaptation has not yet run on hardware. No consumption-saving claim follows from this baseline. See [beta 4 design](next-stage.md).
