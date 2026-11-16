"""One versioned, read-only picker list for all Control4 consumers."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.service import async_register_admin_service

from .config import DOMAIN
from .device_catalog import device_choices, has_identifier
from .sensor import _catalog_row, paged_attributes


def picker_rows(hass):
    """Descriptors only: no live levels, gestures, provider queries or writes."""
    rows = []
    for choice in device_choices(hass):
        rows.append({"entity_id": choice["choice_id"],
                     "domain": choice.get("source_load", {}).get("domain", "event"),
                     "name": choice["name"] + (" — " + choice["area"] if choice.get("area") else ""),
                     "state": "unknown", "availability": "unknown",
                     "attributes": {"device_choice": choice}})
    # Physical choices cover registered endpoints. Keep independent native HA
    # groups as useful direct targets; never reimport groups exported by C4.
    devices = dr.async_get(hass)
    for entry in er.async_get(hass).entities.values():
        if (entry.domain not in {"light", "switch"} or entry.disabled_by or entry.hidden_by
                or entry.entity_category is not None or entry.platform == "control4"):
            continue
        device = devices.async_get(entry.device_id) if entry.device_id else None
        owner = hass.config_entries.async_get_entry(entry.config_entry_id) if entry.config_entry_id else None
        if has_identifier(device, "control4") or (owner and owner.domain == "control4"):
            continue
        row = _catalog_row(hass, entry, hass.states.get(entry.entity_id))
        if row is None or not row.get("is_group"):
            continue
        rows.append({"entity_id": row["entity_id"], "domain": row["domain"],
                     "name": row["friendly_name"] + (" — " + row["area_name"] if row.get("area_name") else ""),
                     "state": "unknown", "availability": "unknown", "is_group": True,
                     "attributes": {key: row["attributes"][key]
                                    for key in ("supported_color_modes", "supported_features")
                                    if key in row["attributes"]}})
    return sorted(rows, key=lambda row: row["entity_id"])


def async_setup_picker(hass):
    async def get_devices(call):
        rows = picker_rows(hass)
        if len(rows) > 5000:
            raise ServiceValidationError("catalog_limit")
        pages = paged_attributes(rows, "picker")
        if error := pages.get("picker_error"):
            raise ServiceValidationError(error)
        revision = pages["picker_revision"]
        page = call.data.get("page", 1)
        known = call.data.get("revision")
        if page > 1 and known != revision:
            raise ServiceValidationError("snapshot_changed")
        unchanged = page == 1 and known == revision
        total_pages = pages["picker_pages"]
        if page > max(total_pages, 1):
            raise ServiceValidationError("invalid_page")
        result = {"version": 1, "revision": revision, "total": len(rows),
                  "pages": total_pages, "page": page, "unchanged": unchanged}
        if not unchanged:
            result["entities"] = pages.get(f"picker_page_{page:03d}", [])
        return result

    async_register_admin_service(hass, DOMAIN, "get_devices", get_devices,
        schema=vol.Schema({vol.Optional("revision"): vol.Match(r"^[a-f0-9]{64}$"),
                           vol.Optional("page", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=64))}),
        supports_response=SupportsResponse.ONLY)
