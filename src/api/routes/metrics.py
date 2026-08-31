# src/api/routes/metrics.py
from fastapi import APIRouter, Query
from typing import Optional

from ..influx_helper import (
    get_optical_power,
    get_olt_summary,
    get_interfaces,
    get_recent_metrics,
    get_olt_interfaces,
    get_olt_power,
    get_interface_history,
    get_client_locations,
)

router = APIRouter()


@router.get("/optical")
async def get_optical_power_endpoint(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    pon_port: Optional[str] = Query(None, description="Filter by PON port"),
    hours: int = Query(24, description="Hours of history")
):
    """Get optical power metrics for ONUs."""
    data = get_optical_power(olt_name=olt_name)
    return {
        "measurement": "optical_power",
        "filters": {"olt_name": olt_name, "pon_port": pon_port, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/olt")
async def get_olt_metrics(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    hours: int = Query(24, description="Hours of history")
):
    """Get OLT system metrics (CPU, memory, temperature)."""
    data = get_olt_summary()
    if olt_name:
        data = [r for r in data if r.get("olt_name") == olt_name]
    return {
        "measurement": "olt_system",
        "filters": {"olt_name": olt_name, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/interfaces")
async def get_interface_metrics(
    device_name: Optional[str] = Query(None, description="Filter by device"),
    interface_type: Optional[str] = Query(None, description="Filter by type: GE, PON, ONU"),
    hours: int = Query(24, description="Hours of history")
):
    """Get interface traffic metrics."""
    data = get_interfaces(device_name=device_name)
    if interface_type:
        data = [r for r in data if r.get("interface_type") == interface_type]
    return {
        "measurement": "interface_traffic",
        "filters": {"device_name": device_name, "interface_type": interface_type, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/clients")
async def get_client_locations_endpoint(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    search: Optional[str] = Query(None, description="Buscar por MAC, serial o ubicacion"),
    hours: int = Query(168, description="Ventana hacia atras en horas"),
):
    """Ubicacion PON por MAC de cliente (medicion onu_location), ultima por MAC."""
    data = get_client_locations(olt_name=olt_name, search=search, hours=hours)
    return {
        "measurement": "onu_location",
        "filters": {"olt_name": olt_name, "search": search, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/olt/interfaces")
async def get_olt_interface_metrics(
    device_name: Optional[str] = Query(None, description="Filter by OLT name (device_name)"),
    interface_type: Optional[str] = Query(None, description="Filter by type: GE, PON, ONU"),
):
    """Get OLT interface traffic (GE/PON/ONU) with latest rx/tx rate per interface.

    Ventana de 25 min para capturar la ultima tasa de cada interfaz (las OLTs
    escriben cada ~15-20 min por interfaz). `device_name` debe ser el olt_name
    (hostname) de la OLT para empatar con olt_system.
    """
    data = get_olt_interfaces(device_name=device_name)
    if interface_type:
        data = [r for r in data if r.get("interface_type") == interface_type]
    return {
        "measurement": "interface_traffic",
        "filters": {"device_name": device_name, "interface_type": interface_type},
        "count": len(data),
        "data": data,
    }


@router.get("/olt/power")
async def get_olt_power_metrics(
    olt_name: Optional[str] = Query(None, description="Filter by OLT name"),
    online_only: bool = Query(False, description="Solo ONUs online (status=3)"),
):
    """Get ONU optical power/status by OLT (latest per ONU)."""
    data = get_olt_power(olt_name=olt_name, online_only=online_only)
    return {
        "measurement": "optical_power",
        "filters": {"olt_name": olt_name, "online_only": online_only},
        "count": len(data),
        "data": data,
    }


@router.get("/mikrotik")
async def get_mikrotik_metrics(
    device_name: Optional[str] = Query(None, description="Filter by device"),
    hours: int = Query(24, description="Hours of history")
):
    """Get MikroTik router system metrics (CPU, memory, uptime, firmware)."""
    data = get_recent_metrics("mikrotik_system", limit=200)
    if device_name:
        data = [r for r in data if r.get("device_name") == device_name]
    return {
        "measurement": "mikrotik_system",
        "filters": {"device_name": device_name, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/mikrotik/temperature")
async def get_mikrotik_temperature(
    device_name: Optional[str] = Query(None, description="Filter by device"),
    hours: int = Query(24, description="Hours of history")
):
    """Get MikroTik temperature readings (CPU and board sensors)."""
    data = get_recent_metrics("mikrotik_temperature", limit=500)
    if device_name:
        data = [r for r in data if r.get("device_name") == device_name]
    return {
        "measurement": "mikrotik_temperature",
        "filters": {"device_name": device_name, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/mikrotik/cpu-cores")
async def get_mikrotik_cpu_cores(
    device_name: Optional[str] = Query(None, description="Filter by device"),
    hours: int = Query(24, description="Hours of history")
):
    """Get MikroTik per-core CPU utilization."""
    data = get_recent_metrics("mikrotik_cpu_core", limit=500)
    if device_name:
        data = [r for r in data if r.get("device_name") == device_name]
    return {
        "measurement": "mikrotik_cpu_core",
        "filters": {"device_name": device_name, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/mikrotik/interfaces")
async def get_mikrotik_interfaces(
    device_name: Optional[str] = Query(None, description="Filter by device"),
    hours: int = Query(24, description="Hours of history")
):
    """Get MikroTik interface metrics (traffic, errors, discards)."""
    data = get_recent_metrics("mikrotik_interface", limit=5000, minutes=15)
    if device_name:
        data = [r for r in data if r.get("device_name") == device_name]
    return {
        "measurement": "mikrotik_interface",
        "filters": {"device_name": device_name, "hours": hours},
        "count": len(data),
        "data": data,
    }


@router.get("/mikrotik/interfaces/history")
async def get_mikrotik_interfaces_history(
    device_name: str = Query("VIDANET-BACKBONE", description="Equipo a consultar"),
    interfaces: Optional[str] = Query(None, description="Nombres de interfaz separados por coma"),
    minutes: int = Query(20, ge=1, le=180, description="Ventana hacia atrás en minutos"),
    every: str = Query("30s", pattern=r"^\d+[smh]$", description="Ventana de agregación"),
):
    """Serie temporal de rx_bps/tx_bps por interfaz para precargar gráficos."""
    names = [i.strip() for i in interfaces.split(",") if i.strip()] if interfaces else []
    data = get_interface_history(device_name, names, minutes=minutes, every=every)
    return {
        "measurement": "mikrotik_interface",
        "filters": {"device_name": device_name, "interfaces": names,
                    "minutes": minutes, "every": every},
        "count": len(data),
        "data": data,
    }
