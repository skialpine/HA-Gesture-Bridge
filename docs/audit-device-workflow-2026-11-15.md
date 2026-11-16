# Device setup workflow audit: 2026-11-15

Scope: the pending device-selection flow, discovery, new HA device-trigger adapter, MQTT discovery attachment, and their integration tests. Reviewed against `CONTRIBUTING.md` and the existing fresh-occurrence contract. Two High, eight Medium and one Low findings were verified and fixed. The most important fixes prevent retained MQTT messages and load-state changes from becoming physical button gestures.

This report covers the new setup workflow. The earlier three-repository audit is recorded separately in the Control4 driver repository.

## 1. Architecture & modularity

Native event entities remain the preferred inputs. `device_discovery.py:173` falls back to HA-owned physical device triggers only when no native inputs are present. `gesture_profiles.py:30` shares physical-input classification between discovery, configuration validation and runtime attachment; protocol-specific subscriptions remain in adapters.

| Severity | Finding and fix | Evidence |
| --- | --- | --- |
| High | A manually supplied device trigger could map a load turning on into a button press despite the UI filter. The shared physical-input predicate now protects parsing and runtime attachment too. | `config.py:260`, `sources/device_trigger.py:21`; actual HA Switch trigger attachment/state-change probe, plus parameterized load/config/Ping rejection tests. |
| Medium | Identical HA trigger configurations with distinct IDs could subscribe twice. Canonical configurations are rejected within and across source declarations. | `config.py:246`; duplicate-subscription regression. |
| Medium | One MQTT button spanning several topics reached review but could not be saved safely. Discovery now rejects that unsupported topology before review with an actionable message. | `device_discovery.py:210`; multiple-topic regression. |

All component paths above are relative to `custom_components/ha_gesture_bridge/`.

## 2. Security & error handling

| Severity | Finding and fix | Evidence |
| --- | --- | --- |
| High | HA's generic MQTT device-trigger action drops retain provenance. Its use could replay a retained action after subscription/reconnect. Discovered MQTT inputs now use the existing retained-aware subscription with literal topic/payload references. | `device_discovery.py:193`, `sources/__init__.py:218`; installed HA MQTT trigger implementation inspected; retained startup/reconnect and first-live-message regressions. |
| Medium | Editing a control to use a different device retained the old device's LED endpoints. Same-device edits preserve them; a device change clears them with a visible notice, including an empty optional LED form. | `config_flow.py:178`, `config_flow.py:203`; two-device tests with and without optional LED setup. |
| Medium | Expected HA provider-cache errors escaped device selection. Expected `HomeAssistantError` becomes a safe retryable discovery error without exposing provider details. | `device_discovery.py:184`; provider-exception and UI regression tests. |
| Medium | Retained online availability could restore a source whose MQTT device/trigger no longer existed. Availability processing now validates live device/trigger references first. | `sources/__init__.py:248`, `mqtt_device.py:19`; deleted-device/retained-availability regression. |

The critical boundary checks use the existing adapter rather than another broker connection:

```python
if self._closed or message.retain or not self._connected:
    return
```

Both parser and runtime reject unsafe device declarations using the shared predicate:

```python
if not is_physical_device_trigger(config):
    raise ValueError("Only physical button/remote input triggers are supported")
```

## 3. Performance & scalability

| Severity | Finding and fix | Evidence |
| --- | --- | --- |
| Medium | Repeated or concurrent Add control flows could create multiple aggregates for the same physical device. The flow checks ownership at selection and again immediately before saving. | `config_flow.py:111`, `config_flow.py:218`; concurrent-flow and existing-control edit regression. |
| Medium | A restored MQTT reference could become ready while still advertising unavailable. Valid references without an explicit availability topic now recover to unknown availability on the next live occurrence. | `sources/__init__.py:226`; source removal/restoration regression. |

Discovery is bounded to 32 buttons and 256 triggers. No polling loop, retry timer, extra MQTT client or dimming execution path was added. Provider-order changes are canonicalized so they do not cause spurious review failures.

## 4. Readability & maintainability

| Severity | Finding and fix | Evidence |
| --- | --- | --- |
| Medium | Final LED-step validation compared normalized gestures, missing changed underlying action names. It now compares complete discovered inputs before saving pending mappings. | `config_flow.py:143`, `config_flow.py:252`; same-gesture/different-raw-action regression. |
| Low | Diagnostics omitted the new `ha_device_trigger` source kind. It is now included in the safe source-kind allowlist. | `diagnostics.py`; diagnostics regression. |

The eight source choices and their GUI mapping paths were removed. The user chooses a device, reviews its real inputs, accepts or edits a device-based name, assigns optional paddle roles and saves. Intermediate forms use Next; final forms use Save. The frontend's supported subentry `step.<id>.submit` translation hook is used rather than a custom dialog.

## 5. Test simplification

Existing real-HA fixtures and actual Lua consumer harnesses are reused. Tests cover native preference, registry renames, unknown custom actions, trigger teardown during async attachment, retained MQTT input, duplicate controls, topology/provider failures, changed review inputs and removal of old LED authority. No network or physical switch is required by these tests.

Final validation: **305 HA tests passed** with the actual C4 checkout in 5.74 seconds; **78 C4 tests passed, one optional external-runtime check skipped** in 3.78 seconds with the HA, Coordinator and group-lighting boundaries enabled. All 17 portable wire states validated. Standalone Lua and LuaJIT checks each passed 1,085 Switch assertions, 874 Dimmer assertions and 268 LED assertions. Diff whitespace checks passed.

CHV deployment read back all 21 text files, passed HA configuration validation and restarted successfully. The integration loaded, both Office native paddle sources were ready, and existing history remained a snapshot. A live edit flow reached device review with Top/Bottom assignments and an editable device-based name; the inspection flow was aborted without saving. Automated success does not qualify untested physical device models or measure physical click latency.

## 6. Prioritised action plan

| # | Severity | Item | Effort | Impact/effort | Status |
| --- | --- | --- | --- | --- | --- |
| 1 | High | Preserve MQTT retain provenance | M | High | Fixed and tested |
| 2 | High | Enforce physical-input boundary at every entry path | S | High | Fixed and tested |
| 3 | Medium | Clear LED authority when device changes | S | High | Fixed and tested |
| 4 | Medium | Reject duplicate physical subscriptions | S | High | Fixed and tested |
| 5 | Medium | Reject duplicate device controls at save | S | High | Fixed and tested |
| 6 | Medium | Revalidate full discovery before final save | S | High | Fixed and tested |
| 7 | Medium | Revalidate MQTT availability and restored references | S | High | Both findings fixed and tested |
| 8 | Medium | Normalize expected provider failures | S | High | Fixed and tested |
| 9 | Medium | Reject unsupported MQTT topology before review | S | Medium | Fixed and tested |
| 10 | Low | Include new source kind in diagnostics | S | Medium | Fixed and tested |

Land the discovery/adapter safeguards with their boundary tests, then the device/review flow and translations with lifecycle tests. The README and setup guides describe the resulting workflow. No verified finding remains open in this scope.

Known limits: automatic MQTT discovery requires literal payloads without value templates and one topic per physical button. Re-enabling a disabled generic non-MQTT device-trigger source requires reloading or reconfiguring the bridge. These limits are explicit; they do not synthesize missing gestures.
