"""Explicit LED commands and confirmed metadata over HA select/number endpoints."""

from __future__ import annotations

import asyncio
import math

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .config import DOMAIN
from .config_values import KEY
from .entity_binding import RegistryEntityBinding
from .led_config import LEDConfig, LEDEndpoint, color, percent


SET_LED_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("led_id"): vol.All(str, vol.Match(KEY)),
    vol.Optional("color"): color,
    vol.Optional("brightness"): percent,
    vol.Optional("behavior"): vol.In(("follow_load", "follow_load_inverted", "always_on", "always_off")),
})


def _number(value):
    if type(value) is bool:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


class EndpointRuntime:
    """Resolve the same registry identity again immediately before every write."""

    def __init__(self, hass, config: LEDEndpoint, changed):
        self.hass, self.config = hass, config
        self._changed = changed
        self.binding = RegistryEntityBinding(hass, config.entity_id, config.registry_id,
            self._state_changed, self._rebound, frozenset({config.entity_id.split('.', 1)[0]}))

    @callback
    def _state_changed(self, _event):
        self._changed()

    @callback
    def _rebound(self, _entity_id, _state, _initial, _old):
        self._changed()

    def target(self):
        if self.binding.registry_id is not None:
            entry = er.async_get(self.hass).async_get(self.binding.registry_id)
            return entry.entity_id if entry and entry.domain == self.config.entity_id.split('.', 1)[0] else None
        return self.binding.entity_id

    def state(self):
        return self.hass.states.get(target) if (target := self.target()) else None

    def supports(self, value):
        state = self.state()
        if state is None or state.state in {"unknown", "unavailable"} or state.attributes.get("restored"):
            return False
        if state.domain == "select":
            options = state.attributes.get("options")
            return (isinstance(options, (list, tuple)) and all(isinstance(item, str) for item in options)
                    and state.state in options and isinstance(value, str) and value in options
                    and self.hass.services.has_service("select", "select_option"))
        current = _number(state.state)
        minimum, maximum = _number(state.attributes.get("min")), _number(state.attributes.get("max"))
        step = _number(state.attributes.get("step"))
        requested = _number(value)
        if (current is None or minimum is None or maximum is None or minimum > maximum or requested is None
                or not minimum <= current <= maximum or not minimum <= requested <= maximum
                or not self.hass.services.has_service("number", "set_value")):
            return False
        if step is not None:
            if step <= 0:
                return False
            quotient = (requested - minimum) / step
            if not math.isfinite(quotient) or not math.isclose(quotient, round(quotient), rel_tol=0, abs_tol=1e-7):
                return False
        return True

    def available(self):
        return all(self.supports(value) for value in self.config.values.values())

    def mapped_state(self):
        state = self.state()
        if state is None:
            return None
        actual = state.state if state.domain == "select" else _number(state.state)
        return next((key for key, value in self.config.values.items() if actual == value), None)

    def plan(self, value):
        if not self.supports(value):
            raise ServiceValidationError("LED endpoint is unavailable or its configured option/range is unsupported")
        return self, value

    async def write(self, value):
        self.plan(value)
        target = self.target()
        domain = target.split('.', 1)[0]
        if domain == "select":
            await self.hass.services.async_call("select", "select_option",
                {"entity_id": target, "option": value}, blocking=True)
        else:
            await self.hass.services.async_call("number", "set_value",
                {"entity_id": target, "value": value}, blocking=True)


class LEDController:
    """Serialize explicit multi-endpoint actions without optimistic state changes."""

    def __init__(self, hass, owner, configs: tuple[LEDConfig, ...], changed):
        self.hass, self.owner, self.configs, self._changed = hass, owner, configs, changed
        self.channels = {config.id: {kind: EndpointRuntime(hass, endpoint, self._endpoint_changed)
            for kind in ("color", "power", "brightness") if (endpoint := getattr(config, kind)) is not None}
            for config in configs}
        self._started = self._closed = False
        self._last_snapshot = []
        self._lock = asyncio.Lock()

    @callback
    def start(self):
        for endpoints in self.channels.values():
            for endpoint in endpoints.values():
                endpoint.binding.start()
        self._started = True
        self._last_snapshot = self.snapshot()
        async_setup_leds(self.hass).register(self)

    @callback
    def close(self):
        self._closed = True
        async_setup_leds(self.hass).unregister(self)
        for endpoints in self.channels.values():
            for endpoint in endpoints.values():
                endpoint.binding.close()

    @callback
    def _endpoint_changed(self):
        if not self._started or self._closed:
            return
        snapshot = self.snapshot()
        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            self._changed()

    def snapshot(self):
        result = []
        for config in self.configs:
            endpoints = self.channels[config.id]
            available = not self._closed and all(endpoint.available() for endpoint in endpoints.values())
            item = {"id": config.id, "label": config.label, "button_id": config.button_id,
                    "available": available, "colors": config.colors,
                    "brightness_supported": config.brightness is not None,
                    "color_supported": config.color is not None,
                    "behaviors": ["always_on", "always_off"] if config.power else []}
            if config.brightness:
                item["brightness_levels"] = sorted(int(level) for level in config.brightness.values)
            if available:
                power = endpoints["power"].mapped_state() if "power" in endpoints else "on"
                actual_color = endpoints["color"].mapped_state() if "color" in endpoints else "FFFFFF"
                if config.color and actual_color is not None:
                    item["configured_color"] = actual_color
                if power == "off":
                    item["color"] = "000000"
                elif power == "on" and actual_color is not None:
                    item["color"] = actual_color
                if config.power and power in {"on", "off"}:
                    item["behavior"] = "always_" + power
                if "brightness" in endpoints and (brightness := endpoints["brightness"].mapped_state()) is not None:
                    item["brightness"] = int(brightness)
            result.append(item)
        return result

    def _assert_active(self):
        if self._closed or async_setup_leds(self.hass).controllers.get(self.owner.config.control_id) is not self:
            raise ServiceValidationError("LED control was unloaded or reconfigured")

    async def set_led(self, led_id, requested_color=None, brightness=None, behavior=None):
        async with self._lock:
            self._assert_active()
            config = next((item for item in self.configs if item.id == led_id), None)
            if config is None:
                raise ServiceValidationError("LED channel is not configured")
            if requested_color is None and brightness is None and behavior is None:
                raise ServiceValidationError("Provide an LED color, brightness or behavior")
            endpoints = self.channels[led_id]
            if not all(endpoint.available() for endpoint in endpoints.values()):
                raise ServiceValidationError("LED channel is unavailable or its mapping is unsupported")
            planned = []
            if behavior is not None and (config.power is None or behavior not in {"always_on", "always_off"}):
                raise ServiceValidationError("LED behavior is unsupported")
            if requested_color is not None:
                if requested_color not in config.colors:
                    raise ServiceValidationError("LED color is not in the configured palette")
                if requested_color == "000000":
                    if behavior != "always_off":
                        raise ServiceValidationError("Black requires an explicit always_off LED behavior")
                else:
                    if config.color:
                        planned.append(endpoints["color"].plan(config.color.values[requested_color]))
                    elif requested_color != "FFFFFF" or behavior != "always_on":
                        raise ServiceValidationError("This fixed-color LED requires explicit always_on behavior")
            if behavior is not None:
                planned.append(endpoints["power"].plan(config.power.values["on" if behavior == "always_on" else "off"]))
            if brightness is not None:
                if config.brightness is None:
                    raise ServiceValidationError("LED brightness is unsupported")
                key = min(config.brightness.values, key=lambda item: (abs(int(item) - brightness), int(item)))
                planned.append(endpoints["brightness"].plan(config.brightness.values[key]))
            targets = [endpoint.target() for endpoint, _value in planned]
            if len(targets) != len(set(targets)):
                raise ServiceValidationError("LED action contains conflicting endpoint targets")
            # Validate the complete action before touching any endpoint. Recheck
            # active ownership/identity/options again after each awaited write.
            for endpoint, value in planned:
                self._assert_active()
                endpoint.plan(value)
                state = endpoint.state()
                current = state.state if state.domain == "select" else _number(state.state)
                if current != value:
                    await endpoint.write(value)


class LEDRegistry:
    def __init__(self):
        self.controllers = {}

    @callback
    def register(self, controller):
        self.controllers[controller.owner.config.control_id] = controller

    @callback
    def unregister(self, controller):
        identity = controller.owner.config.control_id
        if self.controllers.get(identity) is controller:
            self.controllers.pop(identity)

    async def set_led(self, data):
        controller = next((item for item in self.controllers.values()
                           if item.owner.entity_id == data["entity_id"]), None)
        if controller is None:
            raise ServiceValidationError("LED control is not currently loaded")
        await controller.set_led(data["led_id"], data.get("color"), data.get("brightness"), data.get("behavior"))


@callback
def async_setup_leds(hass):
    runtime = hass.data.setdefault(DOMAIN, {})
    if registry := runtime.get("leds"):
        return registry
    registry = runtime["leds"] = LEDRegistry()

    async def handle(call):
        await registry.set_led(call.data)

    hass.services.async_register(DOMAIN, "set_led", handle, schema=SET_LED_SCHEMA)
    return registry
