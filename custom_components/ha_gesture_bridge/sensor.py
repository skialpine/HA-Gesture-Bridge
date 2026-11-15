"""Installation-level discovery catalog, independent of any consumer platform."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import re
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import EVENT_STATE_CHANGED, Event, HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .config import DOMAIN, Gesture, IDENTITY, KEY

CATALOG_VERSION = "1.0"
CATALOG_UNIQUE_ID = "ha_gesture_bridge_catalog"
_LIGHT_MODES = frozenset({"onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"})
_DIMMABLE_MODES = _LIGHT_MODES - {"onoff"}
_STATE_DOMAINS = frozenset({"switch", "input_boolean", "light", "event"})


def _light_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Project advertised capability types, never arbitrary upstream attributes."""
    result: dict[str, Any] = {}
    modes = attributes.get("supported_color_modes")
    if (isinstance(modes, (list, tuple, set, frozenset)) and modes
            and all(isinstance(mode, str) and mode in _LIGHT_MODES for mode in modes)
            and len(set(modes)) == len(modes)):
        result["supported_color_modes"] = sorted(modes)
    features = attributes.get("supported_features")
    if type(features) is int and 0 <= features <= 4294967295:
        result["supported_features"] = features
    return result


def _load_attributes(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    entity_id = value.get("entity_id")
    domain = value.get("domain")
    state = value.get("state")
    if (not isinstance(domain, str) or domain not in {"switch", "input_boolean", "light"} or not isinstance(entity_id, str)
            or re.fullmatch(rf"{domain}\.[a-z0-9_]+", entity_id) is None
            or not isinstance(state, str) or state not in {"on", "off", "unknown", "unavailable"}
            or type(value.get("available")) is not bool):
        return None
    if value["available"] and value["state"] not in {"on", "off"}:
        return None
    projected = {key: value[key] for key in ("entity_id", "domain", "state", "available")}
    if domain == "light":
        attrs = value.get("attributes")
        projected["attributes"] = _light_attributes(attrs if isinstance(attrs, dict) else {})
    return projected


def _bridge_attributes(attributes: dict[str, Any]) -> dict[str, Any] | None:
    """Recognize configured aggregates conservatively and keep typed metadata."""
    version = attributes.get("bridge_contract_version")
    control_id = attributes.get("bridge_control_id")
    if (not isinstance(version, str) or re.fullmatch(r"1\.[0-9]+", version) is None
            or not isinstance(control_id, str) or IDENTITY.fullmatch(control_id) is None):
        return None
    buttons = attributes.get("bridge_buttons")
    if not isinstance(buttons, list) or not buttons:
        return None
    projected_buttons = []
    identities: set[str] = set()
    for button in buttons:
        if (not isinstance(button, dict) or not isinstance(button.get("id"), str)
                or KEY.fullmatch(button["id"]) is None or button["id"] in identities
                or not isinstance(button.get("label"), str) or not 1 <= len(button["label"]) <= 128
                or not isinstance(button.get("gestures"), list) or not button["gestures"]):
            return None
        identities.add(button["id"])
        gestures = []
        gesture_ids: set[str] = set()
        for value in button["gestures"]:
            if not isinstance(value, dict):
                return None
            try:
                gesture = Gesture.parse({key: value[key] for key in ("id", "kind", "label", "press_count") if key in value})
            except ValueError:
                return None
            if gesture.id in gesture_ids:
                return None
            gesture_ids.add(gesture.id)
            gestures.append(gesture.capability())
        projected_buttons.append({"id": button["id"], "label": button["label"], "gestures": gestures})
    sources = []
    for source in attributes.get("bridge_sources", []) if isinstance(attributes.get("bridge_sources"), list) else []:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or IDENTITY.fullmatch(source["id"]) is None or type(source.get("ready")) is not bool
                or source.get("availability") not in ("available", "unavailable", "unknown")):
            continue
        projected = {key: source[key] for key in ("id", "ready", "availability")}
        button_id = source.get("button_id")
        if isinstance(button_id, str) and KEY.fullmatch(button_id):
            projected["button_id"] = button_id
        sources.append(projected)
    return {"bridge_contract_version": version, "bridge_control_id": control_id,
            "bridge_buttons": projected_buttons, "bridge_sources": sources,
            "bridge_load": _load_attributes(attributes.get("bridge_load"))}


def _availability(state: State | None, *, gesture: bool = False,
                  sources: list[dict[str, Any]] | None = None) -> tuple[str, bool | None]:
    if state is not None and (state.state == "unavailable" or state.attributes.get("restored")):
        return "unavailable", False
    if state is None or state.state == "unknown":
        availability, available = "unknown", None
    else:
        availability, available = "available", True
    if not gesture:
        return availability, available
    sources = sources or []
    ready = [source for source in sources if isinstance(source, dict) and source.get("ready") is True]
    if any(source.get("availability") == "available" for source in ready):
        return "available", True
    reported = [source.get("availability") for source in sources if isinstance(source, dict)]
    if ready:
        return "unknown", None
    if reported and all(item == "unavailable" for item in reported):
        return "unavailable", False
    return "unknown", None


@callback
def _catalog_row(hass: HomeAssistant, entry: er.RegistryEntry, state: State | None) -> dict[str, Any] | None:
    """Project one entity identically for discovery and change filtering."""
    devices, areas = dr.async_get(hass), ar.async_get(hass)
    domain = entry.entity_id.split(".", 1)[0]
    if entry.disabled_by is not None or domain not in _STATE_DOMAINS:
        return None
    attrs = dict(state.attributes) if state else {}
    projected: dict[str, Any] = {}
    if domain in {"switch", "input_boolean"}:
        kind = "switch"
    elif domain == "light":
        # Registry capability data remains useful when a light is unavailable.
        capabilities = {"supported_features": entry.supported_features, **(entry.capabilities or {}), **attrs}
        projected = _light_attributes(capabilities)
        kind = "dimmer" if _DIMMABLE_MODES.intersection(projected.get("supported_color_modes", [])) else "switch"
    else:
        if entry.platform != DOMAIN or (projected := _bridge_attributes(attrs)) is None:
            return None
        kind = "gesture"
    device = devices.async_get(entry.device_id) if entry.device_id else None
    if device and device.disabled_by is not None:
        return None
    area_id = entry.area_id or (device.area_id if device else None)
    area = areas.async_get_area(area_id) if area_id else None
    name = attrs.get("friendly_name") or entry.name or entry.original_name
    if not isinstance(name, str) or not name.strip():
        name = (device.name_by_user or device.name) if device else entry.entity_id
    availability, available = _availability(state, gesture=kind == "gesture",
                                           sources=projected.get("bridge_sources"))
    ready = (availability != "unavailable" and any(source["ready"] for source in projected.get("bridge_sources", []))) if kind == "gesture" else available
    state_value = state.state if state and state.state in ("on", "off", "unknown", "unavailable") else "unknown"
    if kind == "gesture":
        state_value = "ready" if ready else "unavailable" if availability == "unavailable" else "unknown"
    row = {"entity_id": entry.entity_id, "domain": domain, "kind": kind,
           "friendly_name": name or entry.entity_id, "area_name": area.name if area else None,
           "device_id": entry.device_id, "state": state_value,
           "availability": availability, "available": available, "ready": ready, "attributes": projected}
    if kind == "gesture":
        row["bridge_control_id"] = projected["bridge_control_id"]
        load = projected["bridge_load"]
        row["load_entity_id"] = load["entity_id"] if load else None
        row["load_domain"] = load["domain"] if load else None
    return row


@callback
def build_catalog(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return deterministic eligible entity records without loading any adapter."""
    rows = []
    for entry in er.async_get(hass).entities.values():
        if (row := _catalog_row(hass, entry, hass.states.get(entry.entity_id))) is not None:
            rows.append(row)
    return sorted(rows, key=lambda row: row["entity_id"])


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create the singleton installation-level catalog, even with no controls."""
    async_add_entities([GestureCatalogSensor()])


class GestureCatalogSensor(SensorEntity):
    """A metadata-only catalog, with no button event or device-control effects."""

    _attr_name = "HA Gesture Bridge Catalog"
    _attr_unique_id = CATALOG_UNIQUE_ID
    _attr_should_poll = False
    _attr_icon = "mdi:format-list-bulleted"
    _unrecorded_attributes = frozenset({"catalog_entities"})

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._revision = ""

    @property
    def native_value(self) -> int:
        return len(self._rows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"catalog_contract_version": CATALOG_VERSION, "catalog_revision": self._revision,
                "catalog_entities": deepcopy(self._rows)}

    @callback
    def _refresh(self, *, write: bool = True) -> None:
        rows = build_catalog(self.hass)
        revision = sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        if revision == self._revision:
            return
        self._rows, self._revision = rows, revision
        if write:
            self.async_write_ha_state()

    @callback
    def _registry_changed(self, event: Event) -> None:
        self._refresh()

    @callback
    def _state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id", "")
        if entity_id.split(".", 1)[0] not in _STATE_DOMAINS:
            return
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or (entity_id.startswith("event.") and entry.platform != DOMAIN):
            return
        # Ignore volatile brightness, native event timestamps and gesture IDs.
        # Reusing the catalog projection keeps every discovery field significant.
        if _catalog_row(self.hass, entry, event.data.get("old_state")) != _catalog_row(
            self.hass, entry, event.data.get("new_state")
        ):
            self._refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed))
        for event_type in (er.EVENT_ENTITY_REGISTRY_UPDATED, dr.EVENT_DEVICE_REGISTRY_UPDATED,
                           ar.EVENT_AREA_REGISTRY_UPDATED):
            self.async_on_remove(self.hass.bus.async_listen(event_type, self._registry_changed))
        self._refresh(write=False)
