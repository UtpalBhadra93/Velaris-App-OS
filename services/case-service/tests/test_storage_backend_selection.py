"""v3.1.2 — which document store a setting selects, and the update-agent probe.

"minio" is what every install before v3.1.2 has in .env and must keep working;
"s3" is the same client under the name used for an endpoint that is not MinIO;
anything else — including unset and empty — is local disk.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from case_service.storage import factory
from case_service.storage.local_fs import LocalFSBackend


def _select(monkeypatch, tmp_path, mode):
    made = {}

    class FakeS3:
        def __init__(self, **kw):
            made.update(kw)

    monkeypatch.setattr("case_service.storage.s3_backend.S3Backend", FakeS3)
    settings = SimpleNamespace(
        storage_backend=mode, storage_local_path=str(tmp_path), storage_master_key="",
        s3_endpoint="s3.example:8333", s3_access_key="k", s3_secret_key="s",
        s3_bucket="docs", s3_secure=True,
    )
    monkeypatch.setattr("velaris_shared.config.get_settings", lambda: settings)
    factory.reset_storage_backend()
    try:
        return factory.get_storage_backend(), made, FakeS3
    finally:
        factory.reset_storage_backend()


@pytest.mark.parametrize("mode", ["minio", "s3", "S3", "MinIO"])
def test_s3_and_minio_select_the_same_backend(monkeypatch, tmp_path, mode):
    backend, made, FakeS3 = _select(monkeypatch, tmp_path, mode)
    assert isinstance(backend, FakeS3)
    assert made == {"endpoint": "s3.example:8333", "access_key": "k", "secret_key": "s",
                    "bucket": "docs", "secure": True}


@pytest.mark.parametrize("mode", ["local", "", "something-else"])
def test_everything_else_is_local_disk(monkeypatch, tmp_path, mode):
    backend, made, _ = _select(monkeypatch, tmp_path, mode)
    assert isinstance(backend, LocalFSBackend)
    assert made == {}


def test_update_agent_probe_is_unknown_without_systemd(monkeypatch):
    """No systemd is "cannot tell" (None), never "missing" (False)."""
    from case_service.api.routers import platform_updates as pu

    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert pu._update_agent_installed() is None


@pytest.mark.parametrize("rc,expected", [(0, True), (3, False)])
def test_update_agent_probe_reads_the_timer(monkeypatch, rc, expected):
    from pathlib import Path
    from case_service.api.routers import platform_updates as pu

    if not Path("/run/systemd/system").is_dir():
        pytest.skip("no systemd on this host")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/systemctl")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: SimpleNamespace(returncode=rc))
    assert pu._update_agent_installed() is expected


@pytest.mark.parametrize("value", ["3.1.0", "10.20.30", "3.1.0-rc.1"])
def test_rollback_version_pattern_accepts_versions(value):
    from case_service.api.routers import platform_updates as pu

    assert pu._VERSION_RE.fullmatch(value)


@pytest.mark.parametrize("value", [
    '3.0.0"/e;touch PWNED;#',      # GNU sed would execute this
    "3.0.0',{}); import os #",     # python -c string break-out
    "3.0.0 --force", "v3.0.0", "3.0", "", "3.0.0\n4.0.0", "../3.0.0",
])
def test_rollback_version_pattern_refuses_everything_else(value):
    from case_service.api.routers import platform_updates as pu

    assert not pu._VERSION_RE.fullmatch(value)


@pytest.mark.parametrize("endpoint,secure,warns", [
    ("localhost:9000", False, False),      # the bundled container: as before, silent
    ("127.0.0.1:9000", False, False),
    ("minio:9000", False, False),
    ("s3.example.com:443", True, False),
    ("s3.example.com:9000", False, True),  # another machine over plain http
    ("10.0.0.5:8333", False, True),
])
def test_remote_s3_over_plain_http_is_warned(monkeypatch, tmp_path, caplog, endpoint, secure, warns):
    class FakeS3:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr("case_service.storage.s3_backend.S3Backend", FakeS3)
    settings = SimpleNamespace(
        storage_backend="s3", storage_local_path=str(tmp_path), storage_master_key="",
        s3_endpoint=endpoint, s3_access_key="k", s3_secret_key="s",
        s3_bucket="docs", s3_secure=secure,
    )
    monkeypatch.setattr("velaris_shared.config.get_settings", lambda: settings)
    factory.reset_storage_backend()
    try:
        with caplog.at_level("WARNING", logger="case_service.storage.factory"):
            factory.get_storage_backend()
    finally:
        factory.reset_storage_backend()
    assert any("VELARIS_CASE_S3_SECURE" in r.message for r in caplog.records) is warns


# ── S3 setting names (v3.1.2) ────────────────────────────────────────────────
# VELARIS_CASE_S3_* are the documented names; VELARIS_CASE_MINIO_* are what an
# earlier install has in .env and must be read for good.

_S3_ENV = ("VELARIS_CASE_S3_ENDPOINT", "VELARIS_CASE_S3_ACCESS_KEY", "VELARIS_CASE_S3_SECRET_KEY",
           "VELARIS_CASE_S3_BUCKET", "VELARIS_CASE_S3_SECURE",
           "VELARIS_CASE_MINIO_ENDPOINT", "VELARIS_CASE_MINIO_ACCESS_KEY",
           "VELARIS_CASE_MINIO_SECRET_KEY", "VELARIS_CASE_MINIO_BUCKET",
           "VELARIS_CASE_MINIO_SECURE", "VELARIS_MINIO_PASSWORD")


def _settings(monkeypatch, **env):
    from velaris_shared.config import Settings

    for name in _S3_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_old_minio_names_are_still_read(monkeypatch):
    s = _settings(monkeypatch, VELARIS_CASE_MINIO_ENDPOINT="old:9000",
                  VELARIS_CASE_MINIO_ACCESS_KEY="ak", VELARIS_CASE_MINIO_SECRET_KEY="sk",
                  VELARIS_CASE_MINIO_BUCKET="b", VELARIS_CASE_MINIO_SECURE="true")
    assert (s.s3_endpoint, s.s3_access_key, s.s3_secret_key, s.s3_bucket, s.s3_secure) \
        == ("old:9000", "ak", "sk", "b", True)
    # …and the attribute names from before v3.1.2 still answer.
    assert (s.minio_endpoint, s.minio_access_key, s.minio_secret_key, s.minio_bucket, s.minio_secure) \
        == ("old:9000", "ak", "sk", "b", True)


def test_s3_names_are_read_and_win_over_the_old_ones(monkeypatch):
    s = _settings(monkeypatch, VELARIS_CASE_S3_ENDPOINT="new:8333", VELARIS_CASE_S3_SECRET_KEY="new-sk",
                  VELARIS_CASE_MINIO_ENDPOINT="old:9000", VELARIS_CASE_MINIO_SECRET_KEY="old-sk")
    assert s.s3_endpoint == "new:8333"
    assert s.s3_secret_key == "new-sk"


def test_nothing_set_is_the_previous_defaults(monkeypatch):
    s = _settings(monkeypatch)
    assert (s.s3_endpoint, s.s3_access_key, s.s3_bucket, s.s3_secure) \
        == ("localhost:9000", "velaris", "velaris-docs", False)
    assert s.s3_secret_key == "velaris_dev_password"


@pytest.mark.parametrize("endpoint", ["localhost:9000", "127.0.0.1:9000", "minio:9000"])
def test_bundled_minio_password_is_the_fallback_on_this_machine(monkeypatch, endpoint):
    """The install setup made since 2026-08-06: backend=minio, a random
    container password, and no secret for the service — it could not log in."""
    s = _settings(monkeypatch, VELARIS_CASE_MINIO_ENDPOINT=endpoint, VELARIS_MINIO_PASSWORD="container-pw")
    assert s.s3_secret_key == "container-pw"


def test_bundled_password_is_never_used_for_a_remote_endpoint(monkeypatch):
    s = _settings(monkeypatch, VELARIS_CASE_S3_ENDPOINT="s3.example.com:443", VELARIS_MINIO_PASSWORD="container-pw")
    assert s.s3_secret_key == "velaris_dev_password"


def test_a_configured_secret_beats_the_bundled_password(monkeypatch):
    s = _settings(monkeypatch, VELARIS_CASE_MINIO_SECRET_KEY="explicit", VELARIS_MINIO_PASSWORD="container-pw")
    assert s.s3_secret_key == "explicit"


def test_an_empty_secret_setting_is_not_a_secret(monkeypatch):
    """`VELARIS_CASE_S3_SECRET_KEY=` (appended empty by an .env sync) must fall
    through, not sign requests with an empty key."""
    s = _settings(monkeypatch, VELARIS_CASE_S3_SECRET_KEY="", VELARIS_MINIO_PASSWORD="container-pw")
    assert s.s3_secret_key == "container-pw"


def test_the_old_class_name_is_still_importable():
    from case_service.storage import MinIOBackend, S3Backend
    from case_service.storage.minio_backend import MinIOBackend as legacy

    assert MinIOBackend is S3Backend is legacy


@pytest.mark.parametrize("env,expected", [
    # the install setup made before v3.1.2: warned about at every start
    ({"VELARIS_CASE_STORAGE_BACKEND": "minio", "VELARIS_MINIO_PASSWORD": "pw"}, True),
    ({"VELARIS_CASE_STORAGE_BACKEND": "s3", "VELARIS_MINIO_PASSWORD": "pw",
      "VELARIS_CASE_S3_ENDPOINT": "127.0.0.1:9000"}, True),
    # local disk — a leftover container password alone means nothing
    ({"VELARIS_CASE_STORAGE_BACKEND": "local", "VELARIS_MINIO_PASSWORD": "pw"}, False),
    ({"VELARIS_MINIO_PASSWORD": "pw"}, False),
    # an S3 endpoint the operator supplied
    ({"VELARIS_CASE_STORAGE_BACKEND": "s3", "VELARIS_CASE_S3_ENDPOINT": "nas.lan:9000",
      "VELARIS_MINIO_PASSWORD": "pw"}, False),
    ({"VELARIS_CASE_STORAGE_BACKEND": "s3", "VELARIS_CASE_S3_ENDPOINT": "localhost:8333",
      "VELARIS_CASE_S3_SECRET_KEY": "own"}, False),
])
def test_bundled_minio_is_recognised_only_where_it_is(monkeypatch, env, expected):
    monkeypatch.delenv("VELARIS_CASE_STORAGE_BACKEND", raising=False)
    s = _settings(monkeypatch, **env)
    assert factory.bundled_minio_in_use(s) is expected
