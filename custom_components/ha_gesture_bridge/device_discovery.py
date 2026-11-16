"""Discover physical inputs of one HA device without asking for its protocol."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

from homeassistant.components.device_automation import DeviceAutomationType, async_get_device_automations
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .config import Gesture, NativeControlConfig
from .config_values import KEY, text
from .gesture_profiles import PROFILE_ACTIONS, action_definition, is_physical_device_trigger, native_action_definition
from .mqtt_device import resolve_trigger

MAX_BUTTONS = 32
MAX_ACTIONS = 256


class UnsupportedDeviceConfiguration(ValueError):
    """The existing device exposes an input topology this bridge cannot map."""


class SourceMetadataUnavailable(ValueError):
    """An enabled native input exists but has not supplied capabilities yet."""


def _identity(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:24]


def _suggest(label):
    words = set(re.sub(r"[^a-z0-9]+", " ", label.lower()).split())
    if words & {"top", "upper"} and not words & {"bottom", "lower"}:
        return "top"
    if words & {"bottom", "lower"} and not words & {"top", "upper"}:
        return "bottom"
    if "toggle" in words:
        return "toggle"
    return None


@dataclass(frozen=True)
class DiscoveredButton:
    button_id: str
    label: str
    suggested_role: str | None
    actions: dict[str, dict]
    source_entity_id: str | None = None
    source_registry_id: str | None = None
    triggers: tuple[dict, ...] = ()

    @property
    def gestures(self) -> tuple[Gesture, ...]:
        unique = {gesture.id: gesture for action in self.actions.values() if (gesture := Gesture.parse(action))}
        order = {"press": 0, "hold_start": 1, "hold_repeat": 2, "release": 3, "custom": 4}
        return tuple(sorted(unique.values(), key=lambda item: (order[item.kind], item.press_count or 0, item.id)))

    @property
    def gesture_summary(self) -> str:
        return ", ".join(gesture.label for gesture in self.gestures)


@dataclass(frozen=True)
class DeviceDiscovery:
    device_id: str
    device_name: str
    buttons: tuple[DiscoveredButton, ...]

    def build_config(self, roles, *, unique_id, name, load_entity_id=None) -> dict:
        if not self.buttons or not isinstance(roles, dict) or set(roles) != {item.button_id for item in self.buttons}:
            raise ValueError("Confirm a role for every detected button")
        assigned = {key: text(value, "button role", KEY) for key, value in roles.items()}
        if len(set(assigned.values())) != len(assigned):
            raise ValueError("Each button needs a distinct role")
        result = {"unique_id": unique_id, "name": name, "source_device_id": self.device_id,
                  "source_button_roles": assigned}
        if load_entity_id is not None:
            result["load_entity_id"] = load_entity_id
        if all(button.source_entity_id for button in self.buttons):
            result["buttons"] = [{"id": assigned[button.button_id], "label": button.label,
                "source_entity_id": button.source_entity_id, "source_registry_id": button.source_registry_id,
                "actions": deepcopy(button.actions)} for button in self.buttons]
        else:
            triggers, mappings, declarations = [], [], []
            mqtt_sources = {}
            for button in self.buttons:
                role = assigned[button.button_id]
                if button.triggers and button.triggers[0]["config"]["domain"] == "mqtt":
                    for trigger in button.triggers:
                        target = trigger["mqtt"]
                        source = mqtt_sources.setdefault(target["topic"], {
                            "id": _identity("mqtt_", self.device_id + "/" + target["topic"]), "type": "mqtt",
                            "topic": target["topic"], "raw_payload": True,
                            "buttons": [], "mappings": [], "device_trigger_refs": []})
                        if not any(item["id"] == role for item in source["buttons"]):
                            source["buttons"].append({"id": role, "label": button.label})
                        source["mappings"].append({"button_id": role, "match": {"payload": target["payload"]},
                            "gesture": deepcopy(button.actions[trigger["id"]])})
                        source["device_trigger_refs"].append({"device_id": self.device_id, "type": trigger["config"]["type"],
                            "subtype": trigger["config"]["subtype"], "payload": target["payload"]})
                    continue
                declarations.append({"id": role, "label": button.label})
                for trigger in button.triggers:
                    triggers.append(deepcopy(trigger))
                    mappings.append({"button_id": role, "match": {"trigger_id": trigger["id"]},
                                     "gesture": deepcopy(button.actions[trigger["id"]])})
            result["sources"] = list(mqtt_sources.values())
            if triggers:
                result["sources"].append({"id": _identity("device_", self.device_id), "type": "ha_device_trigger",
                    "device_id": self.device_id, "buttons": declarations, "triggers": triggers, "mappings": mappings})
        NativeControlConfig.parse(result)
        return result


def _native_buttons(hass, device_id):
    result = []
    registry = er.async_get(hass)
    for entry in sorted(er.async_entries_for_device(registry, device_id), key=lambda item: item.id):
        if entry.domain != "event" or entry.disabled_by is not None or entry.platform == "ha_gesture_bridge":
            continue
        state = hass.states.get(entry.entity_id)
        if state is not None and "bridge_contract_version" in state.attributes:
            continue
        event_types = state.attributes.get("event_types") if state else None
        if not isinstance(event_types, (list, tuple)) or not event_types:
            event_types = (entry.capabilities or {}).get("event_types")
        if not event_types:
            raise SourceMetadataUnavailable("Native input capabilities are not ready")
        if not isinstance(event_types, (list, tuple)) or not event_types or len(event_types) > MAX_ACTIONS:
            continue
        actions = {}
        for action in event_types:
            if isinstance(action, str) and action.strip() and len(action) <= 128 and not any(ord(char) < 32 for char in action):
                actions[action] = native_action_definition(action, entry.platform, event_types)
        if not actions:
            continue
        label = (entry.name or entry.original_name or (state.attributes.get("friendly_name") if state else None) or entry.entity_id)[:128]
        result.append(DiscoveredButton(_identity("button_", entry.id), label, _suggest(label), actions,
                                      entry.entity_id, entry.id))
    return result


def _trigger_input(raw, device_id):
    if not is_physical_device_trigger(raw) or raw.get("device_id") != device_id:
        return None
    kind, subtype = raw.get("type"), raw.get("subtype")
    if not isinstance(kind, str) or not kind or len(kind) > 128 or kind.startswith(("state.", "value_updated.")):
        return None
    if subtype is not None and (not isinstance(subtype, str) or not subtype or len(subtype) > 128):
        return None
    # Metadata describes the picker, not the attachable device trigger.
    config = {key: deepcopy(value) for key, value in raw.items() if key not in {"metadata", "id", "alias", "enabled"}}
    try:
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None
    if len(canonical) > 8192:
        return None
    action, button = kind, subtype or "button"
    if kind == "action":
        action = subtype or kind
        button = "button"
        # MQTT discovery commonly names actions 1_single/1_double etc. Only a
        # known action suffix grants normalized tap/hold semantics.
        for suffix in sorted(PROFILE_ACTIONS, key=len, reverse=True):
            if action.endswith("_" + suffix):
                button, action = action[:-(len(suffix) + 1)], suffix
                break
        if action not in PROFILE_ACTIONS:
            button = subtype or "button"
    label = button.replace("_", " ").strip().title() or "Button"
    identity = _identity("trigger_", canonical)
    return button, label, {"id": identity, "config": config}, action_definition(action)


async def discover_device(hass, device_id: str) -> DeviceDiscovery:
    device = dr.async_get(hass).async_get(device_id)
    if device is None or device.disabled_by is not None:
        raise ValueError("Device is unavailable")
    name = (device.name_by_user or device.name or "HA Device")[:128]
    buttons = _native_buttons(hass, device_id)
    if not buttons:
        try:
            automations = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, [device_id])
        except HomeAssistantError as err:
            raise ValueError("Device gesture provider is unavailable") from err
        grouped = {}
        seen = set()
        for raw in automations.get(device_id, []):
            discovered = _trigger_input(raw, device_id)
            if discovered is None:
                continue
            group, label, trigger, action = discovered
            if trigger["config"]["domain"] == "mqtt":
                target = resolve_trigger(hass, trigger["config"])
                if target is None or not target.topic or not isinstance(target.payload, str) or not target.payload:
                    continue
                if target.value_template:
                    raise UnsupportedDeviceConfiguration("MQTT device triggers using value templates are not supported")
                if len(target.payload) > 128:
                    raise UnsupportedDeviceConfiguration("MQTT device trigger payload is too long")
                trigger["mqtt"] = {"topic": target.topic, "payload": target.payload}
            if trigger["id"] in seen:
                continue
            seen.add(trigger["id"])
            if len(seen) > MAX_ACTIONS:
                raise UnsupportedDeviceConfiguration("Device reports too many gesture triggers")
            key = (trigger["config"]["domain"], group)
            item = grouped.setdefault(key, {"label": label, "triggers": [], "actions": {}})
            item["triggers"].append(trigger); item["actions"][trigger["id"]] = action
        for (domain, group), item in sorted(grouped.items()):
            if domain == "mqtt" and len({trigger["mqtt"]["topic"] for trigger in item["triggers"]}) > 1:
                raise UnsupportedDeviceConfiguration("MQTT gestures for one button on multiple topics are not supported")
            buttons.append(DiscoveredButton(_identity("button_", device_id + "/" + domain + "/" + group),
                item["label"], _suggest(item["label"]), item["actions"],
                triggers=tuple(sorted(item["triggers"], key=lambda trigger: trigger["id"]))))
    if len(buttons) > MAX_BUTTONS:
        raise UnsupportedDeviceConfiguration("Device reports too many physical buttons")
    if len(buttons) == 1 and buttons[0].suggested_role is None:
        button = buttons[0]
        buttons[0] = DiscoveredButton(button.button_id, button.label, "toggle", button.actions,
            button.source_entity_id, button.source_registry_id, button.triggers)
    discovered = DeviceDiscovery(device_id, name, tuple(buttons))
    if buttons:
        # Surface unsupported mappings at device selection, before promising a
        # review screen whose detected configuration cannot actually be saved.
        try:
            discovered.build_config({button.button_id: button.button_id for button in buttons},
                                    unique_id="discovery-validation", name=name)
        except ValueError as err:
            raise UnsupportedDeviceConfiguration("Detected inputs cannot be mapped safely") from err
    return discovered
