# tests/test_olt_power_query.py
"""Regresiones del query de potencia optica de las OLTs.

El modal de NODOS / OLTs lee `/api/v1/metrics/olt/power`. Aunque el collector
escriba rx_power y tx_power correctamente, el query las entregaba separadas del
`status` de la misma ONU, y el modal pintaba "—" en la fila de cada una.

Causa: `pivot(rowKey: ["_time"])` fusiona por MARCA DE TIEMPO. El estado de la
ONU y su potencia no se escriben en el mismo instante (el collector escribe el
punto de estado durante la fase ONUs y el punto de potencia al final de la fase
optica), asi que cada ONU producia dos filas:

    fila 1: status=3            sin rx_power/tx_power   -> el modal muestra "—"
    fila 2: rx_power/tx_power   sin status              -> no cuenta como online

La clave de fila debe ser la IDENTIDAD de la ONU, no el instante: cada campo se
resuelve con su propio `last()` y recien ahi se fusionan en una fila.
"""
import pytest

from src.api import influx_helper


def _flux(fn, *args, **kwargs) -> str:
    """Captura el flux que la consulta le pasa a _query."""
    captured = {}

    def fake_query(flux):
        captured["flux"] = flux
        return []

    orig = influx_helper._query
    influx_helper._query = fake_query
    try:
        fn(*args, **kwargs)
    finally:
        influx_helper._query = orig
    return captured["flux"]


def test_olt_power_fusiona_por_ouna_y_no_por_instante():
    flux = _flux(influx_helper._query_olt_power, None, False, 45)
    assert 'pivot(rowKey: ["_time"]' not in flux, (
        "pivot por _time parte cada ONU en dos filas (estado / potencia) y el "
        "modal muestra \"—\" aunque los datos esten en InfluxDB"
    )
    assert 'rowKey: ["olt_name", "onu_index"]' in flux


def test_optical_power_fusiona_por_ouna_y_no_por_instante():
    """Misma medicion, mismo fallo: afecta tambien al dashboard Resumen."""
    flux = _flux(influx_helper._query_optical_power, None, None, False)
    assert 'pivot(rowKey: ["_time"]' not in flux
    assert 'rowKey: ["olt_name", "onu_index"]' in flux


def test_olt_power_conserva_el_rango_y_los_filtros():
    """El arreglo del pivot no debe cambiar la ventana ni los filtros."""
    flux = _flux(influx_helper._query_olt_power, "OLT-SISAL-1", True, 45)
    assert '"olt_name"] == "OLT-SISAL-1"' in flux
    assert '"status"] == 3' in flux
    assert "range(start: -45m)" in flux
    assert '"_measurement"] == "optical_power"' in flux


def test_ambos_queries_siguen_pivoteando_por_field():
    """rowKey cambia; columnKey debe seguir siendo _field."""
    for flux in (
        _flux(influx_helper._query_olt_power, None, False, 45),
        _flux(influx_helper._query_optical_power, None, None, False),
    ):
        assert 'columnKey: ["_field"]' in flux
        assert 'valueColumn: "_value"' in flux