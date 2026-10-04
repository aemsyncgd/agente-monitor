# src/api/routes/traffic.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Query

from ...config_manager import get_config_manager
from .. import influx_helper as ih
from ..snmp_live import LiveTrafficReader

router = APIRouter()
READER = LiveTrafficReader()

# Executor propio para lecturas SNMP bajo demanda: no comparte el pool por
# defecto con las queries síncronas de InfluxDB, así el poll del modal no
# espera detrás de ellas.
_SNMP_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="snmp-live")

DAILY_HOURS = 168  # consumo diario: ultimos 7 dias


@router.get("/api/v1/traffic/live")
async def traffic_live(
    device_type: str = Query(...),
    device_ip: str = Query(...),
    interface_name: str = Query(...),
    interface_index: str = Query(None),
):
    """Tráfico en tiempo real (SNMP bajo demanda, no persistido)."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _SNMP_EXECUTOR, READER.read, device_type, device_ip, interface_name, interface_index
        )
    except Exception:
        raise HTTPException(status_code=500, detail="error_lectura_snmp")
    if not result.get("ok"):
        error = result.get("error")
        if error == "dispositivo_no_encontrado":
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=502, detail=error)
    return result


@router.get("/api/v1/traffic/history")
async def traffic_history(
    device_type: str = Query(...),
    device_ip: str = Query(...),
    device_name: str = Query(...),
    interface_name: str = Query(...),
    range_key: str = Query("1sem"),
):
    """Serie histórica + consumo diario desde InfluxDB (todos los rangos)."""
    if range_key not in ih.TRAFFIC_RANGES:
        raise HTTPException(status_code=400, detail="rango_invalido")
    manager = get_config_manager()
    if device_type == "olt":
        device = manager.get_olt_by_ip(device_ip)
    else:
        device = manager.get_mikrotik_by_ip(device_ip)
    if device is None:
        raise HTTPException(status_code=404, detail="dispositivo_no_encontrado")
    try:
        points, daily = await asyncio.gather(
            ih.run_influx(ih.get_traffic_history, device_type, device_name, interface_name, range_key),
            ih.run_influx(ih.get_daily_consumption, device_type, device_name, interface_name, DAILY_HOURS),
        )
    except Exception:
        raise HTTPException(status_code=500, detail="error_influx")
    return {"range": range_key, "points": points, "daily": daily}