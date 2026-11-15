"""Catalog behavior against actual HA registries and entity lifecycle."""

from copy import deepcopy
import logging

from homeassistant.config_entries import SOURCE_USER
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent

from custom_components.ha_gesture_bridge.config import DOMAIN
from custom_components.ha_gesture_bridge.sensor import GestureCatalogSensor, build_catalog


def register(hass, domain, object_id, *, platform="fixture", **kwargs):
    return er.async_get(hass).async_get_or_create(domain, platform, object_id,
        suggested_object_id=object_id, **kwargs)


def bridge_attrs():
    return {"friendly_name": "Office Paddle", "bridge_contract_version": "1.0",
        "bridge_control_id": "office-control", "bridge_buttons": [{"id": "top", "label": "Top",
        "gestures": [{"id": "press_2", "kind": "press", "press_count": 2, "label": "Double Press"}]}],
        "bridge_sources": [{"id": "upper", "button_id": "top", "ready": True, "availability": "available"}],
        "bridge_load": {"entity_id": "light.office", "domain": "light", "state": "on", "available": True,
            "attributes": {"supported_color_modes": ["brightness"], "supported_features": 32,
                "private_token": "never project"}},
        "gesture_sequence": 1, "gesture_event_id": "private-occurrence", "raw_payload": "private-raw"}


async def add_catalog(hass):
    catalog = GestureCatalogSensor()
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    await component.async_add_entities([catalog])
    await hass.async_block_till_done()
    return component, catalog


async def test_empty_installation_has_stable_catalog_without_controls(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"import_yaml": False})
    await hass.async_block_till_done()
    entry = result["result"]
    sensor = hass.states.get("sensor.ha_gesture_bridge_catalog")
    assert sensor is not None and sensor.state == "0"
    assert sensor.attributes["catalog_contract_version"] == "1.0"
    registry = er.async_get(hass)
    catalog_entry = registry.async_get(sensor.entity_id)
    assert catalog_entry.unique_id == "ha_gesture_bridge_catalog"
    assert catalog_entry.config_entry_id == entry.entry_id
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(sensor.entity_id).attributes["catalog_revision"] == sensor.attributes["catalog_revision"]


async def test_catalog_only_admits_registered_eligible_capabilities(hass):
    for domain, name in (("switch", "load"), ("input_boolean", "virtual"), ("sensor", "temperature")):
        item = register(hass, domain, name)
        hass.states.async_set(item.entity_id, "off", {"friendly_name": "Same Name"})
    disabled = register(hass, "switch", "disabled", disabled_by=er.RegistryEntryDisabler.USER)
    hass.states.async_set(disabled.entity_id, "on")
    hass.states.async_set("switch.unregistered", "on")
    for name, modes in (("dimmer", ["brightness"]), ("color", ["hs"]), ("onoff", ["onoff"]),
                        ("unknown", None), ("malformed", ["brightness", "invented"])):
        item = register(hass, "light", name)
        hass.states.async_set(item.entity_id, "on", {"supported_color_modes": modes,
            "supported_features": 32, "credentials": "private"})
    offline = register(hass, "light", "offline", capabilities={"supported_color_modes": ["brightness"]})
    hass.states.async_set(offline.entity_id, "unavailable")
    rows = {row["entity_id"]: row for row in build_catalog(hass)}
    assert set(rows) == {"switch.load", "input_boolean.virtual", "light.dimmer", "light.color", "light.offline",
                        "light.onoff", "light.unknown", "light.malformed"}
    assert rows["switch.load"]["friendly_name"] == rows["input_boolean.virtual"]["friendly_name"]
    assert rows["light.dimmer"]["kind"] == "dimmer"
    for name in ("onoff", "unknown", "malformed"):
        assert rows[f"light.{name}"]["kind"] == "switch"
    assert rows["light.dimmer"]["attributes"] == {"supported_color_modes": ["brightness"], "supported_features": 32}
    assert rows["light.offline"]["availability"] == "unavailable"
    assert rows["light.offline"]["available"] is False
    assert rows["light.offline"]["ready"] is False


async def test_registry_area_and_names_update_revision_without_controlling_anything(hass):
    item = register(hass, "switch", "load", original_name="Original Name")
    hass.states.async_set(item.entity_id, "off")
    component, catalog = await add_catalog(hass)
    first = catalog.extra_state_attributes["catalog_revision"]
    area = ar.async_get(hass).async_create("Office")
    er.async_get(hass).async_update_entity(item.entity_id, area_id=area.id, name="Updated Name")
    await hass.async_block_till_done()
    row = catalog.extra_state_attributes["catalog_entities"][0]
    assert row["friendly_name"] == "Updated Name" and row["area_name"] == "Office"
    assert catalog.extra_state_attributes["catalog_revision"] != first
    assert hass.states.get(item.entity_id).state == "off"
    detached = catalog.extra_state_attributes
    detached["catalog_entities"][0]["friendly_name"] = "Changed copy"
    assert catalog.extra_state_attributes["catalog_entities"][0]["friendly_name"] == "Updated Name"
    er.async_get(hass).async_remove(item.entity_id)
    await hass.async_block_till_done()
    assert catalog.native_value == 0
    await component.async_remove_entity(catalog.entity_id)
    revision = catalog.extra_state_attributes["catalog_revision"]
    register(hass, "switch", "after_removal")
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_revision"] == revision


async def test_bridge_row_projects_capabilities_and_source_readiness_not_event_timestamp(hass):
    item = register(hass, "event", "paddle", platform=DOMAIN)
    attrs = bridge_attrs()
    hass.states.async_set(item.entity_id, "2026-11-14T20:00:00+00:00", attrs)
    _, catalog = await add_catalog(hass)
    first = catalog.extra_state_attributes
    row = first["catalog_entities"][0]
    assert row["state"] == "ready" and row["availability"] == "available" and row["ready"] is True
    assert row["bridge_control_id"] == "office-control"
    assert row["load_entity_id"] == "light.office" and row["load_domain"] == "light"
    assert row["attributes"]["bridge_load"]["attributes"] == {"supported_color_modes": ["brightness"], "supported_features": 32}
    assert "raw_payload" not in row["attributes"] and "gesture_event_id" not in row["attributes"]
    attrs.update(gesture_sequence=2, gesture_event_id="another-occurrence")
    hass.states.async_set(item.entity_id, "2026-11-14T20:00:01+00:00", attrs)
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_revision"] == first["catalog_revision"]
    # Aggregate availability can remain true while an upstream source is lost.
    attrs = deepcopy(attrs)
    attrs["bridge_sources"][0].update(ready=False, availability="unavailable")
    hass.states.async_set(item.entity_id, "2026-11-14T20:00:01+00:00", attrs)
    await hass.async_block_till_done()
    row = catalog.extra_state_attributes["catalog_entities"][0]
    assert row["state"] == row["availability"] == "unavailable" and row["ready"] is False
    # Native event entities are ready before their first physical event.
    attrs = deepcopy(attrs)
    attrs["bridge_sources"][0].update(ready=True, availability="available")
    hass.states.async_set(item.entity_id, "unknown", attrs)
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_entities"][0]["state"] == "ready"
    # Stale source metadata cannot override the aggregate itself going offline.
    for value, extra in (("unavailable", {}), ("unknown", {"restored": True})):
        hass.states.async_set(item.entity_id, value, attrs | extra)
        await hass.async_block_till_done()
        row = catalog.extra_state_attributes["catalog_entities"][0]
        assert row["state"] == row["availability"] == "unavailable" and row["ready"] is False


async def test_malformed_or_unrelated_aggregates_cannot_enter_catalog(hass):
    attrs = bridge_attrs()
    other = register(hass, "event", "unrelated")
    hass.states.async_set(other.entity_id, "unknown", attrs)
    for index, mutation in enumerate(({"bridge_contract_version": []}, {"bridge_control_id": []},
            {"bridge_buttons": [{"id": "top", "label": "Top", "gestures": [{"kind": "press", "press_count": True}]}]})):
        item = register(hass, "event", f"broken_{index}", platform=DOMAIN)
        hass.states.async_set(item.entity_id, "unknown", attrs | mutation)
    assert build_catalog(hass) == []
    good = register(hass, "event", "good", platform=DOMAIN)
    malformed_load = deepcopy(attrs)
    malformed_load["bridge_load"]["domain"] = []
    malformed_load["bridge_sources"] = [{"id": "upper", "ready": True, "availability": []}]
    hass.states.async_set(good.entity_id, "unknown", malformed_load)
    row = build_catalog(hass)[0]
    assert row["load_entity_id"] is None and row["attributes"]["bridge_sources"] == []
    assert row["ready"] is False and row["availability"] == "unknown"


async def test_device_area_association_entity_override_and_disabled_device(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"import_yaml": False})
    await hass.async_block_till_done()
    entry = result["result"]
    device_area = ar.async_get(hass).async_create("Device Area")
    entity_area = ar.async_get(hass).async_create("Entity Area")
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "physical-switch")}, name="Physical Switch")
    dr.async_get(hass).async_update_device(device.id, area_id=device_area.id)
    item = register(hass, "switch", "associated", device_id=device.id)
    hass.states.async_set(item.entity_id, "off")
    row = build_catalog(hass)[0]
    assert row["device_id"] == device.id and row["area_name"] == "Device Area"
    assert row["friendly_name"] == "Physical Switch"
    er.async_get(hass).async_update_entity(item.entity_id, area_id=entity_area.id, name="Named Load")
    assert build_catalog(hass)[0]["area_name"] == "Entity Area"
    dr.async_get(hass).async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    assert build_catalog(hass) == []


async def test_load_metadata_churn_does_not_change_revision_or_emit_gestures(hass):
    item = register(hass, "light", "brightness", supported_features=32,
        capabilities={"supported_color_modes": ["brightness"]})
    hass.states.async_set(item.entity_id, "on", {"brightness": 1})
    _, catalog = await add_catalog(hass)
    before = catalog.extra_state_attributes
    assert before["catalog_entities"][0]["attributes"]["supported_features"] == 32
    gestures = []
    remove = hass.bus.async_listen("ha_gesture_bridge_gesture", lambda event: gestures.append(event))
    hass.states.async_set(item.entity_id, "on", {"brightness": 230, "last_seen": "later", "private": "hidden"})
    hass.states.async_set("sensor.unrelated", "31")
    await hass.async_block_till_done()
    remove()
    assert catalog.extra_state_attributes["catalog_revision"] == before["catalog_revision"]
    assert gestures == []
