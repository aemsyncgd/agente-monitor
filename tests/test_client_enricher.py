# tests/test_client_enricher.py
from src.client_enricher import enrich_clients, _norm_mac


def _olt(mac, serial, estado=3, olt="OLT-1", nodo="N1", pon="2", idx="2_01"):
    return {
        "mac": mac,
        "onu_serial": serial,
        "olt": olt,
        "olt_name": olt,
        "nodo": nodo,
        "pon_port": pon,
        "onu_index": idx,
        "estado_onu": estado,
    }


def _pppoe(user, mac, status="online"):
    return {"username": user, "mac_address": mac, "status": status}


def _wisp(documento, serial="", usuario="", nombre="", mac="", nap="", zona=""):
    return {
        "documento": documento,
        "serial_onu": serial,
        "usuario_pppoe": usuario,
        "nombre": nombre,
        "nap_nombre": nap,
        "zona": zona,
        "estado": "activo",
        "mac": mac,
    }


def test_sin_datos_no_matches():
    matched, pending = enrich_clients([], [], [], [])
    assert matched == [] and pending == []


def test_match_por_serial():
    wisp = [_wisp("DOC1", serial="SER-001", usuario="aa", nombre="Cliente Uno",
                  nap="NAP-1", zona="Centro")]
    olt = [_olt("aa:bb:cc:dd:ee:01", "SER-001", estado=3)]
    matched, pending = enrich_clients(wisp, olt, [], [])
    assert len(matched) == 1 and pending == []
    m = matched[0]
    assert m["coincidencia"] == "serial"
    assert m["olt"] == "OLT-1"
    assert m["estado_onu"] == "3"
    assert m["mac"] == "aa:bb:cc:dd:ee:01"
    assert m["ubicacion_descuadrada"] is False
    assert m["nap"] == "NAP-1"
    assert m["zona"] == "Centro"


def test_match_por_pppoe_y_mac():
    wisp = [_wisp("DOC2", usuario="cd", nombre="Cliente Dos")]
    olt = [_olt("aa:bb:cc:dd:ee:02", "SER-002", estado=3)]
    pppoe = [_pppoe("CD", "AA:BB:CC:DD:EE:02")]
    matched, pending = enrich_clients(wisp, olt, pppoe, [])
    assert len(matched) == 1 and pending == []
    m = matched[0]
    assert m["coincidencia"] == "mac"
    assert m["usuario_pppoe"] == "cd"
    assert m["pppoe_status"] == "online"


def test_mac_normalizada_seis_octetos():
    assert _norm_mac("AA:BB:CC:DD:EE:02") == "aa:bb:cc:dd:ee:02"
    assert _norm_mac("a:b:c:d:e:f") == "0a:0b:0c:0d:0e:0f"
    assert _norm_mac("00:00:00:00:00:00") == ""


def test_pendiente_sin_olt():
    wisp = [_wisp("DOC3", serial="SER-NOPE", usuario="xyz")]
    matched, pending = enrich_clients(wisp, [], [], [])
    assert matched == []
    assert len(pending) == 1
    assert pending[0]["causa"] == "sin_olt"
    assert pending[0]["documento"] == "DOC3"


def test_pendiente_sin_pppoe():
    wisp = [_wisp("DOC4", usuario="nouser")]
    matched, pending = enrich_clients(wisp, [], [], [])
    assert matched == []
    assert pending[0]["causa"] == "sin_pppoe"


def test_descuadrado_deteccion():
    # El serial registrado no coincide con el serial fisico de la ONU del mismo MAC.
    wisp = [_wisp("DOC5", serial="SER-X", usuario="ef", mac="aa:bb:cc:dd:ee:05")]
    olt = [_olt("aa:bb:cc:dd:ee:05", "SER-Y", estado=1)]
    matched, pending = enrich_clients(wisp, olt, [], [])
    assert len(matched) == 1
    assert matched[0]["ubicacion_descuadrada"] is True
    assert matched[0]["coincidencia"] == "mac"
    assert matched[0]["estado_onu"] == "1"


def test_mejor_coincidencia_por_serial():
    # Si el serial coincide, se prefiere esa fila sobre el match por MAC.
    wisp = [_wisp("DOC6", serial="SER-007", usuario="gh")]
    olt_a = [_olt("bb:cc:dd:ee:ff:77", "SER-777", estado=6)]
    olt_b = [_olt("aa:bb:cc:dd:ee:77", "SER-007", estado=3)]
    pppoe = [_pppoe("GH", "AA:BB:CC:DD:EE:77")]
    matched, _ = enrich_clients(wisp, olt_a + olt_b, pppoe, [])
    assert len(matched) == 1
    assert matched[0]["coincidencia"] == "serial"
    assert matched[0]["mac"] == "aa:bb:cc:dd:ee:77"

    canonic = [m for m in matched if m["estado_onu"] == "3"]
    assert len(canonic) == 1


def test_estado_desconocido_se_conserva():
    wisp = [_wisp("DOC7", serial="SER-008")]
    olt = [_olt("aa:bb:cc:dd:ee:08", "SER-008", estado=-1)]
    matched, _ = enrich_clients(wisp, olt, [], [])
    assert matched[0]["estado_onu"] == "-1"