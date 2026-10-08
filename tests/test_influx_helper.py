# tests/test_influx_helper.py
"""
Requiere influxdb_client instalado (en requirements.txt), sin red:
se testea el colapso de filas duplicadas por series de InfluxDB.
"""
import pytest

from src.api.influx_helper import _collapse_olt_rows, _row_completeness


def _row(olt_name, time, cpu=None, memory=None, temperature=None, uptime=0,
         online_onus=None, total_onus=None):
    r = {"olt_name": olt_name, "time": time, "uptime": uptime}
    if cpu is not None:
        r["cpu"] = cpu
    if memory is not None:
        r["memory"] = memory
    if temperature is not None:
        r["temperature"] = temperature
    if online_onus is not None:
        r["online_onus"] = online_onus
    if total_onus is not None:
        r["total_onus"] = total_onus
    return r


def test_row_completeness_ignora_campos_vacios():
    completa = _row("OLT-X", "2026-01-01T00:00:00", cpu=11.0, memory=72.0,
                    temperature=36.0, uptime=1, online_onus=242, total_onus=382)
    legada = _row("OLT-X", "2026-01-01T00:00:00", uptime=0, online_onus=292,
                  total_onus=382)
    assert _row_completeness(completa) > _row_completeness(legada)


def test_colapsa_series_duplicadas_de_la_misma_olt():
    rows = [
        _row("OLT-NARANJILLOS-1", "2026-09-08T17:35:26", uptime=0,
             online_onus=292, total_onus=382),
        _row("OLT-NARANJILLOS-1", "2026-09-09T20:42:24", cpu=10.0, memory=72.0,
             temperature=36.0, uptime=4155452, online_onus=242, total_onus=382),
    ]
    result = _collapse_olt_rows(rows)
    assert len(result) == 1
    assert result[0]["olt_name"] == "OLT-NARANJILLOS-1"
    assert result[0]["cpu"] == 10.0


def test_colapsa_con_empate_elige_la_mas_reciente():
    rows = [
        _row("OLT-PRADO-1", "2026-09-09T20:00:00", cpu=7.0, memory=40.0,
             temperature=32.0),
        _row("OLT-PRADO-1", "2026-09-09T20:30:00", cpu=8.0, memory=41.0,
             temperature=33.0),
    ]
    result = _collapse_olt_rows(rows)
    assert len(result) == 1
    assert result[0]["cpu"] == 8.0


def test_mantiene_un_registro_por_separado_por_olt():
    rows = [
        _row("OLT-NARANJILLOS-1", "2026-09-09T20:42:24", cpu=10.0, memory=72.0),
        _row("OLT-PRADO-1", "2026-09-09T20:42:24", cpu=7.0, memory=40.0),
    ]
    result = _collapse_olt_rows(rows)
    assert len(result) == 2
    assert {r["olt_name"] for r in result} == {"OLT-NARANJILLOS-1", "OLT-PRADO-1"}


def test_ignora_filas_sin_olt_name():
    rows = [
        {"time": "2026-09-09T20:42:24", "cpu": 5.0},
        _row("OLT-PRADO-1", "2026-09-09T20:42:24", cpu=7.0, memory=40.0),
    ]
    result = _collapse_olt_rows(rows)
    assert len(result) == 1
    assert result[0]["olt_name"] == "OLT-PRADO-1"

def test_client_kw_match_and_por_tokens():
    from src.api.influx_helper import _client_kw_match
    row = {"nombre": "Juan Perez Diaz", "documento": "12345678",
           "direccion": "Calle 1", "serial_onu": "GPON001"}
    assert _client_kw_match(row, "perez juan")
    assert _client_kw_match(row, "12345678")
    assert not _client_kw_match(row, "perez gomez")
    assert _client_kw_match({"nombre": "ana maria"}, "ana")
