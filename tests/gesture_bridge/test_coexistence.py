"""Gesture setup, input and unload leave the DeviceSide service domain alone."""
from homeassistant.config_entries import SOURCE_USER
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.ha_gesture_bridge.config import DOMAIN


async def test_deviceside_services_remain_owned_and_are_never_called_automatically(hass):
    calls = []
    async def deviceside_service(call):
        calls.append(call.service)
    for name in ("raise_brightness", "lower_brightness", "stop"):
        hass.services.async_register("deviceside", name, deviceside_service)
    original = dict(hass.services.async_services()["deviceside"])
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"import_yaml": False})
    await hass.async_block_till_done()
    entry = result["result"]
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={("fixture", "coexistence")}, name="Coexisting Paddle")
    source = er.async_get(hass).async_get_or_create("event", "fixture", "coexistence", device_id=device.id,
        suggested_object_id="paddle", original_name="Upper Paddle")
    hass.states.async_set(source.entity_id, "unknown", {"event_types": ["KeyHeldDown", "KeyReleased"]})
    flow = await hass.config_entries.subentries.async_init((entry.entry_id, "control"), context={"source": SOURCE_USER})
    flow = await hass.config_entries.subentries.async_configure(flow["flow_id"], {"device_id": device.id})
    await hass.config_entries.subentries.async_configure(flow["flow_id"], flow["data_schema"]({}))
    await hass.async_block_till_done()
    hass.states.async_set(source.entity_id, dt_util.utcnow().isoformat(), {"event_type": "KeyHeldDown"})
    await hass.async_block_till_done()
    aggregate = next(state for state in hass.states.async_all("event") if state.attributes.get("bridge_control_id"))
    assert aggregate.attributes["event_type"] == "hold_start"
    assert calls == []
    assert hass.services.async_services()["deviceside"] == original
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.services.async_services()["deviceside"] == original
    await hass.services.async_call("deviceside", "stop", {}, blocking=True)
    assert calls == ["stop"]
