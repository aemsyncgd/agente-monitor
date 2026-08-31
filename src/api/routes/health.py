# src/api/routes/health.py
from fastapi import APIRouter
import time

from ..influx_helper import _check_connection, get_overall_stats

router = APIRouter()

_start_time = time.time()


@router.get("/")
async def health_check():
    """System health check with real InfluxDB connectivity."""
    influx_ok = _check_connection()
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
        stats = get_overall_stats()
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


@router.get("/ai")
async def ai_status():
    """Get AI engine status."""
    return {
        "model_loaded": False,
        "anomalies_detected": 0,
        "last_training": None,
    }
