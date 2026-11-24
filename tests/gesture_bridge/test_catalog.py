"""Catalog behavior against actual HA registries and entity lifecycle."""

from copy import deepcopy
import json
import logging
from unittest.mock import Mock

from homeassistant.components.recorder.db_schema import MAX_STATE_ATTRS_BYTES, StateAttributes
from homeassistant.const import EntityCategory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import EVENT_STATE_CHANGED
from homeassistant.util import dt as dt_util
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
import pytest

from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent
from custom_components.ha_gesture_bridge import sensor as sensor_module
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
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
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
    attrs = bridge_attrs() | {"bridge_source_device_id": "physical-source-device"}
    hass.states.async_set(item.entity_id, "2026-11-14T20:00:00+00:00", attrs)
    _, catalog = await add_catalog(hass)
    first = catalog.extra_state_attributes
    row = first["catalog_entities"][0]
    assert row["state"] == "ready" and row["availability"] == "available" and row["ready"] is True
    assert row["bridge_control_id"] == "office-control"
    assert row["attributes"]["bridge_source_device_id"] == "physical-source-device"
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
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
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


async def test_linked_feedback_keeps_catalog_names_and_aliases(hass, monkeypatch):
    load = register(hass, "light", "fixture_lamp")
    event = register(hass, "event", "fixture_paddle", platform=DOMAIN)
    attrs = bridge_attrs()
    attrs["bridge_load"]["entity_id"] = load.entity_id
    hass.states.async_set(load.entity_id, "on")
    hass.states.async_set(event.entity_id, "unknown", attrs)
    _, catalog = await add_catalog(hass)
    before = catalog.extra_state_attributes
    rebuild = Mock(wraps=build_catalog)
    monkeypatch.setattr(sensor_module, "build_catalog", rebuild)

    hass.states.async_set(load.entity_id, "off")
    updated = deepcopy(attrs)
    updated["bridge_load"]["state"] = "off"
    hass.states.async_set(event.entity_id, "unknown", updated)
    await hass.async_block_till_done()
    after = catalog.extra_state_attributes
    assert after["catalog_revision"] != before["catalog_revision"]
    assert after["catalog_names_revision"] == before["catalog_names_revision"]
    assert after["catalog_entities"][0]["entity_aliases"] == [load.entity_id]
    assert after["catalog_entities"][0]["attributes"]["bridge_load"]["state"] == "off"
    rebuild.assert_not_called()


async def test_load_metadata_churn_does_not_rebuild_catalog_or_emit_gestures(hass, monkeypatch):
    item = register(hass, "light", "brightness", supported_features=32,
        capabilities={"supported_color_modes": ["brightness"]})
    source = register(hass, "event", "native_paddle")
    hass.states.async_set(item.entity_id, "on", {"brightness": 1})
    hass.states.async_set(source.entity_id, "unknown", {"event_type": None})
    aggregate = GestureBridgeEvent(NativeControlConfig.parse({
        "unique_id": "catalog-control", "name": "Catalog Control",
        "source_entity_id": source.entity_id, "load_entity_id": item.entity_id,
        "actions": {"double": {"kind": "press", "press_count": 2}},
    }))
    events = EntityComponent(logging.getLogger(__name__), "event", hass)
    await events.async_add_entities([aggregate])
    register(hass, "event", aggregate.entity_id.split(".", 1)[1], platform=DOMAIN)
    await hass.async_block_till_done()
    _, catalog = await add_catalog(hass)
    before = catalog.extra_state_attributes
    load = next(row for row in before["catalog_entities"] if row["entity_id"] == item.entity_id)
    assert load["attributes"]["supported_features"] == 32
    rebuild = Mock(wraps=build_catalog)
    monkeypatch.setattr(sensor_module, "build_catalog", rebuild)

    # A genuine physical input establishes an occurrence to protect from replay.
    hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(), {"event_type": "double"})
    await hass.async_block_till_done()
    first = hass.states.get(aggregate.entity_id)
    assert first.attributes["gesture_sequence"] == 1
    assert first.attributes["bridge_update_kind"] == "gesture"
    hass.states.async_set(item.entity_id, "on", {"brightness": 230, "last_seen": "later", "private": "hidden"})
    hass.states.async_set("sensor.unrelated", "31")
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_revision"] == before["catalog_revision"]
    rebuild.assert_not_called()
    current = hass.states.get(aggregate.entity_id)
    assert current.attributes["bridge_update_kind"] == "snapshot"
    assert current.state == first.state
    assert current.attributes["gesture_sequence"] == first.attributes["gesture_sequence"]
    assert current.attributes["gesture_event_id"] == first.attributes["gesture_event_id"]

    # A real catalog refresh changes metadata without creating a new occurrence.
    er.async_get(hass).async_update_entity(item.entity_id, name="Updated Light")
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_revision"] != before["catalog_revision"]
    assert rebuild.called
    current = hass.states.get(aggregate.entity_id)
    assert current.state == first.state
    assert current.attributes["gesture_sequence"] == first.attributes["gesture_sequence"]
    assert current.attributes["gesture_event_id"] == first.attributes["gesture_event_id"]


async def test_irrelevant_entity_registry_edit_does_not_rebuild_catalog(hass, monkeypatch):
    unrelated = register(hass, "sensor", "unrelated_temperature")
    _, catalog = await add_catalog(hass)
    before = catalog.extra_state_attributes["catalog_revision"]
    rebuild = Mock(wraps=build_catalog)
    monkeypatch.setattr(sensor_module, "build_catalog", rebuild)
    er.async_get(hass).async_update_entity(unrelated.entity_id, name="Renamed Temperature")
    await hass.async_block_till_done()
    rebuild.assert_not_called()
    assert catalog.extra_state_attributes["catalog_revision"] == before


async def test_state_filter_preserves_names_capabilities_availability_and_removal(hass):
    item = register(hass, "light", "changing")
    hass.states.async_set(item.entity_id, "on", {"supported_color_modes": ["brightness"]})
    _, catalog = await add_catalog(hass)
    assert catalog.extra_state_attributes["catalog_entities"][0]["kind"] == "dimmer"
    hass.states.async_set(item.entity_id, "off", {
        "friendly_name": "Renamed Light", "supported_color_modes": ["onoff"], "supported_features": 32,
    })
    await hass.async_block_till_done()
    row = catalog.extra_state_attributes["catalog_entities"][0]
    assert row["friendly_name"] == "Renamed Light"
    assert row["kind"] == "switch" and row["state"] == "off"
    assert row["attributes"] == {"supported_color_modes": ["onoff"], "supported_features": 32}
    for value, attrs, availability in (("unknown", {}, "unknown"), ("unknown", {"restored": True}, "unavailable"),
                                       ("on", {}, "available")):
        hass.states.async_set(item.entity_id, value, attrs)
        await hass.async_block_till_done()
        assert catalog.extra_state_attributes["catalog_entities"][0]["availability"] == availability
    hass.states.async_remove(item.entity_id)
    await hass.async_block_till_done()
    row = catalog.extra_state_attributes["catalog_entities"][0]
    assert row["state"] == row["availability"] == "unknown" and row["available"] is None


async def test_catalog_rows_remain_live_but_are_excluded_by_actual_recorder_encoder(hass, caplog):
    for index in range(100):
        item = register(hass, "switch", f"load_{index}")
        hass.states.async_set(item.entity_id, "off", {"friendly_name": f"Room {index} Switch"})
    await hass.async_block_till_done()
    changes = []
    remove = hass.bus.async_listen(EVENT_STATE_CHANGED, changes.append)
    _, catalog = await add_catalog(hass)
    remove()
    state = hass.states.get(catalog.entity_id)
    assert state.state == "100"
    assert len(state.attributes["catalog_entities"]) == 100
    assert len(json.dumps(dict(state.attributes)).encode()) > MAX_STATE_ATTRS_BYTES
    change = next(event for event in changes if event.data["entity_id"] == catalog.entity_id)
    with caplog.at_level(logging.WARNING, logger="homeassistant.components.recorder.db_schema"):
        encoded = StateAttributes.shared_attrs_bytes_from_event(change, None)
    recorded = json.loads(encoded)
    assert 0 < len(encoded) < MAX_STATE_ATTRS_BYTES
    assert "catalog_entities" not in recorded
    assert not any(key.startswith("catalog_names_page_") for key in recorded)
    assert recorded["catalog_names_total"] == 100
    assert recorded["catalog_revision"] == state.attributes["catalog_revision"]
    assert recorded["catalog_contract_version"] == "1.0"
    assert "exceed maximum size" not in caplog.text


async def test_names_projection_is_paged_without_changing_full_catalog_or_authority(hass):
    for index in range(1400):
        item = register(hass, "switch", f"bounded_catalog_{index:04d}")
        hass.states.async_set(item.entity_id, "off", {"friendly_name": f"Fixture Room {index:04d} Switch"})
    _, catalog = await add_catalog(hass)
    attrs = catalog.extra_state_attributes
    assert len(attrs["catalog_entities"]) == attrs["catalog_names_total"] == 1400
    assert attrs["catalog_names_version"] == 1 and attrs["catalog_names_pages"] > 8
    projected = []
    for index in range(1, attrs["catalog_names_pages"] + 1):
        page = attrs[sensor_module.names_page_key(index)]
        assert len(json.dumps(page, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()) <= 16384
        assert all(set(row) == {"entity_id", "domain", "friendly_name", "area_name"} for row in page)
        projected.extend(page)
    assert [row["entity_id"] for row in projected] == [row["entity_id"] for row in attrs["catalog_entities"]]
    first_names, first_full = attrs["catalog_names_revision"], attrs["catalog_revision"]
    hass.states.async_set("switch.bounded_catalog_0000", "on", {"friendly_name": "Fixture Room 0000 Switch"})
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_names_revision"] == first_names
    assert catalog.extra_state_attributes["catalog_revision"] != first_full
    hass.states.async_set("switch.bounded_catalog_0000", "on", {"friendly_name": "Renamed Fixture"})
    await hass.async_block_till_done()
    assert catalog.extra_state_attributes["catalog_names_revision"] != first_names


def test_unrepresentable_names_projection_reports_error_instead_of_truncation():
    row = {"entity_id": "switch.fixture", "domain": "switch", "friendly_name": "x" * 16384, "area_name": None}
    projection = sensor_module.names_attributes([row])
    assert projection["catalog_names_error"] == "record_too_large"
    assert projection["catalog_names_total"] == 1 and projection["catalog_names_pages"] == 0
    assert not any(key.startswith("catalog_names_page_") for key in projection)
    row["friendly_name"] = "Fixture"
    projection = sensor_module.names_attributes([row] * 10001)
    assert projection["catalog_names_error"] == "catalog_limit"


async def installation(hass):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    return result["result"]


def physical_device(hass, entry, name):
    return dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture_switch", name)}, name=name)


def control(hass, name, *, source_device_id=None, load=None):
    item = register(hass, "event", name, platform=DOMAIN)
    attrs = bridge_attrs() | {"bridge_control_id": name, "friendly_name": name,
        "bridge_load": load, "bridge_source_device_id": source_device_id}
    hass.states.async_set(item.entity_id, "unknown", attrs)
    return item


async def test_catalog_prefers_bath_and_toilet_gestures_using_real_registry_relations(hass):
    entry = await installation(hass)
    bath = physical_device(hass, entry, "Primary Bath")
    toilet = physical_device(hass, entry, "Primary Toilet")
    area = ar.async_get(hass).async_create("Primary Bath")
    dr.async_get(hass).async_update_device(bath.id, area_id=area.id)
    relay = register(hass, "switch", "primary_bath", platform="zwave_js", device_id=bath.id)
    toilet_relay = register(hass, "switch", "primary_toilet", platform="zwave_js", device_id=toilet.id)
    wrapper = register(hass, "light", "primary_bath_as_light", platform="switch_as_x", device_id=bath.id)
    er.async_get(hass).async_update_entity_options(wrapper.entity_id, "switch_as_x",
        {"entity_id": relay.id, "invert": False})
    configuration = register(hass, "switch", "bath_setting", platform="zwave_js", device_id=bath.id,
        entity_category=EntityCategory.CONFIG)
    plain = register(hass, "switch", "plain", original_name="Plain Switch")
    group = register(hass, "light", "c4_target", device_id=bath.id)
    for item in (relay, toilet_relay, wrapper, configuration, plain):
        hass.states.async_set(item.entity_id, "off", {"friendly_name": "Primary Bath"} if item == wrapper else {})
    hass.states.async_set(group.entity_id, "off", {"friendly_name": "C4 Bath Target", "entity_id": ["light.child"]})
    target = {"entity_id": group.entity_id, "domain": "light", "state": "off", "available": True,
        "attributes": {"supported_color_modes": ["brightness"]}}
    bath_control = control(hass, "bath_gestures", source_device_id=bath.id, load=target)
    toilet_control = control(hass, "toilet_gestures", source_device_id=toilet.id)
    await hass.async_block_till_done()

    rows = {row["entity_id"]: row for row in build_catalog(hass)}
    assert set(rows) == {bath_control.entity_id, toilet_control.entity_id, group.entity_id,
        plain.entity_id, configuration.entity_id}
    preferred = rows[bath_control.entity_id]
    assert preferred["entity_aliases"] == sorted([relay.entity_id, wrapper.entity_id])
    assert preferred["source_entity_id"] == wrapper.entity_id
    assert preferred["friendly_name"] == "Primary Bath (gestures)"
    assert preferred["area_name"] == "Primary Bath"
    assert preferred["attributes"]["bridge_load"]["entity_id"] == group.entity_id
    assert rows[group.entity_id]["is_group"] is True
    assert rows[toilet_control.entity_id]["entity_aliases"] == [toilet_relay.entity_id]
    assert rows[toilet_control.entity_id]["attributes"]["bridge_load"] is None
    assert "entity_aliases" not in rows[plain.entity_id]
    attrs = hass.states.get("sensor.ha_gesture_bridge_catalog").attributes
    projected = [row for index in range(1, attrs["catalog_names_pages"] + 1)
        for row in attrs[sensor_module.names_page_key(index)]]
    choice = next(row for row in projected if row["entity_id"] == bath_control.entity_id)
    assert choice["entity_aliases"] == preferred["entity_aliases"]
    assert choice["source_entity_id"] == wrapper.entity_id
    assert "attributes" not in choice and "device_id" not in choice
    assert hass.states.get(relay.entity_id).state == hass.states.get(group.entity_id).state == "off"


@pytest.mark.parametrize("reference,inverted", [("entity", False), ("registry", False), ("entity", True)])
async def test_catalog_prefers_verified_light_wrapper_without_gesture_control(hass, reference, inverted):
    relay = register(hass, "switch", "closet", platform="zwave_js")
    wrapper = register(hass, "light", "closet_as_light", platform="switch_as_x")
    er.async_get(hass).async_update_entity_options(wrapper.entity_id, "switch_as_x",
        {"entity_id": relay.entity_id if reference == "entity" else relay.id, "invert": inverted})
    hass.states.async_set(relay.entity_id, "off")
    hass.states.async_set(wrapper.entity_id, "off", {"supported_color_modes": ["onoff"]})
    rows = build_catalog(hass)
    if inverted:
        assert {row["entity_id"] for row in rows} == {relay.entity_id, wrapper.entity_id}
        assert all("entity_aliases" not in row for row in rows)
    else:
        assert len(rows) == 1 and rows[0]["entity_id"] == wrapper.entity_id
        assert rows[0]["entity_aliases"] == [relay.entity_id]


async def test_ambiguous_endpoints_controls_and_names_remain_separate(hass):
    entry = await installation(hass)
    device = physical_device(hass, entry, "Same Name")
    first = register(hass, "switch", "endpoint_1", device_id=device.id)
    second = register(hass, "switch", "endpoint_2", device_id=device.id)
    for item in (first, second):
        hass.states.async_set(item.entity_id, "off", {"friendly_name": "Same Name"})
    bridge = control(hass, "remote", source_device_id=device.id)
    rows = build_catalog(hass)
    assert {row["entity_id"] for row in rows} == {first.entity_id, second.entity_id, bridge.entity_id}
    assert all("entity_aliases" not in row for row in rows)
    er.async_get(hass).async_update_entity(second.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    another = control(hass, "another_remote", source_device_id=device.id)
    rows = build_catalog(hass)
    assert {row["entity_id"] for row in rows} == {first.entity_id, bridge.entity_id, another.entity_id}
    assert all("entity_aliases" not in row for row in rows)
    assert all(row.get("source_entity_id") == first.entity_id for row in rows if row["domain"] == "event")


async def test_ambiguous_physical_source_never_replaces_its_separate_linked_target(hass):
    entry = await installation(hass)
    device = physical_device(hass, entry, "Dual Relay")
    first = register(hass, "switch", "dual_relay_1", device_id=device.id)
    second = register(hass, "switch", "dual_relay_2", device_id=device.id)
    target = register(hass, "light", "separate_target", original_name="Separate Target")
    for item in (first, second):
        hass.states.async_set(item.entity_id, "off")
    hass.states.async_set(target.entity_id, "off", {"supported_color_modes": ["brightness"]})
    load = {"entity_id": target.entity_id, "domain": "light", "state": "off", "available": True,
            "attributes": {"supported_color_modes": ["brightness"]}}
    bridge = control(hass, "dual_relay_gestures", source_device_id=device.id, load=load)

    rows = {row["entity_id"]: row for row in build_catalog(hass)}
    assert set(rows) == {first.entity_id, second.entity_id, target.entity_id, bridge.entity_id}
    assert all("entity_aliases" not in row and "source_entity_id" not in row for row in rows.values())
    assert rows[bridge.entity_id]["friendly_name"] == "dual_relay_gestures"
    assert rows[bridge.entity_id]["attributes"]["bridge_load"] == load
    assert rows[target.entity_id]["kind"] == "dimmer"


async def test_changing_physical_source_rebuilds_preference_and_names_revision(hass):
    entry = await installation(hass)
    devices = [physical_device(hass, entry, name) for name in ("Bath", "Toilet")]
    relays = [register(hass, "switch", name, device_id=device.id)
        for name, device in zip(("bath", "toilet"), devices, strict=True)]
    for relay in relays:
        hass.states.async_set(relay.entity_id, "off")
    bridge = control(hass, "gesture_control", source_device_id=devices[0].id)
    await hass.async_block_till_done()
    before = hass.states.get("sensor.ha_gesture_bridge_catalog").attributes
    old = next(row for row in before["catalog_entities"] if row["entity_id"] == bridge.entity_id)
    assert old["entity_aliases"] == [relays[0].entity_id]
    current = hass.states.get(bridge.entity_id)
    hass.states.async_set(bridge.entity_id, current.state,
        dict(current.attributes) | {"bridge_source_device_id": devices[1].id})
    await hass.async_block_till_done()
    after = hass.states.get("sensor.ha_gesture_bridge_catalog").attributes
    changed = next(row for row in after["catalog_entities"] if row["entity_id"] == bridge.entity_id)
    assert changed["entity_aliases"] == [relays[1].entity_id]
    assert before["catalog_names_revision"] != after["catalog_names_revision"]
    assert any(row["entity_id"] == relays[0].entity_id for row in after["catalog_entities"])
