# Installation

[← README](../README.md) · [Operating guide](operation.md)

Configure one radiator first and observe a complete heating and cooling cycle before adding other rooms.

## Requirements

| Component | Required setup |
|---|---|
| Home Assistant | HACS metadata requires 2026.9.0 or later. Tests currently use HA 2026.9.3 with Python 3.14. |
| Valve | Sonoff TRVZB through Zigbee2MQTT, with its climate entity and both opening/closing-limit number entities available. |
| Room sensor | An external Celsius temperature sensor placed to represent the room, away from the radiator's immediate heat. |
| Radiator sensor | A Celsius temperature sensor attached securely to the radiator surface. Keep its position consistent after calibration. |
| Reporting | Fresh measurements from both sensors within the configured timeouts, including repeated equal readings. |
| Valve settings | An opening/closing pair checked for reliable operation on that radiator. |

The current adapter uses the [Sonoff TRVZB interface](https://www.zigbee2mqtt.io/devices/TRVZB.html). Other TRVs are not supported simply because they expose a climate entity.

The setup defaults of 21 / 83 are configurable opening and closing limit values. Check the correct pair for your valve; these numbers do not specify measured flow or stem travel.

## Check temperature reporting

The controller needs to distinguish a stable temperature from a sensor that stopped sending data. This matters particularly during plateau detection.

For Zigbee2MQTT temperature devices, the following device-specific Home Assistant discovery override allows identical measurements to reach HA:

```yaml
homeassistant:
  temperature:
    force_update: true
```

Apply it to both temperature devices where needed. It changes HA event handling, not the device's Zigbee reporting interval. Check the actual device reports while temperature is stable: an update to an unrelated attribute or a republished cached payload is not a fresh temperature measurement.

The configuration flow asks you to confirm that reporting has been verified. Recorder history alone may omit equal reports, so use the sensor's reporting configuration and incoming messages as well.

## Install through HACS

1. Open HACS **Custom repositories**.
2. Add `https://github.com/blackeyeliner/heating-pulse-controller-v2` with category **Integration**.
3. Download **Heating Pulse Controller v2**. Enable prerelease versions if needed to select the current beta.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and select **Heating Pulse Controller v2**.

The integration is distributed as a HACS custom repository, outside the default catalogue.

For manual installation, download the ZIP from [Releases](https://github.com/blackeyeliner/heating-pulse-controller-v2/releases). Extract its `custom_components/heating_pulse_controller_v2` directory into `/config/custom_components/`, then restart HA.

## Configure a room

1. Enter a room name.
2. Leave **existing thermostat** empty to create a native room thermostat.
3. Select the physical TRV, external room sensor, radiator sensor and both valve-limit entities.
4. Enter the operating pair checked for that valve.
5. Select any old automations that write to the valve or synchronise its thermostat. Turn them off before enabling the new controller.
6. Review the algorithm options and reporting confirmation. Add window feedback, a notification service or backup climates if required.

The new entry starts with **Control enabled** off. It provides the thermostat and diagnostic entities directly; templates and numerical helpers are optional migration facilities, not setup requirements.

Only one entry can use a given actuator. The selected automation guards prevent known writers from competing with it. They cannot detect an undeclared automation or another external controller.

## Observe the first cycle

Set the native thermostat to **Heat** with a target above the room temperature, then turn on **Control enabled**. Watch the following sequence:

| Stage | Expected observation |
|---|---|
| Waiting | Both temperatures are fresh. The radiator may need to cool before the first pulse. |
| Calibration | Phase becomes `calibrating`; the valve opens and the radiator temperature begins to rise. |
| Closure | A successful calibration sets Reference temperature and Closing temperature. Other stop reasons appear in Last decision. |
| Residual warming | The valve is closed, but the surface may continue warming before reaching its peak. |
| Cooling | The radiator cools toward Restart temperature. A new pulse also requires room demand. |
| Ordinary pulse | Heating uses the learned cutoff, while continuing to check for a lower plateau. |

Compare the software states with the radiator's physical response. If it stays cold during a reported opening, or calibration repeatedly reaches its time limit, retain the temperature and decision history for investigation. Confirmed valve settings alone cannot establish water flow.

Check the thermostat's **Off** behaviour and firmware frost handover as part of setup. [Controls and frost protection](operation.md#heat-off-and-control-enabled) explains what should remain active in that mode.

## Add the thermostat card

The frontend is included with the integration. Add a manual dashboard card using your actual native climate entity ID:

```yaml
type: custom:heating-pulse-thermostat-card
entity: climate.your_room_heating
```

The wrapper accepts the standard thermostat-card configuration. Refresh an existing browser tab after an update if it still displays the old frontend.

## Updates

Install the update through HACS and restart Home Assistant. The integration remembers whether control was enabled, along with the room mode and target. After startup checks and fresh feedback, compatible entries resume automatically. An interrupted pulse is discarded, and an explicitly disabled controller stays disabled.

Sensor relocation, valve recalibration or firmware changes can make old calibration unsuitable. Recheck reporting and reset calibration as appropriate. Tests against the pinned HA version do not guarantee compatibility with every later release.

## Migrating an external thermostat

For an existing entry that uses an external thermostat, disable control and call `heating_pulse_controller_v2.adopt_thermostat` with its `config_entry_id`. This captures the existing mode and target and creates the native thermostat.

If retaining the old climate entity ID, hand it over through the entity registry and archive the old YAML thermostat before restarting. Keep the old configuration until the new setup is verified. An integration update alone does not require deleting and recreating the entry.
