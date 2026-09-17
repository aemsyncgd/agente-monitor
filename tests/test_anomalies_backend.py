# tests/test_anomalies_backend.py
"""Unit helpers de anomalias (ventanas, series, predicciones, historico).

Sin red: se mockea influx_helper._query.
"""
import pytest

from src.api import influx_helper as ih


FAKE_SERIES = [
    {"time": "2026-09-16T01:00:00Z", "critical": 1, "warning": 3, "risk": 2},
    {"time": "2026-09-16T00:00:00Z", "critical": 0, "warning": 1, "risk": 1},
]

FAKE_PREVIOUS = [
    {"band": "critical", "value": 2},
    {"band": "warning", "value": 5},
    {"band": "risk", "value": 3},
]

FAKE_HISTORY = [
    {"time": "2026-09-16T00:00:00Z", "rx_power": -30.0, "tx_power": 1.2},
    {"time": "2026-09-16T00:30:00Z", "tx_power": 1.3},  # sin rx en ese window
]

FAKE_TREND = {
    ("OLT-1", "GPON0/1", "1_1", "SERIAL-A"): [
        -23.0 - i * 0.5 for i in range(13)
    ],
    ("OLT-1", "GPON0/1", "1_2", "SERIAL-B"): [-20.0] * 13,
}


def test_normalize_anomaly_series_ordena_y_totalea():
    out = ih._normalize_anomaly_series(FAKE_SERIES)
    assert [o["time"] for o in out] == [
        "2026-09-16T00:00:00Z", "2026-09-16T01:00:00Z",
    ]
    assert out[0]["total"] == 1 and out[0]["critical"] == 0
    assert out[0]["warning"] == 1 and out[0]["risk"] == 1
    assert out[1]["total"] == 4 and out[1]["risk"] == 3


def test_counts_from_bands():
    out = ih._counts_from_bands(FAKE_PREVIOUS)
    assert out == {"total": 7, "critical": 2, "warning": 5, "risk": 5}


def test_get_anomaly_series_delega_a_query(monkeypatch):
    monkeypatch.setattr(ih, "_query", lambda flux: [dict(r) for r in FAKE_SERIES])
    out = ih.get_anomaly_series(hours=24)
    assert out[1]["total"] == 4 and out[1]["risk"] == 3


def test_get_anomaly_previous_delega_a_query(monkeypatch):
    monkeypatch.setattr(ih, "_query", lambda flux: [dict(r) for r in FAKE_PREVIOUS])
    out = ih.get_anomaly_previous(hours=24)
    assert out == {"total": 7, "critical": 2, "warning": 5, "risk": 5}


def _fake_trend_query(flux):
    rows = []
    for (olt, pon, idx, serial), series in FAKE_TREND.items():
        for i, v in enumerate(series):
            rows.append({
                "time": f"2026-09-16T00:{i:02d}:00Z",
                "value": v,
                "olt_name": olt,
                "pon_port": pon,
                "onu_index": idx,
                "onu_serial": serial,
            })
    return rows


def test_predict_failures_ordena_y_excluye_estables(monkeypatch):
    monkeypatch.setattr(ih, "_query", _fake_trend_query)
    out = ih.predict_failures(hours=24, max_results=10)
    # Solo la ONU en caida entra; la estable queda fuera
    seriales = [p["onu_serial"] for p in out]
    assert seriales == ["SERIAL-A"]
    falls = [p["hours_to_failure"] for p in out]
    assert falls == sorted(falls)
    assert out[0]["trend_db_per_hour"] < -0.5
    assert 0.0 <= out[0]["confidence"] <= 1.0


def test_onu_optical_history_construye_series(monkeypatch):
    monkeypatch.setattr(ih, "_query", lambda flux: [dict(r) for r in FAKE_HISTORY])
    out = ih.get_onu_optical_history("OLT-1", "SERIAL-A", hours=24)
    assert out["times"] == ["2026-09-16T00:00:00Z", "2026-09-16T00:30:00Z"]
    assert out["rx"][0] == -30.0 and out["rx"][1] is None
    assert out["tx"][1] == 1.3