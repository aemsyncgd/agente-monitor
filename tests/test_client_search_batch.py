# tests/test_client_search_batch.py
"""
Tests de ClientSearchService.find_many: busqueda multi-cliente con coste de
consultas constante respecto al numero de clientes.
"""
from src.client_search import ClientSearchService, _MAX_BATCH


class CountingInflux:
    """Fake Influx que cuenta consultas y sirve filas por medicion.

    Los tags de serie se extraen del regex `onu_serial =~ /.../` para simular
    el filtrado multi-serial de una sola consulta.
    """

    def __init__(self, rows_by_measurement=None):
        self.bucket = "monitoreo"
        self.rows = {k: list(v) for k, v in (rows_by_measurement or {}).items()}
        self.queries = []

    def _serials_in_flux(self, flux):
        import re as _re
        m = _re.search(r'\["onu_serial"\] =~ /\(\?i\)\^\((.*?)\)\$?/?', flux)
        if not m:
            return None
        return {s.strip().lower() for s in m.group(1).split("|")}

    @staticmethod
    def _fields_in_flux(flux):
        """Campos pedidos: igualdad única o alternancia por regex."""
        import re as _re
        m = _re.search(r'\["_field"\] == "(\w+)"', flux)
        if m:
            return {m.group(1)}
        m = _re.search(r'\["_field"\] =~ /\^\((.*?)\)\$/', flux)
        if m:
            return {f.strip() for f in m.group(1).split("|")}
        return None

    def query(self, flux):
        import re as _re
        self.queries.append(flux)
        m = _re.search(r'\["_measurement"\] == "(\w+)"', flux)
        if not m:
            return []
        rows = self.rows.get(m.group(1), [])
        wanted = self._serials_in_flux(flux)
        fields = self._fields_in_flux(flux)
        out = []
        for r in rows:
            if wanted is not None:
                if str(r.get("onu_serial") or r.get("serial_onu") or "").lower() not in wanted:
                    continue
            if fields is not None and str(r.get("field", "")) not in fields:
                continue
            out.append(dict(r))
        return out

    def by_measurement(self, name):
        return [q for q in self.queries if f'"{name}"' in q]


class FakeWisp:
    def __init__(self, serials=None, docs=None):
        self.serial_map = {k.upper(): v for k, v in (serials or {}).items()}
        self.doc_map = {k.upper(): v for k, v in (docs or {}).items()}
        self.calls_serial = []

    def fetch_by_serial(self, serial, force=False):
        self.calls_serial.append(serial)
        return self.serial_map.get(str(serial).upper())

    def fetch_by_document(self, documento):
        return self.doc_map.get(str(documento).strip().upper())


def _enriched(serial, **kw):
    row = {
        "measurement": "clientes_enriquecidos",
        "serial_onu": serial,
        "documento": "12312803",
        "mac": "d0:59:19:7c:33:5c",
        "nap": "I00517",
        "nodo": "PRADO",
        "olt": "OLT-PRADO-1",
        "zona": "PRADO-VIDANET",
        "field": "nombre",
        "value": "PEDRO CLIENTE",
        "time": "2026-09-22T12:00:00+00:00",
    }
    row.update(kw)
    return row


def _optical(serial, field="status", value=3, **tags):
    row = {
        "measurement": "optical_power",
        "onu_serial": serial,
        "field": field,
        "value": value,
        "time": "2026-09-22T12:00:00+00:00",
        "olt_name": "OLT-PRADO-1",
        "pon_port": "GPON0/1",
    }
    row.update(tags)
    return row


def _mk(rows, serials=None, docs=None):
    influx = CountingInflux(rows)
    svc = ClientSearchService(wisp=FakeWisp(serials=serials, docs=docs),
                              influx=influx)
    return svc, influx


S1 = "ZICGCDE2F0D9"
S2 = "ZICGCDEF0002"
S3 = "ZICGCDEF0003"


class TestCamposDeContacto:
    """La ficha debe traer los datos de contacto desde Influx.

    `clientes_enriquecidos` ya contiene direccion/telefono/email, así que no
    hace falta golpear WISP por cliente: bastan las mismas consultas de siempre.
    """

    def _fila(self, serial, field, value):
        return _enriched(serial, field=field, value=value)

    def test_trae_direccion_telefono_y_email_sin_llamar_a_wisp(self):
        filas = [
            self._fila(S1, "nombre", "PEDRO CLIENTE"),
            self._fila(S1, "direccion", "CRA 45 # 12-30"),
            self._fila(S1, "telefono", "3001234567"),
            self._fila(S1, "email", "pedro@vidanet.co"),
        ]
        svc, _ = _mk({"clientes_enriquecidos": filas})
        res = svc.find_many([S1])
        ficha = res["results"][0]["ficha"]
        assert ficha["nombre"] == "PEDRO CLIENTE"
        assert ficha["direccion"] == "CRA 45 # 12-30"
        assert ficha["telefono"] == "3001234567"
        assert ficha["email"] == "pedro@vidanet.co"

    def test_no_hace_llamadas_wisp_por_cliente(self):
        filas = [self._fila(S1, "nombre", "PEDRO")]
        svc, _ = _mk({"clientes_enriquecidos": filas})
        svc.find_many([S1, S2, S3])
        assert svc.wisp.calls_serial == [], (
            "el batch no debe generar una llamada WISP por cliente")

    def test_el_analisis_de_grupo_conserva_el_telefono(self):
        filas = [self._fila(S1, "nombre", "PEDRO"),
                 self._fila(S1, "telefono", "3001234567")]
        svc, _ = _mk({"clientes_enriquecidos": filas,
                      "optical_power": [_optical(S1, "status", 4)]})
        grp = svc.group_analysis(serials=[S1])
        assert grp["total"] == 1
        assert grp["afectados"][0]["telefono"] == "3001234567"

    def test_una_busqueda_por_nombre_tambien_trae_el_telefono(self):
        filas = [self._fila(S1, "nombre", "PEDRO JOSE MALDONADO"),
                 self._fila(S1, "telefono", "3001234567")]
        svc, _ = _mk({"clientes_enriquecidos": filas})
        res = svc.find_many(["PEDRO JOSE MALDONADO"])
        assert res["found"] == 1
        assert res["results"][0]["ficha"]["telefono"] == "3001234567"

    def test_nombre_con_espacios_como_unico_termino(self):
        """Regresión: el literal de búsqueda no puede escapar los espacios.

        `re.escape` convertía "PEDRO JOSE" en "PEDRO\\ JOSE", que no coincide
        con ningún valor, así que buscar sólo por nombre no encontraba nada.
        """
        filas = [self._fila(S1, "nombre", "PEDRO JOSE MALDONADO FIGUEROA")]
        svc, _ = _mk({"clientes_enriquecidos": filas})
        res = svc.find_many(["PEDRO JOSE MALDONADO"])
        assert res["found"] == 1
        assert res["results"][0]["ficha"]["serial_onu"] == S1
        # El término buscado debe llegar literal al flux, sin escapes.
        buscada = [q for q in svc.influx.queries if "strings" in q]
        assert buscada, "no se generó la consulta por nombre"
        assert "pedro jose maldonado" in buscada[0]
        assert "pedro\\ jose" not in buscada[0]

    def test_nombre_con_comilla_no_rompe_el_flux(self):
        filas = [self._fila(S1, "nombre", "ANA MARIA DE LOS SANTOS")]
        svc, _ = _mk({"clientes_enriquecidos": filas})
        svc.find_many(['ANA "LAURA" SANTOS'])
        buscada = [q for q in svc.influx.queries if "strings" in q]
        assert buscada, "no se generó la consulta por nombre"
        assert '\\"laura\\"' in buscada[0]


class TestCosteConstante:
    def test_consultas_no_crecen_con_el_numero_de_clientes(self):
        uno, i1 = _mk({"clientes_enriquecidos": [_enriched(S1)]},
                      serials={S1: {"nombre": "PEDRO", "serial_onu": S1}})
        uno.find_many([S1])
        q_uno = len(i1.queries)

        tres, i3 = _mk(
            {"clientes_enriquecidos": [_enriched(S1, documento="111"),
                                       _enriched(S2, documento="222"),
                                       _enriched(S3, documento="333")]},
            serials={S1: {"serial_onu": S1}, S2: {"serial_onu": S2},
                     S3: {"serial_onu": S3}},
        )
        tres.find_many([S1, S2, S3])

        assert len(i3.queries) == q_uno, (
            f"3 clientes usaron {len(i3.queries)} consultas vs "
            f"{q_uno} para 1 cliente")

    def test_usa_una_sola_consulta_optica_multiserial(self):
        svc, influx = _mk(
            {"clientes_enriquecidos": [_enriched(S1), _enriched(S2)],
             "optical_power": [_optical(S1, "status", 3),
                               _optical(S1, "rx_power", -25.37),
                               _optical(S2, "status", 4),
                               _optical(S2, "rx_power", -40.0)]},
            serials={S1: {"serial_onu": S1}, S2: {"serial_onu": S2}},
        )
        svc.find_many([S1, S2])
        # etapa "ultimo valor optico": una sola consulta para ambos seriales
        opt = [q for q in influx.by_measurement("optical_power")
               if 'group(columns: ["onu_serial", "_field"])' in q]
        assert len(opt) == 1
        assert S1 in opt[0] and S2 in opt[0]

    def test_deduplica_consultas_repetidas(self):
        svc, influx = _mk({"clientes_enriquecidos": [_enriched(S1)]},
                          serials={S1: {"serial_onu": S1}})
        res = svc.find_many([S1, S1, S1])
        assert res["total"] == 1
        assert len([c for c in influx.queries]) <= 5

    def test_limita_el_lote_al_tope(self):
        serials = {f"ZICGC{i:04d}": {"serial_onu": f"ZICGC{i:04d}"}
                   for i in range(_MAX_BATCH + 12)}
        rows = {"clientes_enriquecidos": [_enriched(s) for s in serials]}
        svc, _ = _mk(rows, serials=serials)
        res = svc.find_many(list(serials))
        assert res["truncated"] is True
        assert len(res["results"]) == _MAX_BATCH


class TestResolucionMultiple:
    def test_resuelve_por_serial_documento_mac_y_nombre(self):
        svc, _ = _mk(
            {"clientes_enriquecidos": [
                _enriched(S1, documento="12312803", mac="D0:59:19:7C:33:5C",
                          nombre="PEDRO JOSE MALDONADO"),
            ]},
            serials={S1: {"serial_onu": S1, "nombre": "PEDRO JOSE MALDONADO"}},
        )
        res = svc.find_many([S1, "12312803", "d0:59:19:7c:33:5c", "PEDRO JOSE"])
        assert res["found"] >= 1
        assert res["results"][0]["ficha"]["serial_onu"] == S1

    def test_marca_no_encontrados_sin_romper(self):
        svc, _ = _mk({"clientes_enriquecidos": [_enriched(S1)]},
                     serials={S1: {"serial_onu": S1}})
        res = svc.find_many([S1, "NOEXISTE-XYZ"])
        encontrados = [r for r in res["results"] if r["found"]]
        no_encontrados = [r for r in res["results"] if not r["found"]]
        assert len(encontrados) == 1
        assert len(no_encontrados) == 1
        assert no_encontrados[0]["query"] == "NOEXISTE-XYZ"

    def test_serial_solo_optico_aun_se_reporta(self):
        """Un serial presente en optical_power pero no en la base enriquecida
        debe mostrarse igual: el estado óptico es evidencia suficiente."""
        svc, _ = _mk(
            {"clientes_enriquecidos": [],
             "optical_power": [_optical(S1, "status", 4),
                               _optical(S1, "rx_power", -38.5)]},
        )
        res = svc.find_many([S1])
        assert res["found"] == 1
        ficha = res["results"][0]["ficha"]
        assert ficha["serial_onu"] == S1
        assert ficha["estado_onu"] == 4
        assert ficha["rx_power"] == -38.5
        assert ficha["olt"] == "OLT-PRADO-1"


class TestContenidoFicha:
    def test_ficha_incluye_olt_pon_serial_nap_estado_rx_tx(self):
        svc, _ = _mk(
            {"clientes_enriquecidos": [_enriched(S1)],
             "optical_power": [_optical(S1, "status", 3),
                               _optical(S1, "rx_power", -25.37),
                               _optical(S1, "tx_power", 2.17)]},
            serials={S1: {"serial_onu": S1, "nombre": "PEDRO"}},
        )
        ficha = svc.find_many([S1])["results"][0]["ficha"]
        assert ficha["olt"] == "OLT-PRADO-1"
        assert ficha["pon_port"] == "GPON0/1"
        assert ficha["serial_onu"] == S1
        assert ficha["nap"] == "I00517"
        assert ficha["estado_onu"] == 3
        assert ficha["rx_power"] == -25.37
        assert ficha["tx_power"] == 2.17

    def test_no_consulta_mac_ni_location_por_cliente(self):
        """La base enriquecida trae mac/zona como tag: no hacen falta
        consultas por cliente (ese era el N+1)."""
        svc, influx = _mk({"clientes_enriquecidos": [_enriched(S1)],
                           "optical_power": [_optical(S1)]},
                          serials={S1: {"serial_onu": S1}})
        svc.find_many([S1])
        assert influx.by_measurement("onu_location") == []
        assert influx.by_measurement("onu_mac") == []


class TestHistorico30d:
    def test_incluye_historico_por_defecto(self):
        svc, _ = _mk(
            {"clientes_enriquecidos": [_enriched(S1)],
             "optical_power": [
                 _optical(S1, "status", 3, time="2026-09-24T12:00:00+00:00"),
                 _optical(S1, "status", 4, time="2026-09-22T06:00:00+00:00"),
                 _optical(S1, "status", 4, time="2026-09-22T18:00:00+00:00"),
             ]},
            serials={S1: {"serial_onu": S1}},
        )
        hist = svc.find_many([S1])["results"][0]["historico_30d"]
        assert hist["muestras_offline"] == 2
        assert hist["ultimo_down"] == "2026-09-22T18:00:00+00:00"

    def test_historico_ausente_no_rompe(self):
        svc, _ = _mk({"clientes_enriquecidos": [_enriched(S1)],
                      "optical_power": [_optical(S1)]},
                     serials={S1: {"serial_onu": S1}})
        hist = svc.find_many([S1])["results"][0]["historico_30d"]
        assert hist["muestras_offline"] == 0


def _ping(name, field, value, time="2026-09-22T12:00:00+00:00"):
    return {"measurement": "ping_check", "field": field, "value": value,
            "name": name, "ip": "10.0.0.9", "type": "olt", "time": time}


class TestInfraestructura:
    def test_incluye_ping_de_infraestructura_relacionada(self):
        svc, influx = _mk(
            {"clientes_enriquecidos": [_enriched(S1)],
             "optical_power": [_optical(S1)],
             "ping_check": [
                 {"measurement": "ping_check", "field": "status", "value": 1,
                  "name": "OLT-PRADO-1", "ip": "10.0.0.9", "type": "olt",
                  "time": "2026-09-22T12:00:00+00:00"},
             ]},
            serials={S1: {"serial_onu": S1}},
        )
        res = svc.find_many([S1])
        infra = res["results"][0]["infraestructura"]
        assert any(d["name"] == "OLT-PRADO-1" for d in infra)
        assert len(influx.by_measurement("ping_check")) <= 1

    def test_expone_latencia_perdida_y_downtime(self):
        """`ping_check` guarda 10 campos; el detalle debe exponer los
        operativos, no sólo si la entidad responde."""
        svc, _ = _mk(
            {"clientes_enriquecidos": [_enriched(S1)],
             "optical_power": [_optical(S1)],
             "ping_check": [
                 _ping("OLT-PRADO-1", "status", 1),
                 _ping("OLT-PRADO-1", "latency_ms_avg", 4.2),
                 _ping("OLT-PRADO-1", "latency_ms_max", 9.7),
                 _ping("OLT-PRADO-1", "packet_loss_pct", 12.5),
                 _ping("OLT-PRADO-1", "downtime_seconds", 90.0),
             ]},
            serials={S1: {"serial_onu": S1}},
        )
        infra = svc.find_many([S1])["results"][0]["infraestructura"]
        d = next(x for x in infra if x["name"] == "OLT-PRADO-1")
        assert d["latencia_ms"] == 4.2
        assert d["latencia_max_ms"] == 9.7
        assert d["perdida_pct"] == 12.5
        assert d["downtime_s"] == 90.0
        assert d["estado_ping"] == "up"

    def test_agrupa_por_entidad_y_no_por_campo(self):
        """Cada entidad debe salir una sola vez, no una por cada field."""
        svc, _ = _mk(
            {"clientes_enriquecidos": [_enriched(S1)],
             "optical_power": [_optical(S1)],
             "ping_check": [_ping("OLT-PRADO-1", f, v) for f, v in
                           (("status", 1), ("latency_ms_avg", 4.2),
                            ("packet_loss_pct", 0.0))]},
            serials={S1: {"serial_onu": S1}},
        )
        infra = svc.find_many([S1])["results"][0]["infraestructura"]
        assert [x["name"] for x in infra].count("OLT-PRADO-1") == 1

    def test_campos_ausentes_no_rompen(self):
        """Un ping antiguo sin latencia debe salir, no reventar."""
        svc, _ = _mk(
            {"clientes_enriquecidos": [_enriched(S1)],
             "optical_power": [_optical(S1)],
             "ping_check": [_ping("OLT-PRADO-1", "status", 0)]},
            serials={S1: {"serial_onu": S1}},
        )
        infra = svc.find_many([S1])["results"][0]["infraestructura"]
        d = next(x for x in infra if x["name"] == "OLT-PRADO-1")
        assert d["estado_ping"] == "down"
        assert d["latencia_ms"] is None
        assert d["perdida_pct"] is None


class TestMacCaseInsensitive:
    def test_serial_by_mac_encuentra_mac_en_mayusculas(self):
        svc, _ = _mk({"onu_mac": [
            {"measurement": "onu_mac", "field": "onu_serial", "value": S1,
             "mac": "D0:59:19:7C:33:5C"},
        ]})
        assert svc._serial_by_mac("d0:59:19:7c:33:5c") == S1

    def test_serial_by_mac_acepta_ambas_casos(self):
        svc, _ = _mk({"onu_mac": [
            {"measurement": "onu_mac", "field": "onu_serial", "value": S1,
             "mac": "D0:59:19:7C:33:5C"},
        ]})
        assert svc._serial_by_mac("D0:59:19:7C:33:5C") == S1
        assert svc._serial_by_mac("d0:59:19:7c:33:5c") == S1


class TestEntradasInvalidas:
    def test_ignora_entradas_vacias(self):
        svc, _ = _mk({"clientes_enriquecidos": [_enriched(S1)]},
                     serials={S1: {"serial_onu": S1}})
        res = svc.find_many(["", "   ", S1])
        assert res["total"] == 1

    def test_lista_via_no_revienta(self):
        svc, _ = _mk({"clientes_enriquecidos": []})
        res = svc.find_many([])
        assert res["results"] == []
        assert res["total"] == 0
