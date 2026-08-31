from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.reporte.reporter import DailyReporter, FormatLoader


def test_merge_discovered_hosts_includes_known_hosts_without_data():
    from src.api.routes.reporte import _merge_discovered_hosts

    discovered = ["PRADO-VIDANET", "SISAL-VIDANET"]
    known = ["CORE-VN", "NARANJILLOS-VIDANET", "PRADO-VIDANET"]

    merged = _merge_discovered_hosts(discovered, known)

    assert "NARANJILLOS-VIDANET" in merged
    assert "CORE-VN" in merged
    assert len(merged) == len(set(merged))
    assert merged.index("PRADO-VIDANET") < merged.index("NARANJILLOS-VIDANET")


def test_resolve_host_names_includes_configured_without_data():
    reporter = DailyReporter(influx=MagicMock(), bot_token="", chat_id="")

    names = reporter._resolve_host_names(
        discovered=["PRADO-VIDANET", "SISAL-VIDANET"],
        configured=[{"name": "NARANJILLOS-VIDANET"}, {"name": "PRADO-VIDANET"}],
        has_config=True,
    )

    assert set(names) == {"PRADO-VIDANET", "NARANJILLOS-VIDANET"}
    assert "SISAL-VIDANET" not in names


def test_build_report_renders_host_without_system_data():
    reporter = DailyReporter(influx=MagicMock(), bot_token="", chat_id="")
    fmt = FormatLoader("/nonexistent/report.conf")

    mk_data = [{
        "tags": {"device_name": "NARANJILLOS-VIDANET"},
        "fields": {},
        "ifaces": [],
        "any_fresh": False,
    }]

    sections = reporter._build_report([], mk_data, [], fmt)

    assert "NARANJILLOS-VIDANET" in sections["mk"]
    assert "Sin datos de interfaz" in sections["mk"]


def _interfaces_query():
    now = datetime.now(timezone.utc)
    base = {
        "interface_name": "sfp-sfpplus1-LINK-PRADO-NARANJILLO",
        "device_ip": "192.0.2.13",
        "time": now,
    }
    return [
        dict(base, field="ifHCInOctets", value=1000),
        dict(base, field="ifHCOutOctets", value=500),
        dict(base, field="ifOperStatus", value=1),
        dict(base, interface_name="ether1", value=2000, field="ifHCInOctets"),
        dict(base, interface_name="ether1", value=1000, field="ifHCOutOctets"),
        dict(base, interface_name="ether1", value=1, field="ifOperStatus"),
    ]


def test_get_mikrotik_interfaces_allows_configured_sfp_interface():
    reporter = DailyReporter(influx=MagicMock(), bot_token="", chat_id="")
    reporter._query = MagicMock(return_value=_interfaces_query())
    reporter._compute_rate_mikrotik = MagicMock(return_value=0.0)

    ifaces = reporter._get_mikrotik_interfaces(
        "PRADO-VIDANET", allowed={"sfp-sfpplus1-LINK-PRADO-NARANJILLO"}
    )
    names = [i["iface_name"] for i in ifaces]

    assert "sfp-sfpplus1-LINK-PRADO-NARANJILLO" in names
    assert all(i.get("is_fresh") for i in ifaces)


def test_get_mikrotik_interfaces_skips_sfp_without_allowed():
    reporter = DailyReporter(influx=MagicMock(), bot_token="", chat_id="")
    reporter._query = MagicMock(return_value=_interfaces_query())
    reporter._compute_rate_mikrotik = MagicMock(return_value=0.0)

    ifaces = reporter._get_mikrotik_interfaces("PRADO-VIDANET")
    names = [i["iface_name"] for i in ifaces]

    assert "sfp-sfpplus1-LINK-PRADO-NARANJILLO" not in names
    assert "ether1" in names


def test_get_mikrotik_ping_status_builds_map():
    reporter = DailyReporter(influx=MagicMock(), bot_token="", chat_id="")
    reporter._query = MagicMock(return_value=[
        {"name": "NARANJILLOS-VIDANET", "field": "status", "value": 1},
        {"name": "NARANJILLOS-VIDANET", "field": "latency_ms_avg", "value": 0.47},
        {"name": "HOST-DOWN", "field": "status", "value": 0},
    ])

    out = reporter._get_mikrotik_ping_status(["NARANJILLOS-VIDANET", "HOST-DOWN"])

    assert out["NARANJILLOS-VIDANET"]["up"] is True
    assert out["NARANJILLOS-VIDANET"]["latency"] == 0.47
    assert out["HOST-DOWN"]["up"] is False
    assert reporter._get_mikrotik_ping_status([]) == {}


def test_build_report_ping_up_host_not_offline():
    fmt = FormatLoader("config/telegram-report-format.conf")
    reporter = DailyReporter(influx=MagicMock(), bot_token="", chat_id="")

    mk_data = [
        {"tags": {"device_name": "NARANJILLOS-VIDANET", "device_ip": "192.0.2.1"},
         "fields": {}, "ifaces": [], "any_fresh": False,
         "ping_up": True, "ping_latency": 0.47},
        {"tags": {"device_name": "SISAL-VIDANET", "device_ip": "192.0.2.3"},
         "fields": {}, "ifaces": [], "any_fresh": True, "ping_up": True},
        {"tags": {"device_name": "HOST-DOWN", "device_ip": "10.0.0.1"},
         "fields": {}, "ifaces": [], "any_fresh": False, "ping_up": False},
    ]

    sections = reporter._build_report([], mk_data, [], fmt)

    mk = sections["mk"]
    assert "🟡" in mk
    assert "Responde a ping, sin datos SNMP (0.5 ms)" in mk
    assert "🔴" in mk
    assert "Sin datos de interfaz" in mk

    summary = sections["summary"]
    assert "HOST-DOWN" in summary
    assert "NARANJILLOS-VIDANET" not in summary
