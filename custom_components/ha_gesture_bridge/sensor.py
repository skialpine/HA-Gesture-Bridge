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
NAMES_PAGE_BYTES = 16384
NAMES_MAX_BYTES = 524288
NAMES_MAX_PAGES = 64


def names_page_key(index: int) -> str:
    return f"catalog_names_page_{index:03d}"


def names_attributes(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Project names independently of load/gesture metadata, in bounded pages.

    A coordinator can page entities, but cannot split one entity attribute.
    ASCII JSON bounds leave room inside a 32 KiB entity-response envelope.
    Never truncate rows or names: an unrepresentable projection is explicit.
    """
    names = [{key: row[key] for key in ("entity_id", "domain", "friendly_name", "area_name",
              "entity_aliases", "source_entity_id", "is_group") if key in row} for row in rows]
    encoded = [json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) for row in names]
    canonical = "[" + ",".join(encoded) + "]"
    result = {"catalog_names_version": 1, "catalog_names_revision": sha256(canonical.encode()).hexdigest(),
              "catalog_names_total": len(names), "catalog_names_pages": 0}
    if len(names) > 10000 or len(canonical) > NAMES_MAX_BYTES:
        return result | {"catalog_names_error": "catalog_limit"}
    pages, page, size = [], [], 2
    for row, raw in zip(names, encoded, strict=True):
        if len(raw) + 2 > NAMES_PAGE_BYTES:
            return result | {"catalog_names_error": "record_too_large"}
        addition = len(raw) + bool(page)
        if page and size + addition > NAMES_PAGE_BYTES:
            pages.append(page)
            page, size = [], 2
        size += len(raw) + bool(page)
        page.append(row)
    if page:
        pages.append(page)
    if len(pages) > NAMES_MAX_PAGES:
        return result | {"catalog_names_error": "catalog_limit"}
    result["catalog_names_pages"] = len(pages)
    return result | {names_page_key(index): page for index, page in enumerate(pages, 1)}

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
    result = {"bridge_contract_version": version, "bridge_control_id": control_id,
            "bridge_buttons": projected_buttons, "bridge_sources": sources,
            "bridge_load": _load_attributes(attributes.get("bridge_load"))}
    source_device_id = attributes.get("bridge_source_device_id")
    if isinstance(source_device_id, str) and IDENTITY.fullmatch(source_device_id):
        result["bridge_source_device_id"] = source_device_id
    return result


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


def _is_group(entry: er.RegistryEntry, attributes: dict[str, Any]) -> bool:
    if entry.platform == "group":
        return True
    for key in ("entity_id", "group_entities"):
        members = attributes.get(key)
        if isinstance(members, (list, tuple)) and all(
            isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z0-9_]+", value)
            for value in members
        ):
            return True
    return False


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
    if _is_group(entry, attrs):
        row["is_group"] = True
    if kind == "gesture":
        row["bridge_control_id"] = projected["bridge_control_id"]
        load = projected["bridge_load"]
        row["load_entity_id"] = load["entity_id"] if load else None
        row["load_domain"] = load["domain"] if load else None
    return row


def _preferred_rows(rows: list[dict[str, Any]], registry: er.EntityRegistry) -> list[dict[str, Any]]:
    """Choose representations using registry relations, never names or areas.

    Aliases identify replaced picker entries only. The bridge's controlled load
    remains the explicit bridge_load snapshot, independent of physical input.
    """
    loads = {row["entity_id"]: row for row in rows if row["domain"] != "event"}
    parents = {entity_id: entity_id for entity_id in loads}
    wrappers = set()

    def root(entity_id):
        while parents[entity_id] != entity_id:
            parents[entity_id] = parents[parents[entity_id]]
            entity_id = parents[entity_id]
        return entity_id

    by_registry_id = {entry.id: entry.entity_id for entry in registry.entities.values()}
    for entity_id, row in loads.items():
        entry = registry.async_get(entity_id)
        if row["domain"] != "light" or row.get("is_group") or entry.platform != "switch_as_x":
            continue
        options = entry.options.get("switch_as_x", entry.options.get("light", {}))
        if not isinstance(options, dict) or options.get("invert") is not False:
            continue
        source_id = options.get("entity_id")
        if not isinstance(source_id, str):
            continue
        source_id = by_registry_id.get(source_id, source_id)
        source = loads.get(source_id)
        if (source is None or source["domain"] != "switch" or source.get("is_group")
                or registry.async_get(source_id).platform == "switch_as_x"):
            continue
        left, right = sorted((root(entity_id), root(source_id)))
        parents[right] = left
        wrappers.add(entity_id)

    components: dict[str, list[str]] = {}
    for entity_id in loads:
        components.setdefault(root(entity_id), []).append(entity_id)
    preferred = {key: min(ids, key=lambda item: (item not in wrappers, loads[item]["kind"] != "dimmer", item))
                 for key, ids in components.items()}
    device_components: dict[str, set[str]] = {}
    for entity_id, row in loads.items():
        entry = registry.async_get(entity_id)
        if entry.device_id and not row.get("is_group") and entry.entity_category not in {"config", "diagnostic"}:
            device_components.setdefault(entry.device_id, set()).add(root(entity_id))

    controls: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["domain"] != "event":
            continue
        source = row["attributes"].get("bridge_source_device_id")
        candidates = device_components.get(source, set())
        key = None
        if len(candidates) == 1:
            key = next(iter(candidates))
        elif not source:
            load_id = row.get("load_entity_id")
            key = root(load_id) if load_id in loads else None
        if key is not None:
            controls.setdefault(key, []).append(row)

    result, replaced = {}, set()
    for key, ids in components.items():
        source_id = preferred[key]
        source = loads[source_id]
        linked = controls.get(key, [])
        if len(linked) == 1:
            choice = dict(linked[0])
            suffix = "group, gestures" if source.get("is_group") else "gestures"
            choice.update(friendly_name=f"{source['friendly_name']} ({suffix})", area_name=source["area_name"],
                          source_entity_id=source_id, entity_aliases=sorted(ids))
            if source.get("is_group"):
                choice["is_group"] = True
            result[choice["entity_id"]] = choice
            replaced.update(ids)
        else:
            choice = dict(source)
            aliases = sorted(set(ids) - {source_id})
            if aliases:
                choice["entity_aliases"] = aliases
            result[source_id] = choice
            replaced.update(set(ids) - {source_id})
            # Several configured controls do not prove a single best one.
            for control in linked:
                result[control["entity_id"]] = dict(control, source_entity_id=source_id)
    for row in rows:
        if row["entity_id"] not in replaced:
            result.setdefault(row["entity_id"], row)
    return sorted(result.values(), key=lambda row: row["entity_id"])


@callback
def build_catalog(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return deterministic eligible entity records without loading any adapter."""
    rows = []
    for entry in er.async_get(hass).entities.values():
        if (row := _catalog_row(hass, entry, hass.states.get(entry.entity_id))) is not None:
            rows.append(row)
    return _preferred_rows(rows, er.async_get(hass))


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create the singleton installation-level catalog, even with no controls."""
    async_add_entities([GestureCatalogSensor()])


class GestureCatalogSensor(SensorEntity):
    """A metadata-only catalog, with no button event or device-control effects."""

    _attr_name = "HA Gesture Bridge Catalog"
    _attr_unique_id = CATALOG_UNIQUE_ID
    _attr_should_poll = False
    _attr_icon = "mdi:format-list-bulleted"
    _unrecorded_attributes = frozenset({"catalog_entities", *(names_page_key(index) for index in range(1, NAMES_MAX_PAGES + 1))})

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._revision = ""
        self._names: dict[str, Any] = {}

    @property
    def native_value(self) -> int:
        return len(self._rows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"catalog_contract_version": CATALOG_VERSION, "catalog_revision": self._revision,
                "catalog_entities": deepcopy(self._rows), **deepcopy(self._names)}

    @callback
    def _refresh(self, *, write: bool = True) -> None:
        rows = build_catalog(self.hass)
        revision = sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        if revision == self._revision:
            return
        self._rows, self._revision = rows, revision
        self._names = names_attributes(rows)
        if write:
            self.async_write_ha_state()

    @callback
    def _registry_changed(self, event: Event) -> None:
        if (event.event_type == er.EVENT_ENTITY_REGISTRY_UPDATED
                and event.data.get("entity_id", "").split(".", 1)[0] not in _STATE_DOMAINS):
            return
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
