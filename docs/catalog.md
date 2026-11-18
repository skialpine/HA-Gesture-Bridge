# Discovery catalog

An installation creates one `sensor.ha_gesture_bridge_catalog`, even when no gesture controls are configured. Its registry unique ID is `ha_gesture_bridge_catalog`. Renaming the entity in HA preserves that identity; consumers should allow a different entity ID.

The sensor state is the number of catalog rows. Its attributes are:

| Attribute | Meaning |
| --- | --- |
| `catalog_contract_version` | Schema version, currently `1.0`. Consumers should reject unsupported major versions and ignore unfamiliar additive fields. |
| `catalog_revision` | SHA-256 hash of the canonical row content. Changes only when projected metadata changes. |
| `catalog_entities` | Preferred device records sorted by exact entity ID. Equivalent ordinary representations are replaced by their most capable entity. |
| `catalog_names_version` | Additive names-only transport version, currently integer `1`. |
| `catalog_names_revision` | SHA-256 hash of the names-only rows; unaffected by load state or capabilities. |
| `catalog_names_total` | Number of names-only rows, equal to the sensor state/full catalog count. |
| `catalog_names_pages` | Number of byte-bounded names pages (zero for an empty catalog or explicit error). |
| `catalog_names_page_NNN` | A list of names-only rows for page `001` through `064`; only existing pages are published. |
| `catalog_names_error` | Optional `record_too_large` or `catalog_limit`; the names projection is unavailable without truncation. |

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
| `entity_aliases` | Optional sorted entity IDs replaced by this preferred picker entry. These are selection aliases, not additional controlled loads. |
| `source_entity_id` | Optional physical relay/light representation used for UI classification; independent of the controlled load. |
| `is_group` | Optional `true` when actual registry/state data identifies a group. |

Gesture rows additionally contain `bridge_control_id`, `load_entity_id`, and `load_domain`. The latter two are `null` when the control has no valid linked-load snapshot. Multiple controls may link to the same load; identities remain distinct.

## Eligibility and capability projection

Only enabled entities in HA's entity registry are considered. Disabled devices are excluded. Registered `switch` and `input_boolean` entities appear as `switch`. Registered `light` entities appear as `dimmer` only when their advertised `supported_color_modes` explicitly includes a known brightness-capable mode (`brightness`, `color_temp`, `hs`, `xy`, `rgb`, `rgbw`, `rgbww`, or `white`). Other lights remain conservative on/off `switch` choices. A missing or malformed capability does not grant dimming. Valid stored registry capabilities remain useful while a device is unavailable.

Light `attributes` may include `supported_color_modes` (a list of recognized distinct strings) and `supported_features` (a nonnegative integer). Arbitrary source attributes and brightness values are omitted.

Only `event` entities registered by `ha_gesture_bridge` with valid version-1 bridge capability metadata become `gesture` rows. Their `attributes` contain:

- `bridge_contract_version`, `bridge_control_id`, and typed `bridge_buttons` declarations from the gesture contract.
- `bridge_sources`: source `id`, boolean `ready`, fixed `availability` enum, and optional `button_id`. Error text and source configuration are omitted.
- `bridge_load`: `null` or a typed linked-load snapshot with exact entity ID, domain, state, boolean availability, and the same light capability projection.

A gesture control may stay HA-available while its upstream sources are lost. The catalog therefore derives readiness from `bridge_sources`. Any ready source makes the control selectable as ready; a ready source with known available status establishes availability. When all reporting sources are unavailable, the row is unavailable. Sources that cannot prove physical availability retain `unknown`, even when their listeners are ready. A native event source can be ready before its first physical click.

## Preferred device representation

HA chooses the preferred entries using its entity and device registries. A verified, non-inverted switch-as-light wrapper replaces the underlying switch. A gesture control replaces the ordinary representations of its physical source when the source device has one unambiguous eligible load component. Configuration and diagnostic entities do not count as source-load candidates. The preferred gesture entry uses the physical source name and area and lists the replaced entity IDs in `entity_aliases`.

The physical source association takes precedence over a separate linked HA target. A C4 light group controlled by a wall switch therefore remains a separate light entry. When no physical source relation is available, an explicit linked-load relation may consolidate that load with its gesture control. Groups are never inferred from names, and two independently addressable endpoints or several controls for one source remain separate when there is no unique preferred control.

These are discovery decisions only. `source_entity_id` and aliases never invent or authorize an HA load: only `bridge_load` describes an explicitly configured controlled load. Consumers should validate the preferred entity against their current native discovery before applying aliases. If the preferred entity is missing or incompatible, retain the ordinary native selection.

## Updates and consumer behavior

Entity, device, and area registry updates and relevant state changes refresh the catalog. Refreshing never calls a device service, creates a gesture, or changes an aggregate's event identity. The revision excludes event timestamps, gesture sequences, occurrence IDs, raw source payloads, brightness, and arbitrary attributes. Repeated physical gestures therefore do not churn discovery metadata.

The live `catalog_entities` list and all names-page attributes are excluded from Recorder history to avoid large attribute records. The sensor count, contract versions, revisions and small names manifest remain recordable. Consumers read the current sensor attributes for discovery.

Keep manual exact entity ID entry available alongside a friendly-name picker. Show domain/area/ID where needed to disambiguate names, preserve unavailable existing selections, and refresh options from the catalog rather than silently replacing a selection. Treat capability and availability changes as metadata, never as button presses.

The catalog contains household names and identifiers for authorized local discovery. Downloaded diagnostics intentionally contain anonymous counts and fixed categories: configured control/source counts, source types, linked load domains, invalid control count, aggregate/source readiness and availability counts, unmapped action count, and error count. Diagnostics omit all household IDs/names, mappings, topics, payloads, credentials, arbitrary metadata, and raw error text.

Opt-in [latency acknowledgments](latency.md) additionally expose bounded numeric/UTC timestamp samples in downloaded diagnostics. They retain anonymous control indexes and gesture sequences for local correlation while still omitting household identifiers and payloads. These diagnostic timings are not added to the discovery catalog.

## Bounded names transport

Receivers needing only names/areas should first request the names manifest, then request one `catalog_names_page_NNN` attribute at a time together with the manifest. Each row contains `entity_id`, `domain`, `friendly_name` and nullable `area_name`, plus the optional `entity_aliases`, `source_entity_id` and `is_group` preference fields. Capability and live state payloads are omitted. Names are sorted by exact ID and are not shortened. Each page's compact, ASCII-escaped JSON occupies at most 16 KiB, leaving room for a 32 KiB proxy response envelope. The projection supports at most 10,000 rows, 64 pages and 512 KiB of compact names data; a larger record/projection reports an explicit error and retains the original full catalog.

Validate the same names revision, total and page count on every response, reject duplicate/missing IDs, and publish only after all pages have passed validation. A rename or area edit during transfer invalidates the whole enrichment; a load merely turning on/off does not change the names revision. Empty catalogs use total zero and no pages. Errors or incompatible versions leave native discovery usable.

The Control4 consumer releases each completed discovery snapshot before fetching the next page with the additive `HA_RELEASE_ENTITIES` command. Other consumers may continue reading `catalog_entities`. Both representations describe the same preferred choices and leave gesture/load authorization unchanged.


## Picker service

The admin-only `ha_gesture_bridge.get_devices` service returns a bounded
read-only list of physical device choices and native light and switch groups.
The version 1 envelope contains `revision`, `total`, `pages`, `page` and
`unchanged`. Request page 1 without a revision to read a fresh list. Send that
revision when requesting later pages. If the list changes during a transfer, the
service returns `snapshot_changed`. Sending a matching revision on page 1 gives
an unchanged confirmation without `entities`.

Starting with HA Gesture Bridge 0.1.3, `eligibility_version: 1` accompanies every
response, including unchanged confirmations. It guarantees that the service excludes disabled devices and
disabled, hidden, configuration or diagnostic entities. It excludes Control4
exports by entity platform, entity config entry, device identifier namespace
or device config entry, including lights that wrap exported switches. Exact
non-inverted switch-as-light wrappers can collapse with eligible switches.
When Home Assistant hides a wrapped switch, its visible wrapper can retain its
physical device association. A hidden registry entry is ineligible as a
selection alias.

Changes to the published choices affect the revision, including changes to
their eligibility. The advertised eligibility applies to that revision and is
separate from driver-family capabilities and current control readiness. Older
helpers omit the field. Consumers can use their existing verification path
with an absent or unsupported eligibility version.
