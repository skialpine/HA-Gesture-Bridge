"""Shared bounded configuration values, without platform or runtime imports."""

import re
from typing import Any

MAX_EXACT_INTEGER = 2**53 - 1
KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ENTITY = re.compile(r"([a-z_]+)\.[a-z0-9_]+\Z")


def text(value: Any, label: str, pattern: re.Pattern | None = None) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{label} must be a non-empty string of at most 128 characters")
    if pattern and not pattern.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def entity_id(value: Any, domains: set[str]) -> str:
    if not isinstance(value, str) or not (match := ENTITY.fullmatch(value)) or match[1] not in domains:
        raise ValueError(f"Expected an entity in {', '.join(sorted(domains))}")
    return value

