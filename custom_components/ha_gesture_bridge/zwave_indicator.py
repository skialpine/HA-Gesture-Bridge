"""Discover standard visual Indicator CC controls without model-specific guesses.

The Z-Wave integration owns transport and interviews. This adapter only reads its
advertised values and invokes Indicator CC. It never enables config entities or
changes Configuration/Switch CC. Raw Get responses, rather than optimistic
``setValue`` state, provide the published feedback.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import math

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .leds import async_setup_leds

INDICATOR_CC = 135
VISUAL_INDICATORS = {0x30: "LCD backlight", 0x40: "Button letters", 0x41: "Button digits",
                     0x42: "Button commands", **{0x42 + n: f"Button {n} indicator" for n in range(1, 13)}}
MAX_INDICATORS = 16


def _resolve_node(hass, device_id):
    device = dr.async_get(hass).async_get(device_id)
    if device is None or device.disabled_by is not None or not any(domain == "zwave_js" for domain, _ in device.identifiers):
        return None
    try:
        # Optional integration dependencies must not be imported on non-Z-Wave installations.
        from homeassistant.components.zwave_js.helpers import async_get_node_from_device_id
        return async_get_node_from_device_id(hass, device_id)
    except (ImportError, ValueError):
        return None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _permits(value, requested):
    metadata = value.metadata
    if metadata.readable is not True or metadata.writeable is not True or metadata.secret:
        return False
    if metadata.type == "boolean":
        return type(requested) is bool
    if metadata.type != "number" or not _number(requested):
        return False
    if not _number(metadata.min) or not _number(metadata.max) or not metadata.min <= requested <= metadata.max:
        return False
    allowed = metadata.data.get("allowed")
    if allowed is None:
        return True
    if not isinstance(allowed, list):
        return False
    for item in allowed:
        if not isinstance(item, dict):
            continue
        if item.get("value") == requested:
            return True
        low, high, step = item.get("from"), item.get("to"), item.get("step", 1)
        if (_number(low) and _number(high) and _number(step) and step > 0
                and low <= requested <= high and (requested - low) % step == 0):
            return True
    return False


@dataclass(frozen=True)
class IndicatorChannel:
    endpoint: int
    indicator: int
    label: str
    level_id: str | None = None
    binary_id: str | None = None
    legacy_id: str | None = None

    @property
    def id(self):
        return f"indicator_{self.endpoint}_{self.indicator}"


def discover_indicators(hass, device_id):
    """Return only capabilities whose standard metadata defines safe light control."""
    node = _resolve_node(hass, device_id)
    if node is None:
        return ()
    groups = {}
    for value in node.values.values():
        if value.command_class != INDICATOR_CC:
            continue
        endpoint = value.endpoint or 0
        if type(endpoint) is not int or not 0 <= endpoint <= 127:
            continue
        if value.property_ == "value" and value.property_key is None and value.cc_version == 1:
            if _permits(value, 0) and _permits(value, 255):
                groups[(endpoint, 0)] = {"legacy_id": value.value_id}
            continue
        if type(value.property_) is not int or value.property_ not in VISUAL_INDICATORS:
            continue
        key = (endpoint, value.property_)
        if type(value.property_key) is not int:
            continue
        if value.property_key == 1 and all(_permits(value, n) for n in range(100)):
            groups.setdefault(key, {})["level_id"] = value.value_id
        elif value.property_key == 2 and _permits(value, False) and _permits(value, True):
            groups.setdefault(key, {})["binary_id"] = value.value_id
    # Never offer a legacy command alongside modern indicators on the same endpoint.
    modern_endpoints = {endpoint for endpoint, indicator in groups if indicator != 0}
    channels = [IndicatorChannel(endpoint, indicator, VISUAL_INDICATORS.get(indicator, "Status indicator"), **values)
                for (endpoint, indicator), values in sorted(groups.items())
                if indicator != 0 or endpoint not in modern_endpoints]
    # A partial list would silently lose controls. Leave an oversized topology unsupported.
    return tuple(channels) if len(channels) <= MAX_INDICATORS else ()


def indicator_summary(hass, device_id):
    """Describe automatic discovery without probing hardware or requiring Z-Wave."""
    node = _resolve_node(hass, device_id)
    if node is None:
        return "No standard Z-Wave status LED controls detected."
    channels = discover_indicators(hass, device_id)
    if channels:
        count = len(channels)
        brightness = any(channel.level_id for channel in channels)
        return f"{count} standard Z-Wave indicator{'s' if count != 1 else ''}: on/off" + (", brightness." if brightness else ".")
    identify = any(value.command_class == INDICATOR_CC and value.property_ in (0x50, "identify")
                   for value in node.values.values())
    return ("Z-Wave exposes identification blinking only; no standard status LED control."
            if identify else "No standard Z-Wave status LED controls detected.")


class ZWaveIndicatorController:
    """Use existing Z-Wave transport with bounded read-only rediscovery and cleanup."""

    _command_class = INDICATOR_CC

    def _discover(self):
        return discover_indicators(self.hass, self.device_id)

    def __init__(self, hass, owner, device_id, changed):
        self.hass, self.owner, self.device_id, self._changed = hass, owner, device_id, changed
        self._node = None
        self._channels = ()
        self._confirmed = {}
        self._unsubscribers = []
        self._timer = None
        self._task = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._last_snapshot = []

    @callback
    def start(self):
        async_setup_leds(self.hass).register(self)
        self._refresh()
        self._timer = async_track_time_interval(self.hass, self._refresh, timedelta(seconds=5))

    @callback
    def close(self):
        self._closed = True
        async_setup_leds(self.hass).unregister(self)
        if self._timer:
            self._timer()
            self._timer = None
        self._detach()
        if self._task:
            self._task.cancel()
            self._task = None

    def _detach(self):
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()

    def _ready(self, node):
        # NodeStatus.UNKNOWN=0 and DEAD=3 must not look like a usable endpoint.
        return node is not None and node.ready and node.client.connected and node.status not in (0, 3)

    @callback
    def _refresh(self, _now=None):
        if self._closed:
            return
        node = _resolve_node(self.hass, self.device_id)
        if not self._ready(node):
            node = None
        rebound = node is not self._node
        if rebound:
            self._detach()
            self._node = node
            self._confirmed.clear()
            if node:
                for event in ("value added", "value removed", "value updated", "metadata updated", "ready", "interview completed", "interview started", "dead", "alive"):
                    self._unsubscribers.append(node.on(event, self._node_changed))
        if node:
            channels = self._discover()
            if channels != self._channels:
                self._channels = channels
                self._confirmed.clear()
                rebound = True
        self._notify()
        if node and (rebound or any(not self._confirmed.get(channel.id) for channel in self._channels)):
            self._schedule_reads()

    @callback
    def _node_changed(self, event):
        self._refresh()
        value = event.get("value")
        if value is None or value.command_class == self._command_class:
            self._schedule_reads()

    def _schedule_reads(self):
        if self._closed or self._node is None or not self._channels or self._lock.locked():
            return
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_background_task(self._read_all(), "Read standard indicator feedback")

    async def _read_all(self):
        async with self._lock:
            node, channels = self._node, self._channels
            for channel in channels:
                if self._closed:
                    return
                try:
                    await self._read(channel, node)
                except ServiceValidationError:
                    continue

    def _notify(self):
        if self._closed:
            return
        snapshot = self.snapshot()
        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            self._changed()

    def _publish_confirmation(self, channel, node, confirmed):
        """Retired reads must neither publish nor clear a replacement's feedback."""
        if (not self._closed and self._node is node
                and _resolve_node(self.hass, self.device_id) is node
                and channel in self._channels
                and async_setup_leds(self.hass).controllers.get(self.owner.config.control_id) is self):
            self._confirmed[channel.id] = confirmed
            self._notify()

    def snapshot(self):
        available = not self._closed and self._ready(self._node) and _resolve_node(self.hass, self.device_id) is self._node
        result = []
        for channel in self._channels:
            item = {"id": channel.id, "label": channel.label,
                    "button_id": "toggle" if len(self._channels) == 1 else channel.id,
                    "available": bool(available), "colors": ["000000", "FFFFFF"],
                    "brightness_supported": channel.level_id is not None,
                    "color_supported": False, "behaviors": ["always_on", "always_off"]}
            if available:
                item.update(self._confirmed.get(channel.id, {}))
            result.append(item)
        return result

    def _assert_active(self, channel, node):
        if (self._closed or self._node is not node or not self._ready(node)
                or _resolve_node(self.hass, self.device_id) is not node
                or channel not in self._discover()
                or async_setup_leds(self.hass).controllers.get(self.owner.config.control_id) is not self):
            raise ServiceValidationError("Standard indicator is unavailable or was reconfigured")

    async def _invoke(self, channel, node, method, *args):
        self._assert_active(channel, node)
        from zwave_js_server.const import CommandClass
        from zwave_js_server.exceptions import BaseZwaveJSServerError
        endpoint = node.endpoints.get(channel.endpoint)
        if endpoint is None:
            raise ServiceValidationError("Standard indicator endpoint is unavailable")
        try:
            response = await endpoint.async_invoke_cc_api(CommandClass(self._command_class), method, *args, wait_for_result=True)
        except (BaseZwaveJSServerError, TimeoutError) as error:
            raise ServiceValidationError("Z-Wave indicator command failed") from error
        self._assert_active(channel, node)
        if method == "set" and isinstance(response, dict) and response.get("status") in (0, 2):
            raise ServiceValidationError("Z-Wave indicator rejected the command")
        return response

    async def _read(self, channel, node):
        try:
            response = await self._invoke(channel, node, "get", *([] if channel.legacy_id else [channel.indicator]))
        except ServiceValidationError:
            # Missing confirmation lets the existing poll retry this channel.
            self._publish_confirmation(channel, node, {})
            raise
        confirmed = {}
        if channel.legacy_id:
            if type(response) is int and response in (0, 255):
                confirmed["color"] = "FFFFFF" if response else "000000"
        elif isinstance(response, list):
            values = {item["propertyId"]: item.get("value") for item in response
                      if isinstance(item, dict) and item.get("indicatorId") == channel.indicator
                      and type(item.get("propertyId")) is int}
            level, binary = values.get(1), values.get(2)
            if channel.level_id and type(level) is int and 0 <= level <= 99:
                confirmed.update(color="FFFFFF" if level else "000000", brightness=round(level * 100 / 99))
            if channel.binary_id and (type(binary) is bool or type(binary) is int and binary in (0, 255)):
                if not binary or not channel.level_id:
                    confirmed["color"] = "FFFFFF" if binary else "000000"
        if "color" in confirmed:
            confirmed["behavior"] = "always_off" if confirmed["color"] == "000000" else "always_on"
        self._publish_confirmation(channel, node, confirmed)

    async def set_led(self, led_id, requested_color=None, brightness=None, behavior=None):
        async with self._lock:
            self._refresh()
            channel = next((item for item in self._channels if item.id == led_id), None)
            node = self._node
            if channel is None:
                raise ServiceValidationError("Standard indicator is not supported")
            self._assert_active(channel, node)
            if behavior is not None and behavior not in {"always_on", "always_off"}:
                raise ServiceValidationError("Standard indicator behavior is unsupported")
            if requested_color is not None:
                expected = {"always_on": "FFFFFF", "always_off": "000000"}.get(behavior)
                if requested_color != expected:
                    raise ServiceValidationError("Standard indicators require explicit on/off behavior, not a color change")
            if brightness is not None and (not _number(brightness) or not 0 <= brightness <= 100 or not channel.level_id):
                raise ServiceValidationError("Standard indicator brightness is unsupported")
            if requested_color is None and brightness is None and behavior is None:
                raise ServiceValidationError("Provide an indicator brightness or behavior")
            # Power checks use the exact quantized level sent to the device.
            level = round(brightness * 99 / 100) if brightness is not None else None
            if behavior == "always_off" and level not in (None, 0):
                raise ServiceValidationError("Off conflicts with a nonzero indicator brightness")
            if behavior == "always_on" and level == 0:
                raise ServiceValidationError("On conflicts with zero indicator brightness")
            if behavior is None:
                # Intensity is also power; a brightness-only command must not
                # turn a standard indicator on or off as a side effect.
                await self._read(channel, node)
                actual = self._confirmed.get(channel.id, {}).get("color")
                if actual is None or (level == 0) != (actual == "000000"):
                    raise ServiceValidationError("Provide explicit indicator behavior to change power")
            if channel.legacy_id:
                await self._invoke(channel, node, "set", 0 if behavior == "always_off" else 255)
            else:
                values = []
                if channel.level_id and (brightness is not None or behavior is not None):
                    target_level = level if level is not None else 0 if behavior == "always_off" else 99
                    values.append({"indicatorId": channel.indicator, "propertyId": 1, "value": target_level})
                if channel.binary_id and behavior is not None:
                    values.append({"indicatorId": channel.indicator, "propertyId": 2, "value": behavior == "always_on"})
                await self._invoke(channel, node, "set", values)
            await self._read(channel, node)
