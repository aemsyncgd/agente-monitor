# tests/test_snmp_live.py
import pytest
from types import SimpleNamespace

from src.api import snmp_live as sl


def setup_function():
    sl.clear_live_cache()


def test_safe_int():
    assert sl._safe_int("123") == 123
    assert sl._safe_int("'123'") == 123
    assert sl._safe_int(None) is None
    assert sl._safe_int("nope") is None
    assert sl._safe_int("OVS-DSL") is None


def test_derive_live_rate_first_sample_none():
    key = ("1.1.1.1", "GE0/1", "2")
    rx, tx = sl.derive_live_rate(key, 1000, 500, now=100.0)
    assert rx is None and tx is None
    assert sl._LIVE_RATE_CACHE[key] == (100.0, 1000, 500)


def test_derive_live_rate_second_sample():
    key = ("1.1.1.1", "GE0/1", "2")
    sl.derive_live_rate(key, 1000, 500, now=100.0)
    rx, tx = sl.derive_live_rate(key, 1240, 794, now=130.0)
    assert rx == 64.0   # 240*8/30
    assert tx == 78.4   # 294*8/30


def test_derive_live_rate_ignores_reset():
    key = ("1.1.1.1", "GE0/1", "2")
    sl.derive_live_rate(key, 1000, 500, now=100.0)
    rx, tx = sl.derive_live_rate(key, 900, 600, now=130.0)
    assert rx is None
    assert tx == pytest.approx(26.6667, abs=1e-4)


def test_derive_live_rate_ttl_expired():
    key = ("1.1.1.1", "GE0/1", "2")
    sl.derive_live_rate(key, 1000, 500, now=100.0)
    rx, tx = sl.derive_live_rate(key, 1240, 794, now=200.0)
    assert rx is None and tx is None
    rx, tx = sl.derive_live_rate(key, 1480, 1088, now=230.0)
    assert rx == 64.0
    assert tx == 78.4


def test_get_device_credentials(monkeypatch):
    fake = SimpleNamespace(
        get_olt_by_ip=lambda ip: SimpleNamespace(ip="1.1.1.1", community="pub1", hostname="OLT-1")
        if ip == "1.1.1.1" else None,
        get_mikrotik_by_ip=lambda ip: SimpleNamespace(ip="2.2.2.2", community="pub2", hostname="MK-1")
        if ip == "2.2.2.2" else None,
    )
    monkeypatch.setattr(sl, "get_config_manager", lambda: fake)
    assert sl.get_device_credentials("olt", "1.1.1.1") == {"ip": "1.1.1.1", "community": "pub1", "hostname": "OLT-1"}
    assert sl.get_device_credentials("mikrotik", "2.2.2.2") == {"ip": "2.2.2.2", "community": "pub2", "hostname": "MK-1"}
    assert sl.get_device_credentials("olt", "9.9.9.9") is None
    assert sl.get_device_credentials("mikrotik", "9.9.9.9") is None


def test_reader_read_happy_path(monkeypatch):
    fake = SimpleNamespace(
        get_olt_by_ip=lambda ip: SimpleNamespace(ip=ip, community="pub1", hostname="OLT-1"),
        get_mikrotik_by_ip=lambda ip: None,
    )
    monkeypatch.setattr(sl, "get_config_manager", lambda: fake)

    def fake_get(ip, community, oids, timeout=4.0, retries=1):
        assert ip == "1.1.1.1" and community == "pub1"
        return {oids[0]: "1000", oids[1]: "500"}

    monkeypatch.setattr(sl, "_pysnmp_get_many", fake_get)
    reader = sl.LiveTrafficReader()
    first = reader.read("olt", "1.1.1.1", "GE0/1", interface_index="5", now=100.0)
    assert first["ok"] is True
    assert first["rx_octets"] == 1000 and first["tx_octets"] == 500
    assert first["rx_bps"] is None and first["tx_bps"] is None
    assert first["interface_index"] == "5"
    assert first["device_name"] == "OLT-1"

    def fake_get2(ip, community, oids, timeout=4.0, retries=1):
        return {oids[0]: "1240", oids[1]: "794"}

    monkeypatch.setattr(sl, "_pysnmp_get_many", fake_get2)
    second = reader.read("olt", "1.1.1.1", "GE0/1", interface_index="5", now=130.0)
    assert second["rx_bps"] == 64.0
    assert second["tx_bps"] == 78.4


def test_reader_read_resolves_index(monkeypatch):
    fake = SimpleNamespace(
        get_olt_by_ip=lambda ip: SimpleNamespace(ip=ip, community="pub1", hostname="OLT-1"),
        get_mikrotik_by_ip=lambda ip: None,
    )
    monkeypatch.setattr(sl, "get_config_manager", lambda: fake)
    monkeypatch.setattr(
        sl,
        "_pysnmp_ifname_walk",
        lambda ip, community, timeout=8.0, retries=1: {"18": "GPON05ONU17", "2": "GE0/1"},
    )
    seen = {}

    def fake_get(ip, community, oids, timeout=4.0, retries=1):
        seen["oids"] = oids
        return {o: "10" for o in oids}

    monkeypatch.setattr(sl, "_pysnmp_get_many", fake_get)
    res = sl.LiveTrafficReader().read("olt", "1.1.1.1", "GPON05ONU17", now=10.0)
    assert res["ok"] is True and res["interface_index"] == "18"
    assert oids_use_index(seen["oids"], "18")


def oids_use_index(oids, idx):
    return any(o.endswith(f".{idx}") for o in oids)


def test_reader_read_snmp_error(monkeypatch):
    fake = SimpleNamespace(
        get_olt_by_ip=lambda ip: SimpleNamespace(ip=ip, community="pub1", hostname="OLT-1"),
        get_mikrotik_by_ip=lambda ip: None,
    )
    monkeypatch.setattr(sl, "get_config_manager", lambda: fake)
    monkeypatch.setattr(sl, "_pysnmp_get_many", lambda *a, **kw: None)
    res = sl.LiveTrafficReader().read("olt", "1.1.1.1", "GE0/1", interface_index="5", now=10.0)
    assert res["ok"] is False and res["error"] == "snmp_error"


def test_reader_read_device_not_found(monkeypatch):
    fake = SimpleNamespace(
        get_olt_by_ip=lambda ip: None,
        get_mikrotik_by_ip=lambda ip: None,
    )
    monkeypatch.setattr(sl, "get_config_manager", lambda: fake)
    res = sl.LiveTrafficReader().read("olt", "1.1.1.1", "GE0/1", interface_index="5", now=10.0)
    assert res["ok"] is False and res["error"] == "dispositivo_no_encontrado"


def test_reader_read_interface_not_found(monkeypatch):
    fake = SimpleNamespace(
        get_olt_by_ip=lambda ip: SimpleNamespace(ip=ip, community="pub1", hostname="OLT-1"),
        get_mikrotik_by_ip=lambda ip: None,
    )
    monkeypatch.setattr(sl, "get_config_manager", lambda: fake)
    monkeypatch.setattr(sl, "_pysnmp_ifname_walk", lambda *a, **kw: {})
    res = sl.LiveTrafficReader().read("olt", "1.1.1.1", "SINVALUE", now=10.0)
    assert res["ok"] is False and res["error"] == "interfaz_no_encontrada"