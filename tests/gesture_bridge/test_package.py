"""The independent package cannot accidentally install DeviceSide or C4."""
import json
import zipfile
from tools.build_integration import ROOT, build, sources

def test_deterministic_standalone_package(tmp_path):
    one, two = tmp_path / "one.zip", tmp_path / "two.zip"
    assert build(one) == build(two)
    with zipfile.ZipFile(one) as package:
        expected = sources()
        assert set(package.namelist()) == set(expected)
        assert all(package.read(name) == content for name, content in expected.items())
        assert not any("deviceside" in name or "tests/" in name or name.endswith(".c4z") for name in expected)
        manifest = json.loads(package.read("ha_gesture_bridge/manifest.json"))
        assert manifest["domain"] == "ha_gesture_bridge"
        assert manifest["config_flow"] is True
        # HA's Integrations page excludes helper-classified entries. Controls
        # are managed as subentries of this service, like DeviceSide.
        assert manifest["integration_type"] == "service"
        assert "deviceside" not in manifest.get("dependencies", [])
        assert manifest["requirements"] == []
        assert "zwave_js" in manifest["after_dependencies"]
        assert "zwave_js" not in manifest.get("dependencies", [])  # Optional for other protocols.
        for name in ["config_flow.py", "event.py", "sensor.py", "diagnostics.py", "latency.py", "led_discovery.py", "led_profiles.py", "zwave_parameters.py", "zwave_indicator.py", "services.yaml", "strings.json", "translations/en.json", "brand/icon.png"]:
            assert "ha_gesture_bridge/" + name in expected
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["name"] == "HA Gesture Bridge"
    assert hacs["homeassistant"] == "2026.11.1"
