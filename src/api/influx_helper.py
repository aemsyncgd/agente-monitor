# src/api/influx_helper.py
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import logging
import re
import threading
import time
from typing import Optional, List, Dict, Any
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

logger = logging.getLogger(__name__)

INFLUX_URL = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "vidanet")
INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "monitoreo")

# Caché TTL para las consultas pesadas "todas las OLTs" del dashboard.
# Evita que el refresco periodico del front (~30s) y la apertura del detalle
# disparen queries de varios MB contra InfluxDB de forma simultanea.
_DASH_TTL = 45.0
_DASH_CACHE: Dict[str, Any] = {}
_DASH_LOCK = threading.Lock()
# Single-flight por clave: cuando varias keys expiran a la vez y el front pide
# las ~7 rutas pesadas en paralelo, solo una query por clave corre contra
# InfluxDB y el resto espera el resultado, en vez de saturar el motor con
# queries simultáneas (causa de los "context canceled" en el log de influxd).
_DASH_INFLIGHT: Dict[str, threading.Event] = {}

# Executor dedicado para queries síncronas de InfluxDB. Influx puede quedar muy
# lento (chunked reads de decenas de segundos); aislarlo del pool por defecto
# evita que esas lecturas saturen el executor compartido y cuelguen a los demás
# to_thread (SNMP, etc.) o al propio event loop via cola del pool.
_INFLUX_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="influx"
)
_INFLUX_TIMEOUT = 20.0


async def run_influx(func, *args, _timeout: float = _INFLUX_TIMEOUT):
    """Ejecuta una función síncrona de InfluxDB en su executor dedicado con
    cota dura: si Influx tarda más que `_timeout`, el request devuelve a tiempo
    (el hilo queda huérfano hasta que Influx libere la lectura, autolimitado por
    el `timeout` del cliente)."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(_INFLUX_EXECUTOR, func, *args), timeout=_timeout
    )


def _dash_cache(key: str, loader, *args):
    now = time.monotonic()
    with _DASH_LOCK:
        hit = _DASH_CACHE.get(key)
        if hit is not None and now - hit[0] < _DASH_TTL:
            return hit[1]
        # re-chequeo: otro hilo pudo completar mientras esperabamos el lock
        hit = _DASH_CACHE.get(key)
        if hit is not None and now - hit[0] < _DASH_TTL:
            return hit[1]
        event = _DASH_INFLIGHT.get(key)
        if event is None:
            event = threading.Event()
            _DASH_INFLIGHT[key] = event
            is_leader = True
        else:
            is_leader = False

    if is_leader:
        try:
            data = loader(*args)
            with _DASH_LOCK:
                _DASH_CACHE[key] = (time.monotonic(), data)
                expired = [k for k, v in _DASH_CACHE.items()
                           if time.monotonic() - v[0] >= _DASH_TTL]
                for k in expired:
                    _DASH_CACHE.pop(k, None)
            return data
        finally:
            event.set()
            with _DASH_LOCK:
                _DASH_INFLIGHT.pop(key, None)

    # seguidor: espera a que el lider publique el resultado (o falle)
    event.wait(timeout=_INFLUX_TIMEOUT + 5)
    with _DASH_LOCK:
        hit = _DASH_CACHE.get(key)
        if hit is not None and time.monotonic() - hit[0] < _DASH_TTL:
            return hit[1]
    raise RuntimeError(f"cache load failed/stale for {key}")

_client: Optional[InfluxDBClient] = None
_query_api = None

_status_window_minutes: int = 3  # default, overridden at startup

def set_status_window(minutes: int):
    global _status_window_minutes
    _status_window_minutes = minutes

def get_status_window() -> int:
    return _status_window_minutes


def _get_query_api():
    global _client, _query_api
    if _query_api is None:
        # timeout: limite de ms para conectar/leer de InfluxDB. Una respuesta
        # chunked que se corta no debe cuelgar el hilo por tiempo indefinido.
        _client = InfluxDBClient(
            url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=30000
        )
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
                except Exception:
                    pass
                try:
                    # get_field() lanza si la consulta es agregada (count, ...);
                    # get_value() sí existe -> no encadenarlos en un solo try.
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


def resolve_interface_index(device_name: str, interface_name: str) -> Optional[str]:
    """Devuelve el tag interface_index (ifIndex) de una interfaz de OLT usando
    los datos que ya muestrea `olt_traffic_history` (evita reandar SNMP).

    None si no hay datos aún (por ejemplo mientras el sampler llena la base).
    """
    if not device_name or not interface_name:
        return None
    esc_device = str(device_name).replace("\\", "\\\\").replace('"', '\\"')
    esc_iface = str(interface_name).replace("\\", "\\\\").replace('"', '\\"')
    flux = (
        f'from(bucket: "{INFLUX_BUCKET}")'
        f' |> range(start: -168h)'
        f' |> filter(fn: (r) => r["_measurement"] == "olt_traffic_history")'
        f' |> filter(fn: (r) => r["_field"] == "rx_octets")'
        f' |> filter(fn: (r) => r["olt_name"] == "{esc_device}")'
        f' |> filter(fn: (r) => r["interface_name"] == "{esc_iface}")'
        f' |> group()'
        f' |> last()'
        f' |> keep(columns: ["interface_index"])'
    )
    rows = _query(flux)
    for r in rows:
        val = r.get("interface_index")
        if val is not None and str(val) not in ("", "None"):
            return str(val)
    return None


_HEALTH_CHECK_TTL = 10.0
_health_check_cache = {"t": 0.0, "ok": True}


def _check_connection() -> bool:
    """Ping real a InfluxDB, cacheado ~10s para que el polling de /health (~30s)
    no dispare una query contra InfluxDB en cada request."""
    now = time.monotonic()
    with _DASH_LOCK:
        if now - _health_check_cache["t"] < _HEALTH_CHECK_TTL:
            return _health_check_cache["ok"]
    ok = False
    try:
        api = _get_query_api()
        api.query(
            f'from(bucket: "{INFLUX_BUCKET}") |> range(start: -1m) |> limit(n: 1)',
            org=INFLUX_ORG,
        )
        ok = True
    except Exception:
        ok = False
    with _DASH_LOCK:
        _health_check_cache["t"] = time.monotonic()
        _health_check_cache["ok"] = ok
    return ok


_OLT_COMPLETENESS_FIELDS = (
    "cpu", "memory", "temperature", "uptime", "online_onus", "total_onus",
    "sys_name", "sys_desc",
)


def _row_completeness(row: Dict[str, Any]) -> int:
    """Cuantos campos utiles trae la fila (para elegir la serie mas completa)."""
    score = 0
    for k in _OLT_COMPLETENESS_FIELDS:
        v = row.get(k)
        if v is not None and v != "" and v != "None":
            score += 1
    return score


def _collapse_olt_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Una sola fila por OLT.

    InfluxDB puede tener varias series para una misma OLT en `olt_system`
    (ej. una serie legada de un collector anterior con esquema distinto:
    sin cpu/memoria/temperatura). `last()` agrupa por serie, asi que la
    consulta devuelve una fila por serie -> la OLT aparece duplicada en el
    dashboard (una tarjeta con datos y otra con ceros). Aqui se colapsa
    quedandose con la fila mas completa y, en empate, la mas reciente.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        name = r.get("olt_name")
        if not name:
            continue
        key = str(name)
        prev = best.get(key)
        cur_score = _row_completeness(r)
        prev_score = _row_completeness(prev) if prev is not None else -1
        if prev is None or cur_score > prev_score:
            best[key] = r
        elif cur_score == prev_score and (r.get("time") or "") > (prev.get("time") or ""):
            best[key] = r
    return list(best.values())


def get_olt_summary() -> List[Dict[str, Any]]:
    """Get latest OLT system stats (one point per OLT).
    Usa una ventana amplia (-30d) para encontrar la ultima medicion de cada OLT
    y filtra unicamente los hostnames que actualmente existen en config/nodes.yaml.
    Colapsa series duplicadas (una fila por OLT).
    """
    return _collapse_olt_rows(_dash_cache("olt_summary", _load_olt_summary))


def _load_olt_summary() -> List[Dict[str, Any]]:
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

    Se traen `rx_bps`/`tx_bps` (los que consumen los dashboards de tasa) mas
    `ifOperStatus`, del que depende el modal de NODOS para pintar Up/Down en las
    interfaces PON. Omitirlo hace que todos los PON aparezcan Down.
    """
    device_filter = ""
    if device_name:
        device_filter = f'|> filter(fn: (r) => r["device_name"] == "{device_name}")'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
    {device_filter}
    |> filter(fn: (r) => r["_field"] == "rx_bps" or r["_field"] == "tx_bps" or r["_field"] == "ifOperStatus")
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
    if device_name is None:
        return _dash_cache("olt_interfaces", lambda: get_interfaces(device_name=None, minutes=45))
    return get_interfaces(device_name=device_name, minutes=45)


def get_olt_power(
    olt_name: Optional[str] = None,
    online_only: bool = False,
    minutes: int = 45,
) -> List[Dict[str, Any]]:
    """Estado ONU (status, serial, rx/tx power) por OLT, ultima medicion por ONU.

    Una fila por ONU: el pivot es por identidad de ONU y no por `_time` porque
    el estado y la potencia de una misma ONU se escriben en instantes distintos
    (fase ONUs y fase optica). Pivotando por `_time` cada ONU se partia en dos
    filas y el modal mostraba "—" justamente en la que trae el estado.
    """
    if olt_name is None and not online_only:
        return _dash_cache("olt_power", _query_olt_power, olt_name, online_only, minutes)
    return _query_olt_power(olt_name, online_only, minutes)


def _query_olt_power(
    olt_name: Optional[str],
    online_only: bool,
    minutes: int,
) -> List[Dict[str, Any]]:
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
    |> pivot(rowKey: ["olt_name", "onu_index"], columnKey: ["_field"], valueColumn: "_value")
    |> yield(name: "optical_power")
'''
    return _query(flux)


def get_optical_power(
    olt_name: Optional[str] = None,
    nodo: Optional[str] = None,
    online_only: bool = False,
) -> List[Dict[str, Any]]:
    """Get latest ONU optical power data.

    El caso "todas las OLTs" (dashboard Resumen: Estado de ONUs y Distribucion de
    Potencia Optica) es una query pesada sobre todos los nodos: se cachea igual
    que get_olt_power para no repetirla en cada refresco del front.
    """
    if olt_name is None and nodo is None and not online_only:
        return _dash_cache(
            "optical_power", _query_optical_power, None, None, False
        )
    return _query_optical_power(olt_name, nodo, online_only)


def _query_optical_power(
    olt_name: Optional[str],
    nodo: Optional[str],
    online_only: bool,
) -> List[Dict[str, Any]]:
    filters = ""
    if olt_name:
        filters += f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'
    if nodo:
        filters += f'|> filter(fn: (r) => r["nodo"] == "{nodo}")'
    if online_only:
        filters += '|> filter(fn: (r) => r["status"] == 3)'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -30m)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    {filters}
    |> last()
    |> pivot(rowKey: ["olt_name", "onu_index"], columnKey: ["_field"], valueColumn: "_value")
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


def _client_kw_match(row: Dict[str, Any], needle: str) -> bool:
    """Match full-text case-insensitive sobre los campos clave de un cliente.

    AND por tokens: "PEREZ JUAN" encuentra "Juan Perez Diaz" aunque no
    aparezcan contiguos (el substring simple nunca matcheaba eso).
    """
    tokens = needle.split()
    haystack = " ".join(
        str(row.get(f) or "").lower()
        for f in ("nombre", "documento", "direccion", "serial_onu",
                  "usuario_pppoe", "mac", "olt", "nap", "zona")
    )
    return all(t in haystack for t in tokens)


def _row_ts(value: Any) -> float:
    """Timestamp comparable (epoch) de un `time` ISO de Influx."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return 0.0


def get_enriched_clients(
    search: Optional[str] = None,
    olt: Optional[str] = None,
    nap: Optional[str] = None,
    zona: Optional[str] = None,
    pon: Optional[str] = None,
    limit: int = 500,
    hours: int = 168,
) -> List[Dict[str, Any]]:
    """Clientes enriquecidos (clientes_enriquecidos), ultima fila por cliente.

    `last()` por serie + merge en Python agrupando por serial/mac/documento.
    El pivot original (`group+last+pivot`) colisionaba por tipos mixtos
    (`estado_onu` integer vs strings) y devolvia 0 filas con ~14s de latencia.

    Filtros de tag (olt/nap/zona y search-identificador) se empujan a Flux:
    Influx los resuelve por indice de serie (~0.05s) en vez de escanear toda
    la medicion (~80s). `search` con espacios (nombre) y `pon` se filtran en
    Python (evita inyeccion Flux y no son tags).
    """
    def _tag_re(value) -> str:
        return f'/(?i)^{re.escape(str(value).strip())}$/'

    tag_filters = ""
    for col, val in (("olt", olt), ("nap", nap), ("zona", zona)):
        if val:
            tag_filters += (f'|> filter(fn: (r) => exists r["{col}"] and '
                            f'r["{col}"] =~ {_tag_re(val)})\n')
    needle = (search or "").strip()
    if needle and " " not in needle:
        # Identificador (serial/doc/mac/pppoe): filtro de tag indexado.
        pat = _tag_re(needle)
        tag_filters += (
            '|> filter(fn: (r) =>'
            f' (exists r["serial_onu"] and r["serial_onu"] =~ {pat}) or'
            f' (exists r["documento"] and r["documento"] =~ {pat}) or'
            f' (exists r["mac"] and r["mac"] =~ {pat}) or'
            f' (exists r["usuario_pppoe"] and r["usuario_pppoe"] =~ {pat}))\n'
        )

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")
    {tag_filters}
    |> last()
    |> yield(name: "clientes_enriquecidos")
'''
    records = _query(flux)
    merged: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        key = (str(rec.get("serial_onu") or "")
               or str(rec.get("mac") or "")
               or str(rec.get("documento") or "")
               or str(rec.get("usuario_pppoe") or ""))
        if not key:
            key = "|".join(
                f"{k}={v}" for k, v in sorted(
                    (k, v) for k, v in rec.items()
                    if k not in ("time", "field", "value")))
        ts = _row_ts(rec.get("time"))
        row = merged.get(key)
        if row is None:
            row = {"_ts": 0.0, "_fts": {}}
            merged[key] = row
        fname = str(rec.get("field") or "")
        if fname and rec.get("value") is not None and ts >= row["_fts"].get(fname, -1.0):
            row[fname] = rec.get("value")
            row["_fts"][fname] = ts
        if ts >= row["_ts"]:
            # Un null de tag (serie sin esa columna) nunca debe tapar un
            # valor real de otra variante de serie del mismo cliente.
            for k, v in rec.items():
                if k not in ("time", "field", "value") and v is not None:
                    row[k] = v
            row["_ts"] = ts

    rows: List[Dict[str, Any]] = []
    for row in merged.values():
        row.pop("_fts", None)
        ts = row.pop("_ts", None)
        row["time"] = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                       if ts else None)
        rows.append(row)

    if olt:
        rows = [r for r in rows if (r.get("olt") or "") == olt]
    if nap:
        rows = [r for r in rows if (r.get("nap") or "") == nap]
    if zona:
        rows = [r for r in rows if (r.get("zona") or "") == zona]
    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if _client_kw_match(r, needle)]
    if pon:
        needle_pon = str(pon).strip().lower()
        rows = [r for r in rows if needle_pon in (r.get("pon_port") or "").lower()]
    rows.sort(key=lambda r: (str(r.get("olt") or ""), str(r.get("nap") or "")))
    return rows[:limit]


def get_client_by_documento(documento: str, hours: int = 168) -> Dict[str, Any]:
    """Ficha enriquecida por documento (clientes_enriquecidos, ultima fila)."""
    esc = str(documento).replace("\\", "\\\\").replace('"', '\\"')
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")
    |> filter(fn: (r) => r["documento"] == "{esc}")
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["_time"], desc: true)
    |> limit(n: 1)
    |> yield(name: "ficha")
'''
    rows = _query(flux)
    return rows[0] if rows else {}


def get_pending_clients(hours: int = 168, limit: int = 500) -> List[Dict[str, Any]]:
    """Clientes pendientes (clientes_pendientes), ultima fila por documento+causa."""
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "clientes_pendientes")
    |> group(columns: ["documento", "causa"], mode: "by")
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> yield(name: "clientes_pendientes")
'''
    rows = _query(flux)
    return rows[:limit]


def get_pending_by_documento(documento: str, hours: int = 168) -> Dict[str, Any]:
    """Ficha de un cliente pendiente por documento (clientes_pendientes)."""
    esc = str(documento).replace("\\", "\\\\").replace('"', '\\"')
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "clientes_pendientes")
    |> filter(fn: (r) => r["documento"] == "{esc}")
    |> last()
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["_time"], desc: true)
    |> limit(n: 1)
    |> yield(name: "pendiente")
'''
    rows = _query(flux)
    return rows[0] if rows else {}


def get_pending_filtered(search: Optional[str] = None,
                         nap: Optional[str] = None,
                         zona: Optional[str] = None,
                         limit: int = 500,
                         hours: int = 168) -> List[Dict[str, Any]]:
    """Pendientes normalizados al shape de enriquecidos y filtrados.

    Los pendientes se marcan con `pendiente: "true"`; `olt` no aplica (una
    fila pendiente todavia no cruza ONU), solo nap/zona/search.
    """
    if limit <= 0:
        return []
    pend_rows = get_pending_clients(hours=hours, limit=limit)
    needle = (search or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for p in pend_rows:
        normalized = {
            "documento": p.get("documento", ""),
            "usuario_pppoe": p.get("usuario_pppoe", ""),
            "serial_onu": p.get("serial_onu", ""),
            "mac": "",
            "nombre": p.get("nombre", ""),
            "direccion": "",
            "olt": "",
            "nodo": "",
            "nap": p.get("nap", ""),
            "zona": p.get("zona", ""),
            "estado_onu": -1,
            "coincidencia": "",
            "ubicacion_descuadrada": "false",
            "telefono": "",
            "time": p.get("time"),
            "causa": p.get("causa", ""),
            "pendiente": "true",
            "estado_servicio": "",
            "pppoe_status": "",
        }
        if nap and (normalized["nap"] or "").lower() != str(nap).strip().lower():
            continue
        if zona and (normalized["zona"] or "").lower() != str(zona).lower():
            continue
        if needle and not _client_kw_match(normalized, needle):
            continue
        out.append(normalized)
        if len(out) >= limit:
            break
    return out


def search_clientes(
    search: Optional[str] = None,
    olt: Optional[str] = None,
    nap: Optional[str] = None,
    zona: Optional[str] = None,
    limit: int = 500,
    hours: int = 168,
    include_pending: bool = True,
) -> List[Dict[str, Any]]:
    """Busqueda unificada para el dashboard: enriquecidos + pendientes.

    Los pendientes se normalizan al mismo shape (claves vacias donde no aplica),
    se marcan con `pendiente: "true"` y conservan su `causa`. Los enriquecidos
    tienen prioridad: si el limite se llena con ellos, los pendientes no entran.
    """
    rows = get_enriched_clients(
        search=search, olt=olt, nap=nap, zona=zona, limit=limit, hours=hours,
    )
    if include_pending and len(rows) < limit:
        rows.extend(get_pending_filtered(
            search=search, nap=nap, zona=zona,
            limit=limit - len(rows), hours=hours,
        ))
    return rows[:limit]


def get_clientes_stats(hours: int = 168) -> Dict[str, Any]:
    """Totales de enriquecidos / pendientes / descuadrados para el panel.

    Cuenta clientes DISTINTOS (group+last por clave), no puntos: el conteo
    plano de puntos devolia ~1.2M porque cada cliente se re-escribe cada
    ciclo. Las tres consultas corren en paralelo (~4s en total).
    """
    def _count_distinct(group_key: str, measurement: str,
                        extra: str = "") -> int:
        flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "{measurement}")
    {extra}
    |> group(columns: ["{group_key}"])
    |> last()
    |> group()
    |> count()
    |> yield(name: "count")
'''
        rows = _query(flux)
        if rows and rows[0].get("value") is not None:
            try:
                return int(rows[0]["value"])
            except (TypeError, ValueError):
                return 0
        return 0

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_total = pool.submit(
            _count_distinct, "serial_onu", "clientes_enriquecidos",
            '|> filter(fn: (r) => r["_field"] == "estado_onu")\n')
        fut_desc = pool.submit(
            _count_distinct, "serial_onu", "clientes_enriquecidos",
            '|> filter(fn: (r) => r["_field"] == "estado_onu")\n'
            '|> filter(fn: (r) => r["ubicacion_descuadrada"] == "true")\n')
        fut_pend = pool.submit(
            _count_distinct, "documento", "clientes_pendientes",
            '|> filter(fn: (r) => r["_field"] == "nombre")\n')
        total = fut_total.result()
        descuadrados = fut_desc.result()
        pendientes = fut_pend.result()
    return {
        "enriquecidos": total,
        "descuadrados": descuadrados,
        "pendientes": pendientes,
    }


PIPELINE_MEASUREMENTS = (
    ("clientes_enriquecidos", "Enriquecimiento clientes", 30, "estado_onu"),
    ("clientes_pendientes", "Clientes pendientes", 30, "nombre"),
    ("optical_power", "Potencia óptica (OLT)", 30, "rx_power"),
    ("onu_mac", "MAC ONU (OLT)", 30, "learned"),
    ("onu_location", "Ubicación PON (OLT)", 60, "serial_onu"),
    ("routeros_pppoe", "Sesiones PPPoE (RouterOS)", 60, "user"),
    ("routeros_dhcp", "Leases DHCP (RouterOS)", 60, "address"),
    ("wisp_clients", "Clientes WISP", 60, "doc"),
)


def get_pipeline_status(hours: int = 24, lookback_hours: int = 168) -> Dict[str, Any]:
    """Regresa la frescura de escritura por medicion clave del pipeline (T0.2).

    Para cada medicion reporta la fecha de la ultima escritura (`last_write`),
    cuantos puntos se escribieron en las ultimas `hours` horas (`count_24h`) y
    si esta `stale` (sin escritura reciente segun el umbral de cada fuente).
    NO marca nada como falla grave: expone el hecho para que el dashboard lo
    muestre tal cual (p. ej. routeros_pppoe quedara 'sin datos' mientras no se
    configuren credenciales).
    """
    def _last_write(measurement: str, field: str) -> Optional[str]:
        # Filtra a UN campo representativo: del mismo tipo en toda la medicion,
        # asi `group() |> max()` no tropieza con "schema collision".
        rows = _query(f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{lookback_hours}h)
    |> filter(fn: (r) => r["_measurement"] == "{measurement}")
    |> filter(fn: (r) => r["_field"] == "{field}")
    |> last()
    |> group()
    |> max(column: "_time")
''')
        for r in rows:
            t = r.get("time")
            if t:
                return t
        return None

    def _count_24h(measurement: str, field: str) -> int:
        rows = _query(f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "{measurement}")
    |> filter(fn: (r) => r["_field"] == "{field}")
    |> count()
''')
        total = 0
        for r in rows:
            try:
                total += int(r["value"])
            except (KeyError, TypeError, ValueError):
                continue
        return total

    def _loader() -> Dict[str, Any]:
        res = []
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
        except Exception:
            now = None
        for measurement, label, fresh_min, rep_field in PIPELINE_MEASUREMENTS:
            last_write = _last_write(measurement, rep_field)
            count = _count_24h(measurement, rep_field)
            stale = False
            hours_ago = None
            if last_write and now is not None:
                try:
                    dt = datetime.fromisoformat(last_write)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    hours_ago = max(0.0, (now - dt).total_seconds() / 3600.0)
                    stale = (now - dt).total_seconds() > fresh_min * 60
                except Exception:
                    stale = False
            res.append({
                "measurement": measurement,
                "label": label,
                "last_write": last_write,
                "count_24h": count,
                "fresh_after_minutes": fresh_min,
                "hours_ago": round(hours_ago, 2) if hours_ago is not None else None,
                "stale": stale,
            })

        # Medición sintetica: cache Redis de fichas (Fase 2). Sin Redis -> "sin datos".
        try:
            from ..redis_store import get_client_store
            meta = get_client_store().meta()
            built = meta.get("built_at")
            fresh_min = 30
            stale = False
            hours_ago = None
            if built and now is not None:
                try:
                    built_dt = datetime.fromisoformat(str(built).replace("Z", "+00:00"))
                    if built_dt.tzinfo is None:
                        built_dt = built_dt.replace(tzinfo=timezone.utc)
                    hours_ago = max(0.0, (now - built_dt).total_seconds() / 3600.0)
                    stale = (now - built_dt).total_seconds() > fresh_min * 60
                except Exception:
                    stale = False
            res.append({
                "measurement": "redis_cache",
                "label": "Cache Redis (fichas)",
                "last_write": built,
                "count_24h": meta.get("count", 0),
                "fresh_after_minutes": fresh_min,
                "hours_ago": round(hours_ago, 2) if hours_ago is not None else None,
                "stale": stale,
            })
        except Exception:
            pass

        return {
            "as_of": now.isoformat() if now else None,
            "measurements": res,
        }

    return _dash_cache("pipeline_status", _loader)


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

    Agrupa por el tag `mac` + `_field` antes de `last()` para devolver la ultima
    fila por campo incluso si una reinstalacion creo una serie nueva (location es
    tag), y pivota conservando los tags en el rowKey. Nota: la medicion mezcla
    campos int (vlan, estado_onu) y string (onu_serial, ...), por lo que el pivot
    debe hacerse con las series ya separadas por `_field`; agrupar solo por `mac`
    provoca "schema collision: cannot group integer and string types together".
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
    |> group(columns: ["mac", "_field"], mode: "by")
    |> last()
    |> pivot(rowKey: ["mac", "location", "olt_name", "nodo", "olt_ip"], columnKey: ["_field"], valueColumn: "_value")
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


def get_active_device_ips(minutes: int = None) -> dict:
    """IPs con actividad reciente en InfluxDB (4 mediciones). Query pesada que
    el dashboard pide cada ~30s (devices/all, /nodes): se cachea por ventana."""
    if minutes is None:
        minutes = get_status_window()
    return _dash_cache(f"active_ips_{minutes}", _query_active_device_ips, minutes)


def _query_active_device_ips(minutes: int) -> dict:
    """Check which device IPs have recent online activity in InfluxDB within `minutes` lookback.
    Returns dict: {ip: True} for devices active in the last `minutes` minutes.
    Checks:
    1. ping_check where status == 1
    2. mikrotik_system
    3. optical_power (olt_ip tag)
    4. interface_traffic (device_ip tag)
    """
    active = {}
    
    # Check successful pings (status == 1)
    flux_ping = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "ping_check")
    |> filter(fn: (r) => r["_field"] == "status")
    |> filter(fn: (r) => r["_value"] == 1)
    |> distinct(column: "ip")
    |> yield(name: "ping_ips")
'''
    for row in _query(flux_ping):
        ip = row.get("value") or row.get("ip")
        if ip:
            active[str(ip)] = True

    # Check MikroTik system metrics
    flux_mk = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{minutes}m)
    |> filter(fn: (r) => r["_measurement"] == "mikrotik_system")
    |> distinct(column: "device_ip")
    |> yield(name: "mk_ips")
'''
    for row in _query(flux_mk):
        ip = row.get("value") or row.get("device_ip")
        if ip:
            active[str(ip)] = True

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


TRAFFIC_RANGES = {
    "1d": (24, "30s"),
    "1sem": (168, "2m"),
    "1mes": (720, "5m"),
    "3m": (2160, "15m"),
    "6m": (4320, "30m"),
    "9m": (6480, "1h"),
    "1año": (8760, "2h"),
}


def history_params(range_key: str):
    """Mapea el rango del UI (1d/1sem/1mes/3m/6m/9m/1año) a (horas, every)."""
    return TRAFFIC_RANGES.get((range_key or "").lower(), (24, "30s"))


def _traffic_measurement(device_type: str) -> tuple:
    """(measurement, tag_de_nombre, rx_field, tx_field) segun tipo de equipo."""
    if (device_type or "").lower() == "olt":
        return "olt_traffic_history", "olt_name", "rx_octets", "tx_octets"
    return "mikrotik_interface", "device_name", "ifHCInOctets", "ifHCOutOctets"


def get_traffic_history(
    device_type: str,
    device_name: str,
    interface_name: str,
    range_key: str = "1d",
) -> List[Dict[str, Any]]:
    """Serie temporal agregada de rx_bps/tx_bps para una interfaz.

    Misma forma de respuesta para OLTs (`olt_traffic_history`) y MikroTik
    (`mikrotik_interface`).
    """
    hours, every = history_params(range_key)
    measurement, name_tag, _, _ = _traffic_measurement(device_type)
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "{measurement}")
    |> filter(fn: (r) => r["{name_tag}"] == "{device_name}")
    |> filter(fn: (r) => r["interface_name"] == "{interface_name}")
    |> filter(fn: (r) => r["_field"] == "rx_bps" or r["_field"] == "tx_bps")
    |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> keep(columns: ["_time", "rx_bps", "tx_bps"])
    |> sort(columns: ["_time"])
'''
    return _query(flux)


def daily_deltas_from_rows(
    rows: List[Dict[str, Any]],
    rx_field: str = "rx",
    tx_field: str = "tx",
) -> List[Dict[str, Any]]:
    """Deltas diarios de contadores de octetos a partir de filas ordenadas.

    Cada fila tiene {time, rx_field, tx_field} con el primer valor del dia; la
    diferencia entre dias consecutivos es el consumo de ese dia. Se descartan
    deltas negativos (reset de contador) y el primer dia (sin referencia).
    """
    result: List[Dict[str, Any]] = []
    prev_rx = prev_tx = None
    prev_date = None
    for r in rows:
        date = (r.get("time") or "")[:10]
        rx = r.get(rx_field)
        tx = r.get(tx_field)
        if (prev_date is not None and rx is not None and tx is not None
                and prev_rx is not None and prev_tx is not None):
            drx = rx - prev_rx
            dtx = tx - prev_tx
            if drx >= 0 and dtx >= 0:
                result.append({
                    "date": date,
                    "rx_bytes": drx,
                    "tx_bytes": dtx,
                    "total": drx + dtx,
                })
        if rx is not None:
            prev_rx = rx
        if tx is not None:
            prev_tx = tx
        prev_date = date
    return result


def get_daily_consumption(
    device_type: str,
    device_name: str,
    interface_name: str,
    hours: int = 8760,
) -> List[Dict[str, Any]]:
    """Consumo diario por interfaz: deltas de contadores de octetos por dia."""
    measurement, name_tag, rx_field, tx_field = _traffic_measurement(device_type)
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "{measurement}")
    |> filter(fn: (r) => r["{name_tag}"] == "{device_name}")
    |> filter(fn: (r) => r["interface_name"] == "{interface_name}")
    |> filter(fn: (r) => r["_field"] == "{rx_field}" or r["_field"] == "{tx_field}")
    |> aggregateWindow(every: 1d, fn: first, createEmpty: false)
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> keep(columns: ["_time", "{rx_field}", "{tx_field}"])
    |> sort(columns: ["_time"])
'''
    rows = _query(flux)
    return daily_deltas_from_rows(rows, rx_field, tx_field)


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


# ---------------------------------------------------------------------------
# Anomalias: ventanas, series, predicciones por tendencia e historico por ONU
# ---------------------------------------------------------------------------

_ANOMALY_WARNING_DBM = -28.0
_ANOMALY_RISK_DBM = -30.0
_ANOMALY_CRITICAL_DBM = -32.0


def _normalize_anomaly_series(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filas pivoteadas por hora -> {time, total, critical, warning, risk}.

    `risk` agrega la banda media (-32..-30) + critical (< -32): serie que
    origina las predicciones (sparkline morado).
    """
    out = []
    for r in rows:
        critical = int(r.get("critical") or 0)
        warning = int(r.get("warning") or 0)
        risk = int(r.get("risk") or 0)
        out.append({
            "time": r.get("time"),
            "total": critical + warning,
            "critical": critical,
            "warning": warning,
            "risk": risk + critical,
        })
    out.sort(key=lambda x: x.get("time") or "")
    return out


def _counts_from_bands(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Filas {band, value} -> {total, critical, warning, risk}."""
    critical = 0
    warning = 0
    risk = 0
    for r in rows:
        v = int(r.get("value") or 0)
        band = r.get("band")
        if band == "critical":
            critical = v
        elif band == "risk":
            risk = v
        else:
            warning = v
    return {"total": critical + warning, "critical": critical,
            "warning": warning, "risk": risk + critical}


def get_anomaly_series(hours: int = 24) -> List[Dict[str, Any]]:
    """Conteos por hora (ultimas `hours` horas) para los sparklines de KPIs."""
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours + 1}h)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> aggregateWindow(every: 1h, fn: last, createEmpty: false)
    |> filter(fn: (r) => r["_value"] < -28.0)
    |> map(fn: (r) => ({{
        r with
        band: if r["_value"] < -32.0 then "critical" else if r["_value"] < -30.0 then "risk" else "warning"
    }}))
    |> group(columns: ["band"])
    |> aggregateWindow(every: 1h, fn: count, createEmpty: false)
    |> pivot(rowKey: ["_time"], columnKey: ["band"], valueColumn: "_value")
    |> yield(name: "series")
'''
    return _normalize_anomaly_series(_query(flux))


def get_anomaly_previous(hours: int = 24) -> Dict[str, Any]:
    """Conteos en la ventana previa (-2h..-1h) base del % de cambio."""
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{(hours * 2) + 1}h, stop: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> last()
    |> filter(fn: (r) => r["_value"] < -28.0)
    |> map(fn: (r) => ({{
        r with
        band: if r["_value"] < -32.0 then "critical" else if r["_value"] < -30.0 then "risk" else "warning"
    }}))
    |> group(columns: ["band"])
    |> count()
    |> yield(name: "previous")
'''
    return _counts_from_bands(_query(flux))


def predict_failures(hours: int = 24, max_results: int = 20) -> List[Dict[str, Any]]:
    """Prediccion de fallo por tendencia por ONU (misma logica del agente:
    `InfluxReader.predict_failures`). Solo ONUs en caida con riesgo < 48h.

    La tendencia solo usa las ultimas 24 muestras (12 recientes vs 12
    previas), por lo que el rango se acota a 6h con media cada 15m: evita
    materializar 24h x 5m (~216k filas / 41MB por poll) que saturaban
    InfluxDB sin aportar a la prediccion.
    """
    _WINDOW = 15  # minutos por muestra
    rows = _query(f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -6h)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power")
    |> aggregateWindow(every: {_WINDOW}m, fn: mean, createEmpty: false)''')

    onu_data: Dict[tuple, List[float]] = {}
    for r in rows:
        key = (r.get("olt_name"), r.get("pon_port"), r.get("onu_index"),
               r.get("onu_serial"))
        if key not in onu_data:
            onu_data[key] = []
        v = r.get("value")
        if v is not None:
            onu_data[key].append(float(v))

    risks = []
    for (olt, pon, idx, serial), values in onu_data.items():
        if len(values) < 12:
            continue
        recent = values[-12:]
        older = values[-24:-12] if len(values) >= 24 else values[: len(values) // 2]
        if not older:
            continue
        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)
        hours_data = len(recent) * _WINDOW / 60
        if hours_data <= 0:
            continue
        trend = (recent_avg - older_avg) / hours_data
        if trend < -0.5 and recent_avg > -30:
            hours_to_failure = (recent_avg + 30) / abs(trend)
            confidence = min(1.0, abs(trend) / 2.0)
            if hours_to_failure < 48:
                risks.append({
                    "olt_name": olt,
                    "pon_port": pon,
                    "onu_index": idx,
                    "onu_serial": serial,
                    "current_power": round(recent_avg, 2),
                    "trend_db_per_hour": round(trend, 3),
                    "hours_to_failure": round(hours_to_failure, 1),
                    "confidence": round(confidence, 2),
                })
    risks.sort(key=lambda r: r["hours_to_failure"])
    return risks[:max_results]


def get_onu_optical_history(
    olt_name: str, onu_serial: str, hours: int = 24
) -> Dict[str, Any]:
    """Serie 24h rx/tx de una ONU (media cada 30m) para los historicos por card."""
    esc_olt = str(olt_name).replace("\\", "\\\\").replace('"', '\\"')
    esc_serial = str(onu_serial).replace("\\", "\\\\").replace('"', '\\"')
    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
    |> range(start: -{hours}h)
    |> filter(fn: (r) => r["_measurement"] == "optical_power")
    |> filter(fn: (r) => r["_field"] == "rx_power" or r["_field"] == "tx_power")
    |> filter(fn: (r) => r["olt_name"] == "{esc_olt}")
    |> filter(fn: (r) => r["onu_serial"] == "{esc_serial}")
    |> aggregateWindow(every: 30m, fn: mean, createEmpty: false)
    |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    |> sort(columns: ["_time"])
    |> yield(name: "history")
'''
    times, rx, tx = [], [], []
    for r in _query(flux):
        times.append(r.get("time"))
        rx.append(r.get("rx_power"))
        tx.append(r.get("tx_power"))
    return {"times": times, "rx": rx, "tx": tx}
