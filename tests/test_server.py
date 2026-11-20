import pytest
from fastapi.testclient import TestClient

import server
from actions import CATALOG


@pytest.fixture
def client(monkeypatch):
    async def no_warmup():
        return None

    monkeypatch.setattr(server, "_prepare", no_warmup)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as test_client:
        yield test_client


def test_rejects_foreign_host_header(client):
    response = client.get("/api/skills", headers={"host": "attacker.example:8765"})
    assert response.status_code == 403


def test_rejects_writes_without_dashboard_header(client):
    response = client.post("/api/missions", json={"goal": "make a folder"})
    assert response.status_code == 403


def test_rejects_cross_origin_writes(client):
    response = client.post(
        "/api/missions",
        json={"goal": "make a folder"},
        headers={"x-case-client": "dashboard", "origin": "https://attacker.example"},
    )
    assert response.status_code == 403


def test_skills_and_dashboard_are_served(client):
    skills = client.get("/api/skills").json()["skills"]
    assert len(skills) == len([a for a in CATALOG.values() if not a.internal])
    assert "reply" not in {s["name"] for s in skills}
    assert {"open_in_vscode", "pages_create", "send_whatsapp"} <= {s["name"] for s in skills}
    assert {s["risk"] for s in skills} == {"read", "reversible", "one-way"}
    assert "Mission Control" in client.get("/").text


def test_capture_names_are_strictly_validated(client):
    assert client.get("/api/captures/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/api/captures/notamission-1.png").status_code == 404


def test_unknown_commands_and_missions_404(client):
    headers = {"x-case-client": "dashboard"}
    assert client.post("/api/missions/nope/approve", headers=headers).status_code == 404
    assert client.post("/api/missions/nope/explode", headers=headers).status_code == 404


def test_save_as_routine_route_is_reachable(client):
    headers = {"x-case-client": "dashboard"}
    response = client.post("/api/missions/nope/routine", json={"when": "every day at 9am"}, headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Mission not found"


def test_delete_clear_and_restart_endpoints(client, monkeypatch):
    headers = {"x-case-client": "dashboard"}
    assert client.post("/api/missions/nope/delete", headers=headers).status_code == 404
    assert client.post("/api/history/clear", headers=headers).json() == {"deleted": []}
    restarted = []
    monkeypatch.setattr(server, "_restart_process", lambda: restarted.append(True))
    server.engine.active_id = "busy-mission"
    assert client.post("/api/restart", headers=headers).status_code == 409
    server.engine.active_id = None
    response = client.post("/api/restart", headers=headers)
    assert response.status_code == 200 and response.json()["restarting"] is True
    assert client.get("/api/status").json()["started_at"] == response.json()["started_at"]
    assert client.post("/api/restart").status_code == 403
