"""MetaWhatsAppConnector — outbound WhatsApp via the Cloud API.

Two send shapes, and which one is legal depends on the clock rather than on the
caller's preference: inside 24 hours of the customer's last message a free-form
`text` message is allowed; outside it Meta accepts only a **pre-approved
template**. `channels/service.py::check_can_send` decides which; this connector
sends what it is told.

The failure mode that makes the window worth modelling: Meta does not reject a
free-form message sent outside the window with an error a human would notice —
it accepts and never delivers.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector

GRAPH_VERSION = "v21.0"


@register_connector("meta_whatsapp")
class MetaWhatsAppConnector(ConnectorProtocol):
    name = "WhatsApp (Meta Cloud API)"
    connector_type = "meta_whatsapp"
    schema = {
        "credentials": {"access_token": "str"},
        "config": {
            "phone_number_id": "str (the WABA phone number id)",
            "app_secret": "str (optional — webhook signature verification)",
        },
    }

    def __init__(self, config: dict, credentials: dict):
        self._token = credentials["access_token"]
        self._phone_number_id = config["phone_number_id"]
        self._base_url = (
            f"https://graph.facebook.com/{GRAPH_VERSION}/{self._phone_number_id}/messages"
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json"}

    async def execute(self, input_data: dict) -> dict[str, Any]:
        to = input_data["to_number"]
        template = input_data.get("template_name")

        if template:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": input_data.get("template_language") or "en_US"},
                },
            }
            params = input_data.get("template_params") or []
            if params:
                payload["template"]["components"] = [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(p)} for p in params],
                }]
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": input_data["body"]},
            }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._base_url, headers=self._headers(), json=payload)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"WhatsApp error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        messages = data.get("messages") or [{}]
        return {
            "message_sid": messages[0].get("id"),
            "status": "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        url = f"https://graph.facebook.com/{GRAPH_VERSION}/{self._phone_number_id}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {self._token}"})
        return resp.status_code == 200
