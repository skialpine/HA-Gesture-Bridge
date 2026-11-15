# DeviceSide ancestry

This project was cloned from the local skialpine DeviceSide fork at commit `654db93117411f3ea0413dbcbe899b05873185bb`. The complete DeviceSide history and original MIT license are preserved.

It ships the independent `ha_gesture_bridge` integration. DeviceSide command adapters remain available in the ancestor commits; the gesture integration has no DeviceSide runtime dependency. Source adapters, configuration, lifecycle and evidence are kept modular so a future contribution to DeviceSide can reuse them. The `deviceside` remote points to the original fork for comparison and contribution.

No DeviceSide behavior is modified by installing this integration.

## Offer the addition upstream

The independently named `custom_components/ha_gesture_bridge` and `tests/gesture_bridge` trees are additive relative to DeviceSide; they do not replace its command engine or existing tests. `contracts`, gesture fixtures and the contract validation tools are likewise separate. To review the contribution, compare only those paths against the recorded ancestor, rather than applying this repository's application-repurposing commit wholesale. The parent DeviceSide README, Marketplace metadata and workflows must remain its own. Combining the two under one Marketplace domain would be a later explicit integration/migration decision.

Example after committing the addition:

```sh
git diff 654db93117411f3ea0413dbcbe899b05873185bb HEAD -- custom_components/ha_gesture_bridge tests/gesture_bridge tests/fixtures/gestures contracts tools/validate_gesture_contract.py tools/generate_gesture_fixtures.py
```
