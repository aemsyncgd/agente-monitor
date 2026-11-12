# tests/test_syslog_relay.py
"""Relay syslog del host + extracción de la IP de origen en log_monitor.

El NAT de podman rootless hacía que todos los equipos aparecieran como la IP
interna del contenedor (10.89.6.x). El relay escucha en el host y antepone
``__SRCIP__=<ip real>`` al mensaje; log_monitor la recupera como device_ip.
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relay = _load("syslog_relay_under_test", "syslog_relay.py")
log_monitor = _load("log_monitor_relay_under_test", "log_monitor.py")


def test_conserva_pri_timestamp_host_y_tag():
    raw = "<11>Nov  5 15:06:00 YAGUA-VIDANET script,error: PRUEBA_MONITOREO: fallo"

    line = relay.build_forward_line(raw, "192.168.88.1")

    assert line == (
        "<11>Nov  5 15:06:00 YAGUA-VIDANET script,error: "
        "__SRCIP__=192.168.88.1 PRUEBA_MONITOREO: fallo"
    )


def test_conserva_pri_distinto_de_notice():
    raw = "<134>Nov  5 15:06:00 OLT-1 system,error,critical: laser off"

    line = relay.build_forward_line(raw, "10.0.0.5")

    assert line.startswith("<134>Nov  5 15:06:00 OLT-1 system,error,critical: ")
    assert "__SRCIP__=10.0.0.5 laser off" in line


def test_sin_cabecera_rfc3164_usa_ip_del_peer_como_host():
    line = relay.build_forward_line("mensaje suelto sin cabecera", "10.20.30.40")

    assert "__SRCIP__=10.20.30.40 suelto sin cabecera" in line
    assert line.startswith("<13>")
    # host = IP del peer; la primera palabra se conserva como tag (igual que rsyslog).
    assert re.match(r"^<13>[A-Z][a-z]{2}\s+\d{1,2} \d{2}:\d{2}:\d{2} 10\.20\.30\.40 mensaje: ",
                    line)


def test_topico_routeros_sin_dos_puntos_se_conserva_como_tag():
    """Sin esto el filtro de debug de rsyslog dejaba de ver ",debug" y se colaba ruido."""
    raw = "<30>Nov  5 11:19:35 172.16.126.1 pppoe,ppp,debug,packet     sent PADI"

    line = relay.build_forward_line(raw, "172.16.126.1")

    assert " 172.16.126.1 pppoe,ppp,debug,packet: " in line
    assert "__SRCIP__=172.16.126.1 sent PADI" in line


def test_colon_dentro_del_mensaje_no_se_confunde_con_el_tag():
    """Regresión: la marca acababa en medio del mensaje ("type: __SRCIP__")."""
    raw = "<30>Nov  5 11:22:20 172.16.126.1 netwatch,info event down [ type: simple, host: 10.0.1.13 ]"

    line = relay.build_forward_line(raw, "172.16.126.1")

    assert (
        " 172.16.126.1 netwatch,info: __SRCIP__=172.16.126.1 "
        "event down [ type: simple, host: 10.0.1.13 ]"
    ) in line


def test_prefijo_de_sesion_pppoe_queda_dentro_del_mensaje():
    raw = (
        "<30>Nov  5 11:22:32 172.16.126.1 pppoe,ppp,error "
        "<4694>: user PENGCONGC1 authentication failed"
    )

    line = relay.build_forward_line(raw, "172.16.126.1")

    assert (
        " 172.16.126.1 pppoe,ppp,error: __SRCIP__=172.16.126.1 "
        "<4694>: user PENGCONGC1 authentication failed"
    ) in line


def test_host_invalido_se_reemplaza_por_ip_del_peer():
    raw = "<11>Nov  5 15:06:00 host:con:dos:puntos tag,info: hola"

    line = relay.build_forward_line(raw, "172.16.0.9")

    assert " 172.16.0.9 tag,info: __SRCIP__=172.16.0.9 hola" in line


def test_no_inyecta_saltos_de_linea():
    raw = "<11>Nov  5 15:06:00 ROUTER tag,error: linea1\nlinea2\rlinea3"

    line = relay.build_forward_line(raw, "10.0.0.7")

    assert "\n" not in line and "\r" not in line
    assert "__SRCIP__=10.0.0.7 linea1 linea2 linea3" in line


def test_ipv6_en_la_marca():
    raw = "<11>Nov  5 15:06:00 ROUTER tag,info: hola"

    line = relay.build_forward_line(raw, "2001:db8::1")

    assert "__SRCIP__=2001:db8::1 hola" in line


def test_log_monitor_extrae_ip_real_y_limpia_la_marca():
    evento = {
        "timestamp": "2026-11-05T15:06:00+00:00",
        "tag": "script,error",
        "severity": "notice",
        "fromip": "127.0.0.1",          # IP del contenedor/relay
        "host": "YAGUA-VIDANET",
        "message": "__SRCIP__=192.168.88.1 PRUEBA_MONITOREO: fallo de enlace",
    }

    result = log_monitor.procesar_log(json.dumps(evento))

    assert result is not None
    stored = json.loads(result["json"])
    assert stored["device_ip"] == "192.168.88.1"
    assert stored["srcip"] == "192.168.88.1"
    assert stored["fromhost_ip"] == "192.168.88.1"
    assert stored["message"] == "PRUEBA_MONITOREO: fallo de enlace"
    assert stored["severity_effective"] == "error"


def test_log_monitor_sin_marca_mantiene_comportamiento_anterior():
    evento = {
        "timestamp": "2026-11-05T15:06:00+00:00",
        "tag": "script,error",
        "severity": "notice",
        "fromip": "10.89.6.6",
        "message": "PRUEBA_MONITOREO: fallo de enlace",
    }

    result = log_monitor.procesar_log(json.dumps(evento))

    assert result is not None
    stored = json.loads(result["json"])
    assert stored["device_ip"] == "10.89.6.6"
    assert stored["message"] == "PRUEBA_MONITOREO: fallo de enlace"
    assert "srcip" not in stored


def test_log_monitor_normaliza_dos_puntos_finales_del_tag():
    """rsyslog a veces guarda el tag como "script,error:": el panel debe verlo limpio."""
    evento = {
        "timestamp": "2026-11-05T15:06:00+00:00",
        "tag": "script,error:",
        "program": "script,error:",
        "severity": "notice",
        "fromip": "192.168.88.1",
        "message": "PRUEBA_TAG: sin dos puntos",
    }

    result = log_monitor.procesar_log(json.dumps(evento))

    assert result is not None
    stored = json.loads(result["json"])
    assert stored["tag"] == "script,error"
    assert stored["program"] == "script,error"
    assert stored["severity_effective"] == "error"
