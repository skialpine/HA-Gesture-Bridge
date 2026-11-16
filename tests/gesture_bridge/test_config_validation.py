"""Shared control-root and gesture rules apply to every declaration shape."""

from copy import deepcopy
from datetime import timedelta

import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig


def control(shape, actions):
    result = {"unique_id": "validation-control", "name": "Validation Control"}
    if shape == "sources":
        result["sources"] = [{"id": "paddle", "type": "native_event", "source_entity_id": "event.paddle",
            "buttons": [{"id": "top", "label": "Top"}], "mappings": [
                {"button_id": "top", "match": {"event_type": action}, "gesture": gesture}
                for action, gesture in actions.items()]}]
    elif shape == "buttons":
        result["buttons"] = [{"id": "top", "label": "Top", "source_entity_id": "event.paddle", "actions": actions}]
    else:
        result.update(source_entity_id="event.paddle", actions=actions)
    return result


@pytest.mark.parametrize("shape", ["legacy", "buttons", "sources"])
def test_root_rejects_typo_but_preserves_ha_platform_metadata(shape):
    config = control(shape, {"tap": {"kind": "press", "press_count": 1}})
    with pytest.raises(ValueError, match="Unknown control configuration field"):
        NativeControlConfig.parse(config | {"load_entty_id": "light.office"})
    parsed = NativeControlConfig.parse(config | {"platform": DOMAIN,
        "entity_namespace": "fixture", "scan_interval": timedelta(seconds=30)})
    assert parsed.control_id == "validation-control" and parsed.load_entity_id is None
    assert parsed.buttons()[0]["gestures"][0]["id"] == "press_1"


@pytest.mark.parametrize("shape", ["legacy", "buttons", "sources"])
def test_selected_device_identity_survives_parsing_for_automatic_leds(shape):
    config = control(shape, {"tap": {"kind": "press", "press_count": 1}})
    assert NativeControlConfig.parse(config).source_device_id is None
    assert NativeControlConfig.parse(config | {"source_device_id": "selected-device"}).source_device_id == "selected-device"
    with pytest.raises(ValueError):
        NativeControlConfig.parse(config | {"source_device_id": ""})


@pytest.mark.parametrize("shape", ["legacy", "buttons", "sources"])
@pytest.mark.parametrize("actions", [
    {"first": {"kind": "press", "press_count": 1, "label": "One"},
     "second": {"kind": "press", "press_count": 1, "label": "Different"}},
    {"first": {"kind": "hold_repeat", "id": "repeat_a"},
     "second": {"kind": "hold_repeat", "id": "repeat_b"}},
    {"first": {"kind": "hold_start"}, "second": {"kind": "custom", "id": "hold_repeat"}},
    {"first": {"kind": "hold_start"}, "second": {"kind": "hold_repeat", "id": "custom_repeat"},
     "third": {"kind": "custom", "id": "hold_repeat"}},
])
def test_all_shapes_enforce_the_same_gesture_identity_rules(shape, actions):
    with pytest.raises(ValueError):
        NativeControlConfig.parse(control(shape, deepcopy(actions)))
