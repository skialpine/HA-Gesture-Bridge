"""Bounded gesture acknowledgments measured entirely on HA's monotonic clock.

This is opt-in at the receiver. It is not an execution backend or a one-way
latency estimator: device classification and physical light response are outside
the measured publish -> receiver -> acknowledgment interval.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from copy import deepcopy
import logging
import math
from time import monotonic

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .config import DOMAIN

_LOGGER = logging.getLogger(__name__)
SERVICE_ACKNOWLEDGE = "acknowledge"
MAX_PENDING = 64
MAX_SAMPLES = 32
MAX_RECEIVERS = 8
PENDING_TTL = 30.0
MAX_INTEGER = 2**53 - 1


def _number(value):
    if type(value) not in (int, float) or not 0 <= value <= MAX_INTEGER or not math.isfinite(value):
        raise vol.Invalid("Expected a finite, non-negative millisecond value")
    return value


def _sequence(value):
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise vol.Invalid("Expected a positive integer gesture sequence")
    return value


ACK_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("bridge_session_id"): vol.All(str, vol.Match(r"^[A-Za-z0-9_-]{1,128}$")),
    vol.Required("gesture_sequence"): _sequence,
    vol.Required("receiver"): vol.All(str, vol.Match(r"^[A-Za-z0-9:_-]{1,128}$")),
    vol.Required("c4_received_epoch_ms"): _number,
    vol.Required("c4_processed_epoch_ms"): _number,
    vol.Optional("c4_forwarded_epoch_ms"): _number,
    vol.Required("c4_processing_ms"): vol.All(_number, vol.Range(max=60000)),
})


class LatencyTracker:
    """Keep short-lived correlation data; never write to the HA state machine."""

    def __init__(self):
        self.entities = {}
        self.pending = OrderedDict()
        self.samples = deque(maxlen=MAX_SAMPLES)

    @callback
    def register(self, entity):
        self.entities[entity.config.control_id] = entity

    @callback
    def unregister(self, entity):
        identity = entity.config.control_id
        if self.entities.get(identity) is not entity:
            return
        self.entities.pop(identity)
        for key in list(self.pending):
            if key[0] == identity:
                self.pending.pop(key)

    def _expire(self, now):
        while self.pending:
            key, record = next(iter(self.pending.items()))
            if now - record["monotonic"] <= PENDING_TTL:
                break
            self.pending.pop(key)

    @callback
    def published(self, entity, sequence, published_utc):
        now = monotonic()
        self._expire(now)
        key = (entity.config.control_id, entity._session_id, sequence)
        self.pending[key] = {"monotonic": now, "published_utc": published_utc, "receivers": set()}
        while len(self.pending) > MAX_PENDING:
            self.pending.popitem(last=False)

    @callback
    def acknowledge(self, data):
        """Validate a fresh occurrence and record an ack without firing events."""
        now = monotonic()
        self._expire(now)
        entity = next((item for item in self.entities.values() if item.entity_id == data["entity_id"]), None)
        if entity is None:
            raise ServiceValidationError("Gesture control is not currently loaded")
        key = (entity.config.control_id, data["bridge_session_id"], data["gesture_sequence"])
        record = self.pending.get(key)
        if record is None or entity._session_id != data["bridge_session_id"]:
            raise ServiceValidationError("Gesture acknowledgment is stale or unknown")
        receiver = data["receiver"]
        if receiver in record["receivers"]:
            raise ServiceValidationError("This receiver already acknowledged the gesture")
        if len(record["receivers"]) >= MAX_RECEIVERS:
            raise ServiceValidationError("Too many receivers acknowledged this gesture")
        received, processed = data["c4_received_epoch_ms"], data["c4_processed_epoch_ms"]
        forwarded = data.get("c4_forwarded_epoch_ms")
        record["receivers"].add(receiver)
        sample = {"gesture_sequence": data["gesture_sequence"],
                  "ha_published_utc": record["published_utc"],
                  "ha_ack_received_utc": dt_util.utcnow().isoformat(timespec="milliseconds"),
                  "round_trip_ms": round((now - record["monotonic"]) * 1000, 3),
                  "c4_received_epoch_ms": received, "c4_processed_epoch_ms": processed,
                  "c4_processing_ms": data["c4_processing_ms"]}
        # UTC may step independently of the receiver's monotonic processing
        # timer. Keep the evidence and flag the mismatch without affecting RTT.
        if (abs(processed - received - data["c4_processing_ms"]) > 5
                or forwarded is not None and not received <= forwarded <= processed):
            sample["c4_clock_discontinuity"] = True
        if forwarded is not None:
            sample["c4_forwarded_epoch_ms"] = forwarded
        self.samples.append((entity.config.control_id, sample))
        # Only fixed numeric/timestamp diagnostic fields enter the log. Entity,
        # control/session/receiver IDs, names and source payloads are omitted.
        _LOGGER.info("Gesture latency acknowledgment: %s", sample)

    @callback
    def diagnostics(self, control_ids):
        """Use anonymous indexes, retaining diagnostics' household-ID allowlist."""
        indexes = {identity: index for index, identity in enumerate(sorted(control_ids), 1)}
        return [{"control_index": indexes[identity], **deepcopy(sample)}
                for identity, sample in self.samples if identity in indexes]


@callback
def async_setup_latency(hass: HomeAssistant) -> LatencyTracker:
    """Register once for both UI controls and the backwards-compatible YAML path."""
    runtime = hass.data.setdefault(DOMAIN, {})
    if tracker := runtime.get("latency"):
        return tracker
    tracker = runtime["latency"] = LatencyTracker()

    async def handle_acknowledgment(call: ServiceCall):
        tracker.acknowledge(call.data)

    hass.services.async_register(DOMAIN, SERVICE_ACKNOWLEDGE, handle_acknowledgment, schema=ACK_SCHEMA)
    return tracker
