"""Export real HA catalog publications for independently run C4 package checks."""

import argparse
import asyncio
from datetime import timedelta
import json
import logging
from pathlib import Path
import tempfile

from homeassistant import config_entries, loader
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components.ha_gesture_bridge.config import DOMAIN, NativeControlConfig
from custom_components.ha_gesture_bridge.event import GestureBridgeEvent
from custom_components.ha_gesture_bridge.sensor import GestureCatalogSensor


async def catalog_fixture(hass):
    """Use actual registries, aggregate and sensor; no network or load actions."""
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Fixture Office")
    for domain in ("switch", "light"):
        for index in range(31):
            object_id = f"catalog_{domain}_{index:02d}"
            modes = {"supported_color_modes": ["brightness"]} if domain == "light" else None
            item = registry.async_get_or_create(domain, "catalog_fixture", object_id,
                suggested_object_id=object_id, original_name=f"Catalog {domain.title()} {index:02d}",
                capabilities=modes)
            registry.async_update_entity(item.entity_id, area_id=area.id)
            attrs = {"supported_color_modes": ["brightness"], "supported_features": 32, "brightness": 128} if modes else {}
            hass.states.async_set(item.entity_id, "unavailable" if index == 30 else "off", attrs)
    source = registry.async_get_or_create("event", "catalog_fixture", "catalog_paddle",
        suggested_object_id="catalog_paddle")
    hass.states.async_set(source.entity_id, "unknown", {"event_types": ["double"]})
    controls = []
    events = EntityPlatform(hass=hass, logger=logging.getLogger(__name__), domain="event",
        platform_name=DOMAIN, platform=None, scan_interval=timedelta(seconds=30), entity_namespace=None)
    for domain in ("switch", "light"):
        control = GestureBridgeEvent(NativeControlConfig.parse({
            "unique_id": f"catalog-{domain}-control", "name": f"Catalog {domain.title()} Control",
            "source_entity_id": source.entity_id, "load_entity_id": f"{domain}.catalog_{domain}_00",
            "actions": {"double": {"kind": "press", "press_count": 2}},
        }))
        control._session_id = f"catalog-{domain}-session"
        await events.async_add_entities([control])
        registry.async_update_entity(control.entity_id, area_id=area.id)
        controls.append(control.entity_id)
    sensor = GestureCatalogSensor()
    sensors = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    await sensors.async_add_entities([sensor])
    await hass.async_block_till_done()
    # HA timestamps are incidental to discovery; normalize them for portable,
    # reproducible exports. Every capability/catalog value is producer output.
    states = []
    for state in sorted(hass.states.async_all(), key=lambda state: state.entity_id):
        states.append({"entity_id": state.entity_id, "state": state.state,
            "attributes": dict(state.attributes), "last_updated": "2026-11-14T20:00:00.000Z"})
    return {"fixture_version": 1, "producer": "ha_gesture_bridge",
        "catalog_entity_id": sensor.entity_id, "catalog_rows": sensor.native_value,
        "switch_entity_id": "switch.catalog_switch_01", "dimmer_entity_id": "light.catalog_light_01",
        "gesture_entity_ids": controls, "states": states}


async def export():
    with tempfile.TemporaryDirectory(prefix="ha-catalog-fixture-") as config_dir:
        hass = HomeAssistant(config_dir)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        loader.async_setup(hass)
        assert await async_load_base_functionality(hass)
        try:
            return await catalog_fixture(hass)
        finally:
            await hass.async_stop(force=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = asyncio.run(export())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, sort_keys=True, indent=2) + "\n")
    print(f"Exported {len(fixture['states'])} HA states and {fixture['catalog_rows']} catalog rows to {args.output}")


if __name__ == "__main__":
    main()
