# Independent LED behavior audit: 2026-11-15

Scope: explicit HA LED behaviors, color/mode separation, C4 properties and
programming, metadata authorization and actual service roundtrip. Findings found
during review were fixed with regressions; no outstanding findings remain.

## 1. Architecture & modularity

The shared service accepts independent color, brightness and behavior fields.
All automatic profiles, standard Indicators and manual mappings use that
contract. The C4 main property and per-channel action share validated choices;
global properties use the capability intersection, individual actions the union.

## 2. Security & error handling

Command version 2 is required before C4 can issue hardware requests, preventing
old companion semantics from silently changing modes. HA validates every field
before the first write and rechecks ownership/capabilities after awaited calls.
Black requires explicit off behavior. Fixed-color devices cannot receive an
independent color command. No outstanding findings.

## 3. Performance & scalability

Existing serialization, bounded discovery and cleanup remain shared. Startup,
reconnect and snapshots do not apply saved behaviors. Bound-device following
requires an explicit policy and fully supported metadata; unsupported saved
policies remain visible but inert. No outstanding findings.

## 4. Readability & maintainability

Visible LED Behavior uses the C4 concepts Follow Load and Follow Bound Device,
with explicit always-on/off choices and an inverted local-load option. Device
encodings are explicit, including ZEN78's reversed load modes. Color commands
never imply a behavior change.

## 5. Test simplification

401 HA tests pass, including every local Zooz mode preserving parameter 2 during
color-only changes. The real HA/C4 boundary verifies a color-only request leaves
power unchanged and an explicit behavior-only request changes it. Both Lua
runtimes test dynamic properties, multiple indicators, mixed capabilities,
old-version rejection and no startup/snapshot hardware writes.

## 6. Prioritised action plan

| Severity | Finding | Fix |
| --- | --- | --- |
| Medium | Per-channel programming originally intersected every indicator's behavior capabilities, hiding supported actions on mixed controls. | Use a union for per-channel action choices; retain intersection for global behavior. |
| Medium | A saved Follow Bound Device policy could remain active after changing to mixed metadata that did not support global following. | Require full capability support in quiet rendering and refresh paths; snapshots remain passive. |

All fixes are included. Deploy the companion API and C4 packages together.
Office startup preserves parameter 2 at zero; physical behavior changes remain
explicit user actions.
