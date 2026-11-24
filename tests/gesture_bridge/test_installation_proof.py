"""The installation proof uses Home Assistant's real API and ban middleware."""

import re

from aiohttp.test_utils import TestClient, TestServer
from homeassistant import config_entries, loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.components import websocket_api
from homeassistant.components.http.ban import KEY_FAILED_LOGIN_ATTEMPTS
from homeassistant.components.http.server import HomeAssistantHTTP
from homeassistant.core import HomeAssistant
from homeassistant.util.ssl import SSLProfile

from custom_components.ha_gesture_bridge import async_setup
from custom_components.ha_gesture_bridge.installation_proof import COMMAND


async def proof_client(hass):
    hass.auth = await auth_manager_from_config(hass, [], [])
    hass.http = HomeAssistantHTTP(
        hass,
        ssl_certificate=None,
        ssl_peer_certificate=None,
        ssl_key=None,
        server_host=["127.0.0.1"],
        server_port=0,
        trusted_proxies=[],
        ssl_profile=SSLProfile.MODERN_V6,
    )
    await hass.http.async_initialize(
        cors_origins=[],
        use_x_forwarded_for=False,
        login_threshold=1,
        is_ban_enabled=True,
        use_x_frame_options=True,
    )
    assert await websocket_api.async_setup(hass, {})
    hass.config.components.add(websocket_api.DOMAIN)
    assert await async_setup(hass, {})
    return TestClient(TestServer(hass.http.app))


async def test_proof_matches_one_running_installation(hass, tmp_path, caplog):
    other_path = tmp_path / "other"
    other_path.mkdir()
    other = HomeAssistant(str(other_path))
    other.config_entries = config_entries.ConfigEntries(other, {})
    loader.async_setup(other)

    try:
        assert await async_load_base_functionality(other)
        async with await proof_client(hass) as current, await proof_client(other) as separate:
            user = await hass.auth.async_create_user("Proof test", group_ids=[GROUP_ID_USER])
            credential = await hass.auth.async_create_refresh_token(user, client_id=str(current.make_url("/")))
            token = hass.auth.async_create_access_token(credential)
            async with await current.ws_connect("/api/websocket") as websocket:
                assert (await websocket.receive_json())["type"] == "auth_required"
                await websocket.send_json({"type": "auth", "access_token": token})
                assert (await websocket.receive_json())["type"] == "auth_ok"
                await websocket.send_json({"id": 1, "type": COMMAND})
                issued = await websocket.receive_json()
                assert issued["success"]
                nonce, proof = issued["result"]["nonce"], issued["result"]["proof"]
                assert issued["result"] == {"version": 1, "nonce": nonce, "proof": proof}
                assert re.fullmatch(r"[0-9a-f]{32}", nonce)
                assert re.fullmatch(r"[0-9a-f]{64}", proof)
                await websocket.send_json({"id": 2, "type": COMMAND})
                assert (await websocket.receive_json())["result"]["nonce"] != nonce
                await websocket.send_json({"id": 3, "type": COMMAND, "nonce": nonce})
                assert (await websocket.receive_json())["result"] == issued["result"]
                for request_id, invalid in enumerate((nonce + "\n", nonce[:-1], "A" * 32, 123), 4):
                    await websocket.send_json({"id": request_id, "type": COMMAND, "nonce": invalid})
                    assert not (await websocket.receive_json())["success"]

            path = f"/api/{COMMAND}"
            for client in (current, separate):
                async with client.get(f"{path}/{nonce}") as response:
                    assert response.status == 200
                    assert response.headers["Cache-Control"] == "no-store"
                    result = await response.json()
                    assert result == {"version": 1, "nonce": nonce, "proof": result["proof"]}
                    assert re.fullmatch(r"[0-9a-f]{64}", result["proof"])
                    assert (result["proof"] == proof) == (client is current)

            for invalid in (path, f"{path}/{'a' * 31}", f"{path}/{'A' * 32}", f"{path}/{'g' * 32}",
                            f"{path}/{'0' * 33}", f"{path}/{nonce}/extra"):
                async with current.get(invalid) as response:
                    assert response.status == 404
            async with current.post(f"{path}/{nonce}") as response:
                assert response.status == 405
            assert not current.app[KEY_FAILED_LOGIN_ATTEMPTS]
            assert not separate.app[KEY_FAILED_LOGIN_ATTEMPTS]
            assert "invalid authentication" not in caplog.text
            assert proof not in caplog.text and token not in caplog.text
    finally:
        await other.async_stop(force=True)
