"""One versioned, read-only picker list for all Control4 consumers."""
from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import EVENT_STATE_CHANGED, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.service import async_register_admin_service

from .config import DOMAIN
from .device_catalog import device_choices, eligible_entry
from .sensor import _catalog_row, paged_attributes

_DOMAINS = frozenset({"light", "switch", "input_boolean", "event"})
_LOAD_FIELDS = ("friendly_name", "supported_color_modes", "supported_features", "entity_id", "group_entities")
_PICKER_PAGES = 256


def _state_facts(state):
    if state is None:
        return None
    attributes = state.attributes
    if state.domain == "event":
        # The Coordinator uses this pair only to infer an associated gesture.
        return attributes.get("bridge_source_device_id"), attributes.get("bridge_load") is None
    return (state.state in ("on", "off"), *(attributes.get(key) for key in _LOAD_FIELDS))


def picker_rows(hass):
    """Descriptors only: no live levels, gestures, provider queries or writes."""
    rows = []
    for choice in device_choices(hass, include_load_names=True):
        rows.append({"entity_id": choice["choice_id"],
                     "domain": choice.get("source_load", {}).get("domain", "event"),
                     "name": choice["name"] + (" — " + choice["area"] if choice.get("area") else ""),
                     "state": "unknown", "availability": "unknown",
                     "attributes": {"device_choice": choice}})
    # Physical choices cover registered endpoints. Keep independent native HA
    # groups as useful direct targets; never reimport groups exported by C4.
    devices = dr.async_get(hass)
    for entry in er.async_get(hass).entities.values():
        if entry.domain not in {"light", "switch"} or entry.platform == "switch_as_x":
            continue
        device = devices.async_get(entry.device_id) if entry.device_id else None
        if not eligible_entry(hass, entry, device):
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
    session, generation, pages = uuid4().hex, 0, None

    @callback
    def changed(*_):
        nonlocal generation, pages
        generation += 1
        pages = None

    @callback
    def registry_changed(event):
        if any(event.data.get(key, "").partition(".")[0] in _DOMAINS for key in ("entity_id", "old_entity_id")):
            changed()

    @callback
    def state_relevant(data):
        return data["entity_id"].partition(".")[0] in _DOMAINS

    @callback
    def state_changed(event):
        if _state_facts(event.data.get("old_state")) != _state_facts(event.data.get("new_state")):
            changed()

    hass.bus.async_listen(EVENT_STATE_CHANGED, state_changed, event_filter=state_relevant)
    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed)
    for event_type in (dr.EVENT_DEVICE_REGISTRY_UPDATED, ar.EVENT_AREA_REGISTRY_UPDATED):
        hass.bus.async_listen(event_type, changed)
    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed)

    async def get_devices(call):
        nonlocal pages
        if pages is None:
            rows = picker_rows(hass)
            if len(rows) > 5000:
                raise ServiceValidationError("catalog_limit")
            pages = paged_attributes(rows, "picker", max_bytes=4 * 1024 * 1024, max_pages=_PICKER_PAGES)
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
        # Opaque boot-scoped proof also covers the Coordinator's native inputs.
        native_revision = sha256(f"{session}:{generation}".encode()).hexdigest()
        result = {"version": 1, "eligibility_version": 1, "revision": revision, "total": pages["picker_total"],
                  "native_revision": native_revision,
                  "pages": total_pages, "page": page, "unchanged": unchanged}
        if not unchanged:
            result["entities"] = pages.get(f"picker_page_{page:03d}", [])
        return result

    async_register_admin_service(hass, DOMAIN, "get_devices", get_devices,
        schema=vol.Schema({vol.Optional("revision"): vol.Match(r"^[a-f0-9]{64}$"),
                           vol.Optional("page", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=_PICKER_PAGES))}),
        supports_response=SupportsResponse.ONLY)
