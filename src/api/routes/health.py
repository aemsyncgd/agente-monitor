# src/api/routes/health.py
from fastapi import APIRouter
import time

from ..influx_helper import (
    _check_connection,
    get_overall_stats,
    get_pipeline_status,
    run_influx,
)

router = APIRouter()

_start_time = time.time()


@router.get("/")
async def health_check():
    """System health check with real InfluxDB connectivity.

    `_check_connection` es una query síncrona a InfluxDB: corre en el executor
    dedicado de Influx con cota de 6s; nunca bloquea el event loop ni espera
    detrás de queries pesadas.
    """
    try:
        influx_ok = await run_influx(_check_connection, _timeout=6)
    except Exception:
        influx_ok = False
    return {
        "status": "healthy" if influx_ok else "degraded",
        "uptime": time.time() - _start_time,
        "components": {
            "influxdb": "connected" if influx_ok else "unreachable",
            "ai_engine": "ready",
            "collectors": "running",
        },
    }


@router.get("/collectors")
async def collector_status():
    """Get collector status from OLT count in InfluxDB."""
    try:
        stats = await run_influx(get_overall_stats, _timeout=15)
        return {
            "olt_collectors": [],
            "mikrotik_collectors": [],
            "total_metrics_collected": stats.get("total_onus", 0) + stats.get("total_interfaces", 0),
            "olts_tracked": stats.get("total_olts", 0),
        }
    except Exception:
        return {
            "olt_collectors": [],
            "mikrotik_collectors": [],
            "total_metrics_collected": 0,
        }


@router.get("/pipeline")
async def pipeline_status():
    """Frescura de escritura por medicion del pipeline de datos (enriquecimiento,
    potencias OLT, sesiones PPPoE, WISP...). No tira errores si Influx no responde."""
    try:
        data = await run_influx(get_pipeline_status, _timeout=15)
        return {"status": "ok", "detail": data}
    except Exception as e:
        return {"status": "error", "detail": {"error": str(e), "measurements": []}}


@router.get("/ai")
async def ai_status():
    """Get AI engine status."""
    return {
        "model_loaded": False,
        "anomalies_detected": 0,
        "last_training": None,
    }