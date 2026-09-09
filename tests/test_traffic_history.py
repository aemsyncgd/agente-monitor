# tests/test_traffic_history.py
import pytest

from src.api import influx_helper as ih


def test_history_params_default():
    assert ih.history_params("1d") == (24, "30s")
    assert ih.history_params("1sem") == (168, "2m")
    assert ih.history_params("1mes") == (720, "5m")
    assert ih.history_params("3m") == (2160, "15m")
    assert ih.history_params("6m") == (4320, "30m")
    assert ih.history_params("9m") == (6480, "1h")
    assert ih.history_params("1año") == (8760, "2h")
    assert ih.history_params("bogus") == (24, "30s")
    assert ih.history_params("") == (24, "30s")


def test_traffic_measurement():
    assert ih._traffic_measurement("olt") == ("olt_traffic_history", "olt_name", "rx_octets", "tx_octets")
    assert ih._traffic_measurement("mikrotik") == ("mikrotik_interface", "device_name", "ifHCInOctets", "ifHCOutOctets")


def test_daily_deltas():
    rows = [
        {"time": "2026-09-01T00:00:00Z", "rx": 1000, "tx": 500},
        {"time": "2026-09-02T00:00:00Z", "rx": 1900, "tx": 1100},
        {"time": "2026-09-03T00:00:00Z", "rx": 3100, "tx": 2100},
    ]
    out = ih.daily_deltas_from_rows(rows, "rx", "tx")
    assert out == [
        {"date": "2026-09-02", "rx_bytes": 900, "tx_bytes": 600, "total": 1500},
        {"date": "2026-09-03", "rx_bytes": 1200, "tx_bytes": 1000, "total": 2200},
    ]


def test_daily_deltas_skips_counter_reset():
    rows = [
        {"time": "2026-09-01T00:00:00Z", "rx": 1000, "tx": 500},
        {"time": "2026-09-02T00:00:00Z", "rx": 800, "tx": 1100},   # reset rx -> negativo
        {"time": "2026-09-03T00:00:00Z", "rx": 1800, "tx": 2000},
    ]
    out = ih.daily_deltas_from_rows(rows, "rx", "tx")
    assert out == [{"date": "2026-09-03", "rx_bytes": 1000, "tx_bytes": 900, "total": 1900}]


def test_get_traffic_history_uses_built_flux(monkeypatch):
    captured = {}

    def fake_query(flux, org=None):
        captured["flux"] = flux
        return [{"time": "2026-09-01T00:00:00Z", "rx_bps": 1.0, "tx_bps": 2.0}]

    monkeypatch.setattr(ih, "_query", fake_query)
    rows = ih.get_traffic_history("olt", "OLT-1", "GPON05ONU17", "1d")
    assert rows == [{"time": "2026-09-01T00:00:00Z", "rx_bps": 1.0, "tx_bps": 2.0}]
    assert 'r["_measurement"] == "olt_traffic_history"' in captured["flux"]
    assert 'r["olt_name"] == "OLT-1"' in captured["flux"]
    assert 'r["interface_name"] == "GPON05ONU17"' in captured["flux"]

    ih.get_traffic_history("mikrotik", "MK-1", "ether1", "1mes")
    assert 'r["device_name"] == "MK-1"' in captured["flux"]
    assert 'r["_measurement"] == "mikrotik_interface"' in captured["flux"]


def test_get_daily_consumption_flows_through_deltas(monkeypatch):
    captured = {}

    def fake_query(flux, org=None):
        captured["flux"] = flux
        return [
            {"time": "2026-09-01T00:00:00Z", "rx_octets": 100, "tx_octets": 50},
            {"time": "2026-09-02T00:00:00Z", "rx_octets": 500, "tx_octets": 250},
        ]

    monkeypatch.setattr(ih, "_query", fake_query)
    daily = ih.get_daily_consumption("olt", "OLT-1", "GPON05ONU17")
    assert daily == [{"date": "2026-09-02", "rx_bytes": 400, "tx_bytes": 200, "total": 600}]
    assert 'r["_field"] == "rx_octets" or r["_field"] == "tx_octets"' in captured["flux"]