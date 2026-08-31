# tests/test_onu_scan.py
import pytest

from src.config_ia import OLTDevice, Node
from src.config_manager import ConfigManager
from src.collectors.olt_collector import (
    OltConfig, OltCollector, normalize_mac, classify_fdb_interface,
    parse_onu_iface, build_mac_metrics,
)
from src.collectors.main import create_collectors_from_config
from src.collectors.onu_scan import select_target_devices, main

FAKE_IFNAME = {"18": "GPON05ONU17", "2": "GE0/1", "17": "VLAN274"}
FAKE_FDB_MAC = {"6.0.235.216.23.233.145": "00 EB D8 17 E9 91"}
FAKE_FDB_PORT = {"6.0.235.216.23.233.145": "18"}
FAKE_ONU_STATUS = {"5.17": "3"}
FAKE_ONU_SERIAL = {"5.17": "VSOL00f530fe"}
FAKE_ONU_MODEL = {"5.17": "V624"}

_TABLES = {
    "1.3.6.1.2.1.31.1.1.1.1": FAKE_IFNAME,
    "1.3.6.1.2.1.17.4.3.1.1": FAKE_FDB_MAC,
    "1.3.6.1.2.1.17.4.3.1.2": FAKE_FDB_PORT,
    "1.3.6.1.4.1.37950.1.1.6.1.1.1.1.5": FAKE_ONU_STATUS,
    "1.3.6.1.4.1.37950.1.1.6.1.1.2.1.5": FAKE_ONU_SERIAL,
    "1.3.6.1.4.1.37950.1.1.6.1.1.2.1.6": FAKE_ONU_MODEL,
}


def test_olt_capturar_mac_default_true():
    olt = OLTDevice()
    assert olt.capturar_mac is True


def test_olt_capturar_mac_roundtrip():
    olt = OLTDevice.from_dict({"ip": "1.1.1.1", "hostname": "OLT-T",
                               "capturar_mac": False})
    assert olt.capturar_mac is False
    assert olt.to_dict()["capturar_mac"] is False


def test_config_manager_persists_capturar_mac(tmp_path):
    nodes_yaml = tmp_path / "nodes.yaml"
    nodes_yaml.write_text(
        "nodes:\n"
        "- name: T\n"
        "  mikrotiks: []\n"
        "  olts:\n"
        "  - ip: 1.1.1.1\n"
        "    hostname: OLT-T\n"
        "    community: public\n"
        "    modelo: V1600G1\n"
        "    pon_count: 8\n"
        "    descripcion: OLT-T\n"
        "    conectado_a: ''\n"
        "    capturar_mac: false\n"
    )
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("influxdb:\n  url: http://x:8086\n  token: t\n  org: o\n  bucket: b\n")
    cm = ConfigManager(str(cfg_yaml), str(nodes_yaml))
    assert cm.get_all_olts()[0].capturar_mac is False
    cm.update_olt("1.1.1.1", {"capturar_mac": True})
    assert cm.get_all_olts()[0].capturar_mac is True
    cm.reload_config()
    assert cm.get_all_olts()[0].capturar_mac is True


def test_create_collectors_propagates_capturar_mac(tmp_path):
    nodes_yaml = tmp_path / "nodes.yaml"
    nodes_yaml.write_text(
        "nodes:\n"
        "- name: T\n"
        "  mikrotiks: []\n"
        "  olts:\n"
        "  - ip: 1.1.1.1\n"
        "    hostname: OLT-T\n"
        "    community: public\n"
        "    modelo: V1600G1\n"
        "    pon_count: 8\n"
        "    descripcion: OLT-T\n"
        "    conectado_a: ''\n"
        "    capturar_mac: false\n"
    )
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("influxdb:\n  url: http://x:8086\n  token: t\n  org: o\n  bucket: b\n")
    cm = ConfigManager(str(cfg_yaml), str(nodes_yaml))
    olt_collectors, _ = create_collectors_from_config(cm, olt_timeout=30)
    assert olt_collectors[0].config.capturar_mac is False


def test_olt_config_default_true():
    assert OltConfig(ip="1.1.1.1", community="x", hostname="OLT-T").capturar_mac is True


def test_normalize_mac():
    assert normalize_mac("00 EB D8 17 E9 91") == "00:EB:D8:17:E9:91"
    assert normalize_mac("00:eb:d8:17:e9:91") == "00:EB:D8:17:E9:91"
    assert normalize_mac("not-a-mac") is None


def test_classify_fdb_interface():
    assert classify_fdb_interface("GPON05ONU17") == "onu"
    assert classify_fdb_interface("GPON05ONU17.1") == "onu"
    assert classify_fdb_interface("GPON05ONU1") == "onu"
    assert classify_fdb_interface("GE0/1") == "ge"
    assert classify_fdb_interface("GE0/8") == "ge"
    assert classify_fdb_interface("VLAN274") == "otro"
    assert classify_fdb_interface("GPON0/1") == "otro"
    assert classify_fdb_interface("trunk1") == "otro"


def test_parse_onu_iface():
    assert parse_onu_iface("GPON05ONU17") == (5, 17)
    assert parse_onu_iface("GPON05ONU17.1") == (5, 17)
    assert parse_onu_iface("GE0/1") is None


def test_build_mac_metrics_onu_emits_both_measurements():
    fdb_macs = {"6.0.235.216.23.233.145": "00 EB D8 17 E9 91"}
    fdb_ports = {"6.0.235.216.23.233.145": "18"}
    if_desc = {"18": "GPON05ONU17"}
    onu_status = {"5.17": "3"}
    onu_serial = {"5.17": "VSOL00f530fe"}
    onu_model = {"5.17": "V624"}

    metrics = build_mac_metrics(
        fdb_macs, fdb_ports, if_desc,
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
        onu_status=onu_status, onu_serial=onu_serial, onu_model=onu_model,
    )
    onu_mac = [m for m in metrics if m["measurement"] == "onu_mac"]
    onu_loc = [m for m in metrics if m["measurement"] == "onu_location"]
    assert len(onu_mac) == 1 and len(onu_loc) == 1

    m = onu_mac[0]
    assert m["tags"]["mac"] == "00:EB:D8:17:E9:91"
    assert m["tags"]["interface_name"] == "GPON05ONU17"
    assert m["tags"]["interface_type"] == "onu"
    assert m["tags"]["location"] == "GPON05_17"
    assert m["fields"]["onu_serial"] == "VSOL00f530fe"
    assert m["fields"]["onu_model"] == "V624"
    assert m["fields"]["estado_onu"] == 3

    l = onu_loc[0]
    assert l["tags"]["location"] == "GPON05_17"
    assert l["tags"]["mac"] == "00:EB:D8:17:E9:91"
    assert l["fields"]["onu_index"] == "5_17"
    assert l["fields"]["pon_port"] == "GPON0/5"


def test_build_mac_metrics_ge_does_not_emit_onu_location():
    """Regla de no-sobrescritura: MAC en GE nunca cambia la ubicacion."""
    fdb_macs = {"6.0.235.216.23.233.145": "00 EB D8 17 E9 91"}
    fdb_ports = {"6.0.235.216.23.233.145": "2"}
    if_desc = {"2": "GE0/1"}

    metrics = build_mac_metrics(
        fdb_macs, fdb_ports, if_desc,
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
    )
    onu_mac = [m for m in metrics if m["measurement"] == "onu_mac"]
    onu_loc = [m for m in metrics if m["measurement"] == "onu_location"]
    assert len(onu_mac) == 1
    assert len(onu_loc) == 0
    assert onu_mac[0]["tags"]["interface_type"] == "ge"
    assert "location" not in onu_mac[0]["tags"]


def test_build_mac_metrics_unknown_ifname_is_otro():
    fdb_macs = {"6.0.235.216.23.233.145": "00 EB D8 17 E9 91"}
    fdb_ports = {"6.0.235.216.23.233.145": "33"}
    if_desc = {}

    metrics = build_mac_metrics(
        fdb_macs, fdb_ports, if_desc,
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
    )
    onu_mac = [m for m in metrics if m["measurement"] == "onu_mac"]
    assert len(onu_mac) == 1
    assert onu_mac[0]["tags"]["interface_type"] == "otro"


def test_build_mac_metrics_skips_missing_port():
    fdb_macs = {"6.0.235.216.23.233.145": "00 EB D8 17 E9 91"}
    fdb_ports = {}
    if_desc = {"18": "GPON05ONU17"}

    metrics = build_mac_metrics(
        fdb_macs, fdb_ports, if_desc,
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
    )
    assert metrics == []


@pytest.mark.asyncio
async def test_capture_macs_async_walks_and_builds():
    cfg = OltConfig(ip="192.0.2.10", community="public",
                    hostname="OLT-PRADO-1", modelo="V1600G1", nodo="PRADO")
    c = OltCollector(config=cfg)
    c.mac_walk_delay = 0
    calls = []

    async def fake_walk(oid, *args, **kwargs):
        calls.append(oid)
        return dict(_TABLES.get(oid, {}))

    c._subprocess_bulkwalk = fake_walk  # type: ignore[assignment]

    result = await c._capture_macs_async()
    assert "errors" in result and "metrics" in result
    ms = result["metrics"]
    assert [m["measurement"] for m in ms] == ["onu_mac", "onu_location"]
    assert ms[0]["tags"]["location"] == "GPON05_17"
    assert calls[0] == c.OID_IF_DESC  # primero ifName
    assert c.OID_FDB_MAC in calls and c.OID_FDB_PORT in calls


def test_capture_macs_sync_returns_result():
    cfg = OltConfig(ip="192.0.2.10", community="public",
                    hostname="OLT-PRADO-1", modelo="V1600G1", nodo="PRADO")
    c = OltCollector(config=cfg)
    c.mac_walk_delay = 0

    async def fake_walk(oid, *args, **kwargs):
        return dict(_TABLES.get(oid, {}))

    c._subprocess_bulkwalk = fake_walk  # type: ignore[assignment]

    result = c.capture_macs()
    assert result.success is True
    assert result.device_name == "OLT-PRADO-1"
    assert len(result.metrics) == 2
    assert [m["measurement"] for m in result.metrics] == ["onu_mac", "onu_location"]


def make_node(name, olts):
    return Node(name=name, olts=olts)


def test_select_target_devices_by_hostname():
    olt = OLTDevice(ip="1.1.1.1", hostname="OLT-PRADO-1", community="public")
    nodes = [make_node("P", [olt])]
    assert select_target_devices(nodes, "OLT-PRADO-1") == [(olt, "P")]
    assert select_target_devices(nodes, "OLT-PRADO-X") == []


def test_select_target_devices_by_ip():
    olt = OLTDevice(ip="1.1.1.1", hostname="OLT-PRADO-1", community="public")
    nodes = [make_node("P", [olt])]
    assert select_target_devices(nodes, "1.1.1.1") == [(olt, "P")]


def test_select_target_devices_all_returns_all():
    o1 = OLTDevice(ip="1.1.1.1", hostname="OLT-A", community="public")
    o2 = OLTDevice(ip="1.1.1.2", hostname="OLT-B", community="public")
    nodes = [make_node("P", [o1]), make_node("Q", [o2])]
    assert len(select_target_devices(nodes, None)) == 2


def test_main_dry_run_no_error(tmp_path, capsys):
    nodes_yaml = tmp_path / "nodes.yaml"
    nodes_yaml.write_text(
        "nodes:\n"
        "- name: P\n"
        "  mikrotiks: []\n"
        "  olts:\n"
        "  - ip: 1.1.1.1\n"
        "    hostname: OLT-PRADO-1\n"
        "    community: public\n"
        "    modelo: V1600G1\n"
        "    pon_count: 8\n"
        "    descripcion: X\n"
        "    conectado_a: ''\n"
    )
    from src.collectors.base import CollectorResult

    def fake_result(olt_device, node_name):
        metric = {
            "measurement": "onu_location",
            "tags": {"olt_name": olt_device.hostname, "olt_ip": olt_device.ip,
                     "nodo": node_name, "mac": "00:EB:D8:17:E9:91",
                     "location": "GPON05_17"},
            "fields": {"onu_index": "5_17", "pon_port": "GPON0/5",
                       "onu_serial": "VSOL00f530fe", "onu_model": "V624",
                       "estado_onu": 3},
        }
        return CollectorResult(
            success=True, metrics=[metric], errors=[],
            duration_seconds=0.1,
            device_name=olt_device.hostname, device_ip=olt_device.ip)

    rc = main(["--olt", "OLT-PRADO-1", "--dry-run",
               "--nodes-path", str(nodes_yaml)],
              collector_factory=fake_result)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OLT-PRADO-1" in out
    assert "00:EB:D8:17:E9:91" in out