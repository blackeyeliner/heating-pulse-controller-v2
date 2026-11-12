# Documentation images

The README uses two explanatory diagrams, a recorded pulse-frequency graph and the thermostat-card image:

| Diagram | Editable source | PNG export |
|---|---|---|
| System overview | [heating-system.svg](heating-system.svg) | [heating-system.png](heating-system.png) |
| Pulse timing and adaptive cooling | [heating-cycle.svg](heating-cycle.svg) | [heating-cycle.png](heating-cycle.png) |
| Recorded pulse frequency | [recorded-pulse-frequency.svg](recorded-pulse-frequency.svg) | [recorded-pulse-frequency.png](recorded-pulse-frequency.png) |

The explanatory SVGs are self-contained, with a white background, accessible titles and descriptions, and no external dependencies. The README embeds them directly and links to the full-size versions. PNG exports are rendered at twice the SVG dimensions: 3200 × 1780 for the system diagram and 3200 × 1940 for pulse timing. Regenerate the PNG whenever its SVG changes.

The timing curves are illustrative, not recorded measurements. Window feedback and adaptive cooling are optional. Valve readback confirms device settings, not physical water flow.

Further illustrations can use real equipment photographs and recorded traces, with an explanatory caption and alt text.

## Hardware and sensor placement

A photograph of one installed radiator, showing the valve and surface sensor, plus a separate view of the room sensor's location. The pair should make the distinction between room, valve and surface temperatures clear.

Keep enough context to show placement. Crop out private objects and readable device identifiers. Identify the actual equipment in the caption.

## Recorded pulse frequency

The current graph plots recorded valve commands, radiator temperature, learned reference, restart threshold and room temperature on a shared elapsed-time axis. It shows pulse-start intervals changing from 111–115 to 76–86 minutes with a constant room target, AC off, window closed and no recovery pauses. The adaptive cooling fraction stayed unchanged, so this illustrates demand-dependent timing rather than adjustment of that fraction.

The traces are step plots of recorded states without smoothing. The PNG is 2240 × 1328; the SVG is self-contained. Room names, calendar dates and entity IDs are omitted. Temperature panels have different scales, and the open/closed trace is a command, not measured water flow.

## Earlier recorded heating cycles

The previous screenshot composite remains available but is no longer embedded in the README.

The composite arranges three actual Home Assistant history screenshots from the same time window in this order:

1. [Valve commands](recorded-valve-pulses.png).
2. [Radiator surface temperature](recorded-radiator-temperature.png).
3. [Room temperature](recorded-room-temperature.png).

The individual PNGs retain the original plot pixels. Deterministic cropping removes the live-value legends; a background-colour mask removes only the calendar-date labels below the plots. Room identifiers and dates are absent from both the visible images and the embedded image payloads. Time-of-day ticks, units and vertical scales remain.

The self-contained composite SVG embeds those sanitized PNGs and uses small uniform scaling adjustments to align their plot boundaries. Its PNG export is 1780 × 2070. No trace was generated, smoothed or redrawn. This is an example of observed behaviour, not a controlled comparison or a water/energy measurement. The 0/5 valve trace is the controller's logical command signal.

## Thermostat states

[thermostat-card-states.png](thermostat-card-states.png) shows the actual bundled card rendered by Home Assistant in four states: Off, Heat while idle, Heating and Antifreeze. The cards use neutral names and demonstration values, not simultaneous readings from installed thermostats.

The browser preview supplies local sample state objects to the real card and native mode feature. Device service calls are disabled in the preview, and no HA state or dashboard configuration is written. The native feature's state context is supplied explicitly because the demonstration entity IDs do not exist in HA. The screenshot preserves the real rendering, including the Off selection during Antifreeze.

## File conventions

Keep editable diagram sources alongside exports. Use descriptive filenames, compress raster images and check legibility at GitHub content width. Add each image together with its caption and alt text; avoid empty placeholders in the README.
