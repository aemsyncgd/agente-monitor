# src/api/influx_helper.py
import os
import logging
from typing import Optional, List, Dict, Any
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

logger = logging.getLogger(__name__)

INFLUX_URL = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "vidanet")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "monitoreo")

_client: Optional[InfluxDBClient] = None
_query_api = None


def _get_query_api():
    global _client, _query_api
    if _query_api is None:
        _client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        _query_api = _client.query_api()
    return _query_api


def _query(flux: str) -> List[Dict[str, Any]]:
    try:
        api = _get_query_api()
        tables = api.query(flux, org=INFLUX_ORG)
        results = []
        for table in tables:
            for record in table:
                row = {"time": None}
                try:
                    row["time"] = record.get_time().isoformat() if record.get_time() else None
                except Exception:
                    pass
                try:
                    row["field"] = record.get_field()
                    row["value"] = record.get_value()
                except Exception:
                    pass
                if hasattr(record, "values") and isinstance(record.values, dict):
                    for k, v in record.values.items():
                        if k not in ("_time", "_field", "_value", "_measurement"):
                            if hasattr(v, "value"):
                                row[k] = v.value
                            else:
                                row[k] = v
                results.append(row)
        return results
    except Exception as e:
        logger.error(f"InfluxDB query failed: {e}")
        return []


def _check_connection() -> bool:
    try:
        api = _get_query_api()
        api.query(f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -1m) |> limit(n: 1)', org=INFLUX_ORG)
        return True
    except Exception:
        return False


def get_olt_summary() -> List[Dict[str, Any]]:
    """Get latest OLT system stats (one point per OLT).
    Usa una ventana amplia (-30d) para encontrar la ultima medicion de cada OLT
    y filtra unicamente los hostnames que actualmente existen en config/nodes.yaml.
    """
    from ..config_manager import get_config_manager
    manager = get_config_manager()
    valid_olts = {olt.hostname for olt in manager.get_all_olts()}

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30d)
    |> filter(fn: (r) => r["_measurement"] == "olt_system")
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["olt_name"])
    |> yield(name: "olt_system")
'''
    results = _query(flux)
    filtered = [r for r in results if r.get("olt_name") in valid_olts]
    filtered.sort(key=lambda r: r.get("olt_name", ""))
    return filtered


def get_interfaces(
    device_name: Optional[str] = None,
    minutes: int = 30,
) -> List[Dict[str, Any]]:
    """Get interface traffic metrics, latest point per interface.

    `minutes` controla la ventana de busqueda; las OLTs escriben cada ~15-20 min,
    por lo que se usa una ventana de 25 min para capturar siempre la ultima tasa.
    """
    device_filter = ""
    if device_name:
        device_filter = f'|> filter(fn: (r) => r["device_name"] == "{device_name}")'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
    {device_filter}
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> yield(name: "interface_traffic")
'''
    return _query(flux)


def get_olt_interfaces(device_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Interfaces de OLTs (GE/PON/ONU) con su ultima tasa rx/tx por interfaz.

    Ventana de 45m: el barrido OLT es paralelo y continuo (pausa de 1 min entre
    pasadas), pero una pasada completa puede estirarse con OLTs lentas; 45m
    cubre siempre al menos el ciclo anterior completo.
    """
    return get_interfaces(device_name=device_name, minutes=45)


def get_olt_power(
    olt_name: Optional[str] = None,
    online_only: bool = False,
    minutes: int = 45,
) -> List[Dict[str, Any]]:
    """Estado ONU (status, serial, rx/tx power) por OLT, ultima medicion por ONU."""
    filters = ""
    if olt_name:
        filters += f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'
    if online_only:
        filters += '|> filter(fn: (r) => r["status"] == 3)'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    {filters}
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> yield(name: "optical_power")
'''
    return _query(flux)


def get_optical_power(
    olt_name: Optional[str] = None,
    nodo: Optional[str] = None,
    online_only: bool = False,
) -> List[Dict[str, Any]]:
    """Get latest ONU optical power data."""
    filters = ""
    if olt_name:
        filters += f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'
    if nodo:
        filters += f'|> filter(fn: (r) => r["nodo"] == "{nodo}")'
    if online_only:
        filters += '|> filter(fn: (r) => r["status"] == "Online")'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    {filters}
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> yield(name: "optical_power")
'''
    return _query(flux)


def get_overall_stats() -> Dict[str, Any]:
    """Dashboard summary: totals and alert counts."""
    olt_flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "olt_system")
    |> last()
    |> group()
    |> count()
    |> yield(name: "olt_count")
'''
    onu_flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> last()
    |> group()
    |> count()
    |> yield(name: "onu_count")
'''
    onu_online_flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> filter(fn: (r) => r["status"] == "Online")
    |> last()
    |> group()
    |> count()
    |> yield(name: "onu_online")
'''
    iface_flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
    |> last()
    |> group()
    |> count()
    |> yield(name: "iface_count")
'''
    low_power_flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> filter(fn: (r) => r["status"] == "Online")
    |> last()
    |> filter(fn: (r) => r["_value"] < -28.0)
    |> group()
    |> count()
    |> yield(name: "low_power_alerts")
'''

    def _count(flux: str) -> int:
        rows = _query(flux)
        if rows and "value" in rows[0]:
            val = rows[0]["value"]
            return int(val) if val is not None else 0
        return 0

    total_olts = _count(olt_flux)
    total_onus = _count(onu_flux)
    online_onus = _count(onu_online_flux)
    total_interfaces = _count(iface_flux)
    low_power_alerts = _count(low_power_flux)

    return {
        "total_olts": total_olts,
        "total_onus": total_onus,
        "online_onus": online_onus,
        "offline_onus": total_onus - online_onus,
        "total_interfaces": total_interfaces,
        "low_power_alerts": low_power_alerts,
        "connection": "healthy" if _check_connection() else "unreachable",
    }


def get_recent_metrics(measurement: str, limit: int = 100, minutes: int = 60) -> List[Dict[str, Any]]:
    """Get the latest data point per device (or device+interface) from a measurement.

    `last()` runs before `pivot` so each series (tag set, e.g. one device or one
    device+interface) contributes a single row instead of one row per collection
    cycle, which used to flood the dashboard with duplicated devices.

    `minutes` controls the lookback window; use a short window (e.g. 15) for
    measurements whose series get dropped by filters so stale rows vanish quickly.
    """
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "{measurement}")
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["_time"], desc: true)
    |> limit(n: {limit})
    |> yield(name: "recent")
'''
    return _query(flux)


def get_client_locations(
    olt_name: Optional[str] = None,
    search: Optional[str] = None,
    hours: int = 168,
) -> List[Dict[str, Any]]:
    """Ultima ubicacion PON por MAC de cliente (medicion onu_location).

    Agrupa por el tag `mac` antes de `last()` para devolver una sola fila por
    cliente incluso si una reinstalacion creo una serie nueva (location es tag).
    El filtro `search` se aplica en Python (case-insensitive) para evitar
    inyeccion Flux; `olt_name` se aplica en el query.
    """
    olt_filter = ""
    if olt_name:
        olt_filter = f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "onu_location")
    {olt_filter}
    |> group(columns: ["mac"], mode: "by")
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["olt_name", "location"])
    |> yield(name: "onu_location")
'''
    rows = _query(flux)
    if search:
        needle = search.strip().lower()
        rows = [
            r for r in rows
            if needle in (r.get("mac") or "").lower()
            or needle in (r.get("onu_serial") or "").lower()
            or needle in (r.get("location") or "").lower()
        ]
    return rows


def get_active_device_ips(minutes: int = 30) -> dict:
    """Check which device IPs have recent data in InfluxDB.
    Returns dict: {ip: True} for devices with data in the last `minutes` minutes.
    Checks optical_power (olt_ip tag) and interface_traffic (device_ip tag).
    """
    active = {}
    # Check OLT IPs from optical_power
    flux_olt = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> distinct(column: "olt_ip")
    |> yield(name: "olt_ips")
'''
    for row in _query(flux_olt):
        ip = row.get("value") or row.get("olt_ip")
        if ip:
            active[str(ip)] = True

    # Check device IPs from interface_traffic
    flux_iface = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
    |> distinct(column: "device_ip")
    |> yield(name: "device_ips")
'''
    for row in _query(flux_iface):
        ip = row.get("value") or row.get("device_ip")
        if ip:
            active[str(ip)] = True

    return active


def get_interface_history(
    device_name: str,
    interface_names: List[str],
    minutes: int = 20,
    every: str = "30s",
) -> List[Dict[str, Any]]:
    """Serie temporal (media por ventana) de rx_bps/tx_bps para interfaces de un equipo.

    Devuelve una fila por {time, interface_name, rx_bps, tx_bps} para precargar
    gráficos en dashboards tipo TV sin esperar acumulación en el cliente.
    """
    if not interface_names:
        return []
    names_set = ", ".join(f'"{n}"' for n in interface_names)
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "mikrotik_interface")
    |> filter(fn: (r) => r["device_name"] == "{device_name}")
    |> filter(fn: (r) => r["_field"] == "rx_bps" or r["_field"] == "tx_bps")
    |> filter(fn: (r) => contains(value: r["interface_name"], set: [{names_set}]))
    |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> keep(columns: ["_time", "interface_name", "rx_bps", "tx_bps"])
    |> sort(columns: ["_time"])
'''
    return _query(flux)


def get_anomalies_from_influx(
    olt_name: Optional[str] = None,
    hours: int = 24,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Detect anomalies from optical power data: ONUs with low rx_power or status changes."""
    olt_filter = ""
    if olt_name:
        olt_filter = f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    {olt_filter}
    |> last()
    |> filter(fn: (r) => r["_value"] < -28.0)
    |> map(fn: (r) => ({{
        r with
        anomaly_type: if r["_value"] < -32.0 then "critical" else "warning",
        description: "Low optical power: " + string(v: r["_value"]) + " dBm"
    }}))
    |> limit(n: {limit})
    |> yield(name: "anomalies")
'''
    return _query(flux)
