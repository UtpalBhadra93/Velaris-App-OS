"""Phase 18 tests — React Native App Codegen.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import io
import pathlib
import zipfile
import pytest



#: The node-side guards that exercise the generated TypeScript.
_JS_TESTS = pathlib.Path(__file__).parent / "codegen_js"


def _find_tsc() -> str | None:
    """A TypeScript compiler, wherever this checkout happens to keep one."""
    import shutil
    found = shutil.which("tsc")
    if found:
        return found
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    for candidate in (
        repo_root / "node_modules/.bin/tsc",
        repo_root / "studio/node_modules/.bin/tsc",
    ):
        if candidate.exists():
            return str(candidate)
    return None


class TestAppConfig:
    def test_default_config(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig()
        assert c.app_name == "Velaris Mobile"
        assert c.app_slug == "velaris-mobile"

    def test_slug_sanitization(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(app_slug="My App!")
        # Should be sanitized
        assert "!" not in c.app_slug
        assert " " not in c.app_slug

    def test_empty_slug_fallback(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(app_slug="@@@")
        assert c.app_slug == "helix-mobile"

    def test_color_validation(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(primary_color="not-a-color")
        assert c.primary_color == "#4ecdc4"

    def test_valid_color_preserved(self):
        from case_service.codegen.generator import AppConfig
        c = AppConfig(primary_color="#ff6b35")
        assert c.primary_color == "#ff6b35"


class TestGenerator:
    def test_generate_app_files(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(app_name="Test App"))
        assert "package.json" in files
        assert "App.tsx" in files
        assert "src/api/ApiContext.tsx" in files
        assert "src/screens/CaseListScreen.tsx" in files

    def test_app_name_in_generated_files(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(app_name="MyCustomApp", app_slug="my-custom-app"))
        # App name should appear in package.json
        assert "my-custom-app" in files["package.json"]
        # App name should appear in app.json
        assert "MyCustomApp" in files["app.json"]

    def test_primary_color_in_generated(self):
        # The colour is declared once in config.ts and imported by the screens,
        # so the check is that it reaches the app and that the screens read it
        # from there — not that a literal is pasted into each file.
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(primary_color="#ff6b35"))
        assert "PRIMARY_COLOR = '#ff6b35'" in files["src/api/config.ts"]
        assert "#ff6b35" in files["app.json"]
        for screen in ("App.tsx", "src/screens/CaseListScreen.tsx"):
            assert "PRIMARY_COLOR" in files[screen]

    def test_api_url_in_config(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(default_api_url="https://helix.example.com"))
        assert "https://helix.example.com" in files["src/api/config.ts"]
        assert "DEFAULT_API_URL" in files["src/api/ApiContext.tsx"]

    def test_tenant_in_config(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(default_tenant="acme"))
        assert "DEFAULT_TENANT = 'acme'" in files["src/api/config.ts"]
        assert "DEFAULT_TENANT" in files["src/api/ApiContext.tsx"]


class TestZipGeneration:
    def test_generate_zip(self):
        from case_service.codegen.generator import AppConfig, generate_zip
        zip_data = generate_zip(AppConfig(app_slug="zip-test"))
        assert isinstance(zip_data, bytes)
        assert len(zip_data) > 0

    def test_zip_contains_files(self):
        from case_service.codegen.generator import AppConfig, generate_zip
        zip_data = generate_zip(AppConfig(app_slug="zip-test"))
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            names = zf.namelist()
            assert any("package.json" in n for n in names)
            assert any("App.tsx" in n for n in names)
            assert any("CaseListScreen.tsx" in n for n in names)

    def test_zip_has_app_slug_prefix(self):
        from case_service.codegen.generator import AppConfig, generate_zip
        zip_data = generate_zip(AppConfig(app_slug="my-app"))
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            names = zf.namelist()
            assert all(n.startswith("my-app/") for n in names)


class TestCodegenAPI:
    async def test_list_platforms(self, client):
        resp = await client.get("/api/v1/codegen/platforms")
        assert resp.status_code == 200
        platforms = resp.json()["platforms"]
        assert len(platforms) >= 1
        assert platforms[0]["id"] == "react-native-expo"

    async def test_preview(self, client):
        resp = await client.post("/api/v1/codegen/preview", json={
            "app_name": "Test App",
            "app_slug": "test-app",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["file_count"] > 0
        assert "package.json" in data["files"]

    async def test_preview_with_color(self, client):
        resp = await client.post("/api/v1/codegen/preview", json={
            "app_name": "Colored App",
            "primary_color": "#ff00ff",
        })
        assert resp.status_code == 200
        assert "#ff00ff" in resp.json()["files"]["src/api/config.ts"]

    async def test_generate_zip_endpoint(self, client):
        resp = await client.post("/api/v1/codegen/generate", json={
            "app_name": "Download Test",
            "app_slug": "download-test",
        })
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert "download-test.zip" in resp.headers.get("content-disposition", "")

        # Verify it's a valid zip
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            assert len(zf.namelist()) > 0

    async def test_empty_app_name_rejected(self, client):
        resp = await client.post("/api/v1/codegen/preview", json={
            "app_name": "",
        })
        # Pydantic validation
        assert resp.status_code == 422


class TestTheExportIsRunnable:
    """The scaffold's whole value is that it runs unmodified.

    Every check here failed at least once against the real export (2026-08-19):
    App.tsx did not parse, the four icons `app.json` names were never emitted,
    and My Work called an endpoint that does not exist.
    """

    def test_jsx_braces_survive_rendering(self):
        # Templates used to double every literal brace and `render()` un-escaped
        # once, so a nested object inside an already-open JSX expression came
        # out as `headerStyle: {{ ... }}` — a syntax error, which is why the
        # generated app had never compiled. Braces are literal now; this is what
        # notices if anyone reintroduces an escaping pass.
        from case_service.codegen.generator import AppConfig, generate_app
        app_tsx = generate_app(AppConfig())["App.tsx"]
        assert "headerStyle: { backgroundColor:" in app_tsx
        assert "{{" not in app_tsx.replace("={{", "")

    def test_no_placeholder_is_left_in_the_output(self):
        # `__APP_NAME__` reaching a customer's phone is the failure this scheme
        # trades the brace escaping for, so it has to be loud.
        import re
        from case_service.codegen.generator import AppConfig, generate_app, _UNSUBSTITUTED
        for name, content in generate_app(AppConfig()).items():
            assert not _UNSUBSTITUTED.findall(content), f"{name} still has a placeholder in it"

    def test_an_unknown_placeholder_raises_rather_than_shipping(self):
        import pytest as _pytest
        from case_service.codegen.generator import AppConfig, render
        with _pytest.raises(ValueError, match="never substituted"):
            render("const x = '__NO_SUCH_TOKEN__';", AppConfig())

    def test_the_configured_version_reaches_the_app(self):
        # `package.json` and the Settings footer both hard-coded 1.0.0, so a
        # v2.3.0 build reported itself as v1.0.0 in the one place a user looks.
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig(app_version="2.3.1"))
        assert '"version": "2.3.1"' in files["package.json"]
        assert '"version": "2.3.1"' in files["app.json"]
        assert "APP_VERSION = '2.3.1'" in files["src/api/config.ts"]
        assert "1.0.0" not in files["src/screens/SettingsScreen.tsx"]

    def test_the_selected_case_types_reach_the_app(self):
        # App Builder has always accepted a case type selection and the
        # generated app has always ignored it, offering every case type on the
        # server instead.
        import json
        from case_service.codegen.generator import AppConfig, generate_app
        chosen = ["11111111-1111-1111-1111-111111111111"]
        files = generate_app(AppConfig(case_type_ids=chosen))
        assert json.dumps(chosen) in files["src/api/config.ts"]
        # Importing the helper is not applying it — deleting just the `.filter`
        # call left an unused import behind and this test still passed.
        assert ".filter(ct => caseTypeAllowed(ct.id))" in files["src/screens/CreateCaseScreen.tsx"]
        # and the empty selection still means "all of them"
        assert "ALLOWED_CASE_TYPE_IDS: string[] = []" in generate_app(AppConfig())["src/api/config.ts"]

    def test_screens_do_not_freeze_the_api_at_first_mount(self):
        # `useFocusEffect(useCallback(() => load(), []))` captured the first
        # `api`, so every list screen kept calling the build-time default URL
        # with no token however Settings was edited. Proven against the running
        # app on 2026-08-19: after configuring a server and reloading, requests
        # still went to the default.
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig())
        for screen in ("CaseListScreen", "MyWorkScreen"):
            body = files[f"src/screens/{screen}.tsx"]
            assert "useCallback(() => { load(); }, [load])" in body, (
                f"{screen} must re-run its load when the API changes"
            )
            assert "}, [request])" in body, f"{screen}'s load must depend on request"

    def test_nothing_renders_before_stored_settings_are_read(self):
        # Screens used to mount and fire their first request in the same tick as
        # the async read of AsyncStorage, i.e. always against the defaults.
        from case_service.codegen.generator import AppConfig, generate_app
        assert "if (!ready)" in generate_app(AppConfig())["App.tsx"]

    def test_the_token_never_goes_to_plain_storage(self):
        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig())
        api = files["src/api/ApiContext.tsx"]
        assert "AsyncStorage" not in api, "the API layer must go through storage.ts, not AsyncStorage"
        assert "setSecret(K.token" in api and "setSecret(K.refresh" in api

    def test_the_token_is_pinned_to_the_server_that_issued_it(self):
        # A base URL is typed by a person. Without this the app would attach a
        # live bearer token to a request aimed at whatever host was typed.
        from case_service.codegen.generator import AppConfig, generate_app
        api = generate_app(AppConfig())["src/api/ApiContext.tsx"]
        assert "s.origin === originOf(baseRef.current)" in api

    def test_there_is_no_field_for_pasting_a_raw_token(self):
        # The scaffold used to ask the user to paste a JWT into Settings, which
        # is both unusable (they expire in minutes) and the reason the token was
        # sitting in plain storage.
        from case_service.codegen.generator import AppConfig, generate_app
        settings = generate_app(AppConfig())["src/screens/SettingsScreen.tsx"]
        assert "Auth Token" not in settings
        assert "signOut" in settings

    def test_every_asset_app_json_names_is_in_the_zip(self):
        # `expo start` fails outright on a missing icon, so an export without
        # these is not a runnable app.
        import json
        import re
        from case_service.codegen.generator import AppConfig, generate_app, generate_zip

        cfg = AppConfig(app_slug="asset-test")
        referenced = set(re.findall(r"\./(assets/[\w-]+\.png)", generate_app(cfg)["app.json"]))
        assert referenced, "app.json stopped naming any assets — is this test still right?"
        with zipfile.ZipFile(io.BytesIO(generate_zip(cfg))) as zf:
            names = set(zf.namelist())
        for asset in referenced:
            assert f"asset-test/{asset}" in names, f"{asset} is referenced but never emitted"

    def test_assets_are_real_pngs_in_the_configured_colour(self):
        from PIL import Image
        from case_service.codegen.generator import AppConfig, generate_assets

        assets = generate_assets(AppConfig(primary_color="#4ecdc4"))
        icon = Image.open(io.BytesIO(assets["assets/icon.png"]))
        assert icon.size == (1024, 1024)
        assert icon.getpixel((0, 0))[:3] == (0x4E, 0xCD, 0xC4)

    def test_assets_stay_out_of_the_text_preview(self):
        # `preview_app` calls `.encode()` on every value and renders it in the
        # browser; a PNG is neither.
        from case_service.codegen.generator import AppConfig, generate_app
        assert not [p for p in generate_app(AppConfig()) if p.endswith(".png")]

    def test_every_endpoint_the_app_calls_exists(self):
        # My Work called `/my/work`, which has never been a route — the screen
        # 404'd on open. Checked against the live route table rather than a list
        # of paths someone maintains by hand.
        import re
        from case_service.main import app as fastapi_app
        from case_service.codegen.generator import AppConfig, generate_app

        routes = {getattr(r, "path", "") for r in fastapi_app.routes}
        # Every path the app knows lives in one table, so this cannot miss one
        # by failing to guess how it was written at the call site.
        endpoints = generate_app(AppConfig())["src/api/endpoints.ts"]
        called = set()
        for path in re.findall(r"[\'`](/[A-Za-z0-9._~\-/${}]*)[\'`]", endpoints):
            called.add(re.sub(r"\$\{[^}]+\}", "{case_id}", path))
        assert called, "no API paths found — has endpoints.ts been renamed?"
        for path in sorted(called):
            assert f"/api/v1{path}" in routes, f"the app calls {path}, which is not a route"

    def test_every_screen_calls_only_through_the_endpoint_table(self):
        # A path written inline is a path the check above cannot see.
        import re
        from case_service.codegen.generator import AppConfig, generate_app
        for name, content in generate_app(AppConfig()).items():
            if not name.startswith("src/screens/"):
                continue
            inline = re.findall(r"request\(\s*[\'\"`]/", content)
            assert not inline, f"{name} builds an API path inline instead of using EP"


class TestTheGeneratedSecurityCodeBehaves:
    """Run the generated TypeScript, rather than reading it.

    The credential handling and the server-URL policy are the two places where
    being wrong hands someone else a live bearer token, and neither can be
    exercised from Python or from a browser: the keystore branch only exists on
    iOS and Android. These compile the emitted `.ts` files and execute them on
    node with the three native modules stubbed.

    Skipped where node or typescript is unavailable — the pass is evidence, the
    skip is not a pass.
    """

    @staticmethod
    def _run(tmp_path, sources: list[str], script: str):
        import json
        import os
        import shutil
        import subprocess
        import pytest as _pytest

        node = shutil.which("node")
        if not node:
            _pytest.skip("node is not installed")
        tsc = _find_tsc()
        if not tsc:
            _pytest.skip("no typescript compiler available")

        from case_service.codegen.generator import AppConfig, generate_app
        files = generate_app(AppConfig())

        src = tmp_path / "src"
        src.mkdir()
        for name in sources:
            (src / f"{name}.ts").write_text(files[f"src/api/{name}.ts"])
        # The three native modules have no node implementation; the harness
        # supplies them at require() time.
        (src / "shims.d.ts").write_text(
            "declare module 'react-native';\n"
            "declare module 'expo-secure-store';\n"
            "declare module '@react-native-async-storage/async-storage';\n"
        )
        (tmp_path / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {
                "target": "es2019", "module": "commonjs", "strict": True,
                "esModuleInterop": True, "skipLibCheck": True,
                "outDir": "out", "rootDir": "src", "types": [],
            },
        }))

        compiled = subprocess.run([tsc, "-p", str(tmp_path)], capture_output=True, text=True, cwd=tmp_path)
        assert compiled.returncode == 0, f"the generated TypeScript did not compile:\n{compiled.stdout}{compiled.stderr}"

        env = {**os.environ, "GENERATED_OUT": str(tmp_path / "out")}
        result = subprocess.run(
            [node, str(_JS_TESTS / script)], capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def test_tokens_go_to_the_keystore_and_upgrades_leave_no_plaintext(self, tmp_path):
        # An access token in AsyncStorage sits in an unencrypted SQLite file
        # that lands in device backups. It belongs in the Keychain / Android
        # Keystore, and an app upgraded from a build that stored it in the
        # clear has to have that copy removed, not merely stop writing new ones.
        out = self._run(tmp_path, ["storage"], "storage.test.js")
        assert "ALL PASS" in out, out

    def test_the_server_url_policy_refuses_what_it_should(self, tmp_path):
        # The base URL is the one value a person types that decides where a
        # valid bearer token is sent.
        out = self._run(tmp_path, ["url"], "url.test.js")
        assert "ALL PASS" in out, out
