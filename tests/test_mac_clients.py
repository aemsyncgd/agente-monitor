# tests/test_mac_clients.py
import src.api.influx_helper as ih
import src.api.routes.metrics as routes_mod

FAKE_LOCATION = {
    "time": "2026-08-30T12:00:00Z",
    "mac": "00:EB:D8:17:E9:91",
    "location": "GPON05_17",
    "pon_port": "GPON0/5",
    "onu_index": "5_17",
    "onu_serial": "VSOL00f530fe",
    "onu_model": "V624",
    "estado_onu": 3,
    "olt_name": "OLT-PRADO-1",
    "olt_ip": "192.0.2.10",
    "nodo": "PRADO",
}

FAKE_LOCATION_2 = {
    "time": "2026-08-30T12:00:00Z",
    "mac": "00:AA:BB:CC:DD:EE",
    "location": "GPON01_02",
    "pon_port": "GPON0/1",
    "onu_index": "1_02",
    "onu_serial": "VSOL00aaaaaa",
    "onu_model": "V624",
    "estado_onu": 3,
    "olt_name": "OLT-SISAL-1",
    "olt_ip": "192.0.2.5",
    "nodo": "SISAL",
}


def test_get_client_locations_queries_onu_location(monkeypatch):
    captured = {}

    def fake_query(flux):
        captured["flux"] = flux
        return [dict(FAKE_LOCATION)]

    monkeypatch.setattr(ih, "_query", fake_query)
    rows = ih.get_client_locations()

    assert 'r["_measurement"] == "onu_location"' in captured["flux"]
    assert 'group(columns: ["mac"]' in captured["flux"]
    assert "|> last()" in captured["flux"]
    assert rows[0]["mac"] == "00:EB:D8:17:E9:91"
    assert rows[0]["location"] == "GPON05_17"


def test_get_client_locations_applies_olt_filter_in_flux(monkeypatch):
    captured = {}

    def fake_query(flux):
        captured["flux"] = flux
        return [dict(FAKE_LOCATION)]

    monkeypatch.setattr(ih, "_query", fake_query)
    ih.get_client_locations(olt_name="OLT-PRADO-1")

    assert 'r["olt_name"] == "OLT-PRADO-1"' in captured["flux"]


def test_get_client_locations_search_case_insensitive(monkeypatch):
    monkeypatch.setattr(
        ih, "_query",
        lambda flux: [dict(FAKE_LOCATION), dict(FAKE_LOCATION_2)],
    )

    assert len(ih.get_client_locations(search="E9:91")) == 1
    assert ih.get_client_locations(search="E9:91")[0]["mac"] == "00:EB:D8:17:E9:91"
    assert ih.get_client_locations(search="00:aa:bb")[0]["mac"] == "00:AA:BB:CC:DD:EE"
    assert ih.get_client_locations(search="GPON05_17")[0]["mac"] == "00:EB:D8:17:E9:91"
    assert ih.get_client_locations(search="VSOL00aaaaaa")[0]["mac"] == "00:AA:BB:CC:DD:EE"
    assert ih.get_client_locations(search="noexiste-xyz") == []


def test_clients_endpoint_returns_expected_shape(monkeypatch):
    monkeypatch.setattr(
        routes_mod, "get_client_locations",
        lambda **kw: [dict(FAKE_LOCATION)],
    )
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1/metrics")
    res = TestClient(app).get("/api/v1/metrics/clients")

    assert res.status_code == 200
    body = res.json()
    assert body["measurement"] == "onu_location"
    assert body["count"] == 1
    assert body["filters"] == {"olt_name": None, "search": None, "hours": 168}
    assert body["data"][0]["mac"] == "00:EB:D8:17:E9:91"


def test_clients_endpoint_forwards_filters(monkeypatch):
    captured = {}

    def fake_get(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(routes_mod, "get_client_locations", fake_get)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes_mod.router, prefix="/api/v1/metrics")
    res = TestClient(app).get(
        "/api/v1/metrics/clients?olt_name=OLT-PRADO-1&search=e9:91&hours=72"
    )

    assert res.status_code == 200
    assert captured == {"olt_name": "OLT-PRADO-1", "search": "e9:91", "hours": 72}
    body = res.json()
    assert body["filters"]["hours"] == 72