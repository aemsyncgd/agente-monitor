# tests/test_wisp_client.py
import csv
import json
import os
import time

from src.wisp_client import (normalize_client, load_from_csv, WispClient,
                             extract_czwisp_servicios)
from src.config_ia import WISPConfig, load_config_ia


def _default_cfg(**overrides) -> WISPConfig:
    cfg = WISPConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_normalize_client_maps_keys():
    raw = {
        "Nombre Cliente": "Juan Perez",
        "Documento": "12345678",
        "Serial Onu": "OEMT1234",
        "Usuario PPPoE": "ABcd12",
        "Direccion Servicio": "Calle 1",
        "telefono": "5551234",
        "plan": "Plan 50M",
        "Estado": "Activo",
        "Ip Servicio": "10.0.0.5",
        "nombre_nap": "NAP-12",
        "caja_puerto": "3",
        "zona": "Centro",
    }
    c = normalize_client(raw)
    assert c["documento"] == "12345678"
    assert c["serial_onu"] == "OEMT1234".upper()
    assert c["usuario_pppoe"] == "abcd12"
    assert c["nombre"] == "Juan Perez"
    assert c["direccion"] == "Calle 1"
    assert c["telefono"] == "5551234"
    assert c["plan"] == "Plan 50M"
    assert c["estado"] == "activo"
    assert c["ip_servicio"] == "10.0.0.5"
    assert c["nap_nombre"] == "NAP-12"
    assert c["puerto"] == "3"
    assert c["zona"] == "Centro"


def test_load_from_csv_normalizes(tmp_path):
    path = tmp_path / "clientes.csv"
    path.write_text(
        "Documento,Serial Onu,Usuario PPPoE,Nombre Cliente\n"
        "1111,OEMTAAA,user01,Maria\n"
        "2222,OEMTBBB,user02,Pedro\n",
        encoding="utf-8",
    )
    rows = load_from_csv(str(path))
    assert len(rows) == 2
    assert rows[0]["serial_onu"] == "OEMTAAA"
    assert rows[0]["usuario_pppoe"] == "user01"


def test_load_from_csv_missing_returns_empty():
    assert load_from_csv("/no/existe.csv") == []


def test_fetch_list_paginates(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache.json")

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    calls = []

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self._items = [{"documento": f"{i:04d}", "serial_onu": f"SER{i}"}
                           for i in range(15)]

        def get(self, url, timeout=None):
            calls.append(url)
            page = 1
            if "page=" in url:
                page = int(url.split("page=")[1].split("&")[0])
            chunk = self._items[(page - 1) * 10: page * 10]
            return FakeResp({"data": chunk, "page": page, "total": 2})

    cfg = _default_cfg(
        base_url="https://czwisp.test",
        isp_slug="vidanet",
        list_endpoint="/api/clients/{slug}",
        page_size=10,
        cache_hours=0,
    )
    client = WispClient(cfg, api_key="k", session=FakeSession(), cache_path=cache)
    clients = client.fetch_list()
    assert len(clients) == 15
    assert len(calls) == 2
    assert all("page=" in u for u in calls)


def test_empty_list_endpoint_returns_empty():
    cfg = _default_cfg(list_endpoint="")
    w = WispClient(cfg, api_key="k")
    assert w.fetch_list() == []


def test_list_clients_fallback_to_csv(tmp_path):
    csv_path = tmp_path / "semilla.csv"
    csv_path.write_text(
        "Documento,Serial Onu,Usuario PPPoE\n7777,OEMTZZZ,user77\n",
        encoding="utf-8",
    )
    cfg = _default_cfg(
        documents_csv=str(csv_path),
        cache_hours=0,
        list_endpoint="",
    )
    w = WispClient(cfg, api_key="k", cache_path=str(tmp_path / "cache.json"))
    clients = w.list_clients()
    assert len(clients) == 1
    assert clients[0]["documento"] == "7777"


def test_cached_list_used(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps([{"documento": "1", "serial_onu": "A"}], ensure_ascii=False),
                     encoding="utf-8")
    cfg = _default_cfg(cache_hours=24, list_endpoint="")
    w = WispClient(cfg, api_key="k", cache_path=str(cache))
    clients = w.list_clients()
    assert clients == [{"documento": "1", "serial_onu": "A"}]


def test_parse_config_defaults():
    os.environ.setdefault("CONFIG_PATH", "config/config_ia.yaml")
    cfg = load_config_ia("config/config_ia.yaml")
    assert cfg.wisp.enabled is True


def test_onu_suffix():
    from src.fault_detector import OltStatusRefresher
    assert OltStatusRefresher.onu_suffix("3_01") == "3.01"
    assert OltStatusRefresher.onu_suffix("7_10") == "7.10"
    assert OltStatusRefresher.onu_suffix("") == ""


# ---------------------------------------------------------------------------
# Respuesta anidada czwisp: {encontrado, total, servicios:[{cliente, servicio,
# plan, red, facturacion}]}
# ---------------------------------------------------------------------------

def _czwisp_payload(serial="CMDCB00A3095", documento="12345678",
                    usuario="V12345678", encontrado=True):
    return {
        "encontrado": encontrado,
        "total": 1 if encontrado else 0,
        "servicios": [{
            "cliente": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "nombre_cliente": "Juan Perez Rodriguez",
                "tipo_documento": "V",
                "documento": documento,
                "codigo_cliente": "C-1001",
                "telefonos": ["0412-5550001", "0414-0000002"],
                "emails": ["juan@example.com", "ventas@example.com"],
            },
            "servicio": {
                "estado": "ACTIVO",
                "direccion_servicio": "Av. Principal, Casa 5, Sector Centro",
                "coordenadas": "10.4806,-66.9036",
                "sucursal_nombre": "NARANJILLOS",
                "fecha_instalacion": "2024-03-15",
                "instalador": "Carlos T",
            },
            "plan": {
                "nombre": "Plan Fibra 200M",
                "tecnologia": "PON",
                "precio_usd": 25.50,
                "velocidad_down_mbps": 200,
                "velocidad_up_mbps": 100,
            },
            "red": {
                "modo": "pppoe",
                "ip_servicio": "10.20.30.40",
                "usuario_pppoe": usuario,
                "mac_router": None,
                "serial_onu": serial,
                "nodo_nombre": "NODO-NARANJILLOS",
                "nap_nombre": "NAP-15",
                "puerto": "15",
                "puerto_pon": "GPON0/3",
            },
            "facturacion": {
                "dia_facturacion": 5,
                "fecha_corte": "2026-09-25",
                "saldo_usd": 0.0,
                "saldo_ves": 0.0,
            },
        }],
    }


def test_extract_czwisp_servicios_flattens():
    services = extract_czwisp_servicios(_czwisp_payload())
    assert len(services) == 1
    s = services[0]
    assert s["nombre_cliente"] == "Juan Perez Rodriguez"
    assert s["serial_onu"] == "CMDCB00A3095"
    assert s["usuario_pppoe"] == "V12345678"
    assert s["direccion_servicio"] == "Av. Principal, Casa 5, Sector Centro"
    assert s["velocidad_down_mbps"] == 200
    assert s["precio_usd"] == 25.50
    assert s["telefonos"] == ["0412-5550001", "0414-0000002"]


def test_extract_czwisp_servicios_no_encontrado():
    assert extract_czwisp_servicios(_czwisp_payload(encontrado=False)) == []
    assert extract_czwisp_servicios({"error": "x"}) == []


def test_normalize_client_czwisp_extended():
    services = extract_czwisp_servicios(_czwisp_payload())
    c = normalize_client(services[0])
    assert c["documento"] == "12345678"
    assert c["serial_onu"] == "CMDCB00A3095"
    assert c["usuario_pppoe"] == "v12345678"
    assert c["nombre"] == "Juan Perez Rodriguez"
    assert c["direccion"] == "Av. Principal, Casa 5, Sector Centro"
    assert c["telefono"] == "0412-5550001, 0414-0000002"
    assert c["email"] == "juan@example.com, ventas@example.com"
    assert c["plan"] == "Plan Fibra 200M"
    assert c["tecnologia"] == "PON"
    assert c["velocidad_down_mbps"] == "200"
    assert c["velocidad_up_mbps"] == "100"
    assert c["precio_usd"] == "25.50"
    assert c["estado"] == "activo"
    assert c["ip_servicio"] == "10.20.30.40"
    assert c["nap_nombre"] == "NAP-15"
    assert c["puerto"] == "15"
    assert c["puerto_pon"] == "GPON0/3"
    assert c["zona"] == "NODO-NARANJILLOS"
    assert c["coordenadas"] == "10.4806,-66.9036"
    assert c["tipo_documento"] == "V"
    assert c["codigo_cliente"] == "C-1001"


def test_flatten_muestra_nombre_de_subobjeto():
    payload = {
        "encontrado": True,
        "total": 1,
        "servicios": [{
            "cliente": {"nombre_cliente": "X", "documento": "1"},
            "servicio": {"estado": "activo"},
            "plan": {"nombre": "P"},
            "red": {"usuario_pppoe": "u1", "serial_onu": "S1",
                    "router": {"nombre": "ROUTER-SISAL", "activo": True},
                    "nodo_nombre": ""},
            "facturacion": {},
        }],
    }
    services = extract_czwisp_servicios(payload)
    c = normalize_client(services[0])
    # router dict -> se extrae .nombre y se usa como zona (no hay nodo_nombre).
    assert c["zona"] == "ROUTER-SISAL"


def test_fetch_by_serial_parses_czwisp():
    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def get(self, url, timeout=None):
            self.calls += 1
            serial = url.split("onu=")[1]
            return FakeResp(_czwisp_payload(serial=serial))

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    cfg = _default_cfg(
        isp_slug="vidanet",
        serial_endpoint="/api/webhooks/cliente/{slug}/servicio?onu={serial}",
        cache_hours=1,
        min_interval_ms=0,
    )
    sess = FakeSession()
    w = WispClient(cfg, api_key="k", session=sess)
    c = w.fetch_by_serial("CMDCB00A3095")
    assert c is not None
    assert c["serial_onu"] == "CMDCB00A3095"
    assert c["documento"] == "12345678"
    assert c["nombre"] == "Juan Perez Rodriguez"
    assert c["zona"] == "NODO-NARANJILLOS"  # nodo_nombre > sucursal_nombre
    assert sess.calls == 1
    # Caché en memoria: segunda llamada no recalcula HTTP.
    c2 = w.fetch_by_serial("CMDCB00A3095")
    assert c2 is not None and sess.calls == 1


def test_fetch_by_serial_unknown_cached():
    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def get(self, url, timeout=None):
            self.calls += 1
            return FakeResp({"encontrado": False, "total": 0, "servicios": []})

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    cfg = _default_cfg(
        isp_slug="vidanet",
        serial_endpoint="/api/webhooks/cliente/{slug}/servicio?onu={serial}",
        cache_hours=1,
        min_interval_ms=0,
    )
    sess = FakeSession()
    w = WispClient(cfg, api_key="k", session=sess)
    assert w.fetch_by_serial("VSOL000000000") is None
    assert sess.calls == 1
    # Desconocido también queda en caché (negativo).
    assert w.fetch_by_serial("VSOL000000000") is None
    assert sess.calls == 1


def test_serial_endpoint_slug_replaced():
    cfg = _default_cfg(
        isp_slug="vidanet",
        serial_endpoint="/api/webhooks/cliente/{slug}/servicio?onu={serial}",
        cache_hours=1,
        min_interval_ms=0,
    )
    seen = {}

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def get(self, url, timeout=None):
            seen["url"] = url
            return FakeResp({"encontrado": False, "total": 0, "servicios": []})

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    w = WispClient(cfg, api_key="k", session=FakeSession())
    w.fetch_by_serial("VSOL009F2188")
    assert seen["url"] == ("https://czwisp.com"
                           "/api/webhooks/cliente/vidanet/servicio?onu=VSOL009F2188")