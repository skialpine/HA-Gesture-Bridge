"""Read-only physical choices; no provider enumeration or input attachment."""

from __future__ import annotations

from collections.abc import Mapping
import re

from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .config import DOMAIN


def has_identifier(device, domain):
    """Only the namespace matters; some integrations append endpoint identity."""
    return bool(device and any(value and value[0] == domain for value in device.identifiers))


def _control4_owner(hass, entry_id):
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    return entry is not None and entry.domain == "control4"


def eligible_device(hass, device):
    return bool(device and device.disabled_by is None and not has_identifier(device, "control4")
                and not _control4_owner(hass, device.config_entry_id))


def eligible_entry(hass, entry, device, *, allow_hidden=False):
    return (entry.disabled_by is None and (allow_hidden or entry.hidden_by is None)
            and entry.entity_category is None and entry.platform != "control4"
            and not _control4_owner(hass, entry.config_entry_id)
            and (device is None or eligible_device(hass, device)))


def control_event(hass, control_id):
    """Return the registered aggregate and whether consumers can select it."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("event", DOMAIN, control_id)
    if entity_id is None:
        return None, False
    entry = registry.async_get(entity_id)
    device = dr.async_get(hass).async_get(entry.device_id) if entry and entry.device_id else None
    return entry, entry is not None and eligible_entry(hass, entry, device)


def switch_as_light(hass, registry, entry):
    """Resolve a wrapper's actual target and inversion from its HA configuration."""
    if entry.domain != "light" or entry.platform != "switch_as_x":
        return None, None
    owner = hass.config_entries.async_get_entry(entry.config_entry_id) if entry.config_entry_id else None
    options = (owner.options if owner and owner.domain == "switch_as_x"
               else entry.options.get("switch_as_x", entry.options.get("light")))
    if not isinstance(options, Mapping) or not isinstance(reference := options.get("entity_id"), str):
        return None, None
    return registry.async_get(reference), options.get("invert")


def load_record(hass, entry):
    state = hass.states.get(entry.entity_id)
    return {"registry_id": entry.id, "entity_id": entry.entity_id, "domain": entry.domain,
            "available": bool(state and state.state in ("on", "off"))}


def _device_choices(hass, *, include_load_names=False):
    # Imports here keep the existing catalog helpers as the single owner of
    # wrapper equivalence, without coupling their entity projection to choices.
    from .sensor import _catalog_row, _preferred_rows

    registry, devices = er.async_get(hass), dr.async_get(hass)
    loads, inputs, wrapped_devices, hidden_targets = [], set(), {}, {}
    for entity in registry.entities.values():
        if entity.domain not in {"event", "light", "switch"}:
            continue
        device = devices.async_get(entity.device_id) if entity.device_id else None
        if not eligible_entry(hass, entity, device):
            continue
        if entity.domain == "event" and entity.platform != DOMAIN and device:
            inputs.add(device.id)
        elif entity.domain in {"light", "switch"}:
            target, invert = switch_as_light(hass, registry, entity)
            if target is not None:
                target_device = devices.async_get(target.device_id) if target.device_id else None
                if not eligible_entry(hass, target, target_device, allow_hidden=True):
                    continue
                # HA usually hides the wrapped switch. The visible wrapper can
                # still inherit the device without exposing the hidden alias.
                if target_device and target.domain == "switch":
                    wrapped_devices[entity.entity_id] = target_device.id
                    if target.hidden_by is not None and invert is False and target.entity_id not in hidden_targets:
                        base = _catalog_row(hass, target, hass.states.get(target.entity_id))
                        if base and not base.get("is_group"):
                            hidden_targets[target.entity_id] = base
            if not device and entity.entity_id not in wrapped_devices:
                continue
            row = _catalog_row(hass, entity, hass.states.get(entity.entity_id))
            if row and not row.get("is_group"):
                loads.append(row)
    controls = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        for sub in entry.get_subentries_of_type("control"):
            controls.setdefault(sub.data.get("source_device_id"), []).append(sub)
    by_device = {}
    for row in _preferred_rows(hass, [*loads, *hidden_targets.values()], registry):
        entity_id = row["entity_id"]
        if entity_id in hidden_targets:
            continue
        entry = registry.async_get(entity_id)
        aliases = {entity_id, *row.get("entity_aliases", [])}
        owners = {item.device_id for name in aliases
                  if (item := registry.async_get(name)) is not None and item.device_id}
        owners.update(wrapped_devices[name] for name in aliases if name in wrapped_devices)
        if len(owners) == 1:
            by_device.setdefault(next(iter(owners)), []).append((entry, row, aliases - hidden_targets.keys()))
    for device in devices.devices:
        if not eligible_device(hass, device):
            continue
        endpoints = by_device.get(device.id, [])
        # A reviewed candidate namespace is enough to offer inspection, never
        # enough to declare taps. Ordinary sensors/hubs are not remote choices.
        remote = bool(re.search(r"remote|keypad|controller|button|sensor_switch|ts004", device.model or "", re.I))
        if not endpoints and device.id not in inputs and device.id not in controls and not remote:
            continue
        matches = controls.get(device.id, [])
        gesture, gesture_eligible = control_event(hass, matches[0].unique_id) if len(matches) == 1 else (None, False)
        if not endpoints and gesture is not None and not gesture_eligible:
            continue
        area = ar.async_get(hass).async_get_area(device.area_id) if device.area_id else None
        name = (device.name_by_user or device.name or "HA Device")[:128]
        for endpoint, row, aliases in endpoints or [(None, None, ())]:
            label = name
            if len(endpoints) > 1:
                label += " — " + str(row["friendly_name"])[:120]
            record = {"type": "device_choice", "version": 1,
                      "choice_id": f"device:{device.id}:{endpoint.id if endpoint else 'none'}",
                      "device_id": device.id, "name": label,
                      "kind": row["kind"] if row else "remote",
                      "gestures": "confirmed" if gesture_eligible else "candidate"}
            if gesture_eligible:
                record["gesture_entity_id"] = gesture.entity_id
            if endpoint:
                record["source_load"] = load_record(hass, endpoint)
                if include_load_names and row["friendly_name"] != endpoint.entity_id:
                    record["source_load"]["name"] = row["friendly_name"][:128]
            if area:
                record["area"] = area.name[:128]
            yield record, aliases


def device_choices(hass, *, include_load_names=False):
    return sorted((choice for choice, _ in _device_choices(hass, include_load_names=include_load_names)),
                  key=lambda choice: choice["choice_id"])


def selected_endpoint(hass, device_id, registry_id):
    """Resolve only a currently eligible endpoint or its proven exact alias."""
    registry = er.async_get(hass)
    if (selected := registry.async_get(registry_id)) is not None:
        for choice, aliases in _device_choices(hass):
            if choice["device_id"] == device_id and selected.entity_id in aliases:
                return registry.async_get(choice["source_load"]["registry_id"])
    raise ValueError("invalid_endpoint")
