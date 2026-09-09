# tests/test_clientes_api.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import clientes as clientes_route
from src.api.routes.clientes import router

_ROW = {
    "documento": "12345678",
    "usuario_pppoe": "ab123456cd",
    "serial_onu": "OEMT-001",
    "mac": "aa:bb:cc:dd:ee:01",
    "nombre": "Juan Perez",
    "direccion": "Calle 1",
    "olt": "OLT-1",
    "nodo": "N1",
    "nap": "NAP-1",
    "zona": "Centro",
    "estado_onu": 3,
    "coincidencia": "serial",
    "ubicacion_descuadrada": "false",
    "telefono": "5551234",
    "time": "2026-09-09T10:00:00Z",
    "pon_port": "GPON0/5",
    "pendiente": "false",
    "estado_servicio": "Activo",
    "pppoe_status": "up",
}

_PEND_ROW = {
    "documento": "9999",
    "usuario_pppoe": "cc999999",
    "serial_onu": "OEMT-999",
    "mac": "",
    "nombre": "Yulimar Rojas",
    "direccion": "",
    "olt": "",
    "nodo": "",
    "nap": "NAP-9",
    "zona": "Norte",
    "estado_onu": -1,
    "coincidencia": "",
    "ubicacion_descuadrada": "false",
    "telefono": "",
    "time": "2026-09-09T09:00:00Z",
    "pon_port": "",
    "pendiente": "true",
    "causa": "sin_olt",
    "estado_servicio": "",
    "pppoe_status": "",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(clientes_route, "search_clientes",
                        lambda **kw: [dict(_ROW)])
    monkeypatch.setattr(clientes_route, "get_enriched_clients",
                        lambda **kw: [dict(_ROW)])
    monkeypatch.setattr(clientes_route, "get_pending_clients",
                        lambda **kw: [dict(_PEND_ROW)])
    monkeypatch.setattr(clientes_route, "get_pending_by_documento",
                        lambda doc, **kw: dict(_PEND_ROW) if doc == "9999" else {})
    monkeypatch.setattr(clientes_route, "get_clientes_stats",
                        lambda **kw: {"enriquecidos": 1, "descuadrados": 0,
                                      "pendientes": 1})
    monkeypatch.setattr(clientes_route, "get_client_by_documento",
                        lambda doc, **kw: dict(_ROW) if doc == "12345678" else {})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_buscar(client):
    resp = client.get("/api/clientes/buscar", params={"q": "perez"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    # pppoe enmascarado para todos los roles (10 chars -> 6 asteriscos)
    assert data[0]["usuario_pppoe"] == "ab******cd"
    assert data[0]["pon_port"] == "GPON0/5"
    assert data[0]["pendiente"] is False
    assert data[0]["estado_servicio"] == "Activo"


def test_buscar_incluye_pendientes(monkeypatch):
    monkeypatch.setattr(clientes_route, "search_clientes",
                        lambda **kw: [dict(_ROW), dict(_PEND_ROW)])
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        resp = c.get("/api/clientes/buscar", params={"q": "rojas"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        pend = [d for d in data if d["pendiente"]]
        assert len(pend) == 1
        assert pend[0]["documento"] == "9999"
        assert pend[0]["estado_onu"] == -1
        assert pend[0]["usuario_pppoe"] == "cc****99"


def test_buscar_requiere_q(client):
    resp = client.get("/api/clientes/buscar", params={"q": ""})
    assert resp.status_code == 422


def test_ficha_encontrado(client):
    resp = client.get("/api/clientes/12345678")
    assert resp.status_code == 200
    data = resp.json()
    assert data["documento"] == "12345678"
    assert data["usuario_pppoe"] == "ab******cd"
    assert data["pon_port"] == "GPON0/5"
    assert data.get("pendiente") is False


def test_ficha_pendiente_fallback(client):
    resp = client.get("/api/clientes/9999")
    assert resp.status_code == 200
    data = resp.json()
    assert data["documento"] == "9999"
    assert data["pendiente"] is True
    assert data["estado_onu"] == -1
    assert data["causa"] == "sin_olt"


def test_ficha_no_encontrado(client):
    resp = client.get("/api/clientes/00000000")
    assert resp.status_code == 404


def test_stats(client):
    resp = client.get("/api/clientes/stats")
    assert resp.status_code == 200
    assert resp.json() == {"enriquecidos": 1, "descuadrados": 0, "pendientes": 1}


def test_pendientes(client):
    resp = client.get("/api/clientes/pendientes")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["cause"] == "sin_olt"
    assert data[0]["documento"] == "9999"


def test_listar_filtros(client):
    resp = client.get("/api/clientes", params={"nap": "NAP-1"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_mask_pppoe_short():
    assert clientes_route._mask_pppoe("ab") == "ab"
    assert clientes_route._mask_pppoe("abcdef") == "ab**ef"


def test_mask_keep_2():
    assert clientes_route._mask("5551234") == "*****34"
    assert clientes_route._mask("") == ""
    assert clientes_route._mask("12") == "**"


def test_viewer_oculta_documento_telefono(monkeypatch):
    rows = [dict(_ROW)]
    monkeypatch.setattr(clientes_route, "get_enriched_clients", lambda **kw: rows)
    app = FastAPI()

    class FakeRequest:
        state = type("S", (), {"role": "viewer"})()

    app.include_router(router)

    # Simular request con rol viewer a traves del state de la aplicacion
    from src.api.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)
    with TestClient(app) as c:
        # middleware resetea role a admin (sin API_KEY) -> probar _apply_masking directo
        pass
    out = clientes_route._apply_masking(dict(_ROW), "viewer")
    assert out["documento"] == "******78"
    assert out["telefono"] == "*****34"
    admin_out = clientes_route._apply_masking(dict(_ROW), "admin")
    assert admin_out["documento"] == "12345678"
    assert admin_out["telefono"] == "5551234"