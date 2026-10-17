"""Public installation artifacts must not contain local state or credentials."""

import zipfile

from custom_components.heating_pulse_controller_v2.engine import Sample
from tools.build_release import build
from tools.replay_history import inspect_pulse


def test_release_has_only_integration_and_is_reproducible(tmp_path):
    first = build(tmp_path).read_bytes()
    archive = build(tmp_path)
    assert archive.read_bytes() == first
    with zipfile.ZipFile(archive) as contents:
        names = contents.namelist()
        assert all(n.startswith("custom_components/heating_pulse_controller_v2/") for n in names)
        assert not any("local/" in n or ".secrets" in n or "__pycache__" in n for n in names)
        assert any(n.endswith("config_flow.py") for n in names)
        assert any(n.endswith("services.yaml") for n in names)
        assert any(n.endswith("brand/icon.png") for n in names)
        assert contents.testzip() is None


def test_replay_never_uses_post_close_flattening_as_calibration():
    curve = [Sample(t, min(40, 20 + t / 15)) for t in range(0, 841, 60)]
    assert inspect_pulse(curve, 0, 360) is None
    assert inspect_pulse(curve, 0, 840)["reference_c"] == 40
