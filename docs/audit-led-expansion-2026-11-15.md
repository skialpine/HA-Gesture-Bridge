# LED model expansion audit: 2026-11-15

Scope: multi-channel and optional LED parameter profiles, C4 channel selection,
native scene association and five-button programming. No outstanding findings.
Validation: 374 HA tests passed; 82 C4 tests passed with one optional runtime
check skipped. Both Lua runtimes passed the focused LED and driver assertions.

## 1. Architecture & modularity

Profiles share reviewed mode/color/brightness schemas. Optional capabilities and
multiple channels reuse the existing transport, service and wire contract.
The added GE/Jasco declarations reuse the same enum mode schema as Zooz. No
runtime source-tree parser, extra connection or gesture-core vendor logic.

## 2. Security & error handling

Hardware fingerprints and full native metadata constrain parameter writes:
configuration provenance, access, byte size, label, exact options, range and
allowed values. Contradictory semantic purposes and partial parameters fail
validation. Every awaited operation rechecks current ownership and topology.
No outstanding findings.

## 3. Performance & scalability

At most 16 indicators are advertised. Discovery examines cached values, while
background reads confirm the selected channels. Startup/reconnect never set
LEDs or loads. The existing serialized command and unsubscribe lifecycle is
shared across models. No outstanding findings.

## 4. Readability & maintainability

Official device definitions were checked for ZEN32's firmware-dependent color
and brightness tables and scene numbering, and GE/Jasco hardware identities.
C4 uses one shared selector/resolver, preserving native role strings and
resolving custom choices by stable channel suffix after label changes.
Monochrome summaries say single color rather than claiming physical white.

## 5. Test simplification

Boundary tests cover all five independent HA inputs, actual C4 programming
event registration/firing, five LED targets, unsupported brightness, alternate
firmware tables, native scene associations, source loss, ownership, schema
changes and cleanup. Existing Top/Bottom/Toggle programming remains covered.
No redundant timing sleeps or live hardware writes were added.

## 6. Prioritised action plan

No outstanding fixes. Deploy the HA profile/runtime pair together and load the
updated C4 packages to expose all custom indicator choices. Office discovery
and Edwards ZEN78 native metadata were checked against actual hardware; command
tests for the additional models remain simulated, not physical qualification.
