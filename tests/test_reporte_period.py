"""Tests del soporte de `period` (daily/weekly/monthly) en el reporte.

El scheduler llama `run(period=...)` en los jobs semanal y mensual; antes de
este cambio `DailyReporter.run()` no aceptaba el argumento y esos jobs fallaban
con TypeError en cada ejecución programada.
"""

import pytest

from src.reporte.reporter import DailyReporter


class _FakeInflux:
    bucket = "monitoreo"

    def __init__(self):
        self.queries = []

    def query(self, flux):
        self.queries.append(flux)
        return []


@pytest.fixture
def reporter():
    return DailyReporter(
        influx=_FakeInflux(),
        bot_token="test-token",
        chat_id="test-chat",
        format_path="/nonexistent/telegram-report-format.conf",
        config_path="/nonexistent/reporte-config.json",
    )


def test_default_period_is_daily(reporter):
    assert reporter.period == "daily"
    assert reporter.window_hours == 24
    assert reporter.window_label == "24h"


def test_range_start_uses_window(reporter):
    reporter._set_period("weekly")
    assert reporter.window_hours == 168
    assert reporter.window_label == "7d"
    assert reporter._range_start() == "range(start: -168h)"

    reporter._set_period("monthly")
    assert reporter.window_hours == 720
    assert reporter.window_label == "30d"
    assert reporter._range_start() == "range(start: -720h)"


def test_unknown_period_falls_back_to_daily(reporter):
    reporter._set_period("trimestral")
    assert reporter.period == "daily"
    assert reporter.window_hours == 24
    assert reporter._range_start() == "range(start: -24h)"


def test_run_accepts_period_without_typeerror(reporter):
    """Regression: el scheduler invoca run(period=...)."""
    assert reporter.run(period="weekly") is False  # sin datos => False, sin excepción
    assert reporter.period == "weekly"


def test_run_weekly_queries_wider_window(reporter):
    reporter.run(period="weekly")
    assert reporter._query_calls_have("-168h"), "se esperaba la ventana semanal en el Flux"
    assert not reporter._query_calls_have("-24h"), "no debe quedar la ventana diaria hardcodeada"


def test_run_daily_keeps_24h_window(reporter):
    reporter.run(period="daily")
    assert reporter._query_calls_have("-24h")


def test_labelize_is_tolerant_to_old_templates(reporter):
    """Un conf anterior sin {window_label} no debe mostrar el placeholder."""
    legacy = "<b>Total 24h:</b> {total_avg}"
    assert reporter._labelize(legacy, "7d") == legacy  # sin placeholder, no se toca

    new = "<b>Total {window_label}:</b> {total_avg}"
    assert reporter._labelize(new, "7d") == "<b>Total 7d:</b> {total_avg}"


def _query_calls_have(self, needle):
    return any(needle in q for q in self.influx.queries)


DailyReporter._query_calls_have = _query_calls_have