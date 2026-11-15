from pathlib import Path
import yaml
from custom_components.ha_gesture_bridge.config import NativeControlConfig

def test_documented_source_profiles_parse_and_preserve_explicit_identities():
    examples = yaml.safe_load((Path(__file__).resolve().parents[2] / "examples/source-profiles.yaml").read_text())
    assert set(examples) == {"native", "shared_native", "zwave_js", "zha", "mqtt", "bus"}
    for value in examples.values():
        parsed = NativeControlConfig.parse(value)
        assert parsed.control_id == value["unique_id"]
        assert parsed.buttons()
        assert all(button["gestures"] for button in parsed.buttons())
