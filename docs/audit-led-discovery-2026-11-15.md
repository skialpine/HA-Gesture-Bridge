# Automatic LED discovery audit: 2026-11-15

Scope: automatic LED factory, standard Indicator adapter, manufacturer profiles,
parameter transport, event/config integration and boundary tests. No outstanding
findings after fixes. The final full suite passed 353 tests in 7.84 seconds,
including optional actual Control4 consumer checks.

## 1. Architecture & modularity

One factory selects a reviewed manufacturer profile or standard Indicator
support. Custom mappings remain an explicit override. Profiles contain device
identities and LED parameter schemas, outside gesture normalization; the LED
service and consumer contract remain shared. No outstanding findings.

## 2. Security & error handling

Profiles require actual manufacturer/product IDs and exact parameter metadata.
Every awaited action revalidates ownership and capabilities. Raw Get replies,
rather than optimistic cached Set values, provide feedback. Interrupted profile
reads cannot publish against a replacement transport. No outstanding findings.

## 3. Performance & scalability

Discovery checks cached metadata. Hardware reads are background tasks on startup,
reconnect, relevant changes or missing confirmation. The five-second reconnect
check is local; it does not continuously write settings. Per-controller actions
serialize; unload removes timers, subscriptions and pending reads. No outstanding
findings.

## 4. Readability & maintainability

The parameter adapter reuses the standard adapter's transport/lifecycle hooks.
Adding another compatible parameter layout is declarative; another protocol can
use the same factory and wire contract. See `led-profiles.md`. No outstanding
findings.

## 5. Test simplification

Boundary tests exercise actual HA aggregates, the existing LED service, real
Z-Wave value models, disabled configuration entities, schema/fingerprint
rejection, reconnects, unload and confirmed replies. Simulated commands are
distinct from physical LED qualification. No outstanding findings.

## 6. Prioritised action plan

| Severity | Finding | Resolution |
| --- | --- | --- |
| Medium | A fractional brightness request such as 0.1 passed the power check but rounded to raw zero, potentially switching off an on indicator. | Validate power against the exact transmitted level; four regressions pass. |
| Low | An interrupted profile read could retain partial confirmation from a replaced node. | Commit only while the captured node remains current and the controller is active; regression passes. |

The fixes and tests are included together. Physical LED writes remain a separate
qualification step; automatic discovery and startup issue reads only.
