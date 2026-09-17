# src/api/routes/anomalies.py
import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..influx_helper import (
    get_anomalies_from_influx,
    get_anomaly_previous,
    get_anomaly_series,
    get_onu_optical_history,
    predict_failures,
)

router = APIRouter()

_ANOMALY_FILTERS = {
    "warning": ("warning", "low_power"),
    "critical": ("critical",),
}


def _match_type(row_type: str, filtro: str) -> bool:
    """Empata un filtro ("critical"/"warning") con los tipos de fila."""
    return row_type in _ANOMALY_FILTERS.get(filtro, ())


@router.get("/")
async def get_anomalies(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    anomaly_type: Optional[str] = Query(None, description="Filter by type"),
    hours: int = Query(24, description="Hours of history"),
    limit: int = Query(100, description="Max results"),
):
    """Get detected anomalies (low optical power, offline ONUs)."""
    rows = get_anomalies_from_influx(olt_name=olt_name, hours=hours, limit=limit)
    anomalies = []
    for r in rows:
        a = {
            "olt_name": r.get("olt_name"),
            "olt_ip": r.get("olt_ip"),
            "pon_port": r.get("pon_port"),
            "onu_index": r.get("onu_index"),
            "onu_serial": r.get("onu_serial"),
            "rx_power": r.get("value"),
            "anomaly_type": r.get("anomaly_type", "warning"),
            "description": r.get("description", ""),
            "time": r.get("time"),
        }
        if anomaly_type and not _match_type(a["anomaly_type"], anomaly_type):
            continue
        anomalies.append(a)
    return {
        "anomalies": anomalies,
        "count": len(anomalies),
        "filters": {"olt_name": olt_name, "anomaly_type": anomaly_type, "hours": hours},
    }


@router.get("/predictions")
async def get_predictions(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    hours: int = Query(24, description="Hours of history"),
):
    """Predicciones por tendencia (rx en caida) ordenadas por inminencia."""
    preds = predict_failures(hours=hours, max_results=50)
    if olt_name:
        preds = [p for p in preds if p.get("olt_name") == olt_name]
    return {
        "predictions": preds,
        "count": len(preds),
        "filters": {"olt_name": olt_name, "hours": hours},
    }


@router.get("/history")
async def get_history(
    olt_name: str = Query(...),
    onu_serial: str = Query(...),
    hours: int = Query(24, description="Hours of history"),
):
    """Serie 24h de rx/tx de una ONU para los historicos de cada card."""
    try:
        return await asyncio.to_thread(
            get_onu_optical_history, olt_name, onu_serial, hours
        )
    except Exception:
        raise HTTPException(status_code=500, detail="error_influx")


@router.get("/recent")
async def get_recent_anomalies(limit: int = Query(20, description="Max results")):
    """Get recent anomalies for dashboard."""
    rows = get_anomalies_from_influx(limit=limit)
    return [
        {
            "olt_name": r.get("olt_name"),
            "onu_serial": r.get("onu_serial"),
            "pon_port": r.get("pon_port"),
            "onu_index": r.get("onu_index"),
            "rx_power": r.get("value"),
            "anomaly_type": r.get("anomaly_type", "warning"),
            "description": r.get("description", ""),
            "time": r.get("time"),
        }
        for r in rows
    ]


@router.get("/summary")
async def get_anomaly_summary():
    """Resumen: totales 24h + ventana previa (%), series horarias y conteo de
    predicciones. Campos extra son aditivos (compatibles con el front actual)."""
    rows = get_anomalies_from_influx(hours=24, limit=1000)

    by_olt = {}
    critical = 0
    warning = 0
    for r in rows:
        olt = r.get("olt_name", "unknown")
        by_olt[olt] = by_olt.get(olt, 0) + 1
        if r.get("anomaly_type") == "critical":
            critical += 1
        else:
            warning += 1

    previous = get_anomaly_previous(hours=24)
    series = get_anomaly_series(hours=24)
    preds = predict_failures(hours=24, max_results=200)

    return {
        "total_anomalies": len(rows),
        "by_type": {
            "low_power": warning,
            "warning": warning,
            "critical": critical,
        },
        "by_olt": by_olt,
        "last_24h": len(rows),
        "previous": previous,
        "predictions": {"count": len(preds)},
        "series": series,
    }