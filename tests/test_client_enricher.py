# tests/test_client_enricher.py
import json
import time

from src.client_enricher import (
    enrich_clients,
    _norm_mac,
    _merge_olt_rows,
    _build_olt_only_rows,
    ClientEnricher,
    EnrichStats,
)
from src.config_ia import EnrichmentConfig, WISPConfig


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


# ---------------------------------------------------------------------------
# Fuente optical_power (medición real de ONUs) como respaldo del catálogo OLT
# ---------------------------------------------------------------------------

def _optica(serial, status=3, olt="OLT-SISAL-2", nodo="SISAL",
            pon="GPON0/9", idx="9_67"):
    return {
        "onu_serial": serial,
        "olt_name": olt,
        "olt_ip": "172.16.74.4",
        "nodo": nodo,
        "pon_port": pon,
        "onu_index": idx,
        "onu_model": "ADC-2001",
        "status": status,
    }


def test_merge_prefiere_onu_location_y_rellena_faltantes():
    location = [_olt("aa:bb:cc:dd:ee:01", "SER-1", estado=3)]
    optic = [_optica("SER-1", status=4, olt="OLT-OTRA")]
    merged = _merge_olt_rows(location, optic)
    assert len(merged) == 1
    m = merged[0]
    assert m["mac"] == "aa:bb:cc:dd:ee:01"
    assert m["estado_onu"] == "3"
    assert m["olt"] == "OLT-1"


def test_merge_agrega_optical_si_no_hay_location():
    merged = _merge_olt_rows([], [_optica("SER-2", status=4)])
    assert len(merged) == 1
    m = merged[0]
    assert m["onu_serial"].upper() == "SER-2"
    assert m["estado_onu"] == "4"
    assert m["pon_port"] == "GPON0/9"


def test_merge_rellena_estado_desde_optical_si_location_no_lo_tiene():
    location = [_olt("aa:bb:cc:dd:ee:03", "SER-3", estado=-1)]
    merged = _merge_olt_rows(location, [_optica("SER-3", status=4)])
    assert merged[0]["estado_onu"] == "4"


def test_merge_no_pierde_filas_location_sin_serial():
    location = [_olt("aa:bb:cc:dd:ee:04", "", estado=3),
                _olt("aa:bb:cc:dd:ee:05", "SER-5", estado=3)]
    merged = _merge_olt_rows(location, [_optica("SER-5", status=4)])
    assert len(merged) == 2
    sin_serial = [m for m in merged if not m.get("onu_serial")]
    assert len(sin_serial) == 1 and sin_serial[0]["mac"] == "aa:bb:cc:dd:ee:04"


def test_enrich_clients_matchea_serial_desde_optical():
    wisp = [_wisp("DOC8", serial="SER-OP1", usuario="op1", nombre="Cliente Op")]
    olt = [_optica("SER-OP1", status=3)]
    matched, pending = enrich_clients(wisp, olt, [], [])
    assert len(matched) == 1 and pending == []
    m = matched[0]
    assert m["coincidencia"] == "serial"
    assert m["olt"] == "OLT-SISAL-2"
    assert m["pon_port"] == "GPON0/9"
    assert m["estado_onu"] == "3"


def test_serial_rellenado_desde_la_onu_cuando_falta_en_wisp():
    wisp = [_wisp("DOC9", usuario="op2", mac="aa:bb:cc:dd:ee:09")]
    olt = [{"mac": "aa:bb:cc:dd:ee:09", "onu_serial": "SER-9",
            "olt_name": "OLT-1", "nodo": "N1", "pon_port": "2",
            "onu_index": "2_03", "estado_onu": 1}]
    matched, _ = enrich_clients(wisp, olt, [], [])
    assert len(matched) == 1
    assert matched[0]["serial_onu"] == "SER-9"
    assert matched[0]["coincidencia"] == "mac"


def test_build_olt_only_excluye_serials_ya_matriculados():
    rows = [_optica("SER-A", status=3), _optica("SER-B", status=4)]
    out = _build_olt_only_rows(rows, exclude_serials={"SER-A"})
    assert [r["serial_onu"] for r in out] == ["SER-B"]
    assert out[0]["coincidencia"] == "olt_only"
    assert out[0]["estado_onu"] == "4"


def test_build_olt_only_ignora_filas_sin_serial():
    rows = [{"mac": "aa:bb:cc:dd:ee:0a", "olt_name": "OLT-1"}, _optica("SER-B")]
    out = _build_olt_only_rows(rows)
    assert [r["serial_onu"] for r in out] == ["SER-B"]


# ---------------------------------------------------------------------------
# Ciclo completo sin WISP: persiste las ONUs de optical_power en la base
# ---------------------------------------------------------------------------

class _FakeWisp:
    def __init__(self, clients):
        self._clients = clients
        self.config = _WispCfg()

    def list_clients(self):
        return self._clients


class _FakeInflux:
    def __init__(self, results_by_measurement):
        self._query_api = True
        self.bucket = "monitoreo"
        self.results = results_by_measurement
        self.written = []

    def query(self, flux):
        for name, rows in self.results.items():
            if f'== "{name}"' in flux:
                return rows
        return []

    def write_points(self, points):
        self.written.extend(points)
        return len(points)


def _punto_optical(serial, status=3):
    return [
        {"measurement": "optical_power", "field": "status", "value": status,
         "time": 1.0, "onu_serial": serial, "olt_name": "OLT-SISAL-2",
         "olt_ip": "172.16.74.4", "nodo": "SISAL", "pon_port": "GPON0/9",
         "onu_index": "9_67"},
        {"measurement": "optical_power", "field": "rx_power",
         "value": -19.75, "time": 1.0, "onu_serial": serial,
         "olt_name": "OLT-SISAL-2", "olt_ip": "172.16.74.4", "nodo": "SISAL",
         "pon_port": "GPON0/9", "onu_index": "9_67"},
    ]


def test_run_sin_wisp_persiste_onus_de_optical_power():
    influx = _FakeInflux({"optical_power": _punto_optical("OEMT3C52D7C2", 3)})
    enr = ClientEnricher(influx, _FakeWisp([]),
                         EnrichmentConfig(enabled=True, lookback_hours=168))
    stats = enr.run()
    assert stats.matched == 1
    base = [p for p in influx.written
            if p.measurement == "clientes_enriquecidos"]
    assert len(base) == 1
    assert base[0].tags.get("serial_onu") == "OEMT3C52D7C2"
    assert base[0].tags.get("olt") == "OLT-SISAL-2"
    assert base[0].tags.get("nodo") == "SISAL"
    assert base[0].fields.get("pon_port") == "GPON0/9"
    assert base[0].fields.get("estado_onu") == 3


# ---------------------------------------------------------------------------
# Lookup delta-driven WISP por serial (serial_endpoint en vivo por ciclo)
# ---------------------------------------------------------------------------

class _FakeWispSerial(_FakeWisp):
    """WispClient de prueba: responde por serial y registra las llamadas."""

    def __init__(self, by_serial=None):
        super().__init__([])
        self.by_serial = by_serial or {}
        self.serial_calls = []
        self.config = _WispCfg(serial_endpoint="/servicio?onu={serial}",
                               cache_hours=24, unknown_retry_hours=168,
                               max_lookups_per_run=400)

    def fetch_by_serial(self, serial, force=False):
        self.serial_calls.append(serial)
        found = self.by_serial.get(serial)
        if not found:
            return None
        return {**found, "serial_onu": serial}


class _WispCfg:
    def __init__(self, **kw):
        self.serial_endpoint = kw.get("serial_endpoint", "")
        self.cache_hours = kw.get("cache_hours", 24)
        self.unknown_retry_hours = kw.get("unknown_retry_hours", 168)
        self.max_lookups_per_run = kw.get("max_lookups_per_run", 400)


def _olt_serial(serial):
    return {"onu_serial": serial, "olt_name": "OLT-PRADO-1",
            "olt": "OLT-PRADO-1", "nodo": "PRADO", "pon_port": "GPON0/3",
            "onu_index": "3_04", "estado_onu": 3}


def test_lookup_serial_encuentra_cliente_nuevo(tmp_path):
    serial = "CMDCB00A3095"
    wisp = _FakeWispSerial({serial: {
        "documento": "12345678", "usuario_pppoe": "v12345678",
        "nombre": "Juan Perez", "direccion": "Av. 5",
        "telefono": "0412-0001", "email": "a@b.c",
        "plan": "Fibra 200M", "tecnologia": "PON",
        "velocidad_down_mbps": "200", "velocidad_up_mbps": "100",
        "precio_usd": "25.50", "nap_nombre": "NAP-15", "puerto": "15",
        "puerto_pon": "GPON0/3", "coordenadas": "10.48,-66.90",
        "tipo_documento": "V", "codigo_cliente": "C-1", "estado": "activo",
    }})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    clients = []
    olt_rows = [_olt_serial(serial)]
    clients, stats = enr._lookup_olt_serials(clients, olt_rows, EnrichStats())
    assert stats.serial_lookups == 1
    assert stats.serial_found == 1
    assert len(clients) == 1
    assert clients[0]["documento"] == "12345678"
    assert clients[0]["serial_onu"] == serial
    assert wisp.serial_calls == [serial]
    # Se guardó como encontrado; no se re-consulta en el siguiente ciclo.
    clients2, stats2 = enr._lookup_olt_serials([], olt_rows, EnrichStats())
    assert stats2.serial_lookups == 0
    assert len(clients2) == 0


def test_lookup_serial_negativo_se_recuerda_y_reejecuta_tarde(tmp_path):
    serial = "VSOL00CE4B01"
    wisp = _FakeWispSerial({})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    olt_rows = [_olt_serial(serial)]
    _, stats = enr._lookup_olt_serials([], olt_rows, EnrichStats())
    assert stats.serial_lookups == 1
    assert stats.serial_found == 0
    assert wisp.serial_calls == [serial]
    # Primera vez tras el miss: no se reconsulta de inmediato.
    clients2, stats2 = enr._lookup_olt_serials([], olt_rows, EnrichStats())
    assert stats2.serial_lookups == 0
    # Al envejecer el registro (unknown_retry_hours pasados) se reintenta.
    state = json.loads((tmp_path / "state.json").read_text())
    state["wisp_serial_lookups"][serial]["ts"] -= 200 * 3600
    (tmp_path / "state.json").write_text(json.dumps(state))
    _, stats3 = enr._lookup_olt_serials([], olt_rows, EnrichStats())
    assert stats3.serial_lookups == 1
    assert wisp.serial_calls == [serial, serial]


def test_lookup_serial_no_duplica_found_al_reejecutar_cliente_matriculado(tmp_path):
    serial = "CMDCB00A3095"
    wisp = _FakeWispSerial({serial: {"documento": "1", "nombre": "X"}})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    # Ya matcheado en el catálogo: no se hace lookup aunque esté en la OLT.
    clients = [{"documento": "1", "serial_onu": serial, "usuario_pppoe": "x"}]
    _, stats = enr._lookup_olt_serials(clients, [_olt_serial(serial)],
                                       EnrichStats())
    assert stats.serial_lookups == 0
    assert wisp.serial_calls == []


def test_lookup_desconocido_vuelve_cuando_wisp_mete_serial(tmp_path):
    serial = "VSOL009F2188"
    by_serial = {}

    class FakeWispDynamic(_FakeWispSerial):
        def fetch_by_serial(self, serial, force=False):
            self.serial_calls.append(serial)
            c = by_serial.get(serial)
            return {**c, "serial_onu": serial} if c else None

    wisp = FakeWispDynamic(by_serial)
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    olt_rows = [_olt_serial(serial)]
    enr._lookup_olt_serials([], olt_rows, EnrichStats())
    assert wisp.serial_calls == [serial]
    state = json.loads((tmp_path / "state.json").read_text())
    state["wisp_serial_lookups"][serial]["ts"] -= 200 * 3600
    state["wisp_serial_lookups"][serial]["found"] = True
    (tmp_path / "state.json").write_text(json.dumps(state))
    # found=True + TTL expirado -> se vuelve a consultar y ahora encuentra.
    clients, stats = enr._lookup_olt_serials([], olt_rows, EnrichStats())
    assert stats.serial_lookups == 1
    assert stats.serial_found == 0  # by_serial todavía vacío -> None
    assert wisp.serial_calls == [serial, serial]


# ---------------------------------------------------------------------------
# _load_rows: consultas acotadas por campo (evita full-scan multi-serie)
# ---------------------------------------------------------------------------

class _RecordingInflux(_FakeInflux):
    def __init__(self, results=None):
        super().__init__(results or {})
        self.fluxes = []

    def query(self, flux):
        self.fluxes.append(flux)
        return super().query(flux)


def test_load_rows_por_campos_emite_un_last_por_campo():
    enr = ClientEnricher(_RecordingInflux({}), None,
                         EnrichmentConfig(enabled=True, lookback_hours=6))
    rows = enr._load_rows("routeros_pppoe", "pppoe",
                          fields=["mac_address", "status"])
    assert rows == []
    fluxes = enr.influx.fluxes
    assert len(fluxes) == 2
    assert '_field"] == "mac_address"' in fluxes[0]
    assert '_field"] == "status"' in fluxes[1]
    for f in fluxes:
        assert "|> last()" in f
        assert "range(start: -6h)" in f
        assert '== "routeros_pppoe"' in f


def test_load_rows_sin_fields_conserva_full_scan():
    enr = ClientEnricher(_RecordingInflux({}), None,
                         EnrichmentConfig(enabled=True, lookback_hours=6))
    enr._load_rows("onu_location", "onu")
    assert len(enr.influx.fluxes) == 1
    flux = enr.influx.fluxes[0]
    assert "range(start: -6h)" in flux
    assert "|> last()" not in flux
    assert "_field" not in flux


def test_run_pppoe_y_dhcp_consultan_solo_por_campos():
    influx = _RecordingInflux({})
    enr = ClientEnricher(influx, None,
                         EnrichmentConfig(enabled=True, lookback_hours=6))
    enr.run()
    pppoe = [f for f in influx.fluxes if '== "routeros_pppoe"' in f]
    dhcp = [f for f in influx.fluxes if '== "routeros_dhcp"' in f]
    assert len(pppoe) == 3  # mac_address, status, clave_pppoe
    assert len(dhcp) == 1
    for f in pppoe + dhcp:
        assert "|> last()" in f
        assert '_field"] ==' in f
    # La consulta de clave_pppoe debe existir para exponer la password
    assert any('_field"] == "clave_pppoe"' in f for f in pppoe)


def test_run_emite_complemento_olt_only_con_wisp_parcial():
    influx = _FakeInflux({"optical_power": _punto_optical("OEMT1AAAAAAA", 3)})
    clients = [_wisp("DOC-WISP", serial="OEMT1AAAAAAA", usuario="w1")]
    influx2 = _FakeInflux({"optical_power": _punto_optical("OEMT1AAAAAAA", 3),
                           "onu_location": _punto_optical("OEMT2BBBBBBB", 4)})
    enr = ClientEnricher(influx2, _FakeWisp(clients),
                         EnrichmentConfig(enabled=True, lookback_hours=168))
    stats = enr.run()
    assert stats.wisp_total == 1
    assert stats.matched == 2  # 1 WISP-match + 1 OLT-only del otro serial
    base = [p.tags.get("serial_onu") for p in influx2.written
            if p.measurement == "clientes_enriquecidos"]
    assert set(base) == {"OEMT1AAAAAAA", "OEMT2BBBBBBB"}

# ---------------------------------------------------------------------------
# Cadena principal: OLT -> MAC -> PPPoE -> DNI -> WISP por documento
# ---------------------------------------------------------------------------

class _WispCfgDoc(_WispCfg):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.document_endpoint = kw.get(
            "document_endpoint", "/cliente?documento={documento}")
        self.error_retry_hours = kw.get("error_retry_hours", 1)


class _FakeWispDoc(_FakeWisp):
    """WispClient de prueba: responde por documento y registra llamadas."""

    def __init__(self, by_doc=None, ok=True):
        super().__init__([])
        self.by_doc = by_doc or {}
        self.doc_calls = []
        self.ok = ok
        self.config = _WispCfgDoc(
            document_endpoint="/cliente?documento={documento}",
            serial_endpoint="/servicio?onu={serial}",
            cache_hours=24, unknown_retry_hours=168, error_retry_hours=1,
            max_lookups_per_run=400)

    def fetch_by_document_ex(self, dni, force=False):
        self.doc_calls.append(dni)
        return self.by_doc.get(dni), self.ok

    def fetch_by_serial_ex(self, serial, force=False):
        return None, True


def _olt_mac(serial, mac):
    return {"onu_serial": serial, "mac": mac, "olt_name": "OLT-NAR-2",
            "olt": "OLT-NAR-2", "nodo": "NARANJILLOS", "pon_port": "GPON0/2",
            "onu_index": "2_15", "estado_onu": 3}


def test_dni_from_pppoe_formatos():
    from src.client_enricher import _dni_from_pppoe
    assert _dni_from_pppoe("VN12345678-1") == "12345678"
    assert _dni_from_pppoe("a9876543b") == "9876543"
    assert _dni_from_pppoe("aronp") == ""
    assert _dni_from_pppoe("") == ""


def test_doc_chain_encuentra_por_mac_pppoe_y_fuerza_serial(tmp_path):
    mac = "aa:bb:cc:dd:ee:10"
    serial = "SER-DOC-1"
    wisp = _FakeWispDoc({"12345678": {
        "documento": "12345678", "serial_onu": "OTRO-SERIAL",
        "usuario_pppoe": "vn12345678", "nombre": "Maria Lopez",
        "nap_nombre": "NAP-9", "zona": "NARANJILLOS"}})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    olt_rows = [_olt_mac(serial, mac)]
    pppoe_rows = [_pppoe("VN12345678-1", "AA:BB:CC:DD:EE:10")]
    stats = EnrichStats()
    clients, stats = enr._lookup_by_document([], olt_rows, pppoe_rows, stats)
    assert wisp.doc_calls == ["12345678"]
    assert stats.doc_lookups == 1 and stats.doc_found == 1
    assert len(clients) == 1
    assert clients[0]["serial_onu"] == serial  # la OLT manda
    assert clients[0]["nombre"] == "Maria Lopez"
    # Segundo ciclo: estado found fresco -> sin llamadas.
    clients2, stats2 = enr._lookup_by_document(
        [], olt_rows, pppoe_rows, EnrichStats())
    assert wisp.doc_calls == ["12345678"]
    assert stats2.doc_lookups == 0


def test_doc_chain_no_encontrado_se_recuerda(tmp_path):
    serial = "SER-DOC-2"
    wisp = _FakeWispDoc({})  # 200 sin cliente -> definitivo
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    olt_rows = [_olt_mac(serial, "aa:bb:cc:dd:ee:20")]
    pppoe_rows = [_pppoe("VN22223333-2", "AA:BB:CC:DD:EE:20")]
    _, stats = enr._lookup_by_document([], olt_rows, pppoe_rows, EnrichStats())
    assert stats.doc_lookups == 1 and stats.doc_found == 0
    _, stats2 = enr._lookup_by_document([], olt_rows, pppoe_rows, EnrichStats())
    assert stats2.doc_lookups == 0  # notfound vigente (unknown_retry_hours)
    # Envejecido -> vuelve a intentar.
    state = json.loads((tmp_path / "state.json").read_text())
    state["wisp_doc_lookups"][serial]["ts"] -= 200 * 3600
    (tmp_path / "state.json").write_text(json.dumps(state))
    _, stats3 = enr._lookup_by_document([], olt_rows, pppoe_rows, EnrichStats())
    assert stats3.doc_lookups == 1


def test_doc_chain_error_transitorio_reintenta_pronto(tmp_path):
    serial = "SER-DOC-3"
    wisp = _FakeWispDoc({"33334444": {"documento": "33334444"}},
                        ok=False)  # 5xx/red -> err, no "no encontrado"
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    olt_rows = [_olt_mac(serial, "aa:bb:cc:dd:ee:30")]
    pppoe_rows = [_pppoe("VN33334444-3", "AA:BB:CC:DD:EE:30")]
    _, stats = enr._lookup_by_document([], olt_rows, pppoe_rows, EnrichStats())
    assert stats.doc_lookups == 1 and stats.doc_found == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["wisp_doc_lookups"][serial].get("err") is True
    # Dentro de error_retry_hours (1h): no re-consulta.
    _, stats2 = enr._lookup_by_document([], olt_rows, pppoe_rows, EnrichStats())
    assert stats2.doc_lookups == 0
    # Pasada 1h: reintenta.
    state = json.loads((tmp_path / "state.json").read_text())
    state["wisp_doc_lookups"][serial]["ts"] -= 2 * 3600
    (tmp_path / "state.json").write_text(json.dumps(state))
    wisp.ok = True
    _, stats3 = enr._lookup_by_document([], olt_rows, pppoe_rows, EnrichStats())
    assert stats3.doc_lookups == 1
    assert stats3.doc_found == 1


def test_doc_chain_siembra_catalogo_fresco_sin_llamada(tmp_path):
    serial = "SER-DOC-4"
    wisp = _FakeWispDoc({"44445555": {"documento": "44445555"}})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    clients = [{"documento": "44445555", "serial_onu": serial,
                "usuario_pppoe": "vn44445555", "nombre": "Test",
                "_ts": time.time() - 3600, "_from_catalog": True}]
    olt_rows = [_olt_mac(serial, "aa:bb:cc:dd:ee:40")]
    clients2, stats = enr._lookup_by_document(clients, olt_rows, [],
                                              EnrichStats())
    assert wisp.doc_calls == []
    assert stats.doc_lookups == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["wisp_doc_lookups"][serial]["via"] == "catalogo"


def test_doc_chain_refresca_catalogo_vencido(tmp_path):
    serial = "SER-DOC-5"
    wisp = _FakeWispDoc({"55556666": {
        "documento": "55556666", "serial_onu": serial,
        "usuario_pppoe": "vn55556666", "nombre": "Refrescado"}})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    enr = ClientEnricher(_FakeInflux({}), wisp, cfg)
    clients = [{"documento": "55556666", "serial_onu": serial,
                "usuario_pppoe": "vn55556666", "nombre": "Viejo",
                "_ts": time.time() - 48 * 3600, "_from_catalog": True}]
    olt_rows = [_olt_mac(serial, "aa:bb:cc:dd:ee:50")]
    clients2, stats = enr._lookup_by_document(clients, olt_rows, [],
                                              EnrichStats())
    assert wisp.doc_calls == ["55556666"]
    assert stats.doc_found == 1
    assert clients2[0]["nombre"] == "Refrescado"
    assert clients2[0]["serial_onu"] == serial


def test_doc_chain_usa_usuario_historico_cuando_falta_rbnor(tmp_path):
    serial = "SER-DOC-6"
    wisp = _FakeWispDoc({"66667777": {"documento": "66667777"}})
    cfg = EnrichmentConfig(enabled=True, lookback_hours=168,
                           state_file=str(tmp_path / "state.json"))
    hist = [{"measurement": "clientes_enriquecidos", "field": "nombre",
             "value": "Hist", "time": 1.0, "serial_onu": serial,
             "usuario_pppoe": "VN66667777-6", "documento": "66667777"}]
    enr = ClientEnricher(_FakeInflux({"clientes_enriquecidos": hist}),
                         wisp, cfg)
    olt_rows = [_olt_mac(serial, "aa:bb:cc:dd:ee:60")]  # sin MAC en RB
    clients, stats = enr._lookup_by_document([], olt_rows, [],
                                             EnrichStats())
    assert wisp.doc_calls == ["66667777"]
    assert stats.doc_found == 1
    assert clients[0]["usuario_pppoe"] == "vn66667777-6"


# ---------------------------------------------------------------------------
# Re-sembrado del catálogo desde wisp_clients + fusión con fuente viva
# ---------------------------------------------------------------------------

def test_merge_catalog_live_fuente_viva_gana_y_quita_flag():
    from src.client_enricher import _merge_catalog_live
    catalog = {"s:SER-A": {"documento": "1", "serial_onu": "SER-A",
                           "nombre": "Catalogo", "nap_nombre": "NAP-1",
                           "_ts": 100.0, "_from_catalog": True}}
    live = [{"documento": "1", "serial_onu": "SER-A", "nombre": "Vivo",
             "direccion": "Av. Viva"}]
    out = _merge_catalog_live(catalog, live)
    assert len(out) == 1
    assert out[0]["nombre"] == "Vivo"          # la viva gana
    assert out[0]["nap_nombre"] == "NAP-1"     # lo que falta se conserva
    assert "_from_catalog" not in out[0]       # se re-persiste
    assert out[0]["_ts"] > 100.0


def test_merge_catalog_live_entrada_nueva_se_agrega():
    from src.client_enricher import _merge_catalog_live
    out = _merge_catalog_live({}, [{"documento": "9", "serial_onu": "SER-B"}])
    assert len(out) == 1
    assert "_from_catalog" not in out[0]


def test_load_wisp_catalog_aliasa_nap_y_fusiona_por_serial():
    rows = [
        {"measurement": "wisp_clients", "field": "nombre", "value": "Viejo",
         "time": 1.0, "serial_onu": "SER-C", "documento": "3",
         "usuario_pppoe": "u3", "nap": "NAP-3"},
        {"measurement": "wisp_clients", "field": "direccion", "value": "Calle 1",
         "time": 1.0, "serial_onu": "SER-C", "documento": "3",
         "usuario_pppoe": "u3", "nap": "NAP-3"},
        {"measurement": "wisp_clients", "field": "nombre", "value": "Nuevo",
         "time": 2.0, "serial_onu": "SER-C", "documento": "3",
         "usuario_pppoe": "u3", "nap": "NAP-3"},
    ]
    enr = ClientEnricher(_FakeInflux({"wisp_clients": rows}), None,
                         EnrichmentConfig(enabled=True, lookback_hours=168))
    catalog = enr._load_wisp_catalog()
    assert len(catalog) == 1
    client = next(iter(catalog.values()))
    assert client["nombre"] == "Nuevo"       # fila más reciente gana
    assert client["direccion"] == "Calle 1"  # campos faltantes se rellenan
    assert client["nap_nombre"] == "NAP-3"   # alias nap -> nap_nombre
    assert client["_ts"] == 2.0
    assert client["_from_catalog"] is True


def test_persist_catalog_no_reescribe_filas_del_catalogo():
    from src.client_enricher import MetricPoint  # noqa: F401
    influx = _FakeInflux({})
    enr = ClientEnricher(influx, None,
                         EnrichmentConfig(enabled=True, lookback_hours=168))
    enr._persist_catalog([
        {"documento": "1", "serial_onu": "S1", "_from_catalog": True},
        {"documento": "2", "serial_onu": "S2"},
    ])
    escritos = [p for p in influx.written
                if p.measurement == "wisp_clients"]
    assert [p.tags["serial_onu"] for p in escritos] == ["S2"]


def test_mac_compartida_no_se_publica_ni_cruza():
    # MAC de infraestructura vista en >=3 seriales (caso real: la RB en GE3
    # de las 3 OLT PRADO, aprendida en 22 ONU): no es del CPE.
    infra = "40:ae:30:5e:d1:56"
    olt = [
        _olt(infra, "SER-A"), _olt(infra, "SER-B"), _olt(infra, "SER-C"),
        _olt("aa:bb:cc:dd:ee:01", "SER-001"),
    ]
    # Match por serial con la MAC compartida: la ficha queda sin MAC.
    wisp = [_wisp("DOC1", serial="SER-A", usuario="elieserp", nombre="E")]
    matched, _ = enrich_clients(wisp, olt, [], [])
    assert len(matched) == 1
    assert matched[0]["mac"] == ""
    # Match solo por MAC compartida: no debe cruzar (pendiente sin_olt).
    wisp2 = [_wisp("DOC2", usuario="otrouser", nombre="O", mac=infra)]
    matched2, pending2 = enrich_clients(wisp2, olt, [], [])
    assert matched2 == []
    assert pending2[0]["causa"] == "sin_olt"


def test_mac_preferencia_pppoe_sobre_fdb_olt():
    # La MAC caller-id de la RB es la del CPE; la FDB de la OLT puede ser
    # otra (bridge/ONT). Con ambas distintas y no compartidas, gana PPPoE.
    wisp = [_wisp("DOC3", serial="SER-003", usuario="elieserp")]
    olt = [_olt("aa:bb:cc:dd:ee:03", "SER-003")]
    pppoe = [_pppoe("ELIESERP", "6c:68:a4:d4:50:a9")]
    matched, _ = enrich_clients(wisp, olt, pppoe, [])
    assert len(matched) == 1
    assert matched[0]["mac"] == "6c:68:a4:d4:50:a9"


def test_mac_fdb_unicada_se_conserva_sin_pppoe():
    # Sin PPPoE activo, la MAC FDB unica de la ONU si se publica.
    wisp = [_wisp("DOC4", serial="SER-004")]
    olt = [_olt("aa:bb:cc:dd:ee:04", "SER-004")]
    matched, _ = enrich_clients(wisp, olt, [], [])
    assert matched[0]["mac"] == "aa:bb:cc:dd:ee:04"


def test_mac_compartida_en_olt_only_tampoco_se_publica():
    infra = "40:ae:30:5e:d1:56"
    olt = [_olt(infra, "SER-A"), _olt(infra, "SER-B"),
           _olt(infra, "SER-C"), _olt("aa:bb:cc:dd:ee:05", "SER-005")]
    out = _build_olt_only_rows(olt)
    by_serial = {_field_row(r): r for r in out}
    assert by_serial["SER-A"]["mac"] == ""
    assert by_serial["SER-005"]["mac"] == "aa:bb:cc:dd:ee:05"


def _field_row(row):
    return (row.get("serial_onu") or "").upper()
