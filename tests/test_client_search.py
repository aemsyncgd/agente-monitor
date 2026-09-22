# tests/test_client_search.py
"""
Tests de ClientSearchService.find: WISP vivo + Influx, sin pivot.
"""
import time
from datetime import datetime, timezone

import pytest

from src.client_search import ClientSearchService, _MAX_GROUP


class FakeWisp:
    def __init__(self, serials=None, docs=None):
        self.serial_map = {k.upper(): v for k, v in (serials or {}).items()}
        self.doc_map = {k.upper(): v for k, v in (docs or {}).items()}
        self.calls_serial = []
        self.calls_doc = []

    def fetch_by_serial(self, serial, force=False):
        self.calls_serial.append(serial)
        return self.serial_map.get(serial.upper())

    def fetch_by_document(self, documento):
        self.calls_doc.append(documento)
        return self.doc_map.get(str(documento).strip().upper())


class FakeInflux:
    """Devuelve filas según el measurement filtrado en el flux."""

    def __init__(self, rows_by_measurement=None):
        self.bucket = "monitoreo"
        self.rows = dict(rows_by_measurement or {})

    def query(self, flux):
        import re as _re
        m = _re.search(r'\[\"_measurement\"\] == \"(\w+)\"', flux)
        if not m:
            return []
        return self.rows.get(m.group(1), [])


def _opt_row(field, value, **tags):
    row = {"measurement": "optical_power", "field": field, "value": value,
           "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
           "olt_name": "OLT-SISAL-2", "olt_ip": "10.0.0.2",
           "pon_port": "GPON0/2", "onu_index": "1/0/1", "onu_model": "HG8245",
           "nodo": "Nodo Centro"}
    row.update(tags)
    return row


def _onu_mac_row(field, value, mac, **tags):
    row = {"measurement": "onu_mac", "field": field, "value": value,
           "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
           "mac": mac}
    row.update(tags)
    return row


def _mk_service(**rows):
    return ClientSearchService(
        wisp=FakeWisp(serials=rows.pop("serials", None),
                      docs=rows.pop("docs", None)),
        influx=FakeInflux(rows_by_measurement=rows),
    )


WISP_SERIAL = {
    "documento": "V-5384589",
    "serial_onu": "GPON00F58B52",
    "usuario_pppoe": "MARIAGG",
    "mac": "aa:bb:cc:dd:ee:01",
    "nombre": "MARIA GRACIELA GASTELLO DE HERNANDEZ",
    "direccion": "Sector El Toco casa nro 84-A",
    "telefono": "0412-1234567",
    "plan": "50M",
    "tecnologia": "GPON",
    "estado": "activo",
    "ip_servicio": "10.0.65.19",
    "nap_nombre": "I01313",
    "puerto_pon": "5",
    "zona": "Guacara",
}


def test_find_por_serial_fusiona_wisp_y_optical():
    svc = _mk_service(
        serials={"GPON00F58B52": WISP_SERIAL},
        optical_power=[
            _opt_row("status", 3),
            _opt_row("rx_power", -18.5),
            _opt_row("tx_power", 2.1),
        ],
    )
    res = svc.find(serial="gpon00f58b52")
    assert res["found"] is True
    f = res["ficha"]
    assert f["nombre"] == "MARIA GRACIELA GASTELLO DE HERNANDEZ"
    assert f["documento"] == "V-5384589"
    assert f["serial_onu"] == "GPON00F58B52"
    assert f["estado_onu"] == 3
    assert f["rx_power"] == -18.5
    assert f["olt"] == "OLT-SISAL-2"
    assert f["pon_port"] == "GPON0/2"
    assert "wisp" in f["source"] and "influx" in f["source"]


def test_find_por_documento_deriva_serial():
    svc = _mk_service(
        docs={"V-5384589": WISP_SERIAL},
        optical_power=[_opt_row("status", 3), _opt_row("rx_power", -20.0)],
    )
    res = svc.find(documento="v-5384589")
    assert res["found"] is True
    assert res["ficha"]["serial_onu"] == "GPON00F58B52"
    assert svc.wisp.calls_doc == ["v-5384589"]


def test_find_por_mac_resuelve_serial_via_influx():
    svc = _mk_service(
        serials={"VSOL005ECDCA": {**WISP_SERIAL, "serial_onu": "VSOL005ECDCA",
                                  "mac": "a1:b2:c3:d4:e5:f6"}},
        onu_mac=[_onu_mac_row("onu_serial", "VSOL005ECDCA",
                              "a1:b2:c3:d4:e5:f6")],
        optical_power=[_opt_row("status", 1), _opt_row("rx_power", -32.0)],
    )
    res = svc.find(mac="A1:B2:C3:D4:E5:F6")
    assert res["found"] is True
    assert res["ficha"]["serial_onu"] == "VSOL005ECDCA"
    assert res["ficha"]["mac"] == "a1:b2:c3:d4:e5:f6"


def test_find_sin_resultados_retorna_found_false():
    svc = _mk_service(optical_power=[])
    res = svc.find(serial="NOEXISTE1")
    assert res["found"] is False
    assert res["ficha"] is None


def test_find_parcial_con_optical_sin_wisp():
    svc = _mk_service(optical_power=[
        _opt_row("status", 1, onu_serial="GPON00AAA1"),
        _opt_row("rx_power", -30.0, onu_serial="GPON00AAA1"),
    ])
    res = svc.find(serial="GPON00AAA1")
    assert res["found"] is True
    assert res["ficha"]["estado_onu"] == 1
    assert res["ficha"]["nombre"] == ""
    assert "influx" in res["ficha"]["source"]


def test_find_no_usa_pivot_en_flux():
    svc = _mk_service(serials={"GPON00F58B52": WISP_SERIAL},
                      optical_power=[_opt_row("status", 3)])
    svc.find(serial="GPON00F58B52")
    captured = []
    orig = svc.influx.query
    svc.influx.query = captured.append
    svc.clear_cache()
    svc.find(serial="GPON00F58B52")
    assert captured, "debería haber consultado Influx"
    assert all("pivot" not in str(c) for c in captured)


def test_cache_corta_evita_reconsultar_wisp():
    wisp = FakeWisp(serials={"GPON00F58B52": WISP_SERIAL})
    svc = ClientSearchService(wisp=wisp,
                              influx=FakeInflux(rows_by_measurement={
                                  "optical_power": [_opt_row("status", 3)]}))
    svc.find(serial="GPON00F58B52")
    svc.find(serial="GPON00F58B52")
    assert len(wisp.calls_serial) == 1


def test_cache_expira_y_reconsulta():
    wisp = FakeWisp(serials={"GPON00F58B52": WISP_SERIAL})
    svc = ClientSearchService(wisp=wisp,
                              influx=FakeInflux(rows_by_measurement={
                                  "optical_power": [_opt_row("status", 3)]}))
    svc.find(serial="GPON00F58B52")
    svc._cache.clear()
    svc.find(serial="GPON00F58B52")
    assert len(wisp.calls_serial) == 2