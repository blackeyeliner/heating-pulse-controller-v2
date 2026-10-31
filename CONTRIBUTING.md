# Contributing

The current hardware adapter supports Sonoff TRVZB through Zigbee2MQTT. For a new adapter or a change to the heating algorithm, start with an issue describing the setup, the behaviour you need and the observations available to test it.

## Development setup

Use Python 3.14.2 or later within the 3.14 series, [uv](https://docs.astral.sh/uv/), and Node.js with its built-in test runner. The lockfile pins the Home Assistant test environment.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=custom_components/heating_pulse_controller_v2 --cov-report=term-missing
node --test tests/frontend.test.mjs
uv run python tools/build_release.py
```

The tests use temporary HA configuration and simulated device services. They do not connect to a live Home Assistant instance. The build writes an integration-only ZIP and SHA-256 file to `dist/`.

Some combined macOS Python 3.14 runs have passed assertions and then exited with code 139 during interpreter finalisation. That remains a failed process; preserve the trace and check the Linux CI result. The issue does not explain assertion failures or exceptions in a running HA installation.

## Changes

The [code map](docs/architecture.md#code-map) describes the main modules. Keep thermal decisions in the engine and HA/device behaviour in the adapters.

A behaviour change should include:

- The observed trigger and intended result.
- A regression test that fails on the previous behaviour.
- Relevant timing, missing-data, cancellation and restart cases.
- Updated documentation and the checks run.

State separately whether a change was tested on hardware. Tests with simulated service responses cannot establish valve movement or heating performance.

## Reporting problems

Use the [bug report form](.github/ISSUE_TEMPLATE/bug_report.yml). Include versions, settings, time bounds and timezone, plus a short explanation of what you expected and what happened.

For heating behaviour, the useful record is a complete pulse and cooling episode with room/radiator temperatures, thermostat target, Phase, Last decision, reference, cutoff, restart threshold and valve feedback. Include known window or door changes, other heaters and infrastructure restarts.

Share only relevant, redacted exports. Remove tokens, private addresses and unrelated household data; do not attach a complete HA backup or `.storage` directory.

The [history replay tool](docs/validation.md#replaying-recorded-history) can inspect the original recorded intervals. It cannot predict the temperatures that different valve commands would have produced.
