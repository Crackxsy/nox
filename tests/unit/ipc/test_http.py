"""HTTP app: /health without auth, /api/* with bearer token, placeholders, headers, uvicorn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from nox.ipc.http import SECURITY_HEADERS, HttpServer, HttpSettings, create_app
from nox.ipc.server import read_runtime_info

TOKEN = "session-token-for-tests-0123456789"


def _app(pet: Path | None = None, dashboard: Path | None = None) -> Any:
    async def health() -> dict[str, Any]:
        return {"status": "running", "components": {"ipc": "available"}}

    def state(path: str | None) -> dict[str, Any]:
        return {"path": path, "version": 3}

    def providers() -> list[Any]:
        return [{"id": "rules", "available": True}]

    return create_app(
        health=health,
        state=state,
        providers=providers,
        session_token=lambda: TOKEN,
        pet_dist=pet,
        dashboard_dist=dashboard,
    )


@pytest.fixture
async def client() -> Any:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as c:
        yield c


async def test_health_without_auth(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert TOKEN not in r.text
    assert r.headers["cache-control"] == "no-store"


async def test_api_state_requires_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/state")
    assert r.status_code == 401
    assert r.json()["error"] == "auth.denied"
    assert r.headers["www-authenticate"] == "Bearer"
    r = await client.get("/api/state", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    r = await client.get("/api/state", headers={"Authorization": "Basic " + TOKEN})
    assert r.status_code == 401
    r = await client.get(
        "/api/state", headers={"Authorization": f"Bearer {TOKEN}"}, params={"path": "pet"}
    )
    assert r.status_code == 200
    assert r.json() == {"path": "pet", "version": 3}


async def test_api_providers_and_unknown(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/providers")
    assert r.status_code == 401
    r = await client.get("/api/providers", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()[0]["id"] == "rules"
    r = await client.get("/api/whatever", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 404
    r = await client.get("/api/whatever")
    assert r.status_code == 401


async def test_security_headers_on_ui_and_api(client: httpx.AsyncClient) -> None:
    for path, headers in (
        ("/pet", {}),
        ("/dashboard", {}),
        ("/api/state", {"Authorization": f"Bearer {TOKEN}"}),
        ("/api/state", {}),
    ):
        r = await client.get(path, headers=headers)
        for key, value in SECURITY_HEADERS.items():
            assert r.headers.get(key) == value, (path, key)
    r = await client.get("/health")
    assert "referrer-policy" not in r.headers  # hardening targets the token-bearing surfaces


async def test_placeholder_when_ui_not_built(client: httpx.AsyncClient) -> None:
    r = await client.get("/pet")
    assert r.status_code == 503
    assert "not built" in r.text
    assert "<script" not in r.text
    r = await client.get("/dashboard")
    assert r.status_code == 503 and "dashboard" in r.text


async def test_static_ui_served_when_built(tmp_path: Path) -> None:
    pet = tmp_path / "pet-dist"
    pet.mkdir()
    (pet / "index.html").write_text("<!doctype html><title>pet</title><div id=app></div>")
    (pet / "app.js").write_text("console.log('pet')")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(pet=pet)), base_url="http://test"
    ) as c:
        r = await c.get("/pet/")
        assert r.status_code == 200 and "<title>pet</title>" in r.text
        assert r.headers["x-content-type-options"] == "nosniff"
        r = await c.get("/pet/app.js")
        assert r.status_code == 200 and "console.log" in r.text
        r = await c.get("/pet/../../etc/passwd")
        assert r.status_code in (404, 400)
        r = await c.get("/dashboard")
        assert r.status_code == 503


def test_settings() -> None:
    s = HttpSettings.from_config({"host": "127.0.0.1", "port": 47800, "http_port": 47801})
    assert s.port == 47801
    with pytest.raises(ValidationError):
        HttpSettings(host="10.0.0.1")


async def test_uvicorn_server_start_stop(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    server = HttpServer(_app(), HttpSettings(port=0), runtime_dir=runtime)
    await server.start()
    port = server.port
    try:
        assert read_runtime_info(runtime)["http_port"] == server.port
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{server.url}/health")
            assert r.status_code == 200 and r.json()["status"] == "running"
            assert "server" not in r.headers
            r = await c.get(f"{server.url}/api/state", headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.status_code == 200
            assert r.headers["referrer-policy"] == "no-referrer"
    finally:
        await server.stop()
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        async with httpx.AsyncClient() as c:
            await c.get(f"http://127.0.0.1:{port}/health", timeout=2)
    await server.stop()  # idempotent
