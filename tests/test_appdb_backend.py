# tests/test_appdb_backend.py
from __future__ import annotations

import pytest


def test_backend_name_reads_configuration_only(library, monkeypatch):
    from appdb import engine

    monkeypatch.delenv("OCEANFRAME_DB_URL", raising=False)
    monkeypatch.delenv("OCEANFRAME_DB_USER", raising=False)
    monkeypatch.delenv("OCEANFRAME_DB_DSN", raising=False)
    assert engine.backend_name() == "sqlite"
    assert engine.describe().startswith("sqlite (")

    monkeypatch.setenv("OCEANFRAME_DB_USER", "oceanframe")
    monkeypatch.setenv("OCEANFRAME_DB_PASSWORD", "Secret#1")
    monkeypatch.setenv("OCEANFRAME_DB_DSN", "oceanframe-oracle:1521/FREEPDB1")
    assert engine.backend_name() == "oracle"
    described = engine.describe()
    assert described == "oracle (oceanframe-oracle:1521/FREEPDB1)"
    assert "Secret" not in described


def test_strict_mode_refuses_sqlite(library, monkeypatch):
    from appdb import engine
    from appdb.bootstrap import init_app_db

    if engine.dialect_name() != "sqlite":
        pytest.skip("strict mode only refuses SQLite")
    monkeypatch.setenv("OCEANFRAME_REQUIRE_ORACLE", "on")
    with pytest.raises(RuntimeError, match="OCEANFRAME_REQUIRE_ORACLE"):
        init_app_db()


def test_healthz_names_the_backend(library):
    from fastapi.testclient import TestClient

    import main
    from appdb.engine import dialect_name

    body = TestClient(main.app).get("/healthz").json()
    assert body == {"status": "ok", "app_db": dialect_name()}


def test_sqlite_badge_on_curation_page(library):
    from fastapi.testclient import TestClient

    import main
    from appdb.engine import dialect_name

    html = TestClient(main.app).get("/curation").text
    assert ("Local SQLite database" in html) == (dialect_name() == "sqlite")


def test_curation_page_has_the_registry_ui(library):
    from fastapi.testclient import TestClient

    import main
    from curation import settings as curation_settings

    html = TestClient(main.app).get("/curation").text
    for needle in ('id="hub-dialog"', 'id="hub-registry-button"', '/static/curation/hub.js'):
        assert needle in html
    assert f'<code id="hub-env-root">{curation_settings.split_root()}</code>' in html
