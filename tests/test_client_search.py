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
    """Devuelve filas según el measurement y filtros de tag en el flux."""

    def __init__(self, rows_by_measurement=None):
        self.bucket = "monitoreo"
        self.rows = dict(rows_by_measurement or {})

    def query(self, flux):
        import re as _re
        m = _re.search(r'\[\"_measurement\"\] == \"(\w+)\"', flux)
        if not m:
            return []
        rows = self.rows.get(m.group(1), [])
        out = []
        for r in rows:
            if self._tag_filter_match(flux, r):
                if self._field_filter_match(flux, r):
                    if self._contains_match(flux, r):
                        out.append(r)
        return out

    @staticmethod
    def _tag_filter_match(flux, row):
        import re as _re
        for m in _re.finditer(r'\[\"(onu_serial|mac|serial_onu|nen)\"\] == \"([^\"]+)\"', flux):
            tag, value = m.group(1), m.group(2).lower()
            if str(row.get(tag, "")).lower() != value and str(row.get("value", "")).lower() != value:
                return False
        return True

    @staticmethod
    def _field_filter_match(flux, row):
        import re as _re
        m = _re.search(r'\[\"_field\"\] == \"([^\"]+)\"', flux)
        if not m:
            return True
        return str(row.get("field", "")) == m.group(1)

    @staticmethod
    def _contains_match(flux, row):
        import re as _re
        m = _re.search(r'subv: \"([^\"]+)\"', flux)
        if not m:
            return True
        return m.group(1).lower() in str(row.get("value", "")).lower()


def _opt_row(field, value, **tags):
    row = {"measurement": "optical_power", "field": field, "value": value,
           "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
           "olt_name": "OLT-SISAL-2", "olt_ip": "10.0.0.2",
           "pon_port": "GPON0/2", "onu_index": "1/0/1", "onu_model": "HG8245",
           "nodo": "Nodo Centro", "onu_serial": "GPON00F58B52"}
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


def test_find_por_nombre_resuelve_serial_por_base():
    svc = _mk_service(
        serials={"VSOL005ECDCA": {**WISP_SERIAL, "serial_onu": "VSOL005ECDCA"}},
        clientes_enriquecidos=[{
            "measurement": "clientes_enriquecidos", "field": "nombre",
            "value": "FREDDY MANUEL SIFONTES ROSALES",
            "serial_onu": "VSOL005ECDCA", "mac": "a1:b2:c3:d4:e5:f6",
            "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
        }],
        optical_power=[_opt_row("status", 3, onu_serial="VSOL005ECDCA")],
    )
    res = svc.find(nombre="sifontes")
    assert res["found"] is True
    assert res["ficha"]["serial_onu"] == "VSOL005ECDCA"
    assert svc.wisp.calls_serial == ["VSOL005ECDCA"]


def test_find_por_nombre_sin_coincidencia_found_false():
    svc = _mk_service(
        clientes_enriquecidos=[],
        optical_power=[],
    )
    res = svc.find(nombre="inexistente")
    assert res["found"] is False


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


# ---------------------------------------------------------------------------
# discover_serials + group_analysis
# ---------------------------------------------------------------------------

def test_discover_serials_por_olt_pon_distinct():
    svc = _mk_service(optical_power=[
        {"measurement": "optical_power", "field": "onu_serial",
         "value": "GPON00A1", "olt_name": "OLT-SISAL-2",
         "pon_port": "GPON0/2", "onu_serial": "GPON00A1"},
        {"measurement": "optical_power", "field": "onu_serial",
         "value": "GPON00A2", "olt_name": "OLT-SISAL-2",
         "pon_port": "GPON0/2", "onu_serial": "GPON00A2"},
        {"measurement": "optical_power", "field": "onu_serial",
         "value": "GPON00A1", "olt_name": "OLT-SISAL-2",
         "pon_port": "GPON0/2", "onu_serial": "GPON00A1"},  # duplicado
    ])
    serials = svc.discover_serials(olt_name="OLT-SISAL-2",
                                   pon_port="GPON0/2")
    assert serials == ["GPON00A1", "GPON00A2"]


def test_discover_serials_por_nap_usa_base_enriquecida():
    svc = _mk_service(clientes_enriquecidos=[
        {"measurement": "clientes_enriquecidos", "field": "serial_onu",
         "value": "GPON00B1", "nap": "I01313", "serial_onu": "GPON00B1"},
        {"measurement": "clientes_enriquecidos", "field": "serial_onu",
         "value": "GPON00B2", "nap": "I01313", "serial_onu": "GPON00B2"},
    ])
    serials = svc.discover_serials(nap="I01313")
    assert "GPON00B1" in serials and "GPON00B2" in serials


def test_group_analysis_por_serials_agrega_estados():
    svc = _mk_service(
        serials={
            "GPON00A1": {**WISP_SERIAL, "serial_onu": "GPON00A1",
                         "direccion": "Calle 1", "nombre": "Cliente Uno"},
            "GPON00A2": {**WISP_SERIAL, "serial_onu": "GPON00A2",
                         "direccion": "Calle 2", "nombre": "Cliente Dos"},
        },
        optical_power=[
            _opt_row("status", 3, onu_serial="GPON00A1"),
            _opt_row("rx_power", -18.0, onu_serial="GPON00A1"),
            _opt_row("status", 1, onu_serial="GPON00A2"),
            _opt_row("rx_power", -30.0, onu_serial="GPON00A2"),
        ],
    )
    res = svc.group_analysis(serials=["GPON00A1", "GPON00A2"])
    assert res["total"] == 2
    assert res["offline"] == 1
    assert res["loss"] == 1
    assert res["low_power"] == 1
    af = res["afectados"][0]
    assert af["nombre"] == "Cliente Dos"
    assert af["direccion"] == "Calle 2"
    assert af["rx_power"] == -30.0


def test_group_analysis_descubre_por_olt_pon():
    svc = _mk_service(
        serials={"GPON00A1": {**WISP_SERIAL, "serial_onu": "GPON00A1"}},
        optical_power=[
            _opt_row("status", 1, onu_serial="GPON00A1"),
            _opt_row("rx_power", -31.0, onu_serial="GPON00A1"),
        ],
    )
    res = svc.group_analysis(olt_name="OLT-SISAL-2", pon_port="GPON0/2")
    assert res["total"] == 1
    assert res["loss"] == 1
    assert "OLT OLT-SISAL-2 · PON GPON0/2" in res["scope"]


def test_group_analysis_sin_entradas():
    svc = _mk_service()
    res = svc.group_analysis(serials=[])
    assert res["total"] == 0
    assert res["afectados"] == []


def test_group_analysis_trunca_a_max_group():
    serials = {f"GPON00A{i:03d}": {**WISP_SERIAL,
                                   "serial_onu": f"GPON00A{i:03d}"}
               for i in range(_MAX_GROUP + 5)}
    svc = _mk_service(serials=serials)
    res = svc.group_analysis(serials=list(serials.keys()))
    assert res["truncado"] is True
    assert res["total"] == _MAX_GROUP