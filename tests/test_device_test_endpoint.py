# tests/test_device_test_endpoint.py
#
# Reproduce el mensaje engañoso del endpoint POST /api/devices/test:
# cuando el ICMP falla, igual imprimía
#   "[DIAGNÓSTICO] El equipo responde a Ping pero NO a SNMP"
# (raíz: el diagnóstico se imprimía sin validar details["ping"]).

import asyncio
from unittest.mock import patch

from src.api.routes.devices import DeviceTestRequest
from src.api.routes.devices import test_device_connection as device_test_endpoint


def _run(ip="172.16.74.21", community="VN@net", dev_type="olt", ping_ok=False, snmp_ok=False):
    req = DeviceTestRequest(ip=ip, community=community, type=dev_type, hostname="TEST")
    ping_res = {
        "ok": ping_ok,
        "tool": "fping" if ping_ok else None,
        "returncode": 0 if ping_ok else 2,
        "attempts": [] if ping_ok else ["ping rc=2: Operation not permitted", "fping rc=1"],
    }
    snmp_logs = (
        ["  --> Consulta SNMP Exitosa! [OK]"]
        if snmp_ok
        else ["  --> Fallo de respuesta SNMP: No SNMP response received before timeout [FAIL]"]
    )
    with patch("src.api.routes.devices.icmp_ping", return_value=ping_res), \
         patch("src.api.routes.devices._snmp_probe",
               return_value=(snmp_ok, {"sysDescr": "V1600G1B"} if snmp_ok else {}, snmp_logs)):
        return asyncio.run(device_test_endpoint(req))


def test_no_afirma_responde_a_ping_cuando_icmp_fallo():
    resp = _run(ping_ok=False, snmp_ok=False)

    assert resp.success is False
    assert not any("responde a Ping" in line for line in resp.logs), \
        "el test no puede afirmar que el equipo responde a Ping si el ICMP falló"
    assert "STATUS ERROR" in resp.logs[-1]


def test_status_parcial_cuando_ping_ok_y_snmp_bloqueado():
    resp = _run(ping_ok=True, snmp_ok=False)

    assert resp.success is False
    assert "STATUS PARCIAL" in resp.logs[-1]
    assert any("responde a Ping pero NO a SNMP" in line for line in resp.logs)


def test_status_ok_cuando_ping_y_snmp_ok():
    resp = _run(ping_ok=True, snmp_ok=True)

    assert resp.success is True
    assert "STATUS OK" in resp.logs[-1]
