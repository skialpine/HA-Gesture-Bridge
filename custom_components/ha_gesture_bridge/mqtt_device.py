"""Resolve HA-owned MQTT trigger metadata without losing retain provenance."""

from homeassistant.components.mqtt.models import DATA_MQTT
from homeassistant.helpers import device_registry as dr


def resolve_trigger(hass, reference):
    """An exact live device/type/subtype identity must resolve unambiguously."""
    device = dr.async_get(hass).async_get(reference["device_id"])
    if device is None or device.disabled_by is not None:
        return None
    data = hass.data.get(DATA_MQTT)
    triggers = getattr(data, "device_triggers", {})
    matches = [item for item in triggers.values() if item.device_id == reference["device_id"]
               and item.type == reference["type"] and item.subtype == reference["subtype"]]
    return matches[0] if len(matches) == 1 else None


def references_match(hass, references, topic):
    for reference in references:
        trigger = resolve_trigger(hass, reference)
        if (trigger is None or trigger.topic != topic or trigger.payload != reference["payload"]
                or trigger.value_template):
            return False
    return True
