"""Validate wire fixtures without loading Home Assistant. Run from the repo root."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts/gesture-v1.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


def timestamp(value: str) -> datetime:
    if not RFC3339.fullmatch(value):
        raise ValueError("Timestamps must use RFC3339 date, time, seconds and timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must have a timezone")
    return result


def validate_state(state: dict[str, Any]) -> None:
    """Check schema plus cross-field invariants JSON Schema cannot express."""
    VALIDATOR.validate(state)
    attrs = state["attributes"]
    # jsonschema's RFC3339 checker is an optional dependency; do not silently
    # accept invalid timestamps when that extra is not installed.
    for value in [state["last_updated"], *([attrs["gesture_occurred_at"]] if "gesture_occurred_at" in attrs else [])]:
        timestamp(value)
    buttons: dict[str, dict[str, Any]] = {}
    for button in attrs["bridge_buttons"]:
        if button["id"] in buttons:
            raise ValueError("Duplicate button identity")
        buttons[button["id"]] = button
        ids: set[str] = set()
        for gesture in button["gestures"]:
            if gesture["id"] in ids:
                raise ValueError("Duplicate gesture identity")
            ids.add(gesture["id"])
            if gesture["kind"] == "press":
                if gesture["id"] != f"press_{gesture['press_count']}":
                    raise ValueError("Press identity must match count")
            elif gesture["id"].startswith("press_"):
                raise ValueError("Press identities are reserved for press gestures")
    source_ids = [source["id"] for source in attrs["bridge_sources"]]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("Duplicate source identity")
    if load := attrs["bridge_load"]:
        if load["entity_id"].split(".", 1)[0] != load["domain"]:
            raise ValueError("Load domain must match its entity ID")
    if "gesture_event_id" not in attrs:
        if state["state"] not in ("unknown", "unavailable"):
            raise ValueError("A last-gesture timestamp requires a gesture envelope")
        return
    if state["state"] in ("unknown", "unavailable"):
        if attrs["bridge_update_kind"] != "snapshot":
            raise ValueError("An unavailable state cannot be a live gesture")
    else:
        timestamp(state["state"])
    expected_id = f"{attrs['gesture_session_id']}:{attrs['gesture_sequence']}"
    if attrs["gesture_event_id"] != expected_id:
        raise ValueError("Event identity must match session and sequence")
    if attrs["event_type"] == "press" and attrs["gesture_id"] != f"press_{attrs['press_count']}":
        raise ValueError("Press identity must match count")
    if attrs["bridge_update_kind"] != "gesture":
        return  # Historical snapshots may refer to capabilities no longer configured.
    if attrs["gesture_session_id"] != attrs["bridge_session_id"]:
        raise ValueError("Live gesture must belong to the current producer session")
    button = buttons.get(attrs["button_id"])
    gesture = next((item for item in button["gestures"] if item["id"] == attrs["gesture_id"]), None) if button else None
    if gesture is None or gesture["kind"] != attrs["event_type"]:
        raise ValueError("Live gesture must match a declared capability")
    if gesture.get("press_count") != attrs.get("press_count"):
        raise ValueError("Press count must match its declared capability")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", type=Path, default=ROOT / "tests/fixtures/gestures/v1.json")
    args = parser.parse_args()
    suite = json.loads(args.fixture.read_text())
    for case in suite["cases"]:
        validate_state(case["state"])
    print(f"Validated {len(suite['cases'])} version 1 states")


if __name__ == "__main__":
    main()
