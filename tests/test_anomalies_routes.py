# tests/test_anomalies_routes.py
"""Routes de anomalias ampliadas (misma logica de TestClient sin red)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import anomalies as anom_route
from src.api.routes.anomalies import router


FAKE_ROW = {
    "olt_name": "OLT-1", "pon_port": "GPON0/1", "onu_index": "1_1",
    "onu_serial": "SERIAL-A", "value": -33.97, "anomaly_type": "critical",
    "description": "Low optical power: -33.97 dBm", "time": "t0",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(anom_route, "get_anomalies_from_influx",
                        lambda **kw: [dict(FAKE_ROW)])
    monkeypatch.setattr(anom_route, "get_anomaly_previous",
                        lambda **kw: {"total": 5, "critical": 1,
                                      "warning": 4, "risk": 4})
    monkeypatch.setattr(anom_route, "get_anomaly_series",
                        lambda **kw: [{"time": "t0", "total": 2, "critical": 1,
                                       "warning": 1, "risk": 2}])
    monkeypatch.setattr(
        anom_route, "predict_failures",
        lambda *a, **kw: [{
            "olt_name": "OLT-1", "pon_port": "GPON0/1", "onu_index": "1_1",
            "onu_serial": "SERIAL-A", "current_power": -29.0,
            "trend_db_per_hour": -1.0, "hours_to_failure": 1.0,
            "confidence": 0.5,
        }],
    )
    monkeypatch.setattr(
        anom_route, "get_onu_optical_history",
        lambda *a, **kw: {"times": ["t0"], "rx": [-30.0], "tx": [1.0]},
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/anomalies")
    with TestClient(app) as c:
        yield c


def test_summary_incluye_previous_series_y_predicciones(client):
    resp = client.get("/api/v1/anomalies/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_anomalies"] == 1
    assert data["by_type"]["critical"] == 1
    assert data["previous"] == {"total": 5, "critical": 1, "warning": 4, "risk": 4}
    assert data["series"][0]["total"] == 2
    assert data["predictions"]["count"] == 1


def test_history_ok(client):
    resp = client.get(
        "/api/v1/anomalies/history",
        params={"olt_name": "OLT-1", "onu_serial": "SERIAL-A", "hours": 24},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["times"] == ["t0"]
    assert data["rx"][0] == -30.0 and data["tx"][0] == 1.0


def test_predictions_ok_y_filtro_por_olt(client):
    resp = client.get("/api/v1/anomalies/predictions")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    assert resp.json()["predictions"][0]["hours_to_failure"] == 1.0

    resp2 = client.get("/api/v1/anomalies/predictions",
                       params={"olt_name": "OLT-2"})
    assert resp2.json()["count"] == 0