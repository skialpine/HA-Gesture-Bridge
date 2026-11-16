"""Automatic LED configuration parameters using reviewed metadata templates."""

from __future__ import annotations

from dataclasses import dataclass
import math

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .led_profiles import BrightnessSetting, ColorSetting, LEDChannelProfile, LEDProfile
from .zwave_indicator import ZWaveIndicatorController, _resolve_node


def _behaviors(definition):
    return {"follow_load": definition.follow_load_value,
            "follow_load_inverted": definition.follow_load_inverted_value,
            "always_on": definition.on_value, "always_off": definition.off_value}


@dataclass(frozen=True)
class ParameterChannel:
    definition: LEDChannelProfile
    mode_id: str
    color_id: str | None = None
    brightness_id: str | None = None
    color: ColorSetting | None = None
    brightness: BrightnessSetting | None = None
    endpoint: int = 0

    @property
    def id(self):
        return self.definition.id

    @property
    def label(self):
        return self.definition.label


def _setting(node, variants):
    matches = [(variant, value.value_id) for variant in variants
               for value in node.values.values() if variant.schema.matches(value)]
    return matches[0] if len(matches) == 1 else None


def discover_parameters(hass, device_id, profile):
    """Require a complete, non-conflicting topology of reviewed LED parameters."""
    node = _resolve_node(hass, device_id)
    if node is None or not profile.matches_node(node) or not 1 <= len(profile.channels) <= 16:
        return ()
    channels, identities, targets = [], set(), set()
    for declaration in profile.channels:
        modes = [value for value in node.values.values() if declaration.mode.matches(value)]
        color = _setting(node, declaration.colors) if declaration.colors else None
        brightness = _setting(node, declaration.brightness) if declaration.brightness else None
        if (len(modes) != 1 or declaration.colors and color is None
                or declaration.brightness and brightness is None or declaration.id in identities):
            return ()
        ids = [modes[0].value_id, *([color[1]] if color else []), *([brightness[1]] if brightness else [])]
        if len(ids) != len(set(ids)) or targets.intersection(ids):
            return ()
        identities.add(declaration.id)
        targets.update(ids)
        channels.append(ParameterChannel(declaration, modes[0].value_id,
            color[1] if color else None, brightness[1] if brightness else None,
            color[0] if color else None, brightness[0] if brightness else None))
    return tuple(channels)


class ZWaveParameterLEDController(ZWaveIndicatorController):
    """Share reconnect/serialization while restricting all writes to reviewed LEDs."""

    _command_class = 112

    def __init__(self, hass, owner, device_id, changed, profile):
        self.profile = profile
        super().__init__(hass, owner, device_id, changed)

    def _discover(self):
        return discover_parameters(self.hass, self.device_id, self.profile)

    def _button_ids(self):
        """Associate physical scene LEDs with the user's actual native button IDs."""
        roles = {}
        registry = er.async_get(self.hass)
        if self._node is not None:
            scenes = {value.value_id: int(value.property_key)
                for value in self._node.values.values()
                if value.command_class == 91 and value.endpoint == 0 and value.property_ == "scene"
                and isinstance(value.property_key, (int, str)) and str(value.property_key).isdecimal()}
            for button in self.owner.config.inputs:
                entry = registry.async_get(button.source_registry_id or button.source_entity_id)
                if entry is None or entry.platform != "zwave_js" or entry.device_id != self.device_id:
                    continue
                matches = [scene for value_id, scene in scenes.items() if entry.unique_id.endswith("." + value_id)]
                if len(matches) == 1:
                    roles.setdefault(matches[0], []).append(button.id)
        result, used = {}, set()
        for channel in self._channels:
            choices = roles.get(channel.definition.scene_id, [])
            if len(choices) == 1 and choices[0] not in used:
                result[channel.id] = choices[0]
                used.add(choices[0])
        for channel in self._channels:
            if channel.id in result:
                continue
            candidate = channel.definition.button_id
            if candidate in used:
                base, suffix = "led_" + channel.id[:48], 2
                candidate = base
                while candidate in used:
                    candidate = f"{base}_{suffix}"
                    suffix += 1
            result[channel.id] = candidate
            used.add(candidate)
        return result

    def snapshot(self):
        available = (not self._closed and self._ready(self._node)
            and _resolve_node(self.hass, self.device_id) is self._node
            and self._channels == self._discover())
        roles = self._button_ids()
        result = []
        for channel in self._channels:
            palette = channel.color.palette if channel.color else (("FFFFFF", 0),)
            item = {"id": channel.id, "label": channel.label, "button_id": roles[channel.id],
                    "available": bool(available), "colors": ["000000", *sorted(color for color, _ in palette)],
                    "brightness_supported": channel.brightness is not None,
                    "color_supported": channel.color is not None,
                    "behaviors": list(_behaviors(channel.definition))}
            if available:
                confirmed = self._confirmed.get(channel.id, {})
                mode = confirmed.get("mode")
                actual_behavior = next((key for key, value in _behaviors(channel.definition).items() if value == mode), None)
                if actual_behavior is not None:
                    item["behavior"] = actual_behavior
                if mode == channel.definition.off_value:
                    item["color"] = "000000"
                elif mode == channel.definition.on_value:
                    if channel.color is None:
                        item["color"] = "FFFFFF"
                    elif "color" in confirmed:
                        item["color"] = confirmed["color"]
                if "brightness" in confirmed:
                    item["brightness"] = confirmed["brightness"]
            result.append(item)
        return result

    async def _read(self, channel, node):
        definition = channel.definition
        settings = [("mode", definition.mode, {value: value for value, _ in definition.mode.states})]
        if channel.color:
            settings.append(("color", channel.color.schema, {value: color for color, value in channel.color.palette}))
        if channel.brightness:
            settings.append(("brightness", channel.brightness.schema, {value: level for level, value in channel.brightness.levels}))
        confirmed = {}
        try:
            for role, schema, mapping in settings:
                response = await self._invoke(channel, node, "get", schema.parameter)
                if type(response) is not int or response not in mapping:
                    raise ServiceValidationError("Z-Wave LED parameter read did not return a supported value")
                confirmed[role] = mapping[response]
        except ServiceValidationError:
            # A successful mode Get does not complete a failed color/brightness
            # read. Retire partial and older confirmation so polling can retry.
            self._publish_confirmation(channel, node, {})
            raise
        self._publish_confirmation(channel, node, confirmed)

    async def set_led(self, led_id, requested_color=None, brightness=None, behavior=None):
        async with self._lock:
            self._refresh()
            channel = next((item for item in self._channels if item.id == led_id), None)
            node = self._node
            if channel is None:
                raise ServiceValidationError("Device LED profile is unsupported or its parameter schema changed")
            self._assert_active(channel, node)
            definition = channel.definition
            palette = dict(channel.color.palette) if channel.color else {"FFFFFF": 0}
            behaviors = _behaviors(definition)
            if behavior is not None and behavior not in behaviors:
                raise ServiceValidationError("LED behavior is unsupported")
            if requested_color is not None and requested_color not in {"000000", *palette}:
                raise ServiceValidationError("LED color is not supported by this device")
            if brightness is not None and (channel.brightness is None
                    or type(brightness) not in (int, float)
                    or not math.isfinite(brightness) or not 0 <= brightness <= 100):
                raise ServiceValidationError("LED brightness is unsupported or outside 0 through 100")
            if requested_color is None and brightness is None and behavior is None:
                raise ServiceValidationError("Provide an LED color, brightness or behavior")
            planned = []
            if requested_color == "000000":
                if behavior != "always_off":
                    raise ServiceValidationError("Black requires an explicit always_off LED behavior")
            elif requested_color is not None:
                if channel.color:
                    planned.append((channel.color.schema.parameter, palette[requested_color]))
                elif requested_color != "FFFFFF" or behavior != "always_on":
                    raise ServiceValidationError("This fixed-color LED requires explicit always_on behavior")
            if behavior is not None:
                planned.append((definition.mode.parameter, behaviors[behavior]))
            if brightness is not None:
                _level, value = min(channel.brightness.levels,
                    key=lambda pair: (abs(pair[0] - brightness), pair[0]))
                planned.append((channel.brightness.schema.parameter, value))
            # Validate the complete action before the first write; the shared
            # invoke guard rechecks current schema and ownership after each await.
            self._confirmed.pop(channel.id, None)
            self._notify()
            for parameter, value in planned:
                await self._invoke(channel, node, "set", {"parameter": parameter, "value": value})
            await self._read(channel, node)
