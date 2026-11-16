# On-demand preparation audit: 2026-11-16 (HEAD 9a029c9)

## Resolution and follow-up verification

All four findings below are resolved. Feature baseline: `9e445cd`.
Preparation race, provider error classification and shared native validation:
`01c20d1`. Catalog membership simplification: `ae09e62`.

Three independent follow-up reviews covered preparation, catalog and tests/build;
no remaining verified findings were found within this scope. The full HA suite
passes **444 tests** with 5 dependency warnings, including the real C4 consumer
boundary; the contract validator accepts all 17 version-1 states. The two new
regressions exercise an actual manual ConfigFlow completing during service
discovery, and a temporary provider failure without creating a control.

The rebuilt 29-entry archive has SHA-256
`29b43428b8777054159bfe9d2a29889cd9a4604b6bbbdf4d09edc445ab36f7bb`.
CHV deployment on November 16 replaced only the four changed runtime files,
read back all 27 text files against the tested source, then restarted HA.
The integration returned loaded with the same eight subentry and control IDs.
All 12 source records are ready, all eight publications are snapshots, and all
bridge error counters are zero. The device catalog reports version 1, 35 choices
and one page. No integration error was found in startup logs; HA's ordinary
custom-integration loader warning remains. Physical presses were not simulated.

Rollback code snapshot (27 text files) is retained outside the source tree as
`Control4-HA-Drivers/backups/ha-code-before-audit-fixes-20261116T214911Z.zip`,
SHA-256 `03a175538a6a4ce03453d9961f34809aca927bcc6bb56a1e5365d87bf5889563`.
No HA integration configuration, entity identity or device parameter was changed
by this deployment. The original audit below is retained as historical evidence.

The authorization, pinned-source identity and read-only catalog design are sound.
This audit found **2 Medium and 2 Low findings; no Critical or High findings**.
The principal defect is a duplicate-control race between automatic preparation
and the manual HA setup flow.

Scope: current uncommitted changes related to on-demand device activation,
10 tracked files (+159/-43), plus new preparation/catalog modules, their tests
and activation fixture exporter. Changed functions and relevant callers were
read in full. The review followed the Switch repository's
`.claude/skills/audit-code/SKILL.md`, README, catalog/source documentation and
the on-demand OpenSpec design. No in-scope AGENTS.md or CLAUDE.md was found.

Verification: **442 tests passed, 5 dependency warnings, 10.18 seconds**, with
`C4_HA_SWITCH_CHECKOUT=../Control4-HA-Switch` enabled. The slowest test took 0.31
seconds; no real sleep or live network was needed. Additional temporary-HA
probes independently reproduced both Medium findings. `git diff --check` passes.
This audit changed no runtime code, deployed service, package or commit.

## 1. Architecture & modularity

| ID | Severity | Location | Finding |
| --- | --- | --- | --- |
| L2 | Low `simplify` | `custom_components/ha_gesture_bridge/device_catalog.py:41`, `device_choices` | `existing` separately enumerates the same source-device keys immediately collected by `controls`. |

Both traversals use the same integration entries and control subentries.
Remove the redundant set comprehension and use membership in `controls` at
the two existing membership checks. This avoids duplicate registry traversal
without introducing new state.

Keep the shared wrapper-equivalence helpers and separate selected endpoint /
controlled-load identities. Ordinary browsing does not enumerate physical
automation providers, attach inputs, create controls or write device parameters.

## 2. Security & error handling

| ID | Severity | Location | Finding |
| --- | --- | --- | --- |
| M1 | Medium | `custom_components/ha_gesture_bridge/preparation.py:170–207`, `Preparation._enroll` | Existing-control lookup precedes awaited provider discovery, without another lookup before creating a subentry. Manual setup can finish during that await and produce a duplicate physical control. |
| M2 | Medium | `custom_components/ha_gesture_bridge/device_discovery.py:190–191`, `discover_device`; `preparation.py:194–195`, `_enroll` | A temporarily unavailable trigger provider becomes generic ValueError and is reported as unsupported device configuration. |

**M1 reproduction:** use the actual HA service and ConfigFlow managers, pause
only service-side discovery, and complete manual Add control for the same device.
Release discovery. The independently rerun probe reported:

```text
manual save create_entry controls 1
prepare complete controls 2 distinct IDs 2
next prepare ServiceValidationError conflicting_controls
```

The automatic record receives a new UUID, so HA's unique-ID check does not
prevent this duplicate. Concurrent service calls share a task, but the manual
flow does not participate in that service-task map. Its own final uniqueness
check protects only the manual save path.

**M2 reproduction:** in a temporary actual HA instance, make the trigger provider
raise `HomeAssistantError("provider temporarily unavailable")`. The preparation
service returns `ServiceValidationError unsupported_device_configuration` with
zero persisted controls. The documented protocol distinguishes temporary source
metadata loss from unsupported topology; the current result misdirects setup
troubleshooting even though the provider can recover.

No exploitable authorization defect was verified. HA's standard admin-service
gate precedes inspection and mutation, including reuse. Explicit linked-load
authority and pinned registry/source/button identities are preserved.

## 3. Performance & scalability

Nothing else found. Catalog pages and records remain bounded; Recorder excludes
large device projections, and ordinary gesture timestamps do not churn catalog
revisions. Preparation inspects the selected device instead of globally enrolling
or querying every device's automation provider.

## 4. Readability & maintainability

| ID | Severity | Location | Finding |
| --- | --- | --- | --- |
| L1 | Low `simplify` | `custom_components/ha_gesture_bridge/__init__.py:28–30`, `watch_native_metadata.inspect`; `preparation.py:86–88`, `runtime_config` | Native event-types validation is duplicated, including its type, length, string bounds and control-character policy. |

Use one stateless validator for the watcher and recipe reconstruction. Keep their
different fallback behavior in their callers. Do not replace `_native_buttons`
indiscriminately: its filtering policy differs from these all-or-nothing checks.

## 5. Test simplification

No additional verified test defect. Existing tests cover service/service
coalescing, authorization before enrollment and reuse, pinned identities through
rename/removal, explicit links, multi-endpoint choices and real exported
producer/consumer fixtures. They miss service/manual-flow concurrency and the
temporary provider-error classification above.

Add a deterministic actual-flow test that pauses discovery with an asyncio Event,
saves a manual control, then resumes the service. Require one control, the same
returned gesture identity and successful subsequent preparation. Add a provider
exception test expecting `source_metadata_unavailable`, with no new control.

The numeric-model trigger-only remote coverage hypothesis was investigated and
dropped: an arbitrary mocked provider/model combination did not establish a real
supported hardware omission. It is not included in the finding count.

## 6. Prioritised action plan

| # | Severity | Item | Effort | Impact/effort |
| --- | --- | --- | --- | --- |
| 1 | Medium | M1: recheck/reuse current control after discovery awaits | S | High |
| 2 | Medium | M2: preserve source-unavailable error classification | S | High |
| 3 | Low `simplify` | L1: share native event-types validation during the preparation fix | S | Moderate |
| 4 | Low `simplify` | L2: derive configured-device membership from controls | S | Moderate |

For M1, extract the current lookup/conflict/link-pinning fast path in `_enroll`
into this local helper, preserving its existing behavior:

```python
def current_control():
    existing = [
        item for item in entry.get_subentries_of_type("control")
        if item.data.get("source_device_id") == device_id
    ]
    if len(existing) > 1:
        raise ServiceValidationError("conflicting_controls")
    if not existing:
        return None
    subentry = existing[0]
    if subentry.data.get("load_entity_id") and not subentry.data.get("load_registry_id"):
        target = er.async_get(self.hass).async_get(subentry.data["load_entity_id"])
        if target:
            self.hass.config_entries.async_update_subentry(
                entry, subentry,
                data=dict(subentry.data) | {"load_registry_id": target.id},
            )
    return subentry
```

Use it before discovery and again immediately after the await, before generating
automatic data or returning the no-input result:

```python
if subentry := current_control():
    return subentry
# Existing discovery/error handling:
discovery = await discover_device(self.hass, device_id)
if subentry := current_control():
    return subentry
```

No lock, new persistent state, retry or recursive rediscovery is needed.

For M2, reuse the existing exception and existing preparation/Coordinator error
mapping; generalize the exception's native-only docstring:

```python
except HomeAssistantError as err:
    raise SourceMetadataUnavailable(
        "Device gesture provider is unavailable"
    ) from err
```

Land one preparation/error-handling batch with its actual-flow regressions and
L1 simplification, followed by the independent device-catalog L2 simplification.
Run the relevant tests and full suite, then re-audit the fixes. No runtime fixes,
commits or deployment were performed by this audit.
