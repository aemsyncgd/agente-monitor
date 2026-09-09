# tests/test_wisp_client.py
import csv
import json
import os

from src.wisp_client import normalize_client, load_from_csv, WispClient
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