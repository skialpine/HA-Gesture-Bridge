"""Validated source declarations with stable control/button/gesture identities.

Legacy ``source_entity_id/actions`` and ``buttons`` native configurations remain
valid. New controls use ``sources`` with explicit source IDs, button declarations,
and ``mappings`` matching JSON scalar values at dotted field paths. The Z-Wave
scene convenience profile declares counts 1–5, hold and release; devices exposing
fewer actions must supply explicit mappings rather than assume that profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import json
import re
from typing import Any

DOMAIN = "ha_gesture_bridge"
CONTRACT_VERSION = "1.0"
KINDS = frozenset({"press", "hold_start", "hold_repeat", "release", "custom"})
from .config_values import ENTITY, IDENTITY, KEY, MAX_EXACT_INTEGER, entity_id, text
from .led_config import LEDConfig, parse_leds
from .gesture_profiles import is_physical_device_trigger


@dataclass(frozen=True)
class Gesture:
    id: str
    kind: str
    label: str
    press_count: int | None = None

    @classmethod
    def parse(cls, value: Any) -> Gesture:
        if not isinstance(value, dict) or set(value) - {"id", "kind", "label", "press_count"}:
            raise ValueError("An action must define kind, optional id/label, and a press count for presses")
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("Unsupported gesture kind")
        count = value.get("press_count")
        if kind == "press":
            if type(count) is not int or not 1 <= count <= MAX_EXACT_INTEGER:
                raise ValueError("A press requires a positive integer press_count")
            identity = value.get("id", f"press_{count}")
            if identity != f"press_{count}":
                raise ValueError("Press identity must match press_count")
            label = value.get("label", f"{count} Press")
        else:
            if "press_count" in value:
                raise ValueError("Only press gestures have a press_count")
            identity = value.get("id", kind if kind != "custom" else None)
            label = value.get("label", kind.replace("_", " ").title())
            if isinstance(identity, str) and identity.startswith("press_"):
                raise ValueError("Press identities are reserved for press gestures")
        return cls(text(identity, "gesture ID", KEY), kind, text(label, "gesture label"), count)

    def capability(self) -> dict[str, Any]:
        result = {"id": self.id, "kind": self.kind, "label": self.label}
        if self.press_count is not None:
            result["press_count"] = self.press_count
        return result


def _validate_button_gestures(actions: dict[str, Gesture]) -> None:
    """Apply the same gesture identity rules to every source declaration shape."""
    capabilities: dict[str, Gesture] = {}
    for gesture in actions.values():
        if gesture.id in capabilities and capabilities[gesture.id] != gesture:
            raise ValueError("Conflicting declarations for the same gesture ID")
        capabilities[gesture.id] = gesture
    if sum(gesture.kind == "hold_repeat" for gesture in capabilities.values()) > 1:
        raise ValueError("Only one hold repeat declaration is supported per button")
    if (any(gesture.kind == "hold_start" for gesture in capabilities.values())
            and "hold_repeat" in capabilities and capabilities["hold_repeat"].kind != "hold_repeat"):
        raise ValueError("hold_repeat identity is reserved for repeating held notifications")


@dataclass(frozen=True)
class NativeButtonConfig:
    id: str
    label: str
    source_entity_id: str
    actions: dict[str, Gesture]
    source_registry_id: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> NativeButtonConfig:
        if not isinstance(data, dict) or set(data) - {"id", "label", "source_entity_id", "source_registry_id", "actions"}:
            raise ValueError("Each button must define id, label, source_entity_id, and actions")
        raw_actions = data.get("actions")
        if not isinstance(raw_actions, dict) or not raw_actions:
            raise ValueError("At least one action mapping is required")
        actions = {text(action, "source action"): Gesture.parse(value) for action, value in raw_actions.items()}
        _validate_button_gestures(actions)
        return cls(text(data.get("id"), "button ID", KEY), text(data.get("label"), "button label"),
                   entity_id(data.get("source_entity_id"), {"event"}), actions,
                   text(data["source_registry_id"], "source registry ID", IDENTITY) if "source_registry_id" in data else None)

    def repeat_gesture(self) -> Gesture:
        return next((gesture for gesture in self.actions.values() if gesture.kind == "hold_repeat"),
                    Gesture("hold_repeat", "hold_repeat", "Hold Repeat"))

    def capability(self) -> dict[str, Any]:
        gestures = {gesture.id: gesture.capability() for gesture in self.actions.values()}
        if any(gesture.kind == "hold_start" for gesture in self.actions.values()):
            repeat = self.repeat_gesture()
            gestures[repeat.id] = repeat.capability()
        return {"id": self.id, "label": self.label, "gestures": list(gestures.values())}


@dataclass(frozen=True)
class NativeControlConfig:
    control_id: str
    name: str
    source_entity_id: str
    button_id: str
    button_label: str
    actions: dict[str, Gesture]
    load_entity_id: str | None

    source_buttons: tuple[NativeButtonConfig, ...] = ()
    source_configs: tuple[SourceConfig, ...] = ()
    leds: tuple[LEDConfig, ...] = ()
    source_device_id: str | None = None
    load_registry_id: str | None = None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> NativeControlConfig:
        if not isinstance(data, dict):
            raise ValueError("A native control must be a mapping")
        if set(data) - {"unique_id", "name", "source_entity_id", "source_registry_id", "button_id",
                        "button_label", "actions", "buttons", "sources", "load_entity_id", "load_registry_id",
                        "platform", "entity_namespace", "scan_interval", "leds", "source_device_id", "source_button_roles"}:
            raise ValueError("Unknown control configuration field")
        if "source_device_id" in data:
            text(data["source_device_id"], "HA device ID", IDENTITY)
        if "source_button_roles" in data:
            roles = data["source_button_roles"]
            if not isinstance(roles, dict) or not 1 <= len(roles) <= 32:
                raise ValueError("Detected button roles must contain 1 to 32 assignments")
            for identity, role in roles.items():
                text(identity, "detected button ID", KEY); text(role, "button role", KEY)
            if len(set(roles.values())) != len(roles):
                raise ValueError("Detected button roles must be distinct")
        load_registry_id = None
        if "load_registry_id" in data:
            if not data.get("load_entity_id"):
                raise ValueError("A load entity is required with a load registry ID")
            load_registry_id = text(data["load_registry_id"], "load registry ID", IDENTITY)
        leds = parse_leds(data.get("leds", []))
        if "sources" in data:
            if any(key in data for key in ("buttons", "source_entity_id", "source_registry_id", "button_id", "button_label", "actions")):
                raise ValueError("sources cannot be mixed with legacy native source fields")
            raw = data["sources"]
            if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
                raise ValueError("sources must contain 1 to 32 sources")
            sources = tuple(SourceConfig.parse(source) for source in raw)
            attached_triggers = [json.dumps(trigger.config, sort_keys=True, separators=(",", ":"))
                                 for source in sources for trigger in source.device_triggers]
            if len(set(attached_triggers)) != len(attached_triggers):
                raise ValueError("The same HA device trigger cannot be subscribed more than once")
            if len({source.id for source in sources}) != len(sources):
                raise ValueError("Source IDs must be unique")
            native_ids = [source.source_entity_id for source in sources if source.source_entity_id]
            registry_ids = [source.source_registry_id for source in sources if source.source_registry_id]
            if len(set(registry_ids)) != len(registry_ids):
                raise ValueError("Use one shared_native source for the same registry identity")
            if len(set(native_ids)) != len(native_ids):
                raise ValueError("Use one shared_native source for buttons sharing an event entity")
            for index, source in enumerate(sources):
                for previous in sources[:index]:
                    if source_channel(source) == source_channel(previous):
                        for mapping in source.mappings:
                            for old in previous.mappings:
                                if filters_overlap({**source.filters, **mapping.match}, {**previous.filters, **old.match}):
                                    raise ValueError("Overlapping sources would publish duplicate physical occurrences")
            buttons = tuple(button for source in sources for button in source.buttons)
            if len(buttons) > 32 or len({button.id for button in buttons}) != len(buttons):
                raise ValueError("Button IDs must be unique across sources")
            first = buttons[0]
            load = data.get("load_entity_id")
            return cls(text(data.get("unique_id"), "control identity", IDENTITY), text(data.get("name"), "control name"),
                       first.source_entity_id, first.id, first.label, first.actions,
                       entity_id(load, {"switch", "input_boolean", "light"}) if load is not None else None,
                       buttons, sources, leds=leds, source_device_id=data.get("source_device_id"), load_registry_id=load_registry_id)
        if "buttons" in data:
            if any(key in data for key in ("source_entity_id", "source_registry_id", "button_id", "button_label", "actions")):
                raise ValueError("buttons cannot be mixed with legacy source/action fields")
            raw_buttons = data["buttons"]
            if not isinstance(raw_buttons, list) or not 1 <= len(raw_buttons) <= 32:
                raise ValueError("buttons must contain 1 to 32 button mappings")
            buttons = tuple(NativeButtonConfig.parse(button) for button in raw_buttons)
        else:
            legacy = {"id": data.get("button_id", "top"), "label": data.get("button_label", "Top"),
                      "source_entity_id": data.get("source_entity_id"), "actions": data.get("actions")}
            if "source_registry_id" in data:
                legacy["source_registry_id"] = data["source_registry_id"]
            buttons = (NativeButtonConfig.parse(legacy),)
        if len({button.id for button in buttons}) != len(buttons):
            raise ValueError("Button IDs must be unique")
        registry_ids = [button.source_registry_id for button in buttons if button.source_registry_id]
        if len(set(registry_ids)) != len(registry_ids):
            raise ValueError("Each legacy button must have a distinct native registry identity")
        if len({button.source_entity_id for button in buttons}) != len(buttons):
            raise ValueError("Each button must have a distinct native source entity")
        load = data.get("load_entity_id")
        first = buttons[0]
        return cls(text(data.get("unique_id"), "control identity", IDENTITY),
            text(data.get("name"), "control name"), first.source_entity_id, first.id, first.label, first.actions,
            entity_id(load, {"switch", "input_boolean", "light"}) if load is not None else None, buttons,
            leds=leds, source_device_id=data.get("source_device_id"), load_registry_id=load_registry_id)

    @property
    def inputs(self) -> tuple[NativeButtonConfig, ...]:
        # Preserve compatibility with direct construction of the original class.
        return self.source_buttons or (NativeButtonConfig(self.button_id, self.button_label,
                                                        self.source_entity_id, self.actions),)

    def buttons(self) -> list[dict[str, Any]]:
        return [button.capability() for button in self.inputs]

    def runtime_sources(self) -> tuple[SourceConfig, ...]:
        """Translate legacy declarations once; every input uses one runtime.

        Keep the saved configuration shape and its existing identities intact.
        Legacy wire source names are a presentation concern of the aggregate.
        """
        if self.source_configs:
            return self.source_configs
        return tuple(SourceConfig(
            id=button.id,
            type="native_event",
            buttons=(button,),
            mappings=tuple(SourceMapping(button.id, {"event_type": action}, gesture)
                           for action, gesture in button.actions.items()),
            filters={},
            source_entity_id=button.source_entity_id,
            source_registry_id=button.source_registry_id,
        ) for button in self.inputs)


@dataclass(frozen=True)
class SourceMapping:
    button_id: str
    match: dict[str, Any]
    gesture: Gesture


@dataclass(frozen=True)
class DeviceTriggerConfig:
    id: str
    config: dict[str, Any]


def _device_triggers(raw, device_id):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 256:
        raise ValueError("Device sources require 1 to 256 reported triggers")
    result, identities, configs = [], set(), set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "config"}:
            raise ValueError("Each device trigger needs an ID and HA trigger configuration")
        identity = text(item["id"], "trigger ID", KEY)
        config = item["config"]
        if (identity in identities or not isinstance(config, dict) or config.get("platform") != "device"
                or config.get("device_id") != device_id or not isinstance(config.get("domain"), str)
                or not re.fullmatch(r"[a-z_]+", config["domain"]) or config["domain"] == "mqtt"):
            raise ValueError("Device trigger identity must belong to the selected HA device")
        if not is_physical_device_trigger(config):
            raise ValueError("Only physical button/remote input triggers are supported")
        try:
            encoded = json.dumps(config, allow_nan=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as err:
            raise ValueError("Device trigger must contain JSON configuration") from err
        if len(encoded) > 8192:
            raise ValueError("Device trigger configuration is too large")
        if encoded in configs:
            raise ValueError("The same HA device trigger cannot be subscribed more than once")
        configs.add(encoded)
        identities.add(identity)
        result.append(DeviceTriggerConfig(identity, deepcopy(config)))
    return tuple(result)


def _filters(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError(f"{label} must be a mapping of at most 32 field paths")
    result = {}
    for path, expected in value.items():
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", path) or len(path) > 256:
            raise ValueError("Invalid declarative field path")
        if expected is not None and type(expected) not in (str, bool, int, float):
            raise ValueError("Field filters must compare JSON scalar values")
        if isinstance(expected, str) and len(expected) > 512:
            raise ValueError("Field filter value is too long")
        if type(expected) in (int, float):
            import math
            if not -MAX_EXACT_INTEGER <= expected <= MAX_EXACT_INTEGER or not math.isfinite(expected):
                raise ValueError("Field filter number must be finite and exact")
        result[path] = expected
    return result


@dataclass(frozen=True)
class SourceConfig:
    id: str
    type: str
    buttons: tuple[NativeButtonConfig, ...]
    mappings: tuple[SourceMapping, ...]
    filters: dict[str, Any]
    source_entity_id: str | None = None
    event_type: str | None = None
    topic: str | None = None
    availability_topic: str | None = None
    payload_available: str = "online"
    payload_not_available: str = "offline"
    inactivity_timeout: float | None = None
    source_registry_id: str | None = None
    device_triggers: tuple[DeviceTriggerConfig, ...] = ()
    raw_payload: bool = False
    device_trigger_refs: tuple[dict, ...] = ()

    @classmethod
    def parse(cls, data: Any) -> SourceConfig:
        if not isinstance(data, dict):
            raise ValueError("A source must be a mapping")
        allowed = {"id", "type", "buttons", "mappings", "filters", "source_entity_id", "event_type", "topic",
                   "availability_topic", "payload_available", "payload_not_available", "inactivity_timeout", "source_registry_id",
                   "device_id", "actions", "button_id", "button_label", "triggers", "raw_payload", "device_trigger_refs"}
        if set(data) - allowed:
            raise ValueError("Unknown source configuration field")
        kind = data.get("type")
        if not isinstance(kind, str):
            raise ValueError("Source type must be a string")
        kind = {"zwave_js_value_notification": "zwave_js", "zha_event": "zha", "zigbee2mqtt": "mqtt"}.get(kind, kind)
        if kind not in {"native_event", "shared_native", "zwave_js", "zha", "mqtt", "bus", "ha_device_trigger"}:
            raise ValueError("Unsupported source type")
        identity = text(data.get("id"), "source ID", KEY)
        source_entity = None
        if kind in {"native_event", "shared_native"}:
            source_entity = entity_id(data.get("source_entity_id"), {"event"})
        elif "source_entity_id" in data or "source_registry_id" in data:
            raise ValueError("source_entity_id requires a native event source")
        if kind not in {"zwave_js", "zha", "ha_device_trigger"} and "device_id" in data:
            raise ValueError("device_id requires an HA device source")
        triggers = ()
        if kind == "ha_device_trigger":
            device_id = text(data.get("device_id"), "HA device ID", IDENTITY)
            triggers = _device_triggers(data.get("triggers"), device_id)
        elif "triggers" in data:
            raise ValueError("triggers requires an HA device trigger source")
        registry_id = text(data["source_registry_id"], "source registry ID", IDENTITY) if "source_registry_id" in data else None
        raw_buttons = data.get("buttons")
        if raw_buttons is None and "actions" in data:
            raw_buttons = [{"id": data.get("button_id", "top"), "label": data.get("button_label", "Top")}]
        if not isinstance(raw_buttons, list) or not 1 <= len(raw_buttons) <= 32:
            raise ValueError("Each source requires 1 to 32 buttons")
        declarations = []
        for raw in raw_buttons:
            if not isinstance(raw, dict) or set(raw) - {"id", "label", "scene"}:
                raise ValueError("Source buttons define id, label, and optional Z-Wave scene")
            if kind != "zwave_js" and "scene" in raw:
                raise ValueError("scene is only supported for Z-Wave sources")
            declarations.append((text(raw.get("id"), "button ID", KEY), text(raw.get("label"), "button label"), raw.get("scene")))
        if len(declarations) > 1 and any(len(identity) + len(item[0]) + 1 > 128 for item in declarations):
            raise ValueError("Combined source/button identity exceeds the wire contract bound")
        if len({item[0] for item in declarations}) != len(declarations):
            raise ValueError("Source button IDs must be unique")
        raw_maps = data.get("mappings")
        if "actions" in data:
            if raw_maps is not None or len(declarations) != 1 or kind != "native_event":
                raise ValueError("actions is a single native_event convenience mapping")
            if not isinstance(data["actions"], dict) or not data["actions"]:
                raise ValueError("actions must not be empty")
            raw_maps = [{"button_id": declarations[0][0], "match": {"event_type": action}, "gesture": gesture}
                        for action, gesture in data["actions"].items()]
        if raw_maps is None and kind == "zwave_js":
            raw_maps = []
            for bid, _, scene in declarations:
                if type(scene) is not int or not 1 <= scene <= 255:
                    raise ValueError("Z-Wave default mappings require an explicit scene number per button")
                for count in range(1, 6):
                    raw_maps.append({"button_id": bid, "match": {"scene": scene, "value": "KeyPressed" if count == 1 else f"KeyPressed{count}x"},
                                     "gesture": {"kind": "press", "press_count": count}})
                for action, gesture in (("KeyHeldDown", "hold_start"), ("KeyReleased", "release")):
                    raw_maps.append({"button_id": bid, "match": {"scene": scene, "value": action}, "gesture": {"kind": gesture}})
        if not isinstance(raw_maps, list) or not 1 <= len(raw_maps) <= 256:
            raise ValueError("mappings must contain 1 to 256 explicit mappings")
        mappings = []
        actions_by_button = {bid: {} for bid, _, _ in declarations}
        for index, raw in enumerate(raw_maps):
            if not isinstance(raw, dict) or set(raw) != {"button_id", "match", "gesture"} or not isinstance(raw["button_id"], str) or raw["button_id"] not in actions_by_button:
                raise ValueError("Each mapping requires a declared button_id, match, and gesture")
            match = _filters(raw["match"], "Mapping match")
            if not match:
                raise ValueError("A mapping must explicitly match an action")
            gesture = Gesture.parse(raw["gesture"])
            mappings.append(SourceMapping(raw["button_id"], match, gesture))
            actions_by_button[raw["button_id"]][str(index)] = gesture
        for index, mapping in enumerate(mappings):
            if any(filters_overlap(mapping.match, previous.match) for previous in mappings[:index]):
                raise ValueError("Mapping rules must not overlap")
        buttons = tuple(NativeButtonConfig(bid, label, source_entity or "", actions_by_button[bid], registry_id) for bid, label, _ in declarations)
        for button in buttons:
            if not button.actions:
                raise ValueError("Every button needs at least one mapping")
            _validate_button_gestures(button.actions)
        filters = _filters(data.get("filters", {}), "Source filters")
        if kind == "ha_device_trigger":
            trigger_ids = {trigger.id for trigger in triggers}
            if filters or any(set(mapping.match) != {"trigger_id"} or mapping.match["trigger_id"] not in trigger_ids
                              for mapping in mappings):
                raise ValueError("Device trigger mappings must reference reported trigger IDs")
            if {mapping.match["trigger_id"] for mapping in mappings} != trigger_ids:
                raise ValueError("Each reported trigger needs one gesture mapping")
        if kind in {"zha", "zwave_js"}:
            device = text(data.get("device_id"), "HA device ID")
            if "device_id" in filters and filters["device_id"] != device:
                raise ValueError("Conflicting device filter")
            filters["device_id"] = device
        for mapping in mappings:
            for path in filters.keys() & mapping.match.keys():
                if not filters_overlap({path: filters[path]}, {path: mapping.match[path]}):
                    raise ValueError("Mapping contradicts its source filter")
        event_type = data.get("event_type")
        if kind == "bus":
            event_type = text(event_type, "bus event type")
            if event_type in {"state_changed", "*"}:
                raise ValueError("Use native_event for state sources; state_changed and wildcard bus subscriptions are forbidden")
            if not filters:
                raise ValueError("A generic bus source requires an explicit identity filter")
        elif event_type is not None:
            raise ValueError("event_type is only configurable for bus sources")
        topic = availability = None
        raw_payload, device_trigger_refs = False, ()
        if kind == "mqtt":
            topic = text(data.get("topic"), "MQTT action topic")
            availability = data.get("availability_topic")
            if availability is not None:
                availability = text(availability, "MQTT availability topic")
            if any(char in (topic + (availability or "")) for char in ("+", "#", "\x00")) or topic == availability:
                raise ValueError("MQTT topics must be distinct literal topics")
            raw_payload = data.get("raw_payload", False)
            if type(raw_payload) is not bool:
                raise ValueError("raw_payload must be a boolean")
            if "device_trigger_refs" in data:
                refs = data["device_trigger_refs"]
                if not raw_payload or not isinstance(refs, list) or not 1 <= len(refs) <= 256:
                    raise ValueError("MQTT device trigger references require literal payloads")
                for ref in refs:
                    if not isinstance(ref, dict) or set(ref) != {"device_id", "type", "subtype", "payload"}:
                        raise ValueError("Invalid MQTT device trigger reference")
                    text(ref["device_id"], "MQTT HA device ID", IDENTITY)
                    for field in ("type", "subtype", "payload"):
                        text(ref[field], "MQTT trigger " + field)
                device_trigger_refs = tuple(deepcopy(refs))
        elif any(key in data for key in ("topic", "availability_topic", "payload_available", "payload_not_available", "raw_payload", "device_trigger_refs")):
            raise ValueError("MQTT fields require an MQTT source")
        timeout = data.get("inactivity_timeout")
        if timeout is not None and (type(timeout) not in (int, float) or not 0.1 <= timeout <= 3600):
            raise ValueError("inactivity_timeout must be 0.1 to 3600 seconds")
        available = text(data.get("payload_available", "online"), "available payload")
        unavailable = text(data.get("payload_not_available", "offline"), "unavailable payload")
        if available == unavailable:
            raise ValueError("Availability payloads must differ")
        return cls(identity, kind, buttons, tuple(mappings), filters, source_entity, event_type, topic, availability,
                   available, unavailable, timeout, registry_id, triggers, raw_payload, device_trigger_refs)


def filters_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Conservatively reject rule pairs that could match the same occurrence."""
    for path in left.keys() & right.keys():
        a, b = left[path], right[path]
        if (type(a) is bool or type(b) is bool) and type(a) is not type(b):
            return False
        if a != b:
            return False
    return True


def source_channel(source: SourceConfig) -> tuple[str, str]:
    if source.type == "ha_device_trigger":
        return "ha_device_trigger", source.device_triggers[0].config["device_id"]
    if source.source_entity_id:
        return "native", source.source_entity_id
    if source.type == "mqtt":
        return "mqtt", source.topic
    return "bus", {"zwave_js": "zwave_js_value_notification", "zha": "zha_event"}.get(source.type, source.event_type)
