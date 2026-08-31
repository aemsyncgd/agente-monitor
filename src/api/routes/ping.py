# src/api/routes/ping.py
"""
API Routes - Ping Monitor status.
Returns current ICMP ping status for all configured targets with live InfluxDB data.
"""

import os
import yaml
from pathlib import Path
from fastapi import APIRouter
from typing import List, Dict, Any

from ..influx_helper import _query

router = APIRouter(prefix="/api/ping", tags=["ping"])


@router.get("/status")
async def get_ping_status():
    """Get current ping status for all targets with live data from InfluxDB."""
    targets_path = os.environ.get(
        "PING_TARGETS_PATH",
        str(Path(__file__).parent.parent.parent.parent / "config" / "ping_targets.yaml")
    )

    if not os.path.exists(targets_path):
        return {"targets": [], "error": "ping_targets.yaml not found"}

    with open(targets_path) as f:
        data = yaml.safe_load(f)

    # Load target configs
    target_map = {}
    for t in data.get("targets", []):
        target_map[t["name"]] = {
            "name": t["name"],
            "ip": t["ip"],
            "type": t.get("type", "unknown"),
            "enabled": t.get("enabled", True),
            "status": -1,  # unknown
            "latency": 0.0,
            "loss": 0,
        }

    # Query InfluxDB for live data
    try:
        flux = '''
        from(bucket: "monitoreo")
            |> range(start: -30m)
            |> filter(fn: (r) => r["_measurement"] == "ping_check")
            |> last()
        '''
        results = _query(flux)

        for point in results:
            name = point.get("name", "")
            field = point.get("field", "")
            value = point.get("value", 0)

            if name in target_map:
                if field == "status":
                    target_map[name]["status"] = int(value)
                elif field == "latency_ms_avg":
                    target_map[name]["latency"] = float(value)
                elif field == "packet_loss_pct":
                    target_map[name]["loss"] = int(value)
    except Exception as e:
        pass  # Return config-only data if InfluxDB fails

    targets = list(target_map.values())
    up = sum(1 for t in targets if t["status"] == 1)
    down = sum(1 for t in targets if t["status"] == 0)

    return {"targets": targets, "count": len(targets), "up": up, "down": down}


@router.get("/targets")
async def get_ping_targets():
    """List all ping targets from config."""
    targets_path = os.environ.get(
        "PING_TARGETS_PATH",
        str(Path(__file__).parent.parent.parent.parent / "config" / "ping_targets.yaml")
    )

    if not os.path.exists(targets_path):
        return {"targets": []}

    with open(targets_path) as f:
        data = yaml.safe_load(f)

    return {"targets": data.get("targets", [])}
