# Configure input sources

Use native HA event entities when available. The UI's native profile intersects recognized mappings with each entity's advertised `event_types`. The separate upper/lower selectors preserve stable `top`/`bottom` identities. Manual JSON allows custom event names, additional buttons or another input interface. JSON in the advanced UI is a control object (or the source list where explicitly requested); the UI validates it before saving.

Complete configuration examples are in [source-profiles.yaml](../examples/source-profiles.yaml). They are examples of supported interfaces, not claims that any particular device model supports every listed gesture. Replace the source identities and action names with your actual device's reports.

## Fields shared by source adapters

A control has `unique_id`, `name`, optional `load_entity_id` and either the legacy native shape or a `sources` list. Do not mix shapes. Each source has a unique `id`, `type`, a `buttons` list with stable IDs/labels, source `filters` and explicit `mappings`. A mapping has `button_id`, `match` and `gesture`.

A gesture specifies `kind` and, for a press, `press_count`. Press IDs are stable `press_1`, `press_2`, etc. Custom actions need a stable explicit `id`. Source action labels can change without changing those identities.

Filters and matches use literal scalar equality and dotted field paths, including array positions such as `args.0`. Missing is distinct from JSON null; booleans are distinct from numeric values. No templates or executable expressions are evaluated. Overlapping rules or duplicate input paths are rejected. An explicit identity filter is required for generic bus sources.

## Native and shared native

Legacy shorthand uses `source_entity_id`, `button_id`, `button_label` and `actions`. A `buttons` list supports separate paddle entities. These forms remain compatible with existing development YAML.

Use `type: native_event` with one source entity and an `actions` convenience map, or `type: shared_native` with explicit matches on `event_type` and button-identifying attributes. Put a shared entity's buttons inside one source rather than subscribing to it repeatedly. The adapter baselines initialization/restoration and compares actual native occurrence timestamps so repeated fresh actions remain distinct.

## Z-Wave Central Scene

Use `type: zwave_js`, select the HA device and optionally filter endpoint, home ID and node ID. The adapter accepts Central Scene command class 91, property `scene`, and normalizes its numeric `property_key` as the `scene` match field. Scene numbers remain separate from the configured stable button IDs. It is manufacturer-independent.

An explicit mappings list declares the actual supported subset. The convenience profile is enabled by omitting mappings and assigning an integer `scene` to each button; it declares one through five taps, held and release. Choose it only when those reports are supported by the device. Native UI profiles are preferable when HA already advertises the actual event types. Enable scene reporting in the device using its manufacturer/firmware-specific settings.

## ZHA

Use `type: zha`, select the HA device, and filter endpoint/cluster as appropriate. Explicit mappings can match `command`, list arguments (`args.0`) or dictionary arguments (`args.press_type`). There is no universal ZHA command vocabulary; inspect the reports for your selected control and map only those actions. Fixtures cover the interface, not all Zigbee remotes.

## MQTT and Zigbee2MQTT

Use `type: mqtt` (alias `zigbee2mqtt`), an exact action `topic`, optional `availability_topic` and mappings such as `action: double`. JSON objects are read by field path. A plain string payload is exposed as both `action` and `payload`. Topics are literal; wildcards and using the same action/availability topic are rejected.

HA's existing MQTT integration supplies authentication and the broker connection. Retained actions are never physical gestures. Availability accepts plain online/offline payloads and Zigbee2MQTT's JSON state envelope; retained availability is metadata only. Without device availability, a ready subscription reports physical availability as unknown. Broker disconnect clears readiness and held bookkeeping; reconnect does not replay retained presses. No experimental event entity option is required.

## Other HA bus events

Use `type: bus`, an exact `event_type` and an identity `filters` map. This can represent Wi-Fi, Bluetooth or another integration that exposes button events. Avoid subscribing to a second representation of an already configured physical input. Generic `state_changed` sources and wildcard (`*`) event subscriptions are refused to prevent aggregate feedback cycles.

## Holds and lifecycle

A repeating held source produces one `hold_start`, then `hold_repeat` until a reported release or reset. Optional `inactivity_timeout` (0.1–3600 seconds) clears held bookkeeping only; it never publishes a fake release. Source removal, unknown tracking, reload and broker loss also clear held state. Profiles must not promise releases the source never sends.

Linked-load updates, capabilities, source readiness and catalog refreshes remain metadata snapshots. They do not increment the gesture sequence or operate the linked load.

## Registry identities and renames

UI-created/imported native sources save `source_registry_id` where HA has a registry entry. That UUID is authoritative across renames and reloads; deleting it does not bind a different device that reuses its former entity ID. Runtime-only legacy YAML sources capture a known registry identity and follow renames while loaded, but textual YAML cannot be rewritten automatically. Import them into the UI before relying on rename persistence across HA restarts, or maintain the YAML source ID yourself. Sources that have no registry entry retain explicit entity-ID behavior.
