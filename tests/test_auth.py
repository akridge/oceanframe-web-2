"""Login, sessions, roles and the app-wide gate."""
from __future__ import annotations

import pytest

ADMIN = ("boss", "admin-pass-1")


@pytest.fixture
def app_client(library, monkeypatch):
    """Login switched on, with a bootstrap admin."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("OCEANFRAME_AUTH", "on")
    monkeypatch.setenv("OCEANFRAME_ADMIN_USERNAME", ADMIN[0])
    monkeypatch.setenv("OCEANFRAME_ADMIN_PASSWORD", ADMIN[1])
    from appdb.bootstrap import init_app_db

    init_app_db()
    import main

    return TestClient(main.app, base_url="http://testserver")


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _make_user(client, username, role, password="user-pass-1"):
    res = client.post("/api/auth/admin/users", json={
        "username": username, "password": password, "role": role, "display_name": username.title(),
    })
    assert res.status_code == 200, res.text
    return res.json()["user"]


def test_login_off_runs_everything_as_local_admin(library):
    from fastapi.testclient import TestClient

    import main

    client = TestClient(main.app)
    me = client.get("/api/auth/me").json()
    assert me["auth_enabled"] is False
    assert me["user"]["username"] == "local"
    assert me["user"]["role"] == "admin"
    # Pages are open and the login page just bounces home.
    assert client.get("/").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303


def test_gate_redirects_pages_and_refuses_api(app_client):
    page = app_client.get("/collections?x=1", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?next=%2Fcollections%3Fx%3D1"
    assert app_client.get("/api/library/status").status_code == 401
    assert app_client.get("/api/auth/me").status_code == 401


def test_login_keeps_a_fragment_in_next_and_refuses_other_sites(app_client):
    kept = app_client.get("/login", params={"next": "/curation?project=p1#view=matrix"})
    assert kept.status_code == 200 and 'data-next="/curation?project=p1#view=matrix"' in kept.text
    for hostile in ("//evil.example/x#a", "https://evil.example/", "/\\evil.example"):
        refused = app_client.get("/login", params={"next": hostile})
        assert 'data-next="/"' in refused.text


def test_exempt_paths_stay_open(app_client):
    from appdb.engine import dialect_name

    assert app_client.get("/healthz").json() == {"status": "ok", "app_db": dialect_name()}
    assert app_client.get("/login").status_code == 200
    assert app_client.get("/static/ui/tokens.css").status_code == 200


def test_login_sets_httponly_cookie_and_logout_clears_it(app_client):
    res = _login(app_client, *ADMIN)
    assert res.status_code == 200
    cookie = res.headers["set-cookie"]
    assert "oceanframe_session=" in cookie
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert app_client.get("/api/auth/me").json()["user"]["username"] == ADMIN[0]

    app_client.post("/api/auth/logout")
    assert app_client.get("/api/auth/me").status_code == 401


def test_only_the_token_hash_is_stored(app_client):
    import hashlib

    from appdb import session_scope
    from auth.models import AuthSession

    _login(app_client, *ADMIN)
    token = app_client.cookies.get("oceanframe_session")
    with session_scope() as s:
        stored = [row.token_hash for row in s.query(AuthSession).all()]
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


def test_wrong_password_and_rate_limit(app_client):
    for _ in range(10):
        assert _login(app_client, ADMIN[0], "nope-nope").status_code == 401
    # Even the right password is refused while the brake is on.
    assert _login(app_client, *ADMIN).status_code == 429


def test_viewer_cannot_write_library(app_client):
    _login(app_client, *ADMIN)
    _make_user(app_client, "vera", "viewer")
    app_client.post("/api/auth/logout")

    _login(app_client, "vera", "user-pass-1")
    assert app_client.get("/api/library/sources").status_code == 200
    assert app_client.post("/api/library/tags", json={"asset_ids": [1], "tags": ["x"]}).status_code == 403
    assert app_client.delete("/api/library/sources/1").status_code == 403
    assert app_client.get("/users", follow_redirects=False).status_code == 403


def test_curator_tags_but_cannot_manage_collections(app_client):
    _login(app_client, *ADMIN)
    _make_user(app_client, "cora", "curator")
    app_client.post("/api/auth/logout")

    _login(app_client, "cora", "user-pass-1")
    assert app_client.post("/api/library/tags", json={"asset_ids": [], "tags": ["x"]}).status_code != 403
    assert app_client.delete("/api/library/sources/999").status_code == 403


def test_last_admin_cannot_be_demoted_or_deactivated(app_client):
    _login(app_client, *ADMIN)
    me = app_client.get("/api/auth/me").json()["user"]
    res = app_client.patch(f"/api/auth/admin/users/{me['id']}", json={"role": "lead"})
    assert res.status_code == 400 and "no active admin" in res.json()["detail"]
    res = app_client.patch(f"/api/auth/admin/users/{me['id']}", json={"is_active": False})
    assert res.status_code == 400

    # With a second admin, demotion is fine.
    _make_user(app_client, "deputy", "admin")
    assert app_client.patch(f"/api/auth/admin/users/{me['id']}", json={"role": "lead"}).status_code == 200


def test_deactivation_ends_existing_sessions(app_client):
    from fastapi.testclient import TestClient

    import main

    _login(app_client, *ADMIN)
    user = _make_user(app_client, "sam", "curator")

    other = TestClient(main.app)
    assert _login(other, "sam", "user-pass-1").status_code == 200
    assert other.get("/api/auth/me").status_code == 200

    app_client.patch(f"/api/auth/admin/users/{user['id']}", json={"is_active": False})
    assert other.get("/api/auth/me").status_code == 401
    assert _login(other, "sam", "user-pass-1").status_code == 401


def test_password_change_signs_out_other_devices(app_client):
    from fastapi.testclient import TestClient

    import main

    _login(app_client, *ADMIN)
    _make_user(app_client, "pat", "curator")
    laptop, desktop = TestClient(main.app), TestClient(main.app)
    _login(laptop, "pat", "user-pass-1")
    _login(desktop, "pat", "user-pass-1")

    res = laptop.put("/api/auth/password", json={"old_password": "user-pass-1", "new_password": "new-pass-22"})
    assert res.status_code == 200
    assert laptop.get("/api/auth/me").status_code == 200  # fresh session for this one
    assert desktop.get("/api/auth/me").status_code == 401
    assert _login(desktop, "pat", "new-pass-22").status_code == 200


def test_cross_site_writes_are_refused(app_client):
    _login(app_client, *ADMIN)
    evil = app_client.post("/api/auth/admin/users", headers={"Origin": "https://evil.example"},
                           json={"username": "mallory", "password": "xxxxxxxx1", "role": "admin"})
    assert evil.status_code == 403
    same = app_client.post("/api/auth/admin/users", headers={"Origin": "http://testserver"},
                           json={"username": "friend", "password": "xxxxxxxx1", "role": "viewer"})
    assert same.status_code == 200


def test_login_next_is_same_site_only():
    from auth.middleware import safe_next

    assert safe_next("/collections?x=1") == "/collections?x=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "", None):
        assert safe_next(bad) == "/"


def test_bootstrap_admin_is_idempotent_and_never_overwrites(app_client, monkeypatch):
    from appdb.bootstrap import init_app_db

    monkeypatch.setenv("OCEANFRAME_ADMIN_PASSWORD", "a-different-one")
    init_app_db()
    assert _login(app_client, *ADMIN).status_code == 200
    users = app_client.get("/api/auth/admin/users").json()["users"]
    assert [u["username"] for u in users] == [ADMIN[0]]


def test_starter_users_are_created_once_and_never_overwritten(app_client, monkeypatch):
    from appdb.bootstrap import init_app_db

    monkeypatch.setenv("OCEANFRAME_STARTER_USERS", "test:lead:lead-pass-1, user1:curator:curator-pass-1,bad,"
                                                   "weak:curator:x,odd:boss:boss-pass-1")
    init_app_db()
    assert _login(app_client, *ADMIN).status_code == 200
    roles = {u["username"]: u["role"] for u in app_client.get("/api/auth/admin/users").json()["users"]}
    assert roles == {ADMIN[0]: "admin", "test": "lead", "user1": "curator"}

    monkeypatch.setenv("OCEANFRAME_STARTER_USERS", "test:viewer:changed-pass-1")
    init_app_db()
    roles = {u["username"]: u["role"] for u in app_client.get("/api/auth/admin/users").json()["users"]}
    assert roles["test"] == "lead"
    app_client.post("/api/auth/logout")
    assert _login(app_client, "test", "lead-pass-1").status_code == 200


def test_anyone_can_sign_up_and_starts_as_a_curator(app_client):
    page = app_client.get("/login")
    assert 'id="signup-form"' in page.text
    res = app_client.post("/api/auth/signup", json={
        "username": "newbie", "password": "new-pass-1", "display_name": "New Person", "role": "admin",
    })
    assert res.status_code == 200, res.text
    me = app_client.get("/api/auth/me").json()["user"]   # signed straight in
    assert me["username"] == "newbie" and me["role"] == "curator" and me["label"] == "New Person"
    assert app_client.post("/api/auth/signup", json={"username": "newbie", "password": "another-1"}).status_code == 400
    assert app_client.post("/api/auth/signup", json={"username": "short", "password": "x"}).status_code == 400


def test_signup_role_is_capped_and_signup_can_be_turned_off(app_client, monkeypatch):
    monkeypatch.setenv("OCEANFRAME_SIGNUP_ROLE", "admin")      # not allowed: falls back to curator
    app_client.post("/api/auth/signup", json={"username": "sneaky", "password": "pass-word-1"})
    assert app_client.get("/api/auth/me").json()["user"]["role"] == "curator"

    monkeypatch.setenv("OCEANFRAME_SIGNUP_ROLE", "viewer")
    app_client.post("/api/auth/logout")
    app_client.post("/api/auth/signup", json={"username": "reader", "password": "pass-word-1"})
    assert app_client.get("/api/auth/me").json()["user"]["role"] == "viewer"

    monkeypatch.setenv("OCEANFRAME_ALLOW_SIGNUP", "off")
    assert app_client.post("/api/auth/signup", json={"username": "late", "password": "pass-word-1"}).status_code == 403
    assert 'id="signup-form"' not in app_client.get("/login").text
