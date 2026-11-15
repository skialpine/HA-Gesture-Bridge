"""Build a deterministic manual-install archive; never deploy or restart HA."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "ha_gesture_bridge"

def sources(root: Path = ROOT) -> dict[str, bytes]:
    component = root / "custom_components" / DOMAIN
    manifest = json.loads((component / "manifest.json").read_text())
    assert manifest["domain"] == DOMAIN and manifest["config_flow"] is True
    assert manifest["requirements"] == []
    assert {p.name for p in (root / "custom_components").iterdir() if p.is_dir() and not p.name.startswith("__")} == {DOMAIN}
    return {DOMAIN + "/" + path.relative_to(component).as_posix(): path.read_bytes()
            for path in component.rglob("*")
            if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".png"}
            and "__pycache__" not in path.parts}

def build(target: Path | None = None, root: Path = ROOT) -> str:
    target = target or root / "dist/ha_gesture_bridge.zip"
    files = sources(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".zip", delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        with zipfile.ZipFile(staging, "w") as output:
            for name, content in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                output.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with zipfile.ZipFile(staging) as result:
            assert result.testzip() is None
            assert set(result.namelist()) == set(files)
        digest = hashlib.sha256(staging.read_bytes()).hexdigest()
        staging.replace(target)
        return digest
    finally:
        staging.unlink(missing_ok=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/ha_gesture_bridge.zip")
    args = parser.parse_args()
    print(f"{args.output} sha256={build(args.output)}")
