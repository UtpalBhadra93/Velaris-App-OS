"""Generate a React Native (Expo) mobile app from Velaris case types.

Produces a complete, runnable mobile app scaffold that connects
back to the case-service API.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from case_service.codegen.templates import (
    PACKAGE_JSON, APP_JSON, EAS_JSON, APP_TSX, API_CONTEXT,
    CONFIG_TS, URL_TS, STORAGE_TS, ENDPOINTS_TS, LOGIN_SCREEN,
    CASE_LIST_SCREEN, CASE_DETAIL_SCREEN, CREATE_CASE_SCREEN,
    MY_WORK_SCREEN, SETTINGS_SCREEN, BABEL_CONFIG, TSCONFIG,
    GITIGNORE, README,
)


@dataclass
class AppConfig:
    app_name: str = "Velaris Mobile"
    app_slug: str = "velaris-mobile"
    primary_color: str = "#4ecdc4"
    default_api_url: str = "http://localhost:8200"
    default_tenant: str = "default"
    case_type_ids: list[str] = field(default_factory=list)
    # Store metadata
    app_version: str = "1.0.0"
    ios_bundle_id: str = "com.example.helixmobile"
    android_package: str = "com.example.helixmobile"
    app_description: str = ""

    def __post_init__(self):
        self.app_slug = re.sub(r"[^a-z0-9-]", "-", self.app_slug.lower()).strip("-") or "helix-mobile"
        if not re.match(r"^#[0-9a-fA-F]{6}$", self.primary_color):
            self.primary_color = "#4ecdc4"
        if not re.match(r"^\d+\.\d+\.\d+$", self.app_version):
            self.app_version = "1.0.0"


def render(template: str, config: AppConfig) -> str:
    """Substitute the ``__TOKEN__`` placeholders in a template.

    Braces are left alone. An earlier version doubled every literal brace and
    un-escaped once here, which is how a nested object inside an already-open
    JSX expression came out as ``{{ ... }}`` — a syntax error that meant the
    generated app had never compiled. Tokens cannot collide with JS syntax, so
    there is nothing to escape.
    """
    result = template
    for token, value in _tokens(config).items():
        result = result.replace(token, value)

    leftover = _UNSUBSTITUTED.findall(result)
    if leftover:
        # Silence here would ship `__APP_NAME__` to a customer's phone.
        raise ValueError(f"template placeholder never substituted: {sorted(set(leftover))}")
    return result


#: Matches a placeholder this module failed to replace. Deliberately narrow —
#: it must not fire on `__DEV__`, `__filename` or any real JS identifier, so it
#: requires the SCREAMING_SNAKE shape the templates actually use.
_UNSUBSTITUTED = re.compile(r"__[A-Z][A-Z0-9_]*[A-Z0-9]__")


def _tokens(config: AppConfig) -> dict[str, str]:
    """Every placeholder the templates may use, and what it becomes."""
    return {
        "__APP_NAME__": config.app_name,
        "__APP_SLUG__": config.app_slug,
        "__PRIMARY_COLOR__": config.primary_color,
        "__DEFAULT_API_URL__": config.default_api_url,
        "__DEFAULT_TENANT__": config.default_tenant,
        "__APP_VERSION__": config.app_version,
        "__IOS_BUNDLE_ID__": config.ios_bundle_id,
        "__ANDROID_PACKAGE__": config.android_package,
        "__APP_DESCRIPTION__": config.app_description or config.app_name,
        "__CASE_TYPE_IDS_JSON__": json.dumps(config.case_type_ids or []),
    }


def generate_app(config: AppConfig) -> dict[str, str]:
    """Generate all files for the app.

    Returns a dict of path → content.
    """
    files = {
        "package.json": render(PACKAGE_JSON, config),
        "app.json": render(APP_JSON, config),
        "eas.json": render(EAS_JSON, config),
        "App.tsx": render(APP_TSX, config),
        "src/api/ApiContext.tsx": render(API_CONTEXT, config),
        "src/api/config.ts": render(CONFIG_TS, config),
        "src/api/endpoints.ts": render(ENDPOINTS_TS, config),
        "src/api/storage.ts": render(STORAGE_TS, config),
        "src/api/url.ts": render(URL_TS, config),
        "src/screens/LoginScreen.tsx": render(LOGIN_SCREEN, config),
        "src/screens/CaseListScreen.tsx": render(CASE_LIST_SCREEN, config),
        "src/screens/CaseDetailScreen.tsx": render(CASE_DETAIL_SCREEN, config),
        "src/screens/CreateCaseScreen.tsx": render(CREATE_CASE_SCREEN, config),
        "src/screens/MyWorkScreen.tsx": render(MY_WORK_SCREEN, config),
        "src/screens/SettingsScreen.tsx": render(SETTINGS_SCREEN, config),
        "babel.config.js": BABEL_CONFIG,
        "tsconfig.json": TSCONFIG,
        ".gitignore": GITIGNORE,
        "README.md": render(README, config),
    }
    return files


#: Placeholder artwork, in the sizes Expo expects. `app.json` names all four,
#: and `expo start` fails outright on a missing icon — so an export without
#: them is not a runnable app, which is the whole point of the scaffold. They
#: are deliberately plain: a solid square in the configured primary colour,
#: obvious placeholder art for whoever replaces it before shipping to a store.
ASSET_SIZES: dict[str, tuple[int, int]] = {
    "assets/icon.png":          (1024, 1024),
    "assets/adaptive-icon.png": (1024, 1024),
    "assets/splash.png":        (1284, 2778),
    "assets/favicon.png":       (48, 48),
}


def generate_assets(config: AppConfig) -> dict[str, bytes]:
    """The placeholder PNGs, keyed by path.

    Binary, so these are *not* part of `generate_app()`: that returns text the
    preview endpoint measures and shows in the browser, and a PNG is neither
    readable there nor `.encode()`-able.
    """
    from PIL import Image

    rgb = tuple(int(config.primary_color[i:i + 2], 16) for i in (1, 3, 5))
    out: dict[str, bytes] = {}
    for path, size in ASSET_SIZES.items():
        buf = io.BytesIO()
        Image.new("RGBA", size, (*rgb, 255)).save(buf, format="PNG")
        out[path] = buf.getvalue()
    return out


def generate_zip(config: AppConfig) -> bytes:
    """Generate the app as a zip file for download."""
    files = generate_app(config)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(f"{config.app_slug}/{path}", content)
        for path, blob in generate_assets(config).items():
            zf.writestr(f"{config.app_slug}/{path}", blob)
    return buf.getvalue()
