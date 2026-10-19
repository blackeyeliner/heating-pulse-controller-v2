"""Validate metadata and build a deterministic integration-only installation ZIP."""

import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "heating_pulse_controller_v2"
COMPONENT = ROOT / "custom_components" / DOMAIN


def build(destination: Path) -> Path:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True and manifest["version"]
    assert manifest["requirements"] == []
    assert hacs["homeassistant"] == "2026.9.0"
    assert json.loads((COMPONENT / "strings.json").read_text()) == json.loads(
        (COMPONENT / "translations/en.json").read_text()
    )
    paths = sorted(
        p
        for p in COMPONENT.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".json", ".png", ".yaml", ".js"}
        and "__pycache__" not in p.parts
    )
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{DOMAIN}-{manifest['version']}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in paths:
            name = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            output.writestr(info, path.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


if __name__ == "__main__":
    print(build(ROOT / "dist"))
