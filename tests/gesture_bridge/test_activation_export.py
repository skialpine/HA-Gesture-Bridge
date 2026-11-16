"""Actual producer output used by the independently packaged consumers."""

import json
import os
from pathlib import Path

from tools.generate_activation_fixtures import activation_fixture


async def test_exported_activation_roundtrip_is_reproducible(hass):
    exported = await activation_fixture(hass)
    first, linked = exported["preparation_result"], exported["linked_result"]
    assert first["gesture_entity_id"] == linked["gesture_entity_id"]
    assert first["controlled_load_origin"] == "selected_endpoint"
    assert linked["controlled_load_origin"] == "explicit_link"
    assert linked["controlled_load"]["entity_id"] != linked["source_load"]["entity_id"]
    assert exported["fresh_gesture"]["attributes"]["press_count"] == 2
    assert exported["fresh_gesture"]["attributes"]["gesture_sequence"] == 1
    assert not exported["waiting_result"]["input_ready"]
    assert exported["renamed_result"]["gesture_entity_id"] == first["gesture_entity_id"]
    assert exported["renamed_result"]["input_ready"]
    a, b = exported["endpoint_results"]
    assert a["gesture_entity_id"] == b["gesture_entity_id"] == first["gesture_entity_id"]
    assert a["controlled_load"]["entity_id"] != b["controlled_load"]["entity_id"]
    assert a["controlled_load_origin"] == b["controlled_load_origin"] == "selected_endpoint"
    broken = exported["broken_result"]
    assert broken["controlled_load_origin"] == "explicit_link"
    assert broken["controlled_load"]["available"] is False
    assert "registry_id" not in broken["controlled_load"]
    checkout = os.environ.get("C4_HA_SWITCH_CHECKOUT")
    if checkout:
        path = Path(checkout) / "tests/fixtures/discovery/ha-activation-v1.json"
        assert json.loads(path.read_text()) == exported
