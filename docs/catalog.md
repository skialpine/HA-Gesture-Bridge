# Discovery catalog

An installation creates one `sensor.ha_gesture_bridge_catalog`, even when no gesture controls are configured. Its registry unique ID is `ha_gesture_bridge_catalog`. Renaming the entity in HA preserves that identity; consumers should allow a different entity ID.

The sensor state is the number of catalog rows. Its attributes are:

| Attribute | Meaning |
| --- | --- |
| `catalog_contract_version` | Schema version, currently `1.0`. Consumers should reject unsupported major versions and ignore unfamiliar additive fields. |
| `catalog_revision` | SHA-256 hash of the canonical row content. Changes only when projected metadata changes. |
| `catalog_entities` | Deterministic list of records sorted by exact entity ID. |

Each row has the following fields:

| Field | Type and meaning |
| --- | --- |
| `entity_id` | Exact HA entity ID; use this identity when making a selection. |
| `domain` | `switch`, `input_boolean`, `light`, or `event`. |
| `kind` | `switch`, `dimmer`, or `gesture`. |
| `friendly_name` | Readable HA name, with registry/device/ID fallback. Names need not be unique. |
| `area_name` | Entity area name, otherwise device area name, otherwise `null`. |
| `device_id` | Associated HA registry device ID, or `null`. |
| `state` | Loads: `on`, `off`, `unknown`, or `unavailable`. Gestures: `ready`, `unknown`, or `unavailable`. |
| `availability` | `available`, `unavailable`, or `unknown`. Unknown is not evidence of availability. |
| `available` | `true`, `false`, or `null`, corresponding to the availability enum. |
| `ready` | Loads use the same nullable status as `available`. Gesture controls use a boolean: at least one input reports ready. |
| `attributes` | Allowlisted typed capability metadata, described below. |

Gesture rows additionally contain `bridge_control_id`, `load_entity_id`, and `load_domain`. The latter two are `null` when the control has no valid linked-load snapshot. Multiple controls may link to the same load; identities remain distinct.

## Eligibility and capability projection

Only enabled entities in HA's entity registry are considered. Disabled devices are excluded. Registered `switch` and `input_boolean` entities appear as `switch`. Registered `light` entities appear as `dimmer` only when their advertised `supported_color_modes` explicitly includes a known brightness-capable mode (`brightness`, `color_temp`, `hs`, `xy`, `rgb`, `rgbw`, `rgbww`, or `white`). Other lights remain conservative on/off `switch` choices. A missing or malformed capability does not grant dimming. Valid stored registry capabilities remain useful while a device is unavailable.

Light `attributes` may include `supported_color_modes` (a list of recognized distinct strings) and `supported_features` (a nonnegative integer). Arbitrary source attributes and brightness values are omitted.

Only `event` entities registered by `ha_gesture_bridge` with valid version-1 bridge capability metadata become `gesture` rows. Their `attributes` contain:

- `bridge_contract_version`, `bridge_control_id`, and typed `bridge_buttons` declarations from the gesture contract.
- `bridge_sources`: source `id`, boolean `ready`, fixed `availability` enum, and optional `button_id`. Error text and source configuration are omitted.
- `bridge_load`: `null` or a typed linked-load snapshot with exact entity ID, domain, state, boolean availability, and the same light capability projection.

A gesture control may stay HA-available while its upstream sources are lost. The catalog therefore derives readiness from `bridge_sources`. Any ready source makes the control selectable as ready; a ready source with known available status establishes availability. When all reporting sources are unavailable, the row is unavailable. Sources that cannot prove physical availability retain `unknown`, even when their listeners are ready. A native event source can be ready before its first physical click.

## Updates and consumer behavior

Entity, device, and area registry updates and relevant state changes refresh the catalog. Refreshing never calls a device service, creates a gesture, or changes an aggregate's event identity. The revision excludes event timestamps, gesture sequences, occurrence IDs, raw source payloads, brightness, and arbitrary attributes. Repeated physical gestures therefore do not churn discovery metadata.

Keep manual exact entity ID entry available alongside a friendly-name picker. Show domain/area/ID where needed to disambiguate names, preserve unavailable existing selections, and refresh options from the catalog rather than silently replacing a selection. Treat capability and availability changes as metadata, never as button presses.

The catalog contains household names and identifiers for authorized local discovery. Downloaded diagnostics intentionally contain only anonymous counts and fixed categories: configured control/source counts, source types, linked load domains, invalid control count, aggregate/source readiness and availability counts, unmapped action count, and error count. Diagnostics omit all household IDs/names, mappings, topics, payloads, credentials, arbitrary metadata, and raw error text.
