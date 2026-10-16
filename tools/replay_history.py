"""Inspect detector behaviour on recorded HA curves; never simulate energy savings.

Input: a JSON object with a `data` list from HA history (or that list directly).
Use an isolated development environment. No HA connection or valve commands.
"""

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.heating_pulse_controller_v2.engine import Controller, Phase, Sample


def samples(series):
    result = []
    for item in series:
        try:
            value = float(item["state"])
            at = datetime.fromisoformat(item["last_updated"]).timestamp()
        except ValueError, KeyError, TypeError:
            continue
        if math.isfinite(value):
            result.append(Sample(at, value))
    return sorted(result, key=lambda s: s.at)


def inspect_pulse(curve, opened, closed):
    """Feed only recorded reports while logically open, never post-close peaks."""
    controller = Controller()
    controller.phase = Phase.CALIBRATING
    detected = None
    for sample in curve:
        if not opened <= sample.at < closed:
            continue
        controller.radiator_report(sample.value, sample.at)
        if controller._plateau():
            controller._accept_reference(sample.at)
            detected = {
                "after_seconds": round(sample.at - opened, 1),
                "reference_c": controller.reference.temperature,
                "closing_c": controller.stop_temperature,
            }
            break
    return detected


def replay(data, radiator_id, valve_id):
    series = data["data"] if isinstance(data, dict) else data
    indexed = {items[0]["entity_id"]: items for items in series if items}
    curve = samples(indexed[radiator_id])
    valve = samples(indexed[valve_id])
    pulses = []
    for index, (start, end) in enumerate(zip(valve, valve[1:], strict=False), 1):
        if start.value <= 0 or end.value != 0:
            continue
        stop = valve[index + 1].at if index + 1 < len(valve) else end.at + 600
        after = [s for s in curve if end.at <= s.at <= min(end.at + 600, stop)]
        peak = max(after, key=lambda s: s.value) if after else None
        pulses.append(
            {
                "pulse": len(pulses) + 1,
                "open_seconds": round(end.at - start.at, 1),
                "open_samples": sum(start.at <= s.at < end.at for s in curve),
                "plateau": inspect_pulse(curve, start.at, end.at),
                "post_close_peak_c": peak.value if peak else None,
                "post_close_peak_after_seconds": round(peak.at - end.at, 1) if peak else None,
            }
        )
    return {
        "scope": "Logical valve intervals; recorded state changes are not all sensor reports. No counterfactual thermal simulation.",
        "pulses": pulses,
        "plateaus_detected": sum(p["plateau"] is not None for p in pulses),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument("--radiator", required=True)
    parser.add_argument("--valve", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            replay(json.loads(args.history.read_text()), args.radiator, args.valve), indent=2
        )
    )
