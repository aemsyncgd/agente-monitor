# tests/test_redis_store.py
from datetime import datetime, timezone

import fakeredis
import pytest

from src.redis_store import ClientStore

_LOS_VALUES = [1, 4, 6]


def _parse_ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _row(doc, **kw):
    base = {
        "documento": doc,
        "usuario_pppoe": f"user{doc}",
        "serial_onu": f"SER{doc}",
        "mac": f"aa:bb:cc:dd:{int(doc) % 100:02d}:01" if doc else "aa:bb:cc:dd:00:01",
        "nombre": f"Cliente {doc}",
        "direccion": f"Calle {doc}",
        "olt": "OLT-1",
        "nodo": "N1",
        "nap": f"NAP-{doc}",
        "zona": f"Zona-{doc}",
        "estado_onu": 3,
        "coincidencia": "serial",
        "ubicacion_descuadrada": "false",
        "telefono": f"555{doc}",
        "time": "2026-09-12T10:00:00Z",
        "pon_port": "GPON0/1",
        "pendiente": "false",
        "estado_servicio": "Activo",
        "pppoe_status": "up",
        "plan": "",
        "ip_servicio": "",
    }
    base.update(kw)
    return base


@pytest.fixture
def store():
    r = fakeredis.FakeRedis(decode_responses=True)
    return ClientStore(r)


def test_get_ficha_roundtrip(store):
    store.rebuild([_row("1", estado_onu=3)])
    ficha = store.get_ficha("1")
    assert ficha is not None
    assert ficha["documento"] == "1"
    assert ficha["nombre"] == "Cliente 1"
    assert ficha["estado_onu"] == 3
    assert store.get_ficha("noexiste") is None


def test_rebuild_resetea_fichas_viejas(store):
    store.rebuild([_row("1"), _row("2")])
    store.rebuild([_row("1")])
    assert store.get_ficha("1") is not None
    assert store.get_ficha("2") is None
    assert store.count() == 1


def test_indices_serial_mac_user(store):
    store.rebuild([_row("1"), _row("2")])
    assert store.by_serial("ser1") == ["1"]
    assert store.by_serial("SER1") == ["1"]
    assert store.by_mac("aa:bb:cc:dd:01:01") == ["1"]
    assert store.by_user("user2") == ["2"]


def test_search_por_nombre_case_insensitive(store):
    store.rebuild([_row("1", nombre="Juan Perez"), _row("2", nombre="Maria Gomez")])
    assert store.search("juan") == ["1"]
    assert store.search("PEREZ") == ["1"]
    assert store.search("gomez") == ["2"]
    assert store.search("nadie") == []


def test_search_por_documento_exacto_y_serial(store):
    store.rebuild([_row("12345678", serial_onu="OEMT-ABC"), _row("2")])
    assert store.search("12345678") == ["12345678"]
    assert store.search("oemt-abc") == ["12345678"]


def test_search_respeta_limit_y_filtros(store):
    store.rebuild([_row("1", zona="Centro"), _row("2", zona="Centro"),
                   _row("3", zona="Norte")])
    assert store.search("Cliente", limit=1) == ["1"]
    assert set(store.search("Cliente", zona="Centro")) == {"1", "2"}
    assert set(store.search("Cliente", nap="NAP-3")) == {"3"}


def test_los_filtra_estados_down(store):
    store.rebuild([_row("1", estado_onu=1), _row("2", estado_onu=3),
                   _row("3", estado_onu=4), _row("4", estado_onu=-1)])
    docs = store.los(_LOS_VALUES)
    assert sorted(docs) == ["1", "3"]


def test_group_los_por_zona(store):
    store.rebuild([_row("1", zona="A", estado_onu=1), _row("2", zona="A", estado_onu=3),
                   _row("3", zona="B", estado_onu=4)])
    grupos = store.group_los(_LOS_VALUES, by="zona")
    g = {x["clave"]: x for x in grupos}
    assert g["A"]["total"] == 2
    assert g["A"]["los"] == 1
    assert g["B"]["total"] == 1
    assert g["B"]["los"] == 1


def test_meta_reporta_construccion(store):
    antes = datetime.now(timezone.utc).timestamp() - 5
    store.rebuild([_row("1")])
    meta = store.meta()
    assert meta["count"] == 1
    assert meta["built_at"]
    assert antes <= _parse_ts(meta["built_at"]) <= datetime.now(timezone.utc).timestamp() + 5


class _FakeInflux:
    bucket = "monitoreo"

    def __init__(self, rows):
        self._rows = rows

    def query(self, flux):
        assert 'clientes_enriquecidos' in flux
        return [dict(r) for r in self._rows]


def test_rebuild_from_influx_pivota_y_almacena(store):
    fila = dict(_row("10", estado_onu=1))
    fila["time"] = "2026-09-12T10:00:00Z"
    influx = _FakeInflux([fila])
    n = store.rebuild_from_influx(influx)
    assert n == 1
    ficha = store.get_ficha("10")
    assert ficha["estado_onu"] == 1
    assert ficha["nombre"] == "Cliente 10"


def test_rebuild_almacena_fichas_sin_documento_por_serial(store):
    fila = dict(_row("", serial_onu="SER-SOLO", estado_onu=1,
                     zona="Zeta", nap="NAP-Z"))
    del fila["documento"]
    del fila["usuario_pppoe"]
    del fila["mac"]
    influx = _FakeInflux([fila])
    n = store.rebuild_from_influx(influx)
    assert n == 1
    assert store.los() == ["SER-SOLO"]
    assert store.by_serial("ser-solo") == ["SER-SOLO"]
    grupos = {g["clave"]: g for g in store.group_los([1, 4, 6], by="zona")}
    assert grupos["Zeta"]["total"] == 1
    assert grupos["Zeta"]["los"] == 1