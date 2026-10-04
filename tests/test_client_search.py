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
        self.queries = []

    @staticmethod
    def _tag_filter_match(flux, row):
        import re as _re
        for m in _re.finditer(r'\[\"(onu_serial|mac|serial_onu|nen)\"\] == \"([^\"]+)\"', flux):
            tag, value = m.group(1), m.group(2).lower()
            if str(row.get(tag, "")).lower() != value and str(row.get("value", "")).lower() != value:
                return False
        for m in _re.finditer(
            r'\["(onu_serial|mac|serial_onu|nen)"\] =~ /\(\?i\)\^([\w\-:]+)', flux):
            tag, value = m.group(1), m.group(2).lower()
            if str(row.get(tag, "")).lower() != value and str(row.get("value", "")).lower() != value:
                return False
        for m in _re.finditer(
            r'strings\.toLower\(v: r\["(onu_serial|mac|serial_onu|nen)"\]\) '
            r'== strings\.toLower\(v: "([^"]+)"\)', flux):
            tag, value = m.group(1), m.group(2).lower()
            if str(row.get(tag, "")).lower() != value and str(row.get("value", "")).lower() != value:
                return False
        return True

    @staticmethod
    def _field_filter_match(flux, row):
        import re as _re
        m = _re.search(r'\["_field"\] == "([^"]+)"', flux)
        if m:
            return str(row.get("field", "")) == m.group(1)
        m = _re.search(r'\["_field"\] =~ /\^\((.*?)\)\$/', flux)
        if m:
            campos = {f.strip() for f in m.group(1).split("|")}
            return str(row.get("field", "")) in campos
        return True

    @staticmethod
    def _contains_match(flux, row):
        import re as _re
        m = _re.search(r'subv: \"([^\"]+)\"', flux)
        if not m:
            return True
        return m.group(1).lower() in str(row.get("value", "")).lower()

    @classmethod
    def _apply_collapse(cls, flux, rows):
        """Modela el colapso real de group(columns:["_field"]) |> last().

        Con un filtro _field previo todas las filas caen en una sola tabla
        y last() devuelve UNA fila (el bug de _serial_by_nombre).
        """
        if (
            'group(columns: ["_field"])' in flux
            and "|> last()" in flux
            and '_field"] ==' in flux
            and "distinct" not in flux
        ):
            return rows[-1:]
        if (
            'group(columns: ["serial_onu", "mac", "_field"])' in flux
            and "|> last()" in flux
        ):
            # Una fila por (serial, mac, campo): la última de cada grupo.
            último = {}
            for r in rows:
                clave = (r.get("serial_onu"), r.get("mac"), r.get("field"))
                último[clave] = r
            return list(último.values())
        return rows

    def query(self, flux):
        import re as _re
        self.queries.append(flux)
        m = _re.search(r'\["_measurement"\] == "(\w+)"', flux)
        if not m:
            return []
        rows = self.rows.get(m.group(1), [])
        out = []
        for r in rows:
            if self._tag_filter_match(flux, r):
                if self._field_filter_match(flux, r):
                    if self._contains_match(flux, r):
                        out.append(r)
        return self._apply_collapse(flux, out)


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
# _serial_by_nombre: distinct() por serial + ventana corta (sin colapso)
# ---------------------------------------------------------------------------

def _enr_row(value, serial):
    return {
        "measurement": "clientes_enriquecidos", "field": "nombre",
        "value": value, "serial_onu": serial, "mac": "a1:b2:c3:d4:e5:f6",
        "time": datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
    }


def test_serial_by_nombre_sin_colapso_y_ventana_24h():
    svc = _mk_service(clientes_enriquecidos=[
        _enr_row("ANA MARIA PEREZ", "AAA0001"),
        _enr_row("ANA MARIA GOMEZ LOPEZ", "ZZZ0009"),
    ])
    captured = []
    orig = svc.influx.query

    def spy(flux):
        captured.append(flux)
        return orig(flux)

    svc.influx.query = spy
    serial = svc._serial_by_nombre("ana maria")
    assert serial == "AAA0001"
    assert captured, "debería consultar Influx"
    assert any("range(start: -24h)" in f for f in captured)
    assert all('group(columns: ["_field"])' not in f for f in captured)
    assert all("distinct" in f for f in captured)


def test_find_por_nombre_elige_serial_estable():
    svc = _mk_service(
        serials={
            "AAA0001": {**WISP_SERIAL, "serial_onu": "AAA0001"},
            "ZZZ0009": {**WISP_SERIAL, "serial_onu": "ZZZ0009"},
        },
        clientes_enriquecidos=[
            _enr_row("ANA MARIA PEREZ", "AAA0001"),
            _enr_row("ANA MARIA GOMEZ LOPEZ", "ZZZ0009"),
        ],
        optical_power=[
            _opt_row("status", 3, onu_serial="AAA0001"),
            _opt_row("status", 3, onu_serial="ZZZ0009"),
        ],
    )
    res = svc.find(nombre="ana maria")
    assert res["found"] is True
    assert res["ficha"]["serial_onu"] == "AAA0001"
    assert svc.wisp.calls_serial == ["AAA0001"]


# ---------------------------------------------------------------------------
# discover_serials + group_analysis
# ---------------------------------------------------------------------------

def test_discover_serials_por_olt_pon_distinct():
    svc = _mk_service(optical_power=[
        {"measurement": "optical_power", "field": "status", "value": 3,
         "olt_name": "OLT-SISAL-2", "pon_port": "GPON0/2",
         "onu_serial": "GPON00A1"},
        {"measurement": "optical_power", "field": "status", "value": 3,
         "olt_name": "OLT-SISAL-2", "pon_port": "GPON0/2",
         "onu_serial": "GPON00A2"},
        {"measurement": "optical_power", "field": "status", "value": 3,
         "olt_name": "OLT-SISAL-2", "pon_port": "GPON0/2",
         "onu_serial": "GPON00A1"},  # duplicado
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


def test_discover_por_nap_devuelve_seriales_legibles():
    """`group()` antes de `distinct` deja la consulta vacía.

    Influx se queda sólo con la clave de grupo, sin columnas de datos, y el
    cliente no recibe ningún registro. La forma que sí funciona es
    `keep`+`distinct` sin `group()` previo.
    """
    svc = _mk_service()
    svc.discover_serials(nap="I01313")
    flux = [q for q in svc.influx.queries if "I01313" in q][0]
    assert 'keep(columns: ["serial_onu"])' in flux
    assert 'distinct(column: "serial_onu")' in flux
    assert "|> group()" not in flux


def test_busqueda_por_nombre_evita_group_antes_de_distinct():
    svc = _mk_service()
    svc.find_many(["ANA MARIA PEREZ"])
    flux = [q for q in svc.influx.queries if "strings" in q][0]
    assert 'keep(columns: ["serial_onu"])' in flux
    assert "|> group()" not in flux


def _enq_row(serial, field, value, **tags):
    """Fila de `clientes_enriquecidos` (tags compartidos + un campo)."""
    row = {"measurement": "clientes_enriquecidos", "field": field,
           "value": value, "serial_onu": serial,
           "time": datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)}
    row.update(tags)
    return row


def test_group_analysis_por_serials_agrega_estados():
    # El análisis de grupo se apoya en la base enriquecida: `find()` por
    # cliente significaba una llamada WISP por cada uno del grupo.
    svc = _mk_service(
        clientes_enriquecidos=[
            _enq_row("GPON00A1", "nombre", "Cliente Uno",
                     direccion="Calle 1", nap="I01313"),
            _enq_row("GPON00A1", "direccion", "Calle 1", nap="I01313"),
            _enq_row("GPON00A2", "nombre", "Cliente Dos",
                     direccion="Calle 2", nap="I01313"),
            _enq_row("GPON00A2", "direccion", "Calle 2", nap="I01313"),
        ],
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


def test_group_analysis_no_llama_a_wisp_por_cliente():
    """El coste del grupo no debe crecer con el número de clientes."""
    enriched = []
    optical = []
    for i in range(5):
        sn = f"GPON00C{i}"
        enriched.append(_enq_row(sn, "nombre", f"Cliente {i}", nap="I01313"))
        optical.append(_opt_row("status", 3, onu_serial=sn))
    svc = _mk_service(clientes_enriquecidos=enriched, optical_power=optical)
    res = svc.group_analysis(serials=[f"GPON00C{i}" for i in range(5)])
    assert res["total"] == 5
    assert svc.wisp.calls_serial == []


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
    enriched = [_enq_row(f"GPON00A{i:03d}", "nombre", f"Cliente {i}",
                         nap="I01313")
                for i in range(_MAX_GROUP + 5)]
    svc = _mk_service(clientes_enriquecidos=enriched)
    res = svc.group_analysis(serials=[f"GPON00A{i:03d}"
                                      for i in range(_MAX_GROUP + 5)])
    assert res["truncado"] is True
    assert res["total"] == _MAX_GROUP