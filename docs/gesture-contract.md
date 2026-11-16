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

## Validated aggregate examples

These examples run through HA's actual config-entry and event platforms in
[`test_config_entry.py`](../tests/gesture_bridge/test_config_entry.py), using
fixture sources rather than physical hardware:

| Change | Published result |
| --- | --- |
| Rename a control, move `bottom` before `top`, and change their display labels | The registered aggregate entity, control ID, button IDs and `press_2` gesture IDs remain unchanged. Reload creates a new current session; any restored last gesture keeps its original identity and is marked `snapshot`. |
| Receive the first fresh bottom double press after that edit | The same aggregate publishes `button_id=bottom`, `gesture_id=press_2` and sequence `1` in its new current session. |
| Configure three controls and hold one while tapping the others | Each control has its own session and sequence. Repeating the first control's hold produces `hold_repeat`; it does not change either other control. |
| Lose one control's linked load and another control's input source | Only the respective load/source metadata changes. Last-gesture timestamps and IDs remain intact, and the first control still accepts its reported release while its load is unavailable. |

Linked loads remain observational metadata. These publications do not invoke
DeviceSide or any load-control action.

## Validate examples

From the repository root:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
.venv/bin/python -m tools.validate_gesture_contract
.venv/bin/python -m pytest tests/gesture_bridge/test_contract.py
```

The fixture generator is `python -m tools.generate_gesture_fixtures`. Fixture tests
cover missing fields, wrong types, inconsistent session/event IDs, duplicate
capabilities, invalid load targets, restored history, and repeated presses.

## Optional acknowledgment capability

HA Gesture Bridge 0.1.2 adds the optional integer attribute `bridge_acknowledgment_version: 2`. It does not change gesture identity or contract versions 1.0/1.1. A receiver may use the [diagnostic acknowledgment action](latency.md) only when explicitly enabled. Acknowledgments do not produce gesture/state publications or load commands; gesture validity never depends on clocks agreeing or an acknowledgment succeeding.

## Optional status LEDs

An aggregate with an automatic or explicitly configured LED backend adds `bridge_led_version: 1` and `bridge_leds`. This extension does not change the 1.x gesture contract. Automatic discovery may return an empty list when the selected device exposes no supported status LEDs. Aggregates without an LED backend omit both attributes. An empty list or unknown LED version must not enable receiver LED commands.

Each list item contains:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | stable key | Channel identity, retained when renamed |
| `label` | string | Display name, 1–128 Unicode characters without ASCII controls |
| `button_id` | stable key | Logical button associated with this channel, such as `top`, `bottom` or `toggle` |
| `available` | boolean | All required discovered or explicitly configured endpoints can currently be used |
| `colors` | list of uppercase six-hex strings | Exact supported palette, at most 64 nonblack colors plus optional `000000` |
| `brightness_supported` | boolean | The discovered or explicitly configured LED supports brightness |
| `color_supported` | boolean | The LED has an independently writable color setting |
| `behaviors` | list of stable strings | Supported explicit behaviors: `follow_load`, `follow_load_inverted`, `always_on`, `always_off` |
| `behavior` | optional stable string | Last confirmed device behavior; must be one of its supported behaviors |
| `color` | optional six-hex string | Last confirmed mapped color; black requires explicit confirmed power-off |
| `brightness` | optional number 0–100 | Last confirmed mapped percentage |

At most 16 channels are advertised. Channel IDs and logical button IDs are unique within a control. IDs use `[a-z][a-z0-9_]{0,63}`. A missing current color/brightness is unknown, not zero. A device profile must validate its actual reported parameter metadata before granting control; a matching model name alone is insufficient. Receivers must not infer capabilities from model, label or a related load. Metadata omits backend HA entity IDs and option names.

Receivers call `ha_gesture_bridge.set_led` with `entity_id` (aggregate), `led_id`, and at least one of `color`, `brightness` or `behavior`.

- Color-only changes the independent color endpoint and preserves mode/power. Fixed-color indicators do not support color-only commands.
- Brightness-only preserves behavior. An intensity-only standard indicator rejects an implicit power transition unless behavior is explicit.
- Behavior-only changes the mode/power endpoint and preserves color. Follow-load behaviors are advertised only by profiles with reviewed local-load mode semantics.
- A combined request explicitly changes each supplied setting. Black (`000000`) requires `behavior: always_off`, preserves the stored color, and represents power off rather than a fabricated black color parameter.

HA accepts exact palette colors, normalizes hex case and chooses the nearest supported brightness (ties lower). A fixed-color indicator accepts its logical white/on token only with explicit `always_on`; behavior-only commands are preferred for such devices. All writes are prevalidated before the first endpoint call and awaited sequentially, without optimistic feedback. Confirmed LED updates are snapshots, never fresh gestures.

C4 bound-device following is a receiver policy, not a Zooz hardware mode. The receiver sends an explicit `always_on` plus the chosen nonblack on/off color, or `always_off` for a chosen black color, after actual connected-device feedback. Changing saved on/off colors alone does not issue a device command. Setup, reconnect and snapshots never reapply a behavior or send LED commands.

See [LED setup](leds.md) for configuration examples, registry pinning and failure behavior.


### Physical source discovery

`bridge_source_device_id` is an optional HA device-registry identity for the physical input selected in the integration. It is distinct from `bridge_load`, which describes an explicitly controlled HA load. The HA discovery catalog uses this registry identity to choose a single most-capable picker entry for an unambiguous physical source, and publishes replaced entity IDs as selection aliases. Consumers follow the catalog's preferred choices rather than re-inferring physical identity from names or areas. Independently addressable endpoints and multiple controls remain separate when the association is ambiguous. This discovery hint never authorizes a relay command or changes gesture occurrence identity. See [the catalog contract](catalog.md).
