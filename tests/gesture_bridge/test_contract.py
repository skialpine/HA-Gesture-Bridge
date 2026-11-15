from copy import deepcopy
import json

from jsonschema import ValidationError
import pytest

from tools.generate_gesture_fixtures import build_suite
from tools.validate_gesture_contract import ROOT, SCHEMA, VALIDATOR, validate_state


@pytest.fixture
def live_state():
    return deepcopy(build_suite()["cases"][1]["state"])


def test_schema_and_committed_portable_fixtures():
    VALIDATOR.check_schema(SCHEMA)
    suite = json.loads((ROOT / "tests/fixtures/gestures/v1.json").read_text())
    assert suite == build_suite()
    for case in suite["cases"]:
        validate_state(case["state"])


@pytest.mark.parametrize("field,value", [
    ("bridge_contract_version", "2.0"),
    ("bridge_update_kind", "pressed"),
    ("gesture_sequence", 0),
    ("gesture_sequence", True),
    ("gesture_sequence", 9007199254740992),
    ("press_count", 0),
    ("press_count", "2"),
    ("button_id", "Top Paddle"),
    ("gesture_occurred_at", "yesterday"),
])
def test_rejects_bad_field_types_and_ranges(live_state, field, value):
    live_state["attributes"][field] = value
    with pytest.raises((ValidationError, ValueError)):
        validate_state(live_state)


@pytest.mark.parametrize("field", ["button_id", "gesture_event_id", "gesture_sequence", "press_count", "bridge_buttons"])
def test_live_state_requires_complete_envelope(live_state, field):
    del live_state["attributes"][field]
    with pytest.raises((ValidationError, ValueError)):
        validate_state(live_state)


@pytest.mark.parametrize("change", [
    lambda a: a.update(gesture_event_id="session-a:99"),
    lambda a: a.update(gesture_session_id="session-b", gesture_event_id="session-b:1"),
    lambda a: a.update(gesture_id="press_3"),
    lambda a: a.update(button_id="other"),
    lambda a: a["bridge_buttons"].append(deepcopy(a["bridge_buttons"][0])),
    lambda a: a["bridge_buttons"][0]["gestures"].append(deepcopy(a["bridge_buttons"][0]["gestures"][0])),
    lambda a: a["bridge_load"].update(domain="input_boolean"),
    lambda a: a["bridge_load"].update(state="unavailable", available=True),
])
def test_rejects_cross_field_inconsistency(live_state, change):
    change(live_state["attributes"])
    with pytest.raises((ValidationError, ValueError)):
        validate_state(live_state)


def test_historical_snapshot_is_not_claimed_as_a_live_event(live_state):
    attrs = live_state["attributes"]
    attrs.update(bridge_session_id="session-b", bridge_update_kind="snapshot")
    validate_state(live_state)  # Last gesture remains explicitly in the old session.
    attrs["bridge_update_kind"] = "gesture"
    with pytest.raises(ValueError, match="current producer session"):
        validate_state(live_state)


def test_snapshot_can_preserve_removed_capability(live_state):
    attrs = live_state["attributes"]
    attrs["bridge_update_kind"] = "snapshot"
    attrs["bridge_buttons"][0]["gestures"] = [attrs["bridge_buttons"][0]["gestures"][0]]
    validate_state(live_state)


def test_unknown_initial_state_and_unlinked_load_are_valid():
    state = deepcopy(build_suite()["cases"][0]["state"])
    state["attributes"]["bridge_load"] = None
    validate_state(state)


def test_minor_additive_attributes_are_allowed(live_state):
    live_state["attributes"].update(bridge_contract_version="1.1", future_metadata="additive")
    validate_state(live_state)


@pytest.mark.parametrize("value", ["20261114T200000+0000", "2026-11-14T20:00+00:00", "2026-11-14T200000+00:00"])
@pytest.mark.parametrize("field", ["last_updated", "gesture_occurred_at", "state"])
def test_rejects_non_rfc3339_iso_variants(live_state, value, field):
    if field == "gesture_occurred_at":
        live_state["attributes"][field] = value
    else:
        live_state[field] = value
    with pytest.raises((ValueError, ValidationError)):
        validate_state(live_state)
