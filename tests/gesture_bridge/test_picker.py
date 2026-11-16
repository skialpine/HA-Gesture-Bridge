"""Real HA read-only revision/paging service; no synthetic transport shortcut."""
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from homeassistant.core import Context
from homeassistant.const import EntityCategory
from homeassistant.exceptions import Unauthorized, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.ha_gesture_bridge.config import DOMAIN
from test_config_flow import install, device_fixture
from test_preparation import relay


async def read(hass, **data):
    return await hass.services.async_call(DOMAIN, "get_devices", data, blocking=True, return_response=True)


async def test_one_list_revision_is_stable_across_load_changes(hass, monkeypatch):
    entry = await install(hass)
    device, _ = device_fixture(hass, entry)
    load = relay(hass, device)
    forbidden = AsyncMock(side_effect=AssertionError("Browsing enumerated a provider"))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.device_discovery.async_get_device_automations", forbidden)
    first = await read(hass)
    assert first["total"] == len(first["entities"]) == 1
    choice = first["entities"][0]["attributes"]["device_choice"]
    assert choice["device_id"] == device.id
    hass.states.async_set(load.entity_id, "on")
    assert await read(hass, revision=first["revision"]) == {
        "version": 1, "revision": first["revision"], "unchanged": True, "page": 1, "pages": 1, "total": 1}
    er.async_get(hass).async_update_entity(load.entity_id, entity_category=EntityCategory.CONFIG)
    changed = await read(hass, revision=first["revision"])
    assert not changed["unchanged"] and changed["revision"] != first["revision"]
    assert not entry.subentries and forbidden.await_count == 0


async def test_native_groups_keep_kind_but_not_live_levels_or_c4_mirrors(hass):
    await install(hass)
    registry = er.async_get(hass)
    group = registry.async_get_or_create("light", "group", "native", original_name="Native Group")
    exported = registry.async_get_or_create("light", "control4", "export", original_name="C4 Group")
    for item in (group, exported):
        hass.states.async_set(item.entity_id, "on", {"entity_id": ["light.member"],
            "supported_color_modes": ["brightness"], "supported_features": 32, "brightness": 200})
    first = await read(hass)
    assert [row["entity_id"] for row in first["entities"]] == [group.entity_id]
    row = first["entities"][0]
    assert row["is_group"] and row["state"] == "unknown" and "brightness" not in row["attributes"]
    hass.states.async_set(group.entity_id, "off", {"entity_id": ["light.member"],
        "supported_color_modes": ["brightness"], "supported_features": 32, "brightness": None})
    assert (await read(hass, revision=first["revision"]))["unchanged"]


async def test_paging_is_bounded_and_rejects_a_changed_revision(hass):
    await install(hass)
    registry = er.async_get(hass)
    for index in range(200):
        item = registry.async_get_or_create("light", "group", str(index), original_name="Native Group " + str(index))
        hass.states.async_set(item.entity_id, "off", {"entity_id": ["light.member"], "supported_color_modes": ["onoff"]})
    first = await read(hass)
    assert first["pages"] > 1 and len(first["entities"]) < 200
    rows = list(first["entities"])
    for page in range(2, first["pages"] + 1):
        rows.extend((await read(hass, revision=first["revision"], page=page))["entities"])
    assert len(rows) == len({row["entity_id"] for row in rows}) == 200
    registry.async_update_entity(item.entity_id, name="Changed")
    with pytest.raises(ServiceValidationError, match="snapshot_changed"):
        await read(hass, revision=first["revision"], page=2)


async def test_admin_gate_precedes_list_build(hass, monkeypatch):
    await install(hass)
    hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
    monkeypatch.setattr("custom_components.ha_gesture_bridge.picker.picker_rows",
        lambda hass: (_ for _ in ()).throw(AssertionError("Read list before authorization")))
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "get_devices", {}, blocking=True, return_response=True,
            context=Context(user_id="ordinary-user"))
