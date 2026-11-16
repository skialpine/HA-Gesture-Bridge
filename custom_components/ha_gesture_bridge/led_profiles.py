"""Reviewed LED parameter templates bound to real device identities.

Z-Wave JS already resolves device/firmware definitions into value metadata. We
reuse that metadata, including its complete option tables, instead of inferring
LED purposes from arbitrary parameter labels. Sources are the official Z-Wave
JS device definitions and master/Zooz templates; LED purposes are not part of
its current parameter-purpose taxonomy.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.helpers import device_registry as dr


@dataclass(frozen=True)
class ParameterSchema:
    parameter: int
    label: str
    states: tuple[tuple[int, str], ...]

    def matches(self, value) -> bool:
        """Require the exact reviewed configuration definition, never a relay."""
        metadata = value.metadata
        expected = {str(key): label for key, label in self.states}
        if not (value.command_class == 112 and type(value.endpoint) is int and value.endpoint == 0
                and type(value.property_) is int and value.property_ == self.parameter
                and value.property_key is None and metadata.type == "number"
                and metadata.readable is True and metadata.writeable is True
                and metadata.is_from_config is True and metadata.purpose is None
                and not metadata.secret and metadata.label == self.label
                and type(metadata.value_size) is int and metadata.value_size == 1
                and type(metadata.min) is int and type(metadata.max) is int
                and metadata.min == min(key for key, _ in self.states)
                and metadata.max == max(key for key, _ in self.states)
                and metadata.states == expected):
            return False
        # A newer backend can advertise non-contiguous allowed values. A
        # contradictory constraint must not authorize a previously valid write.
        from .zwave_indicator import _permits
        return all(_permits(value, key) for key, _ in self.states)


@dataclass(frozen=True)
class ColorSetting:
    schema: ParameterSchema
    palette: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class BrightnessSetting:
    schema: ParameterSchema
    levels: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class LEDChannelProfile:
    id: str
    label: str
    button_id: str
    mode: ParameterSchema
    colors: tuple[ColorSetting, ...] = ()
    brightness: tuple[BrightnessSetting, ...] = ()
    scene_id: int | None = None
    on_value: int = 3
    off_value: int = 2
    follow_load_value: int = 1
    follow_load_inverted_value: int = 0


@dataclass(frozen=True)
class LEDProfile:
    manufacturer: str
    manufacturer_id: int
    model: str
    fingerprints: tuple[tuple[int, int], ...]
    channels: tuple[LEDChannelProfile, ...]
    manufacturer_aliases: tuple[str, ...] = ()
    model_aliases: tuple[str, ...] = ()

    def matches_node(self, node) -> bool:
        return (node.manufacturer_id == self.manufacturer_id
                and (node.product_type, node.product_id) in self.fingerprints)


_MODE = ((0, "On when load is off"), (1, "On when load is on"), (2, "Always off"), (3, "Always on"))
_RELAY_MODE = ((0, "LED on when relay on, LED off when relay off"),
               (1, "LED on when relay off, LED off when relay on"),
               (2, "LED always off"), (3, "LED always on"))
_COLORS = ((0, "White"), (1, "Blue"), (2, "Green"), (3, "Red"))
_EXTENDED_COLORS = (*_COLORS, (4, "Magenta"), (5, "Yellow"), (6, "Cyan"))
_BRIGHTNESS = ((0, "Bright (100%)"), (1, "Medium (60%)"), (2, "Low (30%)"))
_TEN_BRIGHTNESS = tuple((value, f"{value * 10}%") for value in range(1, 11))
_PALETTE = (("FFFFFF", 0), ("0000FF", 1), ("00FF00", 2), ("FF0000", 3))
_EXTENDED_PALETTE = (*_PALETTE, ("FF00FF", 4), ("FFFF00", 5), ("00FFFF", 6))


def _color(parameter, label="LED Indicator Color", *, extended=False):
    variants = (ColorSetting(ParameterSchema(parameter, label, _COLORS), _PALETTE),)
    if extended:
        variants += (ColorSetting(ParameterSchema(parameter, label, _EXTENDED_COLORS), _EXTENDED_PALETTE),)
    return variants


def _brightness(parameter, label="LED Indicator Brightness", *, ten_levels=False):
    variants = (BrightnessSetting(ParameterSchema(parameter, label, _BRIGHTNESS), ((100, 0), (60, 1), (30, 2))),)
    if ten_levels:
        variants += (BrightnessSetting(ParameterSchema(parameter, label, _TEN_BRIGHTNESS),
                                       tuple((value * 10, value) for value in range(1, 11))),)
    return variants


# One shared status LED. The original status/toggle identity is retained.
_PADDLES = tuple(LEDProfile("Zooz", 0x027A, model, ((0x7000, product),), (
    LEDChannelProfile("status", "Status LED", "toggle", ParameterSchema(2, "LED Indicator", _MODE),
                      _color(color), _brightness(brightness)),))
    for model, product, color, brightness in (
        ("ZEN71", 0xA001, 14, 15), ("ZEN72", 0xA002, 23, 24),
        ("ZEN76", 0xA006, 14, 15), ("ZEN77", 0xA007, 23, 24)))

_ON_OFF = tuple(LEDProfile("Zooz", 0x027A, model, ((0xA000, product),), (
    LEDChannelProfile("status", "Status LED", "toggle", ParameterSchema(2, "LED Indicator", _MODE)),))
    for model, product in (("ZEN26", 0xA001), ("ZEN27", 0xA002)))

_ZEN30 = LEDProfile("Zooz", 0x027A, "ZEN30", ((0xA000, 0xA008),), (
    LEDChannelProfile("dimmer", "Dimmer indicator", "dimmer", ParameterSchema(1, "LED Indicator Mode for Dimmer", _MODE),
        _color(3, "LED Indicator Color for Dimmer"), _brightness(5, "LED Indicator Brightness for Dimmer")),
    LEDChannelProfile("relay", "Relay indicator", "relay", ParameterSchema(2, "LED Indicator Mode for Relay", _MODE),
        _color(4, "LED Indicator Color for Relay"), _brightness(6, "LED Indicator Brightness for Relay"), scene_id=3),
))

# A ZEN32 relay-button indicator and four independently controllable small
# button indicators. Interviewed option tables select firmware variants.
_ZEN32 = LEDProfile("Zooz", 0x027A, "ZEN32", ((0x7000, 0xA008), (0xBB00, 0xBB08)), tuple(
    LEDChannelProfile(identity, label, identity, ParameterSchema(index + 1, f"LED Indicator ({suffix})", _MODE),
        _color(index + 6, f"LED Indicator Color ({suffix})", extended=True),
        _brightness(index + 11, f"LED Indicator Brightness ({suffix})", ten_levels=True), scene_id=scene)
    for index, identity, label, suffix, scene in (
        (0, "relay", "Switch button indicator", "Relay", 5),
        (1, "button_1", "Button 1 indicator", "Button 1", 1),
        (2, "button_2", "Button 2 indicator", "Button 2", 2),
        (3, "button_3", "Button 3 indicator", "Button 3", 3),
        (4, "button_4", "Button 4 indicator", "Button 4", 4))))

_ZEN78 = LEDProfile("Zooz", 0x027A, "ZEN78", ((0x0103, 0x004E),), (
    LEDChannelProfile("status", "Status LED", "toggle", ParameterSchema(15, "LED Indicator Behavior", _RELAY_MODE),
        follow_load_value=0, follow_load_inverted_value=1),))

# These official GE/Jasco definitions reuse the same upstream mode template.
# Registry aliases select candidates only; the live fingerprint authorizes it.
_JASCO = (
    LEDProfile("GE/Enbrighten", 0x0063, "46201 / ZW4008", ((0x4952, 0x3135),), (
        LEDChannelProfile("status", "Status LED", "toggle", ParameterSchema(3, "LED Indicator", _MODE)),),
        ("GE", "Enbrighten", "Jasco"), ("46201",)),
    LEDProfile("Jasco", 0x0063, "46562 / ZW4008", ((0x4952, 0x3136),), (
        LEDChannelProfile("status", "Status LED", "toggle", ParameterSchema(3, "LED Indicator", _MODE)),),
        ("GE/Enbrighten", "GE", "Enbrighten"), ("46562",)),
)

PROFILES = (*_PADDLES, *_ON_OFF, _ZEN30, _ZEN32, _ZEN78, *_JASCO)


def profile_for_device(hass, device_id) -> LEDProfile | None:
    """Select a reviewed candidate without I/O, including while it is offline."""
    device = dr.async_get(hass).async_get(device_id)
    if (device is None or device.disabled_by is not None
            or not any(domain == "zwave_js" for domain, _ in device.identifiers)):
        return None
    manufacturer = (device.manufacturer or "").strip().casefold()
    model = (device.model or "").strip().upper()
    return next((profile for profile in PROFILES
        if manufacturer in {name.casefold() for name in (profile.manufacturer, *profile.manufacturer_aliases)}
        and model in {name.upper() for name in (profile.model, *profile.model_aliases)}), None)


def profile_summary(hass, device_id) -> str:
    profile = profile_for_device(hass, device_id)
    if profile is None:
        return "No reviewed device-specific LED profile detected."
    from .zwave_parameters import discover_parameters
    channels = discover_parameters(hass, device_id, profile)
    if not channels:
        return f"{profile.manufacturer} {profile.model} LED profile found; waiting for supported device parameter metadata."
    features = ["on/off"]
    if any(channel.color is not None for channel in channels):
        features.append("color")
    if any(channel.brightness is not None for channel in channels):
        features.append("brightness")
    return f"{profile.manufacturer} {profile.model}: {len(channels)} status indicator{'s' if len(channels) != 1 else ''}; {', '.join(features)}."
