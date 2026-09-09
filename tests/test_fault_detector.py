# tests/test_fault_detector.py
from src.fault_detector import (
    analyze_network,
    candidate_groups,
    _is_broadcast,
    _norm_state,
    OltStatusRefresher,
    FaultDetectionConfig,
)


def _cfg(**overrides) -> FaultDetectionConfig:
    cfg = FaultDetectionConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _client(estado, olt="OLT-1", nap="", pon="", nombre="", documento=""):
    c = {
        "estado_onu": estado,
        "olt": olt,
        "nodo": olt,
        "onu_index": "2_01",
        "nombre": nombre,
        "documento": documento,
    }
    if nap:
        c["nap"] = nap
    if pon:
        c["pon_port"] = pon
    return c


def test_norm_state():
    assert _norm_state(1) == 1
    assert _norm_state(3) == 3
    assert _norm_state(-1) is None
    assert _norm_state(0) is None
    assert _norm_state("4") == 4
    assert _norm_state("nope") is None
    assert _norm_state(None) is None


def test_analyze_groups_por_nap_y_olt():
    clients = []
    for i in range(5):
        clients.append(_client(1, olt="OLT-1", nap="NAP-A", pon="1",
                               nombre=f"Cliente {i}", documento=f"D{i}"))
    for i in range(3):
        clients.append(_client(3, olt="OLT-1", nap="NAP-B", pon="2",
                               nombre=f"Sano {i}", documento=f"S{i}"))
    clients.append(_client(None, olt="OLT-9", nap="NAP-X"))  # desconocido

    nap_groups, olt_groups = analyze_network(clients, _cfg())
    assert len(nap_groups) == 3
    assert len(olt_groups) == 2
    g = nap_groups["OLT-1::NAP-A"]
    assert g.down == 5 and g.total == 5
    assert len(g.names()) == 5
    b = nap_groups["OLT-1::NAP-B"]
    assert b.down == 0 and b.total == 3
    x = nap_groups["OLT-9::NAP-X"]
    assert x.unknown == 1 and x.down == 0


def test_candidate_requiere_umbral_y_no_broadcast():
    clients = []
    # OLT-1 NAP-A: 5/5 caidos (candidato)
    for i in range(5):
        clients.append(_client(1, olt="OLT-1", nap="NAP-A", nombre=f"c{i}"))
    # OLT-1 NAP-Q: 1 caido de 10 (por debajo del umbral)
    for i in range(10):
        clients.append(_client(1 if i == 0 else 3, olt="OLT-1", nap="NAP-Q",
                               nombre=f"q{i}"))
    # OLT-2 NAP-R: 8/10 caidos -> broadcast de OLT (se suprime el NAP)
    for i in range(10):
        clients.append(_client(1 if i < 8 else 3, olt="OLT-2", nap="NAP-R",
                               nombre=f"r{i}"))

    nap_groups, olt_groups = analyze_network(clients, _cfg(threshold_clients_los=3))
    candidates, broadcast = candidate_groups(nap_groups, olt_groups, _cfg(threshold_clients_los=3))
    assert broadcast == {"OLT-2"}
    keys = [g.key for g in candidates]
    assert "OLT-2::NAP-R" not in keys
    # NAP-Q queda por debajo del umbral (1 < 3)
    assert "OLT-1::NAP-Q" not in keys
    assert "OLT-1::NAP-A" in keys


def test_is_broadcast_ratio():
    cfg = _cfg(threshold_clients_los=3, broadcast_down_ratio=0.6)
    og = analyze_network(
        [_client(1, "B", nap="N") for _ in range(8)]
        + [_client(3, "B", nap="N") for _ in range(2)], cfg
    )[1]["B"]
    assert _is_broadcast(og, cfg) is True

    og2 = analyze_network(
        [_client(1, "C", nap="N") for _ in range(3)]
        + [_client(3, "C", nap="N") for _ in range(7)], cfg
    )[1]["C"]
    # 3/10 < 0.6 -> no broadcast
    assert _is_broadcast(og2, cfg) is False


def test_unknown_reduce_denominador_broadcast():
    cfg = _cfg(threshold_clients_los=3, broadcast_down_ratio=0.6)
    # 8 caidos, 2 desconocidos, 0 online: 8/8 = 1.0 -> broadcast
    og = analyze_network(
        [_client(1, "D", nap="N") for _ in range(8)]
        + [_client(None, "D", nap="N") for _ in range(2)], cfg
    )[1]["D"]
    assert _is_broadcast(og, cfg) is True


def test_onu_suffix():
    assert OltStatusRefresher.onu_suffix("3_01") == "3.01"
    assert OltStatusRefresher.onu_suffix("12_10") == "12.10"
    assert OltStatusRefresher.onu_suffix("3.01") == "3.01"
    assert OltStatusRefresher.onu_suffix("") == ""