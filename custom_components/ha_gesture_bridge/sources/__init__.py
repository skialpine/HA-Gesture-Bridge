"""HA-owned input subscriptions; no controller, broker, or device connections."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..config import SourceConfig
from .native import NativeSourceBinding

_MISSING = object()


def field(data: Any, path: str) -> Any:
    """Resolve an explicit mapping/list field path without expressions or templates."""
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part, _MISSING)
        elif isinstance(data, (list, tuple)) and part.isdecimal() and int(part) < len(data):
            data = data[int(part)]
        else:
            return _MISSING
    return data


def matches(data: Any, filters: dict[str, Any]) -> bool:
    for path, expected in filters.items():
        value = field(data, path)
        if value is _MISSING:
            return False
        # JSON booleans must not compare equal to numeric scene/action codes.
        if type(value) is bool or type(expected) is bool:
            if type(value) is not type(expected) or value != expected:
                return False
        elif value != expected:
            return False
    return True


def timestamp(state) -> datetime | None:
    if state is None or state.state in {"unknown", "unavailable"}:
        return None
    value = dt_util.parse_datetime(state.state)
    return dt_util.as_utc(value) if value is not None and value.tzinfo is not None else None


class SourceAdapter:
    """Normalize live HA occurrences, preserving repeated identical bus events.

    notify(adapter, button_id, gesture) publishes an occurrence; status(adapter)
    publishes metadata only; idle(adapter) clears local hold bookkeeping only.
    """

    def __init__(self, hass, config: SourceConfig, notify, status, idle, unmapped) -> None:
        self.hass, self.config = hass, config
        self.notify, self.status, self.idle, self.unmapped = notify, status, idle, unmapped
        self.ready = False
        self.availability = "unknown"
        self.error = None
        self._unsubs = []
        self._timer = None
        self._closed = False
        self._connected = False
        now = dt_util.utcnow()
        self._started = now.replace(microsecond=now.microsecond // 1000 * 1000)
        self._watermark = None

    def _set_status(self, ready, availability, error=None, publish=True):
        self.ready, self.availability, self.error = ready, availability, error
        if not ready:
            self.idle(self)
        if publish and not self._closed:
            self.status(self)

    async def async_start(self):
        kind = self.config.type
        if kind in {"native_event", "shared_native"}:
            binding = NativeSourceBinding(self.hass, self.config.source_entity_id, self.config.source_registry_id,
                                          self._native, self._native_rebound)
            self.native_binding = binding
            binding.start()
            self._unsubs.append(binding.close)
        elif kind == "mqtt":
            # The existing HA MQTT integration owns credentials, transport and reconnects.
            from homeassistant.components import mqtt
            if "mqtt" not in self.hass.config.components and not self.hass.config_entries.async_entries("mqtt"):
                self._set_status(False, "unavailable", "mqtt_not_configured", publish=False)
                return
            try:
                self._connected = mqtt.is_connected(self.hass)
                self._unsubs.append(await mqtt.async_subscribe(self.hass, self.config.topic, self._mqtt, qos=0))
                if self.config.availability_topic:
                    self._unsubs.append(await mqtt.async_subscribe(self.hass, self.config.availability_topic, self._mqtt_availability, qos=0))
                else:
                    self._set_status(self._connected, "unknown" if self._connected else "unavailable", publish=False)
                self._unsubs.append(mqtt.async_subscribe_connection_status(self.hass, self._mqtt_connection))
            except Exception:
                # No raw broker errors/payloads/credentials in public state or logs.
                self.close()
                self._closed = False
                self._set_status(False, "unavailable", "mqtt_subscription_failed", publish=False)
        else:
            event_type = {"zwave_js": "zwave_js_value_notification", "zha": "zha_event"}.get(kind, self.config.event_type)
            self._unsubs.append(self.hass.bus.async_listen(event_type, self._bus))
            self._set_status(True, "unknown", publish=False)

    @callback
    def _mqtt_connection(self, connected):
        if self._closed:
            return
        self._connected = connected
        if not connected:
            self._set_status(False, "unavailable", "mqtt_disconnected")
        elif self.config.availability_topic:
            self._set_status(False, "unknown")
        else:
            self._set_status(True, "unknown")

    def _native_status(self, state, initial=False):
        if state is not None and "bridge_contract_version" in state.attributes:
            self._set_status(False, "unavailable", "aggregate_source_forbidden", publish=False)
        elif state is None or state.state == "unavailable" or state.attributes.get("restored", False):
            self._set_status(False, "unavailable", publish=False)
        elif timestamp(state) is None and not initial:
            self._set_status(False, "unknown", publish=False)
        else:
            self._set_status(True, "available", publish=False)

    @callback
    def _native_rebound(self, entity_id, state, initial, old_entity_id):
        self.idle(self)
        self._native_status(state, initial=initial)
        self._watermark = timestamp(state)
        if not initial:
            self.status(self)
        return not (state is not None and "bridge_contract_version" in state.attributes)

    @callback
    def _native(self, event):
        if self._closed:
            return
        state, old = event.data.get("new_state"), event.data.get("old_state")
        self._native_status(state)
        revision = timestamp(state)
        if not self.ready or revision is None:
            self.status(self)
            return
        if old is None or old.state == "unavailable":
            self._watermark = max(self._watermark, revision) if self._watermark else revision
            self.idle(self)
            self.status(self)
            return
        if revision < self._started or self._watermark is not None and revision <= self._watermark:
            self.status(self)
            return
        self._watermark = revision
        self._consume(dict(state.attributes))

    @callback
    def _bus(self, event):
        if self._closed or event.time_fired < self._started:
            return
        data = dict(event.data)
        if self.config.type == "zwave_js":
            if type(data.get("command_class")) is not int or data.get("command_class") != 91 or data.get("property") != "scene":
                return
            key = data.get("property_key")
            if type(key) is int or isinstance(key, str) and key.isdecimal():
                data["scene"] = int(key)
            else:
                return
        self._consume(data)

    @callback
    def _mqtt(self, message):
        if self._closed or message.retain or not self._connected:
            return
        if self.config.availability_topic and not self.ready:
            return
        payload = message.payload
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return
        if not isinstance(payload, str) or len(payload) > 65536:
            return
        try:
            data = json.loads(payload)
        except (ValueError, TypeError, RecursionError):
            data = {"action": payload, "payload": payload}
        if not isinstance(data, dict):
            data = {"payload": data}
        self._consume(data)

    @callback
    def _mqtt_availability(self, message):
        if self._closed or not self._connected:
            return
        payload = message.payload
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError:
                return
        if isinstance(payload, str) and len(payload) <= 65536:
            try:
                decoded = json.loads(payload)
            except (ValueError, TypeError, RecursionError):
                pass
            else:
                # Zigbee2MQTT's standard availability payload is {"state":"online"}.
                if isinstance(decoded, dict):
                    payload = decoded.get("state")
        # Retained availability is metadata, never a physical occurrence.
        if payload == self.config.payload_available:
            self._set_status(True, "available")
        elif payload == self.config.payload_not_available:
            self._set_status(False, "unavailable")
        else:
            self._set_status(False, "unknown")

    def _consume(self, data):
        if not matches(data, self.config.filters):
            return
        found = [mapping for mapping in self.config.mappings if matches(data, mapping.match)]
        if len(found) != 1:
            self.unmapped(self, ambiguous=len(found) > 1)
            return
        self._set_status(True, self.availability, publish=False)
        mapping = found[0]
        self.notify(self, mapping.button_id, mapping.gesture)
        if self._timer:
            self._timer()
            self._timer = None
        if self.config.inactivity_timeout:
            self._timer = async_call_later(self.hass, self.config.inactivity_timeout, self._expire)

    @callback
    def _expire(self, _now):
        self._timer = None
        if not self._closed:
            # Expiry is housekeeping only. Never invent a release event.
            self.idle(self)

    @callback
    def close(self):
        self._closed = True
        if self._timer:
            self._timer()
            self._timer = None
        while self._unsubs:
            self._unsubs.pop()()
        self.idle(self)
