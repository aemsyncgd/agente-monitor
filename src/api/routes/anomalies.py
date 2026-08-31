# src/api/routes/anomalies.py
from fastapi import APIRouter, Query
from typing import Optional

from ..influx_helper import get_anomalies_from_influx, get_optical_power

router = APIRouter()


@router.get("/")
async def get_anomalies(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    anomaly_type: Optional[str] = Query(None, description="Filter by type"),
    hours: int = Query(24, description="Hours of history"),
    limit: int = Query(100, description="Max results")
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
        if anomaly_type and a["anomaly_type"] != anomaly_type:
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
    hours: int = Query(24, description="Hours of history")
):
    """Get failure predictions - returns trend-based warnings from optical data."""
    onus = get_optical_power(olt_name=olt_name)
    offline = [o for o in onus if o.get("status") != "Online"]
    low_power = [o for o in onus if o.get("status") == "Online" and (o.get("value") or 0) < -28.0]

    predictions = []
    for o in low_power:
        predictions.append({
            "olt_name": o.get("olt_name"),
            "onu_serial": o.get("onu_serial"),
            "risk_level": "high" if (o.get("value") or 0) < -32.0 else "medium",
            "reason": f"Low RX power: {o.get('value')} dBm",
            "time": o.get("time"),
        })

    return {
        "predictions": predictions,
        "count": len(predictions),
        "filters": {"olt_name": olt_name, "hours": hours},
    }


@router.get("/recent")
async def get_recent_anomalies(limit: int = Query(20, description="Max results")):
    """Get recent anomalies for dashboard."""
    rows = get_anomalies_from_influx(limit=limit)
    return [
        {
            "olt_name": r.get("olt_name"),
            "onu_serial": r.get("onu_serial"),
            "pon_port": r.get("pon_port"),
            "rx_power": r.get("value"),
            "anomaly_type": r.get("anomaly_type", "warning"),
            "description": r.get("description", ""),
            "time": r.get("time"),
        }
        for r in rows
    ]


@router.get("/summary")
async def get_anomaly_summary():
    """Get anomaly summary statistics."""
    rows = get_anomalies_from_influx(hours=24, limit=1000)

    by_olt = {}
    total = len(rows)
    for r in rows:
        olt = r.get("olt_name", "unknown")
        by_olt[olt] = by_olt.get(olt, 0) + 1

    return {
        "total_anomalies": total,
        "by_type": {
            "low_power": sum(1 for r in rows if r.get("anomaly_type") == "warning"),
            "critical": sum(1 for r in rows if r.get("anomaly_type") == "critical"),
        },
        "by_olt": by_olt,
        "last_24h": total,
    }
