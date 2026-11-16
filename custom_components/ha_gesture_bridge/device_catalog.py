"""Read-only physical choices; no provider enumeration or input attachment."""

from __future__ import annotations

import re

from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .config import DOMAIN


def has_identifier(device, domain):
    """Only the namespace matters; some integrations append endpoint identity."""
    return bool(device and any(value and value[0] == domain for value in device.identifiers))


def load_record(hass, entry):
    state = hass.states.get(entry.entity_id)
    return {"registry_id": entry.id, "entity_id": entry.entity_id, "domain": entry.domain,
            "available": bool(state and state.state in ("on", "off"))}


def device_choices(hass):
    # Imports here keep the existing catalog helpers as the single owner of
    # wrapper equivalence, without coupling their entity projection to choices.
    from .sensor import _catalog_row, _preferred_rows

    registry, devices = er.async_get(hass), dr.async_get(hass)
    loads, inputs = [], set()
    for entity in registry.entities.values():
        device = devices.async_get(entity.device_id) if entity.device_id else None
        if ((device and device.disabled_by) or entity.disabled_by or entity.platform == "control4"
                or entity.entity_category is not None):
            continue
        if entity.domain == "event" and entity.platform != DOMAIN and device:
            inputs.add(device.id)
        elif entity.domain in {"light", "switch"} and (device or entity.platform == "switch_as_x"):
            row = _catalog_row(hass, entity, hass.states.get(entity.entity_id))
            if row and not row.get("is_group"):
                loads.append(row)
    controls = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        for sub in entry.get_subentries_of_type("control"):
            controls.setdefault(sub.data.get("source_device_id"), []).append(sub)
    by_device = {}
    for row in _preferred_rows(loads, registry):
        entry = registry.async_get(row["entity_id"])
        if entry:
            owners = {item.device_id for name in [row["entity_id"], *row.get("entity_aliases", [])]
                      if (item := registry.async_get(name)) is not None and item.device_id}
            if len(owners) == 1:
                by_device.setdefault(next(iter(owners)), []).append((entry, row))
    result = []
    for device in devices.devices:
        if device.disabled_by or has_identifier(device, "control4"):
            continue
        endpoints = by_device.get(device.id, [])
        # A reviewed candidate namespace is enough to offer inspection, never
        # enough to declare taps. Ordinary sensors/hubs are not remote choices.
        remote = bool(re.search(r"remote|keypad|controller|button|sensor_switch|ts004", device.model or "", re.I))
        if not endpoints and device.id not in inputs and device.id not in controls and not remote:
            continue
        area = ar.async_get(hass).async_get_area(device.area_id) if device.area_id else None
        name = (device.name_by_user or device.name or "HA Device")[:128]
        for endpoint, row in endpoints or [(None, None)]:
            label = name
            if len(endpoints) > 1:
                label += " — " + str(row["friendly_name"])[:120]
            record = {"type": "device_choice", "version": 1,
                      "choice_id": f"device:{device.id}:{endpoint.id if endpoint else 'none'}",
                      "device_id": device.id, "name": label,
                      "kind": row["kind"] if row else "remote",
                      "gestures": "confirmed" if device.id in controls else "candidate"}
            matches = controls.get(device.id, [])
            if len(matches) == 1:
                gesture_id = registry.async_get_entity_id("event", DOMAIN, matches[0].unique_id)
                if gesture_id:
                    record["gesture_entity_id"] = gesture_id
            if endpoint:
                record["source_load"] = load_record(hass, endpoint)
            if area:
                record["area"] = area.name[:128]
            result.append(record)
    return sorted(result, key=lambda item: item["choice_id"])


def selected_endpoint(hass, device_id, registry_id):
    """Resolve only a currently eligible endpoint or its proven exact alias."""
    from .sensor import _catalog_row, _preferred_rows

    registry = er.async_get(hass)
    selected = registry.async_get(registry_id) if registry_id else None
    if selected is None:
        raise ValueError("invalid_endpoint")
    for choice in device_choices(hass):
        if choice["device_id"] != device_id or "source_load" not in choice:
            continue
        endpoint = registry.async_get(choice["source_load"]["registry_id"])
        if selected.id == endpoint.id:
            return selected
        # Reuse exact switch-as-light equivalence, including a wrapper whose
        # HA entry lacks device_id. Never authorize another device by its name.
        rows = []
        for entry in (selected, endpoint):
            if (row := _catalog_row(hass, entry, hass.states.get(entry.entity_id))) is not None:
                rows.append(row)
        for row in _preferred_rows(rows, registry):
            if selected.entity_id in row.get("entity_aliases", []) and row["entity_id"] == endpoint.entity_id:
                return endpoint
    raise ValueError("invalid_endpoint")
