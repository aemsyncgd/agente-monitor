# tests/test_syslog_severity.py
"""Severidad efectiva de syslog RouterOS + integración con monitor y API.

RouterOS envía casi todos los mensajes con ``severity=notice`` y codifica el
nivel real en el tag (``script,error``, ``system,error,critical``...). Estos
tests fijan ese comportamiento para que el dashboard no vuelva a perder
alertas reales.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from src.syslog_severity import effective_severity, is_important
from src.api.routes import syslog as syslog_route


PRUEBA_EVENT = {
    "timestamp": "2026-11-05T14:48:52.022597+00:00",
    "tag": "script,error",
    "severity": "notice",
    "fromip": "10.89.6.6",
    "device_ip": "10.89.6.6",
    "device_name": "10.89.6.6",
    "message": " PRUEBA_MONITOREO: Mensaje informativo de prueba",
}

INFO_EVENT = {
    "timestamp": "2026-11-05T14:49:00+00:00",
    "tag": "netwatch,info",
    "severity": "notice",
    "device_ip": "10.89.6.6",
    "device_name": "10.89.6.6",
    "message": "host up",
}


@pytest.mark.parametrize("tag,severity,expected", [
    ("script,error", "notice", "error"),
    ("script,error:", "notice", "error"),          # tag con dos puntos finales
    ("system,error,critical", "notice", "critical"),
    ("pppoe,ppp,info,account", "notice", "info"),
    ("netwatch,info", "notice", "info"),
    ("pppoe,ppp,debug,packet", "notice", "debug"),
    ("system,error", "err", "error"),
    ("system,error", "crit", "critical"),          # severity más grave gana
    ("system,info", "warning", "warning"),         # severity más grave gana
    ("", "warning", "warning"),                    # sin tag: se usa severity
    ("topic,sin,nivel", "err", "error"),           # err normalizado
    ("topic,sin,nivel", "", ""),
])
def test_effective_severity(tag, severity, expected):
    assert effective_severity(tag, severity) == expected


def test_evento_prueba_es_importante_por_tag():
    important, reason = is_important(PRUEBA_EVENT)
    assert important is True
    assert reason == "error"


def test_evento_informativo_no_es_importante():
    important, reason = is_important(INFO_EVENT)
    assert important is False
    assert reason is None


class FakeRedis:
    def __init__(self, events):
        self._items = [json.dumps(e) for e in events]

    def zrangebyscore(self, key, start, end):
        return list(self._items)


def test_important_incluye_prueba_y_expone_severidad_efectiva(monkeypatch):
    fake = FakeRedis([PRUEBA_EVENT, INFO_EVENT])
    monkeypatch.setattr(syslog_route, "_get_redis", lambda: fake)

    result = syslog_route.get_syslog_important(minutes=360, limit=100, take=50)

    prueba = [e for e in result["events"] if "PRUEBA_MONITOREO" in e["message"]]
    assert len(prueba) == 1
    assert prueba[0]["severity_effective"] == "error"
    assert prueba[0]["_important_reason"] == "error"


def test_important_no_deja_que_las_noticias_desplacen_a_los_errores(monkeypatch):
    """Regresión: una tormenta de eventos informativos ocultaba el error real."""
    noticias = [
        {
            "timestamp": f"2026-11-05T15:{i % 60:02d}:00+00:00",
            "tag": "netwatch,info",
            "severity": "notice",
            "device_ip": "10.0.0.9",
            "message": f"event down [ host: 10.0.20.{i} ]",
        }
        for i in range(120)
    ]
    fake = FakeRedis(noticias + [PRUEBA_EVENT])
    monkeypatch.setattr(syslog_route, "_get_redis", lambda: fake)

    result = syslog_route.get_syslog_important(minutes=360, limit=100, take=50)

    assert any("PRUEBA_MONITOREO" in e["message"] for e in result["events"])


def test_important_agrupa_reintentos_de_autenticacion(monkeypatch):
    """El id de sesión cambia en cada reintento: deben verse como un solo evento."""
    reintentos = [
        {
            "timestamp": f"2026-11-05T15:00:{i:02d}+00:00",
            "tag": "pppoe,ppp,error",
            "severity": "notice",
            "device_ip": "10.89.6.6",
            "message": f"<{0x4000 + i:x}>: user VN507689069-1 authentication failed",
        }
        for i in range(60)
    ]
    fake = FakeRedis(reintentos + [PRUEBA_EVENT])
    monkeypatch.setattr(syslog_route, "_get_redis", lambda: fake)

    result = syslog_route.get_syslog_important(minutes=360, limit=100, take=50)

    auth = [e for e in result["events"] if e["tag"] == "pppoe,ppp,error"]
    assert len(auth) == 1
    assert auth[0]["_repeat_count"] == 60
    assert any("PRUEBA_MONITOREO" in e["message"] for e in result["events"])


def test_summary_agrupa_por_severidad_efectiva(monkeypatch):
    fake = FakeRedis([PRUEBA_EVENT, INFO_EVENT])
    monkeypatch.setattr(syslog_route, "_get_redis", lambda: fake)

    result = syslog_route.get_syslog_summary(minutes=360, limit=30)

    assert result["total"] == 2
    assert result["by_severity"] == {"error": 1, "info": 1}
    assert all("severity_effective" in e for e in result["recent"])


def test_recent_filtra_por_severidad_efectiva(monkeypatch):
    fake = FakeRedis([PRUEBA_EVENT, INFO_EVENT])
    monkeypatch.setattr(syslog_route, "_get_redis", lambda: fake)

    result = syslog_route.get_syslog_recent(
        minutes=360, limit=100,
        device_ip=None, device_name=None, device_type=None, site=None,
        severity="error", search=None,
    )

    assert result["count"] == 1
    assert result["events"][0]["severity_effective"] == "error"


def _load_log_monitor():
    path = Path(__file__).resolve().parents[1] / "scripts" / "log_monitor.py"
    spec = importlib.util.spec_from_file_location("log_monitor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_log_monitor_ingiere_prueba_con_severidad_efectiva():
    lm = _load_log_monitor()

    result = lm.procesar_log(json.dumps(PRUEBA_EVENT))

    assert result is not None
    stored = json.loads(result["json"])
    assert stored["severity_effective"] == "error"


def test_log_monitor_sigue_descartando_debug_de_paquetes():
    lm = _load_log_monitor()
    debug_line = {
        "timestamp": "2026-11-05T14:50:00+00:00",
        "tag": "pppoe,ppp,debug,packet",
        "severity": "notice",
        "fromip": "10.89.6.6",
        "message": "sent PADI",
    }

    assert lm.procesar_log(json.dumps(debug_line)) is None
