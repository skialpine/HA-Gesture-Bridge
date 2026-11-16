"""Explicit LED-only endpoint mappings; never infer a relay/load backend."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from .config_values import IDENTITY, KEY, MAX_EXACT_INTEGER, entity_id, text

_COLOR = re.compile(r"[0-9A-Fa-f]{6}\Z")


def color(value: Any) -> str:
    if not isinstance(value, str) or _COLOR.fullmatch(value) is None:
        raise ValueError("LED colors must be six hexadecimal digits")
    return value.upper()


def percent(value: Any) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 100 or not math.isfinite(value):
        raise ValueError("LED brightness must be a finite percentage from 0 through 100")
    return value


@dataclass(frozen=True)
class LEDEndpoint:
    entity_id: str
    values: dict[str, str | int | float]
    registry_id: str | None = None

    @classmethod
    def parse(cls, data: Any, kind: str) -> LEDEndpoint:
        fields = {"entity_id", "registry_id"} | ({"on", "off"} if kind == "power" else {"values"})
        if not isinstance(data, dict) or set(data) - fields:
            raise ValueError("Unknown or invalid LED endpoint configuration")
        target = entity_id(data.get("entity_id"), {"select", "number"})
        registry_id = text(data["registry_id"], "LED registry ID", IDENTITY) if "registry_id" in data else None
        raw = {key: data.get(key) for key in ("on", "off")} if kind == "power" else data.get("values")
        if not isinstance(raw, dict) or not raw:
            raise ValueError("Each LED endpoint needs explicit target values")
        if kind == "color" and len(raw) > 64:
            raise ValueError("An LED palette supports at most 64 colors")
        result = {}
        for key, value in raw.items():
            if target.startswith("select."):
                value = text(value, "LED select option")
            elif (type(value) not in (int, float) or not -MAX_EXACT_INTEGER <= value <= MAX_EXACT_INTEGER
                  or not math.isfinite(value)):
                raise ValueError("LED number values must be finite exact numbers")
            if value in result.values():
                raise ValueError("LED endpoint values must identify distinct options")
            if kind == "color":
                key = color(key)
                if key == "000000":
                    raise ValueError("Use a power endpoint for black/off instead of a color mapping")
            elif kind == "brightness":
                if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                    raise ValueError("LED brightness mapping keys must be integer percentages")
                percent(int(key))
            if key in result:
                raise ValueError("Duplicate LED mapping key")
            result[key] = value
        return cls(target, result, registry_id)


@dataclass(frozen=True)
class LEDConfig:
    id: str
    label: str
    button_id: str
    color: LEDEndpoint | None
    power: LEDEndpoint | None
    brightness: LEDEndpoint | None

    @classmethod
    def parse(cls, data: Any) -> LEDConfig:
        if not isinstance(data, dict) or set(data) - {"id", "label", "button_id", "color", "power", "brightness"}:
            raise ValueError("Unknown or invalid LED channel configuration")
        endpoints = {kind: LEDEndpoint.parse(data[kind], kind) if kind in data else None
                     for kind in ("color", "power", "brightness")}
        if not endpoints["color"] and not endpoints["power"]:
            raise ValueError("An LED needs a color or power endpoint")
        targets = [endpoint.entity_id for endpoint in endpoints.values() if endpoint]
        registries = [endpoint.registry_id for endpoint in endpoints.values() if endpoint and endpoint.registry_id]
        if len(set(targets)) != len(targets) or len(set(registries)) != len(registries):
            raise ValueError("An LED channel's color, power and brightness endpoints must be distinct")
        label = text(data.get("label"), "LED label")
        if any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise ValueError("LED labels must not contain control characters")
        return cls(text(data.get("id"), "LED ID", KEY), label,
                   text(data.get("button_id"), "LED button ID", KEY), **endpoints)

    @property
    def colors(self) -> list[str]:
        result = sorted(self.color.values) if self.color else ["FFFFFF"]
        return ["000000", *result] if self.power else result


def parse_leds(data: Any) -> tuple[LEDConfig, ...]:
    if not isinstance(data, list) or len(data) > 16:
        raise ValueError("leds must be a list of at most 16 channels")
    leds = tuple(LEDConfig.parse(item) for item in data)
    if len({led.id for led in leds}) != len(leds) or len({led.button_id for led in leds}) != len(leds):
        raise ValueError("LED IDs and logical button IDs must be unique")
    endpoints = [endpoint for led in leds for endpoint in (led.color, led.power, led.brightness) if endpoint]
    targets = [endpoint.entity_id for endpoint in endpoints]
    registries = [endpoint.registry_id for endpoint in endpoints if endpoint.registry_id]
    if len(set(targets)) != len(targets) or len(set(registries)) != len(registries):
        raise ValueError("LED channels must not reuse endpoint entities or registry identities")
    return leds
