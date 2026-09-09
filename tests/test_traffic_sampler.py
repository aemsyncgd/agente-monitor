# tests/test_traffic_sampler.py
"""Tests de clasificacion de interfaces OLT y del muestreo olt_traffic_history."""
import time

import pytest

from src.collectors.olt_collector import (
    OltConfig, OltCollector, classify_interface_name, _RATE_CACHE,
)

FAKE_IFNAME = {"18": "GPON05ONU17", "2": "GE0/1", "17": "GPON0/5", "20": "VLAN274"}
FAKE_STATUS = {"18": "3", "2": "1", "17": "1", "20": "1"}
FAKE_IN = {"18": "1000", "2": "500", "17": "200"}
FAKE_OUT = {"18": "3000", "2": "0", "17": "400"}
FAKE_IN_NEXT = {"18": "1300", "2": "800", "17": "200"}
FAKE_OUT_NEXT = {"18": "4200", "2": "300", "17": "400"}


def _collector():
    return OltCollector(config=OltConfig(
        ip="10.0.0.1", community="public", hostname="OLT-1",
    ))


def _clear_rate_cache():
    _RATE_CACHE.clear()


def test_classify_interface_name():
    assert classify_interface_name("GE0/1") == "GE"
    assert classify_interface_name("GPON0/5") == "PON"
    assert classify_interface_name("VLAN274") == "PON"
    assert classify_interface_name("GPON05ONU17") == "ONU"
    assert classify_interface_name("GPON05ONU17.1") == "ONU"
    assert classify_interface_name("trunk1") is None


def test_first_sample_no_rates_but_octets():
    _clear_rate_cache()
    c = _collector()
    res = _run_sample(c, FAKE_IFNAME, FAKE_STATUS, FAKE_IN, FAKE_OUT, now=1000.0)
    assert res["errors"] == []
    by_name = {m["tags"]["interface_name"]: m for m in res["metrics"]
               if m["measurement"] == "olt_traffic_history"}
    assert set(by_name) == {"GPON05ONU17", "GE0/1", "GPON0/5", "VLAN274"}
    onu = by_name["GPON05ONU17"]
    assert onu["tags"]["interface_type"] == "ONU"
    assert onu["tags"]["pon_port"] == "GPON0/5"
    assert onu["tags"]["onu_index"] == "5_17"
    assert onu["tags"]["interface_index"] == "18"
    assert onu["fields"]["rx_octets"] == 1000
    assert onu["fields"]["tx_octets"] == 3000
    assert onu["fields"]["status"] == 3
    assert "rx_bps" not in onu["fields"]  # primer muestreo: sin tasa aun


def test_second_sample_derives_rates():
    _clear_rate_cache()
    c = _collector()
    _run_sample(c, FAKE_IFNAME, FAKE_STATUS, FAKE_IN, FAKE_OUT, now=1000.0)
    res = _run_sample(c, FAKE_IFNAME, FAKE_STATUS, FAKE_IN_NEXT, FAKE_OUT_NEXT, now=1300.0)
    by_name = {m["tags"]["interface_name"]: m for m in res["metrics"]
               if m["measurement"] == "olt_traffic_history"}
    onu = by_name["GPON05ONU17"]
    # Delta 300 octets / 300s * 8 = 8 bps; delta 1200/300*8 = 32 bps
    assert onu["fields"]["rx_bps"] == pytest.approx(8.0)
    assert onu["fields"]["tx_bps"] == pytest.approx(32.0)
    ge = by_name["GE0/1"]
    assert ge["tags"]["interface_type"] == "GE"
    assert "pon_port" not in ge["tags"]
    assert "onu_index" not in ge["tags"]
    pon = by_name["GPON0/5"]
    assert pon["tags"]["interface_type"] == "PON"
    assert pon["tags"]["pon_port"] == "GPON0/5"


def test_pause_values_are_ints():
    _clear_rate_cache()
    c = _collector()
    res = _run_sample(c, FAKE_IFNAME, {"18": "1"}, {"18": "0"}, {"18": "0"}, now=1.0)
    ge_after = [m for m in res["metrics"] if m["tags"]["interface_name"] == "GE0/1"]
    assert not ge_after  # GE0/1 no esta en esta pasada


def test_sample_traffic_public_runner_monkeypatched(monkeypatch):
    tables = {
        OltCollector.OID_IF_DESC: FAKE_IFNAME,
        OltCollector.OID_IF_STATUS: FAKE_STATUS,
        OltCollector.OID_IF_HCIN: FAKE_IN,
        OltCollector.OID_IF_HCOUT: FAKE_OUT,
    }

    async def fake_walk(self, oid, timeout=90, retries=1):
        return tables.get(oid, {})
    monkeypatch.setattr(OltCollector, "_subprocess_bulkwalk", fake_walk)

    _clear_rate_cache()
    c = _collector()
    result = c.sample_traffic(now=1000.0)
    assert result.success is True
    assert result.device_name == "OLT-1"
    assert len(result.metrics) == 4


# ==================== OltTrafficSampler ====================

def test_rebuild_collectors_from_config(tmp_path):
    from src.config_manager import ConfigManager
    from src.collectors.olt_traffic_sampler import OltTrafficSampler
    from src.collectors.influx_writer import CollectorInfluxWriter

    nodes_yaml = tmp_path / "nodes.yaml"
    nodes_yaml.write_text(
        "nodes:\n"
        "- name: T\n"
        "  mikrotiks: []\n"
        "  olts:\n"
        "  - ip: 10.0.0.1\n"
        "    hostname: OLT-1\n"
        "    community: public\n"
        "    modelo: V1600G1\n"
        "    pon_count: 8\n"
        "    descripcion: OLT-1\n"
        "    conectado_a: ''\n"
    )
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("influxdb:\n  url: http://x:8086\n  token: t\n  org: o\n  bucket: b\n")
    cm = ConfigManager(str(cfg_yaml), str(nodes_yaml))

    sampler = OltTrafficSampler(cm, writer=None)
    n = sampler.rebuild_collectors()
    assert n == 1
    assert sampler._collectors[0].config.hostname == "OLT-1"
    # la pausa entre walks esta configurada por el sampler
    assert sampler._collectors[0].mac_walk_delay == sampler.walk_delay_seconds


def test_sample_once_writes_results():
    from src.collectors.olt_traffic_sampler import OltTrafficSampler

    class FakeWriter:
        def __init__(self):
            self.wrote = []

        def write_collector_result(self, result):
            self.wrote.append(result)
            return len(result.metrics)

    class FakeCollector:
        def __init__(self, metrics, errors=(), hostname="OLT-1"):
            self._metrics = metrics
            self._errors = errors
            self.config = type("C", (), {"hostname": hostname})()

        def sample_traffic(self):
            from src.collectors.base import CollectorResult
            return CollectorResult(
                success=not self._errors,
                metrics=list(self._metrics),
                errors=list(self._errors),
                duration_seconds=0.1,
                device_name=self.config.hostname,
                device_ip="10.0.0.1",
            )

    writer = FakeWriter()
    sampler = OltTrafficSampler(None, writer)
    sampler._collectors = [
        FakeCollector([{"measurement": "olt_traffic_history", "tags": {}, "fields": {"rx_bps": 1}}]),
        FakeCollector([], errors=["snmp error"], hostname="OLT-2"),
    ]
    sampler.sample_once()
    assert len(writer.wrote) == 1  # solo la OLT con metricas escribio
    assert writer.wrote[0].device_name == "OLT-1"


def _run_sample(c, if_desc, if_status, if_in, if_out, now=None):
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            c._sample_traffic_async(
                if_desc=if_desc, if_status=if_status,
                if_in=if_in, if_out=if_out, now=now,
            )
        )
    finally:
        loop.close()