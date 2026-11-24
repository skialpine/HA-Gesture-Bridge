"""Prove that two routes reach the same running Home Assistant."""

from __future__ import annotations

import hmac
import secrets

from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import HomeAssistantView
import voluptuous as vol

from .config import DOMAIN

COMMAND = f"{DOMAIN}/installation_proof"
NONCE_PATTERN = r"[0-9a-f]{32}"


class InstallationProofView(HomeAssistantView):
    """Return a scoped proof without requiring credentials on the second route."""

    url = f"/api/{COMMAND}/{{nonce:{NONCE_PATTERN}}}"
    name = f"api:{DOMAIN}:installation_proof"
    requires_auth = False

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)

    def proof(self, nonce: str) -> str:
        """Derive a proof for this nonce using the running instance's key."""
        return hmac.digest(self._key, nonce.encode("ascii"), "sha256").hex()

    @callback
    def get(self, request: web.Request, nonce: str) -> web.Response:
        """Answer only a correctly shaped nonce matched by the router."""
        return self.json(
            {"version": 1, "nonce": nonce, "proof": self.proof(nonce)}, headers={"Cache-Control": "no-store"}
        )


@callback
def async_setup_installation_proof(hass: HomeAssistant) -> None:
    """Register once per integration setup when the WebSocket API is in use."""
    if websocket_api.DOMAIN not in hass.config.components:
        return

    view = InstallationProofView()

    @websocket_api.websocket_command({
        vol.Required("type"): COMMAND,
        vol.Optional("nonce"): vol.Match(rf"\A{NONCE_PATTERN}\Z"),
    })
    @callback
    def issue_proof(hass: HomeAssistant, connection: websocket_api.ActiveConnection, message: dict) -> None:
        nonce = message.get("nonce") or secrets.token_hex(16)
        connection.send_result(message["id"], {"version": 1, "nonce": nonce, "proof": view.proof(nonce)})

    hass.http.register_view(view)
    websocket_api.async_register_command(hass, issue_proof)
