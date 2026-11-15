"""Diagnostics expose anonymous counts using an explicit output allowlist."""

from copy import deepcopy
import json
from types import SimpleNamespace

from homeassistant.helpers import entity_registry as er

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge.diagnostics import async_get_config_entry_diagnostics

PRIVATE = "HOUSEHOLD_PRIVATE_VALUE"


def control():
    return {"unique_id": PRIVATE, "name": PRIVATE, "load_entity_id": f"light.{PRIVATE.lower()}",
        "sources": [{"id": "source_private", "type": "mqtt", "topic": PRIVATE,
            "availability_topic": f"{PRIVATE}/availability", "payload_available": f"{PRIVATE}-online",
            "payload_not_available": f"{PRIVATE}-offline", "buttons": [{"id": "top", "label": PRIVATE}],
            "mappings": [{"button_id": "top", "match": {"action": PRIVATE},
                "gesture": {"kind": "press", "press_count": 2, "label": PRIVATE}}]}]}


def entry_with(*controls):
    return SimpleNamespace(subentries={str(i): SimpleNamespace(subentry_type="control", data=data)
        for i, data in enumerate(controls)})


def aggregate(hass, identity, attrs, object_id="private_aggregate"):
    registry_entry = er.async_get(hass).async_get_or_create("event", DOMAIN, identity,
        suggested_object_id=object_id)
    hass.states.async_set(registry_entry.entity_id, "unknown", {"bridge_control_id": identity,
        "friendly_name": PRIVATE, "device_id": PRIVATE, "credentials": PRIVATE,
        "bridge_metadata": {"private": PRIVATE}, **attrs})
    return registry_entry.entity_id


async def test_diagnostics_counts_sources_and_never_exports_household_values(hass):
    config = control()
    entity_id = aggregate(hass, PRIVATE, {"bridge_sources": [
        {"id": PRIVATE, "ready": True, "availability": "available", "error": PRIVATE, "topic": PRIVATE},
        {"id": "second", "ready": False, "availability": "unavailable"},
        {"id": "third", "ready": False, "availability": PRIVATE}],
        "bridge_unmapped_count": 3, "bridge_error_count": 2, "raw_payload": PRIVATE})
    snapshot = hass.states.get(entity_id)
    result = await async_get_config_entry_diagnostics(hass, entry_with(config))
    assert result["configuration"] == {"controls": 1, "sources": 1, "source_types": {"mqtt": 1},
        "linked_load_domains": {"light": 1}, "invalid_controls": 0}
    assert result["runtime"] == {"aggregates": 1, "ready_sources": 1,
        "source_availability": {"available": 1, "unavailable": 1, "unknown": 1},
        "source_errors": 1, "unmapped_actions": 3, "errors": 2}
    serialized = json.dumps(result)
    for secret in (PRIVATE, PRIVATE.lower(), entity_id, "source_private", "private_aggregate"):
        assert secret not in serialized
    assert hass.states.get(entity_id) is snapshot
    assert config == control()


async def test_malformed_configuration_statuses_and_counters_stay_safe(hass):
    malformed = {"unique_id": PRIVATE, "name": PRIVATE, "load_entity_id": f"{PRIVATE}.anything",
        "sources": [{"type": PRIVATE, "credentials": PRIVATE}, None]}
    aggregate(hass, PRIVATE, {"bridge_sources": [None, {"ready": "true", "availability": []}],
        "bridge_error_count": PRIVATE, "bridge_unmapped_count": True})
    aggregate(hass, "unrelated-private", {"bridge_control_id": [], "bridge_sources": []}, "unrelated")
    hass.data[DOMAIN] = {"yaml_controls": []}
    result = await async_get_config_entry_diagnostics(hass, entry_with(malformed))
    assert result["configuration"] == {"controls": 1, "sources": 2, "source_types": {"other": 2},
        "linked_load_domains": {"other": 1}, "invalid_controls": 1}
    assert result["runtime"]["aggregates"] == 1
    assert result["runtime"]["ready_sources"] == result["runtime"]["errors"] == result["runtime"]["unmapped_actions"] == 0
    assert result["runtime"]["source_availability"]["unknown"] == 1
    assert PRIVATE not in json.dumps(result)


async def test_yaml_import_is_counted_once_and_empty_installation_is_valid(hass):
    data = {"unique_id": PRIVATE, "name": PRIVATE, "source_entity_id": "event.private_source",
        "actions": {PRIVATE: {"kind": "press", "press_count": 1}}}
    hass.data[DOMAIN] = {"yaml_controls": {PRIVATE: {"config": deepcopy(data), "entity": PRIVATE}}}
    result = await async_get_config_entry_diagnostics(hass, entry_with(data))
    assert result["configuration"]["controls"] == result["configuration"]["sources"] == 1
    assert result["configuration"]["source_types"] == {"native_event": 1}
    hass.data.pop(DOMAIN)
    result = await async_get_config_entry_diagnostics(hass, entry_with())
    assert result["configuration"]["controls"] == result["runtime"]["aggregates"] == 0
