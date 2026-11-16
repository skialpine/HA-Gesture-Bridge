# Contributing

Source adapters normalize reported input into the stable 1.x gesture contract. Add a new adapter with tests at the HA integration boundary; the core event publication, sequence handling and linked-load snapshots stay shared. Keep C4, DeviceSide and vendor-specific names out of the core.

Preserve control, button, gesture and entity registry identities across labels, source renames and setup changes. Never fabricate tap counts or releases from load state, restored state, retained MQTT actions or timeout cleanup. New source capabilities need actual reported evidence.

Tests distinguish fixture coverage from physical hardware qualification. Add a real-device evidence record before claiming a model verified. Source payloads, identifiers and credentials must not appear in exported diagnostics.

This repo derives from DeviceSide; retain MIT attribution and document reusable source/lifecycle helpers so a later upstream addition remains possible. See UPSTREAM.md.

Run `uv venv --python 3.14 .venv`, `uv pip install --python .venv/bin/python -r requirements-test.txt`, and `.venv/bin/python -m pytest`. Set C4_HA_SWITCH_CHECKOUT only when explicitly running the optional consumer boundary tests.

LED profiles and adapters: see [Adding an LED profile](docs/led-profiles.md). Keep device parameter declarations separate from gesture normalization.
