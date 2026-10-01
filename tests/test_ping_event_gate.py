# tests/test_ping_event_gate.py
"""Dedup del publisher de eventos ping: host_down/host_up solo se publican
cuando el estado del host cambia (no en cada escaneo)."""
from src.collectors.main import _ping_state_change, _ping_published_status


def test_none_until_state_change():
    _ping_published_status.clear()
    # Primera observación: un host ya caído sí alerta (es un cambio vs. desconocido)
    assert _ping_state_change("H", 0) == "host_down"
    # Mismo estado caído en escaneos siguientes: sin evento (NO spam)
    assert _ping_state_change("H", 0) is None
    assert _ping_state_change("H", 0) is None
    # Recupera: transición up → host_up
    assert _ping_state_change("H", 1) == "host_up"
    # Sigue arriba: sin evento
    assert _ping_state_change("H", 1) is None
    # Nueva caída (episodio distinto): vuelve a alertar
    assert _ping_state_change("H", 0) == "host_down"


def test_first_up_does_not_spam():
    """Al arrancar el collector, N hosts arriba no generan N alerts host_up."""
    _ping_published_status.clear()
    assert _ping_state_change("A", 1) is None
    assert _ping_state_change("B", 1) is None
    assert _ping_state_change("B", 1) is None
    assert _ping_published_status == {"A": 1, "B": 1}


def test_hosts_independientes():
    _ping_published_status.clear()
    assert _ping_state_change("N1", 0) == "host_down"
    assert _ping_state_change("N2", 0) == "host_down"
    assert _ping_state_change("N1", 0) is None
    assert _ping_state_change("N2", 0) is None
    assert _ping_state_change("N1", 1) == "host_up"
    # N2 sigue caído: no cambió
    assert _ping_state_change("N2", 0) is None