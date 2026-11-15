"""Cross-protocol identity isolation and shared entity button matching."""
from datetime import timedelta
import logging

from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util

from custom_components.ha_gesture_bridge.config import NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent


async def add(hass, source):
    entity = GestureBridgeEvent(NativeControlConfig.parse({"unique_id": "isolated", "name": "Isolated", "sources": [source]}))
    component = EntityComponent(logging.getLogger(__name__), "event", hass)
    await component.async_add_entities([entity])
    await hass.async_block_till_done()
    return entity


async def test_zwave_home_node_endpoint_filters_all_intersect(hass):
    entity = await add(hass, {"id": "paddle", "type": "zwave_js", "device_id": "selected-device",
        "filters": {"home_id": "selected-home", "node_id": 7, "endpoint": 0},
        "buttons": [{"id": "top", "label": "Top", "scene": 1}]})
    good = {"device_id": "selected-device", "home_id": "selected-home", "node_id": 7, "endpoint": 0,
            "command_class": 91, "property": "scene", "property_key": "001", "value": "KeyPressed2x"}
    for key, value in (("device_id", "other"), ("home_id", "other"), ("node_id", 8), ("endpoint", 1)):
        hass.bus.async_fire("zwave_js_value_notification", good | {key: value})
    await hass.async_block_till_done()
    assert entity._sequence == 0
    hass.bus.async_fire("zwave_js_value_notification", good)
    await hass.async_block_till_done()
    assert entity._sequence == 1


async def test_zha_dictionary_args_and_endpoint_cluster_are_explicit(hass):
    entity = await add(hass, {"id": "remote", "type": "zha", "device_id": "selected-device",
        "filters": {"endpoint_id": 1, "cluster_id": 6}, "buttons": [{"id": "top", "label": "Top"}],
        "mappings": [{"button_id": "top", "match": {"command": "button", "args.press_type": "double"},
                      "gesture": {"kind": "press", "press_count": 2}}]})
    good = {"device_id": "selected-device", "endpoint_id": 1, "cluster_id": 6,
            "command": "button", "args": {"press_type": "double"}}
    for change in ({"endpoint_id": 2}, {"cluster_id": 8}, {"args": ["double"]}):
        hass.bus.async_fire("zha_event", good | change)
    await hass.async_block_till_done()
    assert entity._sequence == 0
    hass.bus.async_fire("zha_event", good)
    await hass.async_block_till_done()
    assert entity._sequence == 1


async def test_same_native_event_type_uses_reported_button_attribute(hass):
    source_id = "event.shared_source"
    hass.states.async_set(source_id, "unknown")
    entity = await add(hass, {"id": "shared", "type": "shared_native", "source_entity_id": source_id,
        "buttons": [{"id": "top", "label": "Top"}, {"id": "bottom", "label": "Bottom"}],
        "mappings": [{"button_id": bid, "match": {"event_type": "double", "button": value},
                      "gesture": {"kind": "press", "press_count": 2}} for bid, value in (("top", "up"), ("bottom", "down"))]})
    for index, (button, expected) in enumerate((("up", "top"), ("down", "bottom"), ("up", "top"))):
        hass.states.async_set(source_id, (dt_util.utcnow() + timedelta(milliseconds=index)).isoformat(),
                              {"event_type": "double", "button": button})
        await hass.async_block_till_done()
        attrs = hass.states.get(entity.entity_id).attributes
        assert attrs["button_id"] == expected and attrs["press_count"] == 2
        assert attrs["gesture_sequence"] == index + 1
