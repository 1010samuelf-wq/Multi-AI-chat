import os
import tempfile
import time

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["APP_PASSWORD"] = "admin-pass-123"
os.environ["MOCK_LLM"] = "1"

import importlib

import pytest
from fastapi.testclient import TestClient

from app import auth, config


@pytest.fixture(scope="module")
def client():
    # Reload so the settings above are picked up even if other tests imported first.
    importlib.reload(config)
    importlib.reload(auth)
    from app import main
    importlib.reload(main)
    main.registry.linger = 0
    with TestClient(main.app) as c:
        yield c


def login(c, user, pw):
    return c.post("/api/login", json={"username": user, "password": pw})


def test_password_hashing():
    h = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert "correct horse" not in h


def test_admin_created_and_login(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/debates").status_code == 401
    assert login(client, "admin", "nope").status_code == 401
    r = login(client, "admin", "admin-pass-123")
    assert r.status_code == 200 and r.json()["is_admin"]
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/api/me").json()["username"] == "admin"


def test_users_are_separate(client):
    login(client, "admin", "admin-pass-123")
    assert client.post("/api/users", json={"username": "sara", "password": "short"}).status_code == 400
    assert client.post("/api/users", json={"username": "sara", "password": "sara-pass-1"}).status_code == 200
    assert client.post("/api/users", json={"username": "SARA", "password": "sara-pass-1"}).status_code == 409

    d = client.post("/api/debates", json={"question": "admin's question", "max_turns": 2}).json()["id"]
    time.sleep(1.5)

    sara = TestClient(client.app)
    assert login(sara, "sara", "sara-pass-1").status_code == 200
    assert not sara.get("/api/me").json()["is_admin"]
    assert sara.get("/api/debates").json() == []
    assert sara.get(f"/api/debates/{d}").status_code == 404
    assert sara.get(f"/api/debates/{d}/events").status_code == 404
    assert sara.post(f"/api/debates/{d}/stop").status_code == 404
    # Admin-only endpoints
    assert sara.get("/api/users").status_code == 403
    assert sara.post("/api/settings/key", json={"anthropic_api_key": "sk-ant-xxxxxxxxxx"}).status_code == 403
    # Sara's own chat is hers only
    s = sara.post("/api/debates", json={"question": "sara's question", "max_turns": 2}).json()["id"]
    assert [x["id"] for x in sara.get("/api/debates").json()] == [s]
    assert client.get(f"/api/debates/{s}").status_code == 404

    users = {u["username"]: u for u in client.get("/api/users").json()}
    assert users["sara"]["chats"] == 1 and users["admin"]["chats"] == 1


def test_change_and_reset_password(client):
    login(client, "admin", "admin-pass-123")
    sid = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "sara")
    sara = TestClient(client.app)
    login(sara, "sara", "sara-pass-1")
    assert sara.post("/api/me/password", json={"current_password": "bad", "new_password": "x" * 9}).status_code == 400
    assert sara.post("/api/me/password", json={"current_password": "sara-pass-1", "new_password": "sara-pass-2"}).status_code == 200
    assert login(TestClient(client.app), "sara", "sara-pass-2").status_code == 200
    # Admin reset signs her out everywhere
    assert client.post(f"/api/users/{sid}/password", json={"password": "sara-pass-3"}).status_code == 200
    assert sara.get("/api/me").status_code == 401
    assert login(sara, "sara", "sara-pass-3").status_code == 200
    # Admin can't delete themselves; can delete sara
    me = client.get("/api/me").json()["id"]
    assert client.delete(f"/api/users/{me}").status_code == 400
    assert client.delete(f"/api/users/{sid}").status_code == 200
    assert sara.get("/api/me").status_code == 401


def test_logout_and_rate_limit(client):
    c = TestClient(client.app)
    login(c, "admin", "admin-pass-123")
    c.post("/api/logout")
    assert c.get("/api/me").status_code == 401
    codes = [login(c, "ghost", "wrong").status_code for _ in range(11)]
    assert codes[0] == 401 and codes[-1] == 429
    # Once limited, even the right password is refused for a while.
    assert login(c, "admin", "admin-pass-123").status_code == 429
    auth._fails.clear()


def test_turn_steps_are_saved(client):
    login(client, "admin", "admin-pass-123")
    d = client.post("/api/debates", json={"question": "steps?", "max_turns": 3}).json()["id"]
    for _ in range(60):
        if client.get(f"/api/debates/{d}").json()["status"] != "running":
            break
        time.sleep(0.2)
    from app import store
    ends = [e for e in store.get_events(d) if e["type"] == "turn_end"]
    kinds = [s["kind"] for s in ends[0]["steps"]]
    assert kinds[:2] == ["thinking", "text"]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert any("VERDICT" in s.get("text", "") for s in ends[0]["steps"] if s["kind"] == "text")


def test_followup_api(client):
    login(client, "admin", "admin-pass-123")
    d = client.post("/api/debates", json={"question": "first", "max_turns": 3}).json()["id"]
    for _ in range(60):
        if client.get(f"/api/debates/{d}").json()["status"] != "running":
            break
        time.sleep(0.2)
    r = client.post(f"/api/debates/{d}/followup", json={"question": "second", "max_turns": 3})
    assert r.status_code == 200 and r.json()["id"] == d
    # Can't stack a follow-up on a running round
    assert client.post(f"/api/debates/{d}/followup", json={"question": "third"}).status_code == 409
    other = TestClient(client.app)
    client.post("/api/users", json={"username": "omar", "password": "omar-pass-1"})
    login(other, "omar", "omar-pass-1")
    assert other.post(f"/api/debates/{d}/followup", json={"question": "x"}).status_code == 404
    for _ in range(60):
        if client.get(f"/api/debates/{d}").json()["status"] != "running":
            break
        time.sleep(0.2)
    from app import store
    types = [e["type"] for e in store.get_events(d)]
    assert types.count("end") == 2 and "followup" in types
