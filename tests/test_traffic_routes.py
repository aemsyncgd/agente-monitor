# tests/test_traffic_routes.py
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import traffic as traffic_route
from src.api.routes.traffic import router


def _fake_manager():
    return SimpleNamespace(
        get_olt_by_ip=lambda ip: SimpleNamespace(ip=ip, community="pub", hostname="OLT-1")
        if ip == "1.1.1.1" else None,
        get_mikrotik_by_ip=lambda ip: SimpleNamespace(ip=ip, community="pub", hostname="MK-1")
        if ip == "2.2.2.2" else None,
    )


class FakeReader:
    def __init__(self, result):
        self.result = result

    def read(self, *a, **kw):
        return self.result

    def raise_on_read(self):
        raise RuntimeError("boom")


@pytest.fixture
def route_client(monkeypatch):
    monkeypatch.setattr(traffic_route, "get_config_manager", _fake_manager)
    monkeypatch.setattr(traffic_route, "ih", traffic_route.ih)  # keep real influx_helper
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, monkeypatch


def test_history_ok(route_client):
    client, monkeypatch = route_client
    monkeypatch.setattr(
        traffic_route.ih, "get_traffic_history",
        lambda *a, **kw: [{"time": "t1", "rx_bps": 10.0, "tx_bps": 20.0}],
    )
    monkeypatch.setattr(
        traffic_route.ih, "get_daily_consumption",
        lambda *a, **kw: [{"date": "2026-09-01", "rx_bytes": 1, "tx_bytes": 2, "total": 3}],
    )
    resp = client.get(
        "/api/v1/traffic/history",
        params={"device_type": "olt", "device_ip": "1.1.1.1", "device_name": "OLT-1", "interface_name": "GPON05ONU17", "range_key": "1mes"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["range"] == "1mes"
    assert data["points"] == [{"time": "t1", "rx_bps": 10.0, "tx_bps": 20.0}]
    assert data["daily"][0]["total"] == 3


def test_history_mikrotik(route_client):
    client, monkeypatch = route_client
    monkeypatch.setattr(traffic_route.ih, "get_traffic_history", lambda *a, **kw: [])
    monkeypatch.setattr(traffic_route.ih, "get_daily_consumption", lambda *a, **kw: [])
    resp = client.get(
        "/api/v1/traffic/history",
        params={"device_type": "mikrotik", "device_ip": "2.2.2.2", "device_name": "MK-1", "interface_name": "ether1"},
    )
    assert resp.status_code == 200


def test_history_invalid_range(route_client):
    client, _ = route_client
    resp = client.get(
        "/api/v1/traffic/history",
        params={"device_type": "olt", "device_ip": "1.1.1.1", "device_name": "OLT-1", "interface_name": "GPON0/5", "range_key": "bogus"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "rango_invalido"


def test_history_device_not_found(route_client):
    client, _ = route_client
    resp = client.get(
        "/api/v1/traffic/history",
        params={"device_type": "olt", "device_ip": "9.9.9.9", "device_name": "X", "interface_name": "GPON0/5"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "dispositivo_no_encontrado"


def test_history_influx_error(route_client):
    client, monkeypatch = route_client
    def boom(*a, **kw):
        raise RuntimeError("influx down")
    monkeypatch.setattr(traffic_route.ih, "get_traffic_history", boom)
    resp = client.get(
        "/api/v1/traffic/history",
        params={"device_type": "olt", "device_ip": "1.1.1.1", "device_name": "OLT-1", "interface_name": "GPON0/5"},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "error_influx"


def test_live_ok(route_client):
    client, monkeypatch = route_client
    monkeypatch.setattr(
        traffic_route, "READER",
        FakeReader({"ok": True, "device_ip": "1.1.1.1", "interface_name": "GE0/1",
                    "rx_bps": 10.0, "tx_bps": 20.0}),
    )
    resp = client.get(
        "/api/v1/traffic/live",
        params={"device_type": "olt", "device_ip": "1.1.1.1", "interface_name": "GE0/1", "interface_index": "2"},
    )
    assert resp.status_code == 200
    assert resp.json()["rx_bps"] == 10.0


def test_live_device_not_found(route_client):
    client, monkeypatch = route_client
    monkeypatch.setattr(traffic_route, "READER", FakeReader({"ok": False, "error": "dispositivo_no_encontrado"}))
    resp = client.get(
        "/api/v1/traffic/live",
        params={"device_type": "olt", "device_ip": "9.9.9.9", "interface_name": "GE0/1"},
    )
    assert resp.status_code == 404


def test_live_snmp_error(route_client):
    client, monkeypatch = route_client
    monkeypatch.setattr(traffic_route, "READER", FakeReader({"ok": False, "error": "snmp_error"}))
    resp = client.get(
        "/api/v1/traffic/live",
        params={"device_type": "olt", "device_ip": "1.1.1.1", "interface_name": "GE0/1"},
    )
    assert resp.status_code == 502


def test_live_reader_exception(route_client):
    client, monkeypatch = route_client
    boom = FakeReader({})
    monkeypatch.setattr(traffic_route, "READER", boom)
    boom.read = boom.raise_on_read
    resp = client.get(
        "/api/v1/traffic/live",
        params={"device_type": "olt", "device_ip": "1.1.1.1", "interface_name": "GE0/1"},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "error_lectura_snmp"