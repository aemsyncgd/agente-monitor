# tests/test_olt_mac_snmp.py
import pytest

from src.collectors.olt_collector import (
    build_vsol_mac_metrics,
    parse_vsol_port,
)
from src.collectors.olt_web_mac import (
    parse_macinfo_html,
    web_mac_credentials,
    web_mac_enabled,
)


def test_parse_vsol_port():
    assert parse_vsol_port("PON5:ONU16") == (5, 16)
    assert parse_vsol_port("pon05:onu3") == (5, 3)
    assert parse_vsol_port('"PON1:ONU1"') == (1, 1)
    assert parse_vsol_port("GE3") is None
    assert parse_vsol_port("CPU") is None
    assert parse_vsol_port("") is None


def test_build_vsol_mac_metrics_attributes_onu():
    macs = {"2": "6C 68 A4 D4 62 B9"}
    ports = {"2": "PON5:ONU17"}
    vlans = {"2": "256"}
    metrics = build_vsol_mac_metrics(
        macs, ports,
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
        vlans=vlans,
        onu_status={"5.17": "3"},
        onu_serial={"5.17": "VSOL00f530fe"},
        onu_model={"5.17": "V624"},
    )
    assert [m["measurement"] for m in metrics] == ["onu_mac", "onu_location"]

    m = metrics[0]
    assert m["tags"]["mac"] == "6C:68:A4:D4:62:B9"
    assert m["tags"]["interface_name"] == "PON5:ONU17"
    assert m["tags"]["interface_type"] == "onu"
    assert m["tags"]["location"] == "GPON05_17"
    assert m["fields"]["vlan"] == 256
    assert m["fields"]["onu_index"] == "5_17"
    assert m["fields"]["pon_port"] == "GPON0/5"
    assert m["fields"]["onu_serial"] == "VSOL00f530fe"
    assert m["fields"]["estado_onu"] == 3

    loc = metrics[1]
    assert loc["measurement"] == "onu_location"
    assert loc["tags"]["location"] == "GPON05_17"
    assert "vlan" in loc["fields"] and "learned" not in loc["fields"]


def test_build_vsol_mac_metrics_ge_has_no_location():
    metrics = build_vsol_mac_metrics(
        {"1": "00 AA BB CC DD EE"}, {"1": "GE3"},
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
    )
    assert len(metrics) == 1
    assert metrics[0]["tags"]["interface_type"] == "ge"
    assert "location" not in metrics[0]["tags"]


def test_build_vsol_mac_metrics_unknown_port_is_otro():
    metrics = build_vsol_mac_metrics(
        {"1": "00 AA BB CC DD EE"}, {"1": "CPU"},
        olt_name="OLT-PRADO-1", olt_ip="192.0.2.10", nodo="PRADO",
    )
    assert len(metrics) == 1
    assert metrics[0]["tags"]["interface_type"] == "otro"


def test_build_vsol_mac_metrics_skips_invalid_and_missing():
    metrics = build_vsol_mac_metrics(
        {"a": "not-a-mac", "b": "00 AA BB CC DD EE"}, {"b": None},
        olt_name="O", olt_ip="1.1.1.1", nodo="N",
    )
    assert metrics == []


SAMPLE_MACINFO = """
<table>
<tr><th>VLAN ID</th><th>MAC</th><th>Type</th><th>Physical Port</th></tr>
<tr><td>256</td><td>6C:68:A4:D4:62:B9</td><td>Dynamic</td><td>PON1:ONU8</td></tr>
<script language="javascript">document.getElementById("maccount").value="12"</script>
<tr><td>256</td><td>30:16:9D:1F:D2:01</td><td>Dynamic</td><td>GE3</td></tr>
<tr><td>100</td><td>40:AE:30:68:3F:E0</td><td>Static</td><td>PON5:ONU16</td></tr>
<tr><td>256</td><td>no-es-mac</td><td>Dynamic</td><td>PON5:ONU20</td></tr>
</table>
"""


def test_parse_macinfo_html():
    rows = parse_macinfo_html(SAMPLE_MACINFO)
    assert len(rows) == 3
    assert rows[0] == {"mac": "6C:68:A4:D4:62:B9", "port": "PON1:ONU8",
                       "vlan": 256, "type": "Dynamic"}
    assert rows[1]["port"] == "GE3"
    assert rows[2] == {"mac": "40:AE:30:68:3F:E0", "port": "PON5:ONU16",
                       "vlan": 100, "type": "Static"}


def test_web_mac_enabled_requires_password(monkeypatch):
    monkeypatch.delenv("OLT_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("OLT_WEB_ENABLED", raising=False)
    assert web_mac_enabled() is False

    monkeypatch.setenv("OLT_WEB_PASSWORD", "secret")
    assert web_mac_enabled() is True
    assert web_mac_credentials() == ("root", "secret")

    monkeypatch.setenv("OLT_WEB_ENABLED", "false")
    assert web_mac_enabled() is False

    monkeypatch.setenv("OLT_WEB_USER", "admin")
    assert web_mac_credentials() == ("admin", "secret")
