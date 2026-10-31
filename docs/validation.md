# Testing and observations

[← README](../README.md) · [Design notes](design-notes.md)

Validation covers the thermal algorithm, Home Assistant integration, device communication and behaviour on real hardware. Each level checks a different part of the system: a simulated service response can test command handling, but cannot establish whether a valve moved or a radiator received hot water.

## Automated tests

Linux CI runs lint and formatting checks, the Python and frontend suites, and the installation-package build. The development dependencies pin the Home Assistant test environment; current results and coverage are available in [GitHub Actions](https://github.com/blackeyeliner/heating-pulse-controller-v2/actions/workflows/check.yml).

| Area | Examples covered |
|---|---|
| Heating algorithm | Demand hysteresis, reference expiry, plateau detection, cutoff calculation, residual warming and adaptive cooling thresholds. |
| Sensor input | Irregular timestamps, fresh equal readings, stale data, gaps and falling curves. |
| Home Assistant | Real state events, config entries, platforms, Store persistence, reload and unload. |
| Valve communication | Ordered commands, correlated reads, late/unrelated replies, drift correction and retry deadlines. |
| Recovery | Reconnection attempts, manual Off, persisted incidents and partial notification/backup failure. |
| Interface and package | Native climate, physical MQTT controls, card behaviour and installation ZIP contents. |

Hardware service delivery is simulated in the Python tests. The frontend suite uses a Node test harness. Tests against the pinned HA version do not establish compatibility with every later release or device firmware.

[Test suite](../tests) · [Development setup](../CONTRIBUTING.md)

## Hardware checks

Checks on installed hardware have exercised:

- Calibration, ordinary cutoff closure, residual warming and cooling.
- Lower-reference replacement and scheduled recalibration.
- Physical target and mode changes reflected in the native thermostat.
- Firmware frost handover and Antifreeze display while the thermostat remains Off.
- Automatic recovery after a compatible restart, with saved requests and settings retained.
- Correlated valve-limit reads through the live MQTT/Zigbee2MQTT path.

Installation checks compare deployed files with the release and run the HA configuration check before observing startup. A software state is compared with the radiator's physical response wherever possible.

## Regression testing

Recorded failures are used to construct focused tests. For example, a fast device response exposed a race in which a temporary MQTT listener was not ready before the request was sent. The regression test delays subscription setup and delivers an immediate response. The persistent-listener implementation must handle it without interrupting the pulse.

Other tests cover unusable calibration candidates, missing reports, configuration drift and replies that arrive after a request has timed out. These tests establish how the controller handles a known condition; they do not reproduce every possible hydraulic or radio environment.

## Remaining limitations

- **Some curves still reach the calibration timeout.** Nearly flat or falling traces can fail to establish a usable reference before the deadline. The controller enters bounded recovery; detector behaviour remains under investigation.
- **Correct limits do not prove heat delivery.** A radiator can remain cold or begin cooling while its valve settings are confirmed open. Readback cannot identify a mechanical obstruction or a change in hot-water supply.
- **Surface temperature is an indirect measurement.** Sensor position and airflow affect the curve. The learned reference is not a direct inlet-water temperature or stored-energy measurement.
- **Consumption is not measured by this integration.** Valve-open time alone cannot establish litres consumed, heat output or financial savings.
- **Rooms interact.** Doors, wind, ventilation and other heaters change the load. The controller does not yet model heat transfer between rooms.
- **Protection depends on available infrastructure.** HA must be running to enforce software deadlines or request backup equipment. Device-local protection has its own hardware and temperature-source dependencies.

## Replaying recorded history

```sh
uv run python tools/replay_history.py history.json \
  --radiator sensor.example_radiator_temperature \
  --valve sensor.example_valve_position
```

Use the entity IDs from the recording. The input is HA's history response array, or an object containing it under `data`. Include explicit time bounds and timezone when exporting a trace.

Replay feeds the detector the measurements from the original open intervals. Recorder may omit equal readings received by the live controller. The tool does not insert missing samples or calculate the temperature trajectory that different valve commands would have produced.
