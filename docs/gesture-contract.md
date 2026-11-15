# Gesture Bridge wire contract 1.x

One HA `event.*` entity represents one configured control. The stock coordinator
forwards its HA state object in `event.data.new_state`; the initial REST response
is the state object directly. The C4 consumer must filter `entity_id` first.

The executable schema is [gesture-v1.schema.json](../contracts/gesture-v1.schema.json).
The cross-field validator is [validate_gesture_contract.py](../tools/validate_gesture_contract.py).
The shared [fixture sequence](../tests/fixtures/gestures/v1.json) is consumed by Python
and Lua tests. These are protocol examples, not physical-device compatibility evidence.

## Current metadata

| Attribute | Meaning |
| --- | --- |
| `bridge_contract_version` | `major.minor`; version 1 consumers accept additive minor versions and reject another major |
| `bridge_control_id` | Stable configuration identity, independent of display labels |
| `bridge_session_id` | New opaque identity on each integration load; never restored from a prior run |
| `bridge_update_kind` | `gesture` for a real new occurrence; `snapshot` for initialization, load state, availability, labels, capabilities, and recovery |
| `bridge_buttons` | Array of `{id, label, gestures}`; each gesture declares `{id, kind, label}` and `press_count` for presses |
| `bridge_sources` | Array of `{id, ready, availability}`; availability is `available`, `unavailable`, or `unknown` |
| `bridge_load` | Null or `{entity_id, domain, state, available}` for an explicitly linked `switch` or `input_boolean` (1.0); light metadata is added in 1.1 |

Button and gesture IDs are lowercase identifiers with underscores. They are stable
keys; changing a label or list order must not change them. Control/session IDs are
opaque ASCII letters, digits, underscores, or hyphens. Sequence/count values are
positive integers no larger than 2^53-1, so Lua's numeric representation remains exact.

The HA `state` is `unknown` before the first gesture, otherwise the last-gesture
timestamp. `last_updated` orders HA state revisions, including load-only snapshots.
Extra HA fields such as `friendly_name` are allowed. Live input sources can remain
usable when the linked load is unavailable. A ready subscription with no upstream
availability signal reports physical-device availability as `unknown`.

## Optional light load extension (1.1)

A `load_entity_id: light.*` publishes `bridge_contract_version: "1.1"`.
Its `bridge_load` also has `attributes`, an object containing only the optional
`supported_color_modes` array, `supported_features` unsigned 32-bit integer,
and finite `brightness` number from 0 through 255. Missing fields do not grant
capabilities. Modes use HA light mode names; malformed values are rejected.
Switch/input_boolean aggregates continue to publish their unchanged 1.0 shape.

This lets the native Dimmer use a single aggregate for gestures and a dimmable
linked HA load. Brightness/capability/availability updates are snapshots and
preserve the last gesture's timestamp and sequence. The original consumer's
1.0 switch/input_boolean behavior is unchanged. Light linking does not imply
continuous dimming, LED support or a verified DeviceSide backend.

## Last actual gesture

| Attribute | Meaning |
| --- | --- |
| `event_type` | `press`, `hold_start`, `hold_repeat`, `release`, or `custom`; null before the first gesture |
| `button_id` | A declared stable button ID |
| `gesture_id` | A declared gesture ID; presses use `press_1`, `press_2`, and so on |
| `press_count` | Positive count for presses only; never attached to another kind |
| `gesture_session_id` | Session in which this occurrence was produced |
| `gesture_sequence` | Strictly increasing within that control's producer session |
| `gesture_event_id` | Exact string `<gesture_session_id>:<gesture_sequence>` |
| `gesture_occurred_at` | Timezone-aware occurrence/receipt timestamp |

For a top double press: `button_id=top`, `gesture_id=press_2`, `event_type=press`,
`press_count=2`. A custom action has an explicitly configured gesture ID and a
`custom` kind. Nothing infers multi-taps from the switched load's state.

## Occurrences and snapshots

Only a live subscription update marked `gesture`, with matching current and
gesture sessions and a valid declared capability, is eligible to fire. Initial
state responses never fire. Snapshot updates do not change last-gesture fields or
the HA last-gesture timestamp, even when they carry a press the consumer missed
during disconnection. On restart, restored last-gesture fields may refer to an old
session; current metadata marks the publication as a snapshot.

Consumers baseline initial state, reject duplicate/older sequences, and never
move their state revision backwards for a delayed response. Two identical fresh
double presses have distinct sequences and timestamps and must both be processed.
Sequence gaps are diagnostic information, not a request to replay offline scenes.

## Validate examples

From the repository root:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m tools.validate_gesture_contract
.venv/bin/python -m pytest tests/test_contract.py
```

The fixture generator is `python -m tools.generate_gesture_fixtures`. Fixture tests
cover missing fields, wrong types, inconsistent session/event IDs, duplicate
capabilities, invalid load targets, restored history, and repeated presses.
