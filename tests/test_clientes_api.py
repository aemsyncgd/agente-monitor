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


# === Fase 1.5: endpoints vitales (WISP+Influx vía ClientSearchService) ===

_FICHA_VITAL = {
    "documento": "V-5384589",
    "nombre": "MARIA GRACIELA GASTELLO DE HERNANDEZ",
    "direccion": "Sector El Toco casa nro 84-A",
    "telefono": "0412-1234567",
    "usuario_pppoe": "MARIAGG",
    "serial_onu": "GPON00F58B52",
    "mac": "aa:bb:cc:dd:ee:01",
    "nap": "I01313",
    "zona": "Guacara",
    "estado_onu": 3,
    "rx_power": -18.5,
}

_GRUPO_RES = {
    "scope": "OLT OLT-SISAL-2 · PON GPON0/2",
    "total": 2,
    "afectados": [
        {"serial_onu": "GPON00A1", "nombre": "Cliente Uno",
         "direccion": "Calle 1", "telefono": "0411", "documento": "V-1",
         "estado_onu": 1, "rx_power": -30.0},
    ],
    "offline": 1, "loss": 1, "low_power": 1,
    "distribucion_estados": {"estado_1": 1, "estado_3": 1},
    "fichas": [], "truncado": False,
}


class _FakeClientSearch:
    def find(self, **kw):
        if kw.get("serial") == "GPON00F58B52":
            return {"found": True, "ficha": dict(_FICHA_VITAL)}
        if kw.get("nombre") == "MARIA":
            return {"found": True, "ficha": dict(_FICHA_VITAL)}
        return {"found": False, "ficha": None}

    def group_analysis(self, **kw):
        return {k: (list(v) if isinstance(v, list) else v)
                for k, v in _GRUPO_RES.items()}


def _app_con_vital(monkeypatch):
    monkeypatch.setattr(clientes_route, "_get_client_search",
                        lambda: _FakeClientSearch())
    app = FastAPI()
    app.include_router(router)
    return app


def test_vital_por_serial(monkeypatch):
    app = _app_con_vital(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital",
                      json={"serial": "GPON00F58B52"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert data["ficha"]["nombre"] == "MARIA GRACIELA GASTELLO DE HERNANDEZ"
        assert data["ficha"]["usuario_pppoe"] == "MA***GG"


def test_vital_por_nombre(monkeypatch):
    app = _app_con_vital(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital", json={"nombre": "MARIA"})
        assert resp.status_code == 200
        assert resp.json()["ficha"]["serial_onu"] == "GPON00F58B52"


def test_vital_no_encontrado(monkeypatch):
    app = _app_con_vital(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital", json={"serial": "NOEXISTE1"})
        assert resp.status_code == 404


# === Búsqueda multi-cliente (find_many) ===

_BATCH_RES = {
    "results": [
        {"query": "GPON00F58B52", "found": True,
         "ficha": dict(_FICHA_VITAL),
         "historico_30d": {"dias_ventana": 30, "muestras_offline": 2,
                           "dias_offline": 1, "ultimo_down": "2026-09-22T18:00"},
         "infraestructura": [{"name": "OLT-SISAL-2", "ip": "10.0.0.2",
                               "type": "olt", "estado_ping": "up",
                               "ultima_medicion": "2026-09-22T12:00"}]},
        {"query": "NOEXISTE1", "found": False, "ficha": None,
         "historico_30d": {}, "infraestructura": []},
    ],
    "total": 2, "found": 1, "truncated": False,
}


class _FakeBatchSearch(_FakeClientSearch):
    def find_many(self, queries, include_history=True, include_infra=True):
        res = {k: (list(v) if isinstance(v, list) else v)
               for k, v in _BATCH_RES.items()}
        res["results"] = [dict(r) for r in _BATCH_RES["results"]]
        return res


def _app_con_batch(monkeypatch):
    monkeypatch.setattr(clientes_route, "_get_client_search",
                        lambda: _FakeBatchSearch())
    app = FastAPI()
    app.include_router(router)
    return app


def test_vital_batch_devuelve_multiples(monkeypatch):
    app = _app_con_batch(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital/batch",
                      json={"queries": ["GPON00F58B52", "NOEXISTE1"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["found"] == 1
        assert data["results"][0]["ficha"]["serial_onu"] == "GPON00F58B52"
        assert data["results"][0]["historico_30d"]["muestras_offline"] == 2
        assert data["results"][0]["infraestructura"][0]["name"] == "OLT-SISAL-2"
        assert data["results"][1]["found"] is False


def test_vital_batch_acepta_lista_plana(monkeypatch):
    app = _app_con_batch(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital/batch",
                      json=["GPON00F58B52", "NOEXISTE1"])
        assert resp.status_code == 200
        assert resp.json()["total"] == 2


def test_vital_batch_requiere_queries(monkeypatch):
    app = _app_con_batch(monkeypatch)
    with TestClient(app) as c:
        assert c.post("/api/clientes/vital/batch", json={}).status_code == 400
        assert c.post("/api/clientes/vital/batch",
                      json={"queries": []}).status_code == 400
        assert c.post("/api/clientes/vital/batch",
                      json={"queries": ["  "]}).status_code == 400


def test_vital_batch_enmascara_viewer(monkeypatch):
    import src.api.auth as auth_mod
    from src.api.auth import AuthMiddleware
    monkeypatch.setattr(clientes_route, "_get_client_search",
                        lambda: _FakeBatchSearch())
    monkeypatch.setattr(auth_mod, "API_KEY_VIEWER", "key-viewer")
    monkeypatch.setattr(auth_mod, "API_KEY_OPERATOR", "")
    monkeypatch.setattr(auth_mod, "API_KEY_ADMIN", "")
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(AuthMiddleware)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital/batch",
                      json={"queries": ["GPON00F58B52", "NOEXISTE1"]},
                      headers={"X-API-Key": "key-viewer"})
        assert resp.status_code == 200
        ficha = resp.json()["results"][0]["ficha"]
        assert ficha["usuario_pppoe"] == "MA***GG"


def test_vital_requiere_parametro(monkeypatch):
    app = _app_con_vital(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/vital", json={})
        assert resp.status_code == 400


def test_grupo_analizar(monkeypatch):
    app = _app_con_vital(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/grupo/analizar",
                      json={"olt_name": "OLT-SISAL-2", "pon_port": "GPON0/2"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["loss"] == 1
        assert data["afectados"][0]["direccion"] == "Calle 1"


def test_grupo_analizar_enmascara_viewer(monkeypatch):
    import src.api.auth as auth_mod
    from src.api.auth import AuthMiddleware
    monkeypatch.setattr(clientes_route, "_get_client_search",
                        lambda: _FakeClientSearch())
    monkeypatch.setattr(auth_mod, "API_KEY_VIEWER", "key-viewer")
    monkeypatch.setattr(auth_mod, "API_KEY_OPERATOR", "")
    monkeypatch.setattr(auth_mod, "API_KEY_ADMIN", "")
    app = FastAPI()
    app.include_router(router)
    app.add_middleware(AuthMiddleware)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/grupo/analizar",
                      json={"serials": ["GPON00A1"]},
                      headers={"X-API-Key": "key-viewer"})
        assert resp.status_code == 200
        af = resp.json()["afectados"][0]
        assert af["documento"] == "*-1"       # _mask keep=2
        assert af["telefono"] == "**11"       # _mask keep=2


def test_grupo_analizar_require_entrada(monkeypatch):
    app = _app_con_vital(monkeypatch)
    with TestClient(app) as c:
        resp = c.post("/api/clientes/grupo/analizar", json={})
        assert resp.status_code == 400


# === Fase 2: cache Redis (ClientStore) ===

class _FakeStore:
    def __init__(self, fichas):
        self._por_doc = {f["documento"]: dict(f) for f in fichas}

    def get_ficha(self, doc):
        return self._por_doc.get(doc)

    def get_fichas(self, docs):
        return [self._por_doc[d] for d in docs if d in self._por_doc]

    def los(self, estados):
        return [d for d, f in self._por_doc.items()
                if f.get("estado_onu") in set(estados)]

    def group_los(self, estados, by="zona"):
        acc = {}
        for d, f in self._por_doc.items():
            k = f.get(by) or "(sin asignar)"
            g = acc.setdefault(k, {"clave": k, "total": 0, "los": 0})
            g["total"] += 1
            if f.get("estado_onu") in set(estados):
                g["los"] += 1
        return list(acc.values())


@pytest.fixture
def redis_client(monkeypatch):
    rows = [dict(_ROW, documento="12345678"),
            dict(_ROW, documento="555", nombre="Ana Los", estado_onu=1,
                 zona="Centro", nap="NAP-1")]
    store = _FakeStore(rows)
    monkeypatch.setattr(clientes_route, "_get_store", lambda: store)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_ficha_usa_redis_primero(redis_client):
    resp = redis_client.get("/api/clientes/12345678")
    assert resp.status_code == 200
    assert resp.json()["documento"] == "12345678"


def test_ficha_cae_a_influx_si_redis_vacia(monkeypatch):
    monkeypatch.setattr(clientes_route, "_get_store",
                        lambda: _FakeStore([]))
    monkeypatch.setattr(clientes_route, "get_client_by_documento",
                        lambda doc, **kw: dict(_ROW) if doc == "12345678" else {})
    monkeypatch.setattr(clientes_route, "get_pending_by_documento",
                        lambda doc, **kw: {})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        resp = c.get("/api/clientes/12345678")
    assert resp.status_code == 200
    assert resp.json()["documento"] == "12345678"


def test_los_lista_clientes_en_loss(redis_client):
    resp = redis_client.get("/api/clientes/los")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["documento"] == "555"
    assert data[0]["estado_onu"] == 1


def test_grupo_agrega_totales_y_los(redis_client):
    resp = redis_client.get("/api/clientes/grupo", params={"by": "zona"})
    assert resp.status_code == 200
    data = resp.json()
    grupo = [g for g in data if g["clave"] == "Centro"][0]
    assert grupo["total"] == 2
    assert grupo["los"] == 1


def test_bulk_devuelve_fichas(redis_client):
    resp = redis_client.post("/api/clientes/bulk",
                             json={"documentos": ["12345678", "555"]})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert {d["documento"] for d in data} == {"12345678", "555"}