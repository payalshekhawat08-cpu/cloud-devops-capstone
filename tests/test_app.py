"""Automated tests run by the CI pipeline (SQLite, no external services)."""
import os
import re
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["APP_ENV"] = "development"

import pytest  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def token(client, path):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([^"]+)"', html).group(1)


def register(client, username="payal", password="correct-horse"):
    return client.post("/register", data={
        "_csrf": token(client, "/register"), "username": username,
        "password": password, "confirm": password}, follow_redirects=False)


def test_health_ok(client):
    resp = client.get("/health")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok" and body["database"] == "up"


def test_index_requires_login(client):
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_register_login_and_post_message(client):
    assert register(client, "alice_1").status_code == 302
    page = client.get("/")
    assert page.status_code == 200 and "alice_1" in page.get_data(as_text=True)
    client.post("/add", data={"_csrf": token(client, "/"), "body": "hello cloud"})
    assert "hello cloud" in client.get("/").get_data(as_text=True)


def test_duplicate_username_rejected(client):
    register(client, "bob_1")
    other = app_module.app.test_client()
    resp = register(other, "bob_1")
    assert resp.status_code == 200
    assert "already taken" in resp.get_data(as_text=True)


def test_weak_password_rejected(client):
    resp = register(client, "carol_1", "short")
    assert resp.status_code == 200 and "8 to 128" in resp.get_data(as_text=True)


def test_wrong_password_fails_then_right_works(client):
    register(client, "dave_1", "right-password")
    other = app_module.app.test_client()
    bad = other.post("/login", data={
        "_csrf": token(other, "/login"), "username": "dave_1", "password": "nope"})
    assert "Wrong username or password" in bad.get_data(as_text=True)
    good = other.post("/login", data={
        "_csrf": token(other, "/login"), "username": "dave_1",
        "password": "right-password"})
    assert good.status_code == 302


def test_post_without_csrf_is_blocked(client):
    resp = client.post("/login", data={"username": "x", "password": "y"})
    assert resp.status_code == 400


def test_logout_clears_session(client):
    register(client, "erin_1")
    client.post("/logout", data={"_csrf": token(client, "/")})
    assert client.get("/").status_code == 302


def test_metrics_exposed(client):
    client.get("/health")
    text = client.get("/metrics").get_data(as_text=True)
    assert "app_requests_total" in text
    assert "app_request_duration_seconds_count" in text
    assert "app_uptime_seconds" in text
