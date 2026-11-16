"""Aggregate HA-owned gesture sources and independent linked-load snapshots."""

from __future__ import annotations

import logging
import math
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.components.event import EventEntity, PLATFORM_SCHEMA as EVENT_PLATFORM_SCHEMA
from homeassistant.core import Event, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .config import CONTRACT_VERSION, DOMAIN, NativeControlConfig
from .sources import SourceAdapter
from .latency import async_setup_latency

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = EVENT_PLATFORM_SCHEMA.extend({
    vol.Required("unique_id"): cv.string,
    vol.Required("name"): cv.string,
    vol.Optional("source_entity_id"): cv.entity_id,
    vol.Optional("source_registry_id"): cv.string,
    vol.Optional("button_id"): cv.string,
    vol.Optional("button_label"): cv.string,
    vol.Optional("actions"): dict,
    vol.Optional("buttons"): list,
    vol.Optional("sources"): list,
    vol.Optional("load_entity_id"): cv.entity_id,
})


async def async_setup_platform(hass, config: dict[str, Any], async_add_entities: AddEntitiesCallback,
                               discovery_info: dict[str, Any] | None = None) -> None:
    """Keep the legacy shorthand and explicit multi-button development setup."""
    try:
        control = NativeControlConfig.parse(config)
    except ValueError as error:
        raise vol.Invalid(str(error)) from error
    entity = GestureBridgeEvent(control)
    hass.data.setdefault(DOMAIN, {}).setdefault("yaml_controls", {})[control.control_id] = {"config": dict(config), "entity": entity}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if control.control_id in entry.data.get("imported_yaml", []):
            return
        if any(sub.unique_id == control.control_id or sub.data.get("unique_id") == control.control_id
               for sub in entry.get_subentries_of_type("control")):
            return
    async_add_entities([entity])


async def async_setup_entry(hass, entry, async_add_entities):
    """Create independently managed UI controls with stable subentry ownership."""
    for subentry in entry.get_subentries_of_type("control"):
        config = NativeControlConfig.parse(dict(subentry.data))
        async_add_entities([GestureBridgeEvent(config)], config_subentry_id=subentry.subentry_id)


class GestureBridgeEvent(EventEntity):
    """Keep independent source history and shared monotonic occurrence identity."""

    _attr_should_poll = False

    def __init__(self, config: NativeControlConfig) -> None:
        self.config = config
        self._attr_unique_id = config.control_id
        self._attr_name = config.name
        self._adapters = []
        self._held_buttons = set()
        self._unmapped_count = 0
        self._error_count = 0
        self._attr_event_types = list(dict.fromkeys(gesture["kind"] for button in config.buttons()
                                                   for gesture in button["gestures"]))
        self._session_id = uuid4().hex
        self._sequence = 0
        self._update_kind = "snapshot"

    @property
    def _source_ready(self) -> bool:
        """Compatibility for single-source integrations and existing checks."""
        return bool(self._adapters and self._adapters[0].ready)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Stay HA-available while loaded so readiness/load metadata remains visible.
        sources = []
        for adapter in self._adapters:
            for button in adapter.config.buttons:
                if self.config.source_configs:
                    source_id = adapter.config.id if len(adapter.config.buttons) == 1 else f"{adapter.config.id}-{button.id}"
                else:
                    source_id = f"native-{button.id}" if len(self.config.inputs) > 1 else "native-source"
                item = {"id": source_id, "ready": adapter.ready, "availability": adapter.availability}
                if self.config.source_configs or len(self.config.inputs) > 1:
                    item["button_id"] = button.id
                if adapter.error:
                    item["error"] = ("A bridge aggregate cannot be used as a native input source"
                                     if not self.config.source_configs and adapter.error == "aggregate_source_forbidden"
                                     else adapter.error)
                sources.append(item)
        version = "1.1" if self.config.load_entity_id and self.config.load_entity_id.startswith("light.") else CONTRACT_VERSION
        return {"bridge_contract_version": version, "bridge_control_id": self.config.control_id,
                "bridge_session_id": self._session_id, "bridge_update_kind": self._update_kind,
                "bridge_buttons": self.config.buttons(), "bridge_sources": sources,
                "bridge_load": self._load_snapshot(), "bridge_unmapped_count": self._unmapped_count,
                "bridge_error_count": self._error_count, "bridge_acknowledgment_version": 2}

    def _load_snapshot(self) -> dict[str, Any] | None:
        if (target := self.config.load_entity_id) is None:
            return None
        state = self.hass.states.get(target)
        value = state.state if state is not None and state.state in ("on", "off", "unknown", "unavailable") else "unavailable"
        domain = target.split(".", 1)[0]
        result = {"entity_id": target, "domain": domain, "state": value,
                  "available": state is not None and state.state in ("on", "off")}
        if domain == "light":
            projected = {}
            if state is not None:
                attrs = state.attributes
                modes = attrs.get("supported_color_modes")
                known = {"onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
                if (isinstance(modes, (list, tuple)) and len(modes) <= 16
                        and all(isinstance(mode, str) and mode in known for mode in modes)
                        and len(set(modes)) == len(modes)):
                    projected["supported_color_modes"] = list(modes)
                features = attrs.get("supported_features")
                if type(features) is int and 0 <= features <= 4294967295:
                    projected["supported_features"] = features
                brightness = attrs.get("brightness")
                if (type(brightness) in (int, float) and 0 <= brightness <= 255
                        and math.isfinite(brightness)):
                    projected["brightness"] = brightness
            result["attributes"] = projected
        return result

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # HA re-adds the same Entity object during an entity-ID rename. Its
        # old adapters have been closed by removal callbacks; discard them so
        # readiness metadata and ownership checks contain only live adapters.
        self._adapters.clear()
        tracker = async_setup_latency(self.hass)
        tracker.register(self)
        self.async_on_remove(lambda: tracker.unregister(self))
        for source_config in self.config.runtime_sources():
            if source_config.source_entity_id == self.entity_id:
                raise ValueError("A control cannot consume its own aggregate entity")
            adapter = SourceAdapter(self.hass, source_config, self._adapter_gesture, self._adapter_status,
                                    self._adapter_idle, self._adapter_unmapped, self._native_target_available)
            self._adapters.append(adapter)
            self.async_on_remove(adapter.close)
            await adapter.async_start()
            if adapter.error:
                self._error_count += 1
        if self.config.load_entity_id:
            self.async_on_remove(async_track_state_change_event(self.hass, [self.config.load_entity_id], self._load_changed))
        self.async_on_remove(self._clear_holds)

    @callback
    def _native_target_available(self, adapter, entity_id):
        """Keep physical input ownership exclusive during setup and rename."""
        return not any(
            other is not adapter and (binding := getattr(other, "native_binding", None)) is not None
            and binding.subscribed_entity_id == entity_id
            for other in self._adapters
        )

    @callback
    def _clear_holds(self) -> None:
        self._held_buttons.clear()

    @callback
    def _adapter_idle(self, adapter):
        for button in adapter.config.buttons:
            self._held_buttons.discard(button.id)

    @callback
    def _adapter_status(self, adapter):
        if adapter.error:
            self._error_count += 1
        self._snapshot()

    @callback
    def _adapter_unmapped(self, adapter, ambiguous=False):
        self._unmapped_count += 1
        if ambiguous:
            self._error_count += 1
        _LOGGER.warning("Unmapped action for gesture control %s/%s", self.config.control_id, adapter.config.id)
        self._snapshot()

    @callback
    def _adapter_gesture(self, adapter, button_id, gesture):
        button = next(button for button in adapter.config.buttons if button.id == button_id)
        if gesture.kind == "hold_start":
            if button_id in self._held_buttons:
                gesture = button.repeat_gesture()
            self._held_buttons.add(button_id)
        elif gesture.kind in {"release", "press"}:
            self._held_buttons.discard(button_id)
        self._publish_gesture(button_id, gesture)

    @callback
    def _publish_gesture(self, button_id, gesture):
        self._sequence += 1
        self._update_kind = "gesture"
        attributes = {"button_id": button_id, "gesture_id": gesture.id,
                      "gesture_session_id": self._session_id, "gesture_sequence": self._sequence,
                      "gesture_event_id": f"{self._session_id}:{self._sequence}",
                      "gesture_occurred_at": dt_util.utcnow().isoformat(timespec="milliseconds")}
        async_setup_latency(self.hass).published(self, self._sequence, attributes["gesture_occurred_at"])
        if gesture.press_count is not None:
            attributes["press_count"] = gesture.press_count
        self._trigger_event(gesture.kind, attributes)
        self.async_write_ha_state()

    @callback
    def _snapshot(self) -> None:
        self._update_kind = "snapshot"
        self.async_write_ha_state()

    @callback
    def _load_changed(self, event: Event) -> None:
        self._snapshot()
