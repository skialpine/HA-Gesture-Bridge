"""Generate the portable state sequence consumed by Python and Lua tests."""

from copy import deepcopy
import json
from pathlib import Path

from tools.validate_gesture_contract import validate_state

ROOT = Path(__file__).resolve().parents[1]
ENTITY_ID = "event.gesture_bridge_fixture"
TIME = "2026-11-14T20:00:{:02d}.000+00:00"
BASE = {
    "entity_id": ENTITY_ID,
    "state": "unknown",
    "last_updated": TIME.format(0),
    "attributes": {
        "bridge_contract_version": "1.0",
        "bridge_control_id": "fixture-control",
        "bridge_session_id": "session-a",
        "bridge_update_kind": "snapshot",
        "bridge_buttons": [{
            "id": "top", "label": "Top",
            "gestures": [
                {"id": f"press_{count}", "kind": "press", "label": f"{count} Press", "press_count": count}
                for count in range(1, 6)
            ] + [
                {"id": kind, "kind": kind, "label": kind.replace("_", " ").title()}
                for kind in ("hold_start", "hold_repeat", "release")
            ] + [{"id": "scene_movie", "kind": "custom", "label": "Movie Scene"}]
        }],
        "bridge_sources": [{"id": "native-top", "ready": True, "availability": "available"}],
        "bridge_load": {"entity_id": "switch.fixture", "domain": "switch", "state": "off", "available": True},
        "event_type": None
    }
}


def live(sequence: int, second: int, *, session: str = "session-a", kind: str = "press", count: int = 2) -> dict:
    state = deepcopy(BASE)
    state["state"] = state["last_updated"] = TIME.format(second)
    attrs = state["attributes"]
    attrs.update({
        "bridge_session_id": session,
        "bridge_update_kind": "gesture",
        "event_type": kind,
        "button_id": "top",
        "gesture_id": f"press_{count}" if kind == "press" else kind,
        "gesture_session_id": session,
        "gesture_sequence": sequence,
        "gesture_event_id": f"{session}:{sequence}",
        "gesture_occurred_at": TIME.format(second)
    })
    if kind == "press":
        attrs["press_count"] = count
    return state


def build_suite() -> dict:
    cases = []

    def add(name: str, state: dict, event: str | None = None, delivery: str = "event") -> None:
        validate_state(state)
        cases.append({"name": name, "delivery": delivery, "state": state, "expected_event": event})

    add("initial_snapshot", deepcopy(BASE), delivery="state")
    first = live(1, 1)
    add("first_double_press", first, "top/press_2")
    add("duplicate_delivery", deepcopy(first))
    add("second_identical_double_press", live(2, 2), "top/press_2")
    snapshot = live(2, 2)
    snapshot["last_updated"] = TIME.format(3)
    snapshot["attributes"]["bridge_update_kind"] = "snapshot"
    snapshot["attributes"]["bridge_load"]["state"] = "on"
    add("load_snapshot_preserves_last_press", snapshot)
    missed = live(5, 5)
    missed["last_updated"] = TIME.format(6)
    missed["attributes"]["bridge_update_kind"] = "snapshot"
    add("snapshot_of_unseen_offline_press", missed)
    add("fresh_press_after_snapshot", live(6, 7), "top/press_2")
    wrong = live(7, 8)
    wrong["entity_id"] = "event.another_control"
    add("another_entity_broadcast", wrong)
    add("hold_start", live(7, 9, kind="hold_start"), "top/hold_start")
    add("hold_repeat", live(8, 10, kind="hold_repeat"), "top/hold_repeat")
    add("reported_release", live(9, 11, kind="release"), "top/release")
    restored = live(9, 11, kind="release")
    restored["last_updated"] = TIME.format(20)
    restored["attributes"].update(bridge_session_id="session-b", bridge_update_kind="snapshot")
    add("restored_history_new_session", restored, delivery="state")
    add("first_fresh_press_new_session", live(1, 21, session="session-b"), "top/press_2")
    late = deepcopy(snapshot)
    add("delayed_old_session_snapshot", late, delivery="state")
    add("triple_press", live(2, 22, session="session-b", count=3), "top/press_3")
    add("out_of_order_sequence", live(1, 21, session="session-b"))
    add("fifth_press_count", live(3, 23, session="session-b", count=5), "top/press_5")
    return {"entity_id": ENTITY_ID, "cases": cases}


def main() -> None:
    target = ROOT / "tests/fixtures/gestures/v1.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(build_suite(), indent=2) + "\n")
    print(f"Wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
