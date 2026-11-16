"""The portable C4 catalog artifact is a real, reproducible HA publication."""

import json
import os
from pathlib import Path

from tools.generate_catalog_fixtures import catalog_fixture
from tools.validate_gesture_contract import validate_state


async def test_catalog_export_contains_typed_loads_and_real_aggregates(hass):
    fixture = await catalog_fixture(hass)
    states = {state["entity_id"]: state for state in fixture["states"]}
    catalog = states[fixture["catalog_entity_id"]]["attributes"]
    # The two gesture controls replace their linked ordinary load entries.
    assert fixture["catalog_rows"] == len(catalog["catalog_entities"]) == 62
    assert not {"switch.catalog_switch_00", "light.catalog_light_00"}.intersection(
        row["entity_id"] for row in catalog["catalog_entities"])
    assert len(fixture["states"]) > 50
    assert {row["area_name"] for row in catalog["catalog_entities"]} == {"Fixture Office"}
    for entity_id in fixture["gesture_entity_ids"]:
        state = states[entity_id]
        validate_state(state)
        assert state["attributes"]["bridge_update_kind"] == "snapshot"
        assert "gesture_sequence" not in state["attributes"]
        assert any(row["entity_id"] == entity_id and row["kind"] == "gesture"
                   for row in catalog["catalog_entities"])
        choice = next(row for row in catalog["catalog_entities"] if row["entity_id"] == entity_id)
        assert choice["entity_aliases"] == [choice["load_entity_id"]]
    # The consumer runs without HA installed. An explicit checkout lets the
    # producer independently prove the committed portable artifact has not drifted.
    checkout = os.environ.get("C4_HA_SWITCH_CHECKOUT")
    if checkout:
        exported = Path(checkout) / "tests/fixtures/discovery/ha-catalog-v1.json"
        assert json.loads(exported.read_text()) == json.loads(json.dumps(fixture))
