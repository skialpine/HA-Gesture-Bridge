"""Normalize reported physical actions; never infer unreported capabilities."""

from copy import deepcopy
import hashlib
import re

STATE_DOMAINS = frozenset({"alarm_control_panel", "binary_sensor", "button", "climate", "cover", "event", "fan",
    "humidifier", "input_boolean", "light", "lock", "number", "select", "sensor", "switch", "text", "vacuum", "water_heater"})

PROFILE_ACTIONS = {
    "KeyPressed": {"kind": "press", "press_count": 1},
    **{f"KeyPressed{count}x": {"kind": "press", "press_count": count} for count in range(1, 6)},
    "KeyHeldDown": {"kind": "hold_start"}, "KeyReleased": {"kind": "release"},
    "single": {"kind": "press", "press_count": 1}, "double": {"kind": "press", "press_count": 2},
    "triple": {"kind": "press", "press_count": 3},
    "single_press": {"kind": "press", "press_count": 1},
    "double_press": {"kind": "press", "press_count": 2},
    "triple_press": {"kind": "press", "press_count": 3},
    "hold": {"kind": "hold_start"}, "long_press": {"kind": "hold_start"},
    "release": {"kind": "release"}, "long_release": {"kind": "release"},
    "short_release": {"kind": "release"}, "short_press": {"kind": "press", "press_count": 1},
}
for _word, _count in (("short", 1), ("double", 2), ("triple", 3), ("quadruple", 4), ("quintuple", 5)):
    PROFILE_ACTIONS[f"remote_button_{_word}_press"] = {"kind": "press", "press_count": _count}
for _word in ("short", "long"):
    PROFILE_ACTIONS[f"remote_button_{_word}_release"] = {"kind": "release"}
PROFILE_ACTIONS["remote_button_long_press"] = {"kind": "hold_start"}


def is_physical_device_trigger(config) -> bool:
    """Recognize a physical-input namespace, never load/configuration state."""
    if not isinstance(config, dict) or config.get("platform") != "device":
        return False
    domain, kind = config.get("domain"), config.get("type")
    if not isinstance(domain, str) or domain in STATE_DOMAINS or not isinstance(kind, str) or not kind or len(kind) > 128:
        return False
    if any(ord(char) < 32 for char in kind) or kind.startswith(("state.", "value_updated.")):
        return False
    if domain == "zwave_js":
        return (kind == "event.value_notification.central_scene" and config.get("command_class") == 91
                and config.get("property") == "scene")
    if domain == "mqtt":
        return kind == "action" and isinstance(config.get("subtype"), str) and bool(config["subtype"])
    return kind in PROFILE_ACTIONS or re.search(
        r"(?:^|[_.])(button|press|click|hold|release|rotate|rotated|shake|shaken)(?:$|[_.])", kind) is not None


def action_definition(action: str) -> dict:
    """Keep unknown, actually reported actions as explicitly custom events."""
    if action in PROFILE_ACTIONS:
        definition = deepcopy(PROFILE_ACTIONS[action])
        if definition["kind"] == "press":
            count = definition["press_count"]
            definition["label"] = {1: "Single tap", 2: "Double tap", 3: "Triple tap"}.get(count, f"{count} taps")
        else:
            definition["label"] = {"hold_start": "Hold", "hold_repeat": "Hold repeat", "release": "Release"}[definition["kind"]]
        return definition
    label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", action).replace("_", " ").strip()[:128]
    return {"kind": "custom", "id": "action_" + hashlib.sha256(action.encode()).hexdigest()[:20],
            "label": label or "Custom Action"}


def native_action_definition(action: str, platform: str, event_types: list | tuple) -> dict:
    """Apply a provider's reported button vocabulary without guessing edges.

    Hue dimmers distinguish the initial down edge from a completed short tap.
    Hue Tap Switch advertises only initial_press, which is its completed action.
    Rotary repeat events must not acquire button hold semantics.
    """
    if platform == "hue":
        tap_only = len(event_types) == 1 and event_types[0] == "initial_press"
        if action == "short_release" or (action == "initial_press" and tap_only):
            return {"kind": "press", "press_count": 1, "label": "Single tap"}
        if action == "repeat" and "long_press" in event_types and "long_release" in event_types:
            return {"kind": "hold_repeat", "label": "Hold repeat"}
    return action_definition(action)
