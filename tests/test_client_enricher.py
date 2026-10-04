# tests/test_client_enricher.py
import json

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
    assert len(pppoe) == 2
    assert len(dhcp) == 1
    for f in pppoe + dhcp:
        assert "|> last()" in f
        assert '_field"] ==' in f


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