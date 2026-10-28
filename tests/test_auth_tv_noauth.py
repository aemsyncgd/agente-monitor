# tests/test_auth_tv_noauth.py
"""Acceso sin API key a los endpoints TV desde IPs no-auth (TV_NOAUTH_IPS).

La excepción se limita a: IP confiable + endpoint de /backbone o /olts + GET/HEAD.
Todo lo demás sigue exigiendo API key.
"""
import ipaddress

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import AuthMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/metrics/mikrotik")
    async def mikrotik():
        return {"ok": True}

    @app.get("/api/v1/metrics/mikrotik/interfaces")
    async def mikrotik_ifaces():
        return {"ok": True}

    @app.get("/api/clientes/00000000")
    async def clientes():
        return {"ok": True}

    @app.post("/api/v1/metrics/mikrotik")
    async def mikrotik_write():
        return {"ok": True}

    app.add_middleware(AuthMiddleware)
    return app


@pytest.fixture
def tv(monkeypatch):
    """Auth enabled + 10.0.4.6 as the only TV no-auth IP (viewer role)."""
    monkeypatch.setattr(auth_mod, "API_KEY_ADMIN", "admin-key")
    monkeypatch.setattr(auth_mod, "API_KEY_VIEWER", "")
    monkeypatch.setattr(auth_mod, "API_KEY_OPERATOR", "")
    monkeypatch.setattr(
        auth_mod, "TV_NOAUTH_NETWORKS", [ipaddress.ip_network("10.0.4.6")]
    )
    monkeypatch.setattr(auth_mod, "TV_NOAUTH_ROLE", "viewer")


def _client(app, host):
    return TestClient(app, client=(host, 50000))


# ── Endpoints TV permitidos ──────────────────────────────────────────────────

def test_tv_endpoint_get_without_key_allowed(tv):
    with _client(_make_app(), "10.0.4.6") as c:
        assert c.get("/api/v1/metrics/mikrotik").status_code == 200


def test_tv_subpath_get_without_key_allowed(tv):
    with _client(_make_app(), "10.0.4.6") as c:
        assert c.get("/api/v1/metrics/mikrotik/interfaces").status_code == 200


# ── Todo lo demás sigue protegido ────────────────────────────────────────────

def test_non_tv_endpoint_without_key_unauthorized(tv):
    # Datos de clientes NO son parte de la excepción TV.
    with _client(_make_app(), "10.0.4.6") as c:
        assert c.get("/api/clientes/00000000").status_code == 401


def test_tv_endpoint_post_without_key_unauthorized(tv):
    # La excepción es solo lectura (GET/HEAD); un POST exige clave.
    with _client(_make_app(), "10.0.4.6") as c:
        assert c.post("/api/v1/metrics/mikrotik").status_code == 401


def test_untrusted_ip_tv_endpoint_unauthorized(tv):
    with _client(_make_app(), "10.0.4.7") as c:
        assert c.get("/api/v1/metrics/mikrotik").status_code == 401


def test_valid_key_still_wins_everywhere(tv):
    with _client(_make_app(), "10.0.4.6") as c:
        assert c.get(
            "/api/clientes/00000000", headers={"X-API-Key": "admin-key"}
        ).status_code == 200
        assert c.post(
            "/api/v1/metrics/mikrotik", headers={"X-API-Key": "admin-key"}
        ).status_code == 200


def test_disabled_when_no_noauth_networks(monkeypatch):
    monkeypatch.setattr(auth_mod, "API_KEY_ADMIN", "admin-key")
    monkeypatch.setattr(auth_mod, "API_KEY_VIEWER", "")
    monkeypatch.setattr(auth_mod, "API_KEY_OPERATOR", "")
    monkeypatch.setattr(auth_mod, "TV_NOAUTH_NETWORKS", [])
    with _client(_make_app(), "10.0.4.6") as c:
        assert c.get("/api/v1/metrics/mikrotik").status_code == 401


# ── Helpers ──────────────────────────────────────────────────────────────────

def test_parse_ip_list_accepts_ip_and_cidr():
    nets = auth_mod._parse_ip_list("10.0.4.6, 10.0.5.0/24, , basura")
    assert len(nets) == 2
    assert ipaddress.ip_address("10.0.4.6") in nets[0]
    assert ipaddress.ip_address("10.0.5.42") in nets[1]


def test_is_tv_noauth_ip(monkeypatch):
    monkeypatch.setattr(
        auth_mod, "TV_NOAUTH_NETWORKS", [ipaddress.ip_network("10.0.4.6")]
    )
    assert auth_mod.is_tv_noauth_ip("10.0.4.6") is True
    assert auth_mod.is_tv_noauth_ip("10.0.4.7") is False
    assert auth_mod.is_tv_noauth_ip("") is False
    assert auth_mod.is_tv_noauth_ip("no-es-ip") is False


def test_is_tv_endpoint_allowlist():
    assert auth_mod.is_tv_endpoint("/api/v1/metrics/mikrotik") is True
    assert auth_mod.is_tv_endpoint("/api/v1/metrics/mikrotik/interfaces") is True
    assert auth_mod.is_tv_endpoint("/api/v1/metrics/olt/interfaces") is True
    assert auth_mod.is_tv_endpoint("/api/v1/anomalies/recent") is True
    assert auth_mod.is_tv_endpoint("/api/clientes/00000000") is False
    assert auth_mod.is_tv_endpoint("/api/v1/metrics/mikrotikX") is False


def test_tv_noauth_role_defaults_to_viewer_on_invalid(monkeypatch):
    monkeypatch.setattr(auth_mod, "TV_NOAUTH_ROLE", "superuser")
    assert auth_mod.tv_noauth_role() == "viewer"
    monkeypatch.setattr(auth_mod, "TV_NOAUTH_ROLE", "operator")
    assert auth_mod.tv_noauth_role() == "operator"
