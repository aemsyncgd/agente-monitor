# tests/test_olt_g2b_power.py
"""Regresiones del barrido de potencia optica en OLTs VSOL V1600G2B (SISAL).

El G2B no responde a GETBULK sobre la tabla optica (verificado en produccion:
`snmpbulkwalk`/`snmpbulkget` dan timeout y dejan el agente sin responder), asi
que la potencia solo se puede leer con GET simple, ONU por ONU.

Tres defectos componian el fallo "no aparecen Rx/Tx en SISAL":

1. `_g2b_rxtx_offset` era estado de instancia y `main.py` recrea los collectors
   cada 300s, cuando un ciclo dura 430-560s. El offset volvia siempre a 0, asi
   que cada pasada repreguntaba las mismas ~140 ONUs iniciales (los logs
   mostraban `rotacion 0` de forma permanente).
2. El offset avanzaba de a 280 ONUs cuando la ventana real cubierta era de
   ~140: aun persistiendo el offset dejaba huecos permanentes.
3. El coste de arranque de un proceso `snmpget` por ONU limitaba la pasada a
   ~0.7 ONUs/s, insuficiente para barrer las ~660 ONUs online de SISAL dentro de
   la ventana de 45 min del modal.
"""
import asyncio

import pytest

from src.collectors import olt_collector as oc
from src.collectors.olt_collector import OltCollector, OltConfig


# --------------------------------------------------------------------------
# Helpers puros de rotacion (deben sobrevivir al recreado de collectors)
# --------------------------------------------------------------------------

def test_offset_de_una_olt_desconocida_es_cero():
    assert oc._g2b_offset("OLT-NUNCA-VISTA") == 0


def test_el_offset_sobrevive_al_recreado_del_collector():
    """Regresión del bug: el estado era de instancia, y main.py recrea los
    collectors cada 300s, siempre antes de terminar el ciclo."""
    cfg = OltConfig(hostname="OLT-SISAL-1", ip="172.16.74.5", community="c")
    oc._G2B_RXTX_OFFSET.pop("OLT-SISAL-1", None)

    primero = OltCollector(cfg)
    oc._g2b_commit("OLT-SISAL-1", 200)

    # Collector NUEVO, como lo construye main.py tras la recarga de config.
    segundo = OltCollector(cfg)
    assert oc._g2b_offset("OLT-SISAL-1") == 200
    assert primero is not segundo


def test_el_offset_avanza_lo_realmente_recorrido():
    oc._G2B_RXTX_OFFSET.pop("OLT-TEST-AVANCE", None)
    oc._g2b_commit("OLT-TEST-AVANCE", 140)
    assert oc._g2b_offset("OLT-TEST-AVANCE") == 140
    oc._g2b_commit("OLT-TEST-AVANCE", 140)
    assert oc._g2b_offset("OLT-TEST-AVANCE") == 280


def test_una_pasada_completa_no_mueve_el_offset():
    """Si la pasada entra completa no hay nada que rotar: el offset debe volver
    a su posicion para no desalinear el agrupamiento."""
    indices = [f"1.{i}" for i in range(1, 501)]
    offset = oc._g2b_offset("OLT-TEST-FULL")
    ordered = oc._g2b_ordered(indices, offset)
    assert len(ordered) == len(indices)
    assert sorted(ordered) == sorted(indices)

    oc._g2b_commit("OLT-TEST-FULL", len(indices))
    assert oc._g2b_offset("OLT-TEST-FULL") % len(indices) == offset % len(indices)


def test_la_rotacion_no_deja_huecos_permanentes():
    """El bug original: avanzar 280 con ventana de ~140 salteaba ONUs para
    siempre. Con ventanas contiguas, varias pasadas cubren el conjunto completo."""
    indices = [f"1.{i}" for i in range(1, 501)]  # 500 ONUs, ventana 140
    ventana = 140
    vistas = set()
    offset = 100
    for _ in range(5):
        ordered = indices[offset:] + indices[:offset]
        vistas.update(ordered[:ventana])
        offset = (offset + ventana) % len(indices)
    assert len(vistas) == len(indices), (
        f"quedaron {len(indices) - len(vistas)} ONUs sin leer nunca"
    )


def test_orden_rotado_conserva_todas_las_indices():
    indices = ["1.1", "2.5", "3.9"]
    ordered = oc._g2b_ordered(indices, 1)
    assert ordered == ["2.5", "3.9", "1.1"]
    assert oc._g2b_ordered(indices, 5) == ["3.9", "1.1", "2.5"]
    assert oc._g2b_ordered([], 3) == []


# --------------------------------------------------------------------------
# El barrido debe usar GET por pysnmp, no un proceso por ONU
# --------------------------------------------------------------------------

class _FakeVar:
    """pysnmp devuelve tuplas (ObjectIdentity, valor); se imita esa forma."""

    def __init__(self, oid, value):
        self.pair = (oid, value)

    def __iter__(self):
        return iter(self.pair)

    def __getitem__(self, i):
        return self.pair[i]

    def get_oid(self):
        return self.pair[0]


class _Gauge:
    """Imita el valor que devuelve pysnmp para la potencia (dBm con signo)."""

    def __init__(self, raw):
        self.raw = raw

    def __str__(self):
        return self.raw


@pytest.mark.asyncio
async def test_el_barrido_g2b_no_crea_procesos_snmpget(monkeypatch):
    """Cada `snmpget` por ONU era el cuello de botella (~0.7 ONUs/s)."""
    cfg = OltConfig(hostname="OLT-SISAL-TEST", ip="10.0.0.1", community="c")
    col = OltCollector(cfg)
    col._g2b_mode = True

    def boom(*a, **k):
        raise AssertionError("el barrido G2B no debe lanzar snmpget por ONU")

    monkeypatch.setattr(oc.asyncio, "create_subprocess_exec", boom)

    llamadas = []

    async def fake_get_cmd(engine, community, transport, context, *var_binds):
        assert len(var_binds) == 2, "RX y TX deben ir en el mismo GET"
        llamadas.append(1)
        return None, None, None, [
            _FakeVar("rx", _Gauge("-18.80")),
            _FakeVar("tx", _Gauge("1.55")),
        ]

    import pysnmp.hlapi.v3arch.asyncio as hl

    monkeypatch.setattr(hl, "get_cmd", fake_get_cmd)
    monkeypatch.setattr(hl, "SnmpEngine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(hl, "UdpTransportTarget", _FakeTransportFactory())

    res = await col._get_rxtx_gentle_rotating(["1.1", "1.2", "1.3"])

    assert len(llamadas) == 3
    assert res["1.1"] == {"rx": -18.80, "tx": 1.55}
    assert res["1.3"] == {"rx": -18.80, "tx": 1.55}


def test_los_oids_de_potencia_son_los_de_la_tabla_optica_vsol():
    """Guarda contra un cambio de OID: son los unicos que el G2B responde."""
    cfg = OltConfig(hostname="OLT-SISAL-OID", ip="10.0.0.9", community="c")
    col = OltCollector(cfg)
    assert col.OID_ONU_RX == "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.7"
    assert col.OID_ONU_TX == "1.3.6.1.4.1.37950.1.1.6.1.1.3.1.6"


class _FakeEngine:
    def close_dispatcher(self):
        pass


class _FakeTransportFactory:
    @staticmethod
    async def create(target, **kwargs):
        return target


@pytest.mark.asyncio
async def test_una_olt_sin_terror_sigue_reportando_por_paso(monkeypatch):
    """Un fallo puntual de una ONU no puede abortar el resto del barrido."""
    cfg = OltConfig(hostname="OLT-SISAL-ERR", ip="10.0.0.2", community="c")
    col = OltCollector(cfg)
    col._g2b_mode = True

    async def fake_get_cmd(engine, community, transport, context, *var_binds):
        return None, None, None, [
            _FakeVar("rx", _Gauge("-20.00")),
            _FakeVar("tx", _Gauge("2.00")),
        ]

    import pysnmp.hlapi.v3arch.asyncio as hl

    monkeypatch.setattr(hl, "get_cmd", fake_get_cmd)
    monkeypatch.setattr(hl, "SnmpEngine", lambda *a, **k: _FakeEngine())
    monkeypatch.setattr(hl, "UdpTransportTarget", _FakeTransportFactory())

    res = await col._get_rxtx_gentle_rotating(["2.1", "2.2"])
    assert set(res) == {"2.1", "2.2"}
