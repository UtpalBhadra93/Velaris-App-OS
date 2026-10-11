"""Talking to the hub: check in, take a bundle, hand the work back.

stdlib only (`urllib`), so a node needs nothing beyond the shared package. The
hub URL must be https unless it is this machine: a bundle is case data and a
sync is a worker's week, and neither crosses a network in clear. An internal CA
is passed with `VELARIS_FIELD_CA_FILE`.

The staff token is read per command (env or file) and never stored in the node:
a stolen device should not also be a stolen login.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ipaddress
import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote, urlsplit

TIMEOUT = 60


class HubError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"hub answered {status}: {detail}")
        self.status = status
        self.detail = detail


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url.rstrip("/")
    if parts.scheme == "http" and parts.hostname and _loopback(parts.hostname):
        return url.rstrip("/")
    raise HubError(0, f"{url!r}: the hub must be https (plain http only to this machine)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class Hub:
    def __init__(self, url: str, token: str, *,
                 transport: Callable[[str, str, dict[str, Any] | None, dict[str, str]],
                                     tuple[int, Any]] | None = None):
        self.url = check_url(url)
        if not token:
            raise HubError(0, "no staff token (VELARIS_FIELD_TOKEN or --token-file)")
        self._token = token
        self._transport = transport or self._urllib

    def _urllib(self, method: str, url: str, body: dict[str, Any] | None,
                headers: dict[str, str]) -> tuple[int, Any]:
        ctx = ssl.create_default_context(cafile=os.environ.get("VELARIS_FIELD_CA_FILE") or None)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                             _NoRedirect)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"null")
            except Exception:                                    # noqa: BLE001
                return exc.code, None
        except urllib.error.URLError as exc:
            raise HubError(0, f"cannot reach the hub: {exc.reason}") from exc

    def _call(self, path: str, body: dict[str, Any] | None) -> Any:
        status, out = self._transport(
            "POST", f"{self.url}/api/v1/field{path}", body,
            {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        if status >= 300:
            detail = out.get("detail") if isinstance(out, dict) else out
            raise HubError(status, str(detail))
        return out

    def contact(self, slug: str, signed: dict[str, str]) -> dict[str, Any]:
        return self._call(f"/nodes/{quote(slug, safe='')}/contact", signed)

    def dispatch(self, slug: str, offline_deny: list[str] | None = None) -> dict[str, Any]:
        return self._call(f"/nodes/{quote(slug, safe='')}/dispatch",
                          {"offline_deny": offline_deny} if offline_deny else {})

    def sync(self, slug: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._call(f"/nodes/{quote(slug, safe='')}/sync", body)
