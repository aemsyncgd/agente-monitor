import os
import json
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Optional, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reporte", tags=["reporte"])

_generating_lock = asyncio.Lock()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "reporte-config.json"


def _normalize_iface_list(v: Any) -> List[Dict]:
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        if isinstance(item, str):
            out.append({"name": item, "analyze": False})
        elif isinstance(item, dict):
            out.append({"name": item.get("name", ""), "analyze": bool(item.get("analyze", False))})
    return [i for i in out if i["name"]]


class InterfaceConfig(BaseModel):
    name: str
    analyze: bool = False


class MikroTikHostConfig(BaseModel):
    name: str
    interfaces: List[InterfaceConfig] = []

    @field_validator("interfaces", mode="before")
    @classmethod
    def _normalize_interfaces(cls, v):
        return _normalize_iface_list(v)


class OltHostConfig(BaseModel):
    name: str
    ge_interfaces: List[InterfaceConfig] = []
    pon_monitoring: bool = True

    @field_validator("ge_interfaces", mode="before")
    @classmethod
    def _normalize_ge_interfaces(cls, v):
        return _normalize_iface_list(v)


class ReporteConfig(BaseModel):
    enabled: bool = True
    schedules: List[str] = ["08:00", "12:30", "16:50", "20:45"]
    mikrotiks: List[MikroTikHostConfig] = []
    olts: List[OltHostConfig] = []


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Error loading {CONFIG_PATH}: {e}")
    return {"enabled": True, "schedules": ["08:00", "12:30", "16:50", "20:45"], "mikrotiks": [], "olts": []}


def _save_config(data: dict) -> bool:
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except Exception as e:
        logger.error(f"Error saving {CONFIG_PATH}: {e}")
        return False


def _merge_discovered_hosts(discovered: List[str], known: List[str]) -> List[str]:
    """Union of hosts with recent data plus configured hosts (even without data).

    Hosts that appear in both are kept once, preserving the discovered order
    and appending the remaining configured hosts sorted by name.
    """
    seen = set()
    merged = []
    for name in discovered:
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    for name in sorted(known):
        if name and name not in seen:
            seen.add(name)
            merged.append(name)
    return merged


@router.get("/config")
async def get_config():
    return _load_config()


@router.put("/config")
async def update_config(config: ReporteConfig):
    data = config.model_dump()
    if _save_config(data):
        return {"status": "ok", "config": data}
    raise HTTPException(status_code=500, detail="Error saving config")


@router.get("/status")
async def get_status():
    cfg = _load_config()
    enabled = cfg.get("enabled", True)
    mk_count = len(cfg.get("mikrotiks", []))
    olt_count = len(cfg.get("olts", []))
    schedules = cfg.get("schedules", ["08:00", "12:30", "16:50", "20:45"])
    analyzed = 0
    for h in cfg.get("mikrotiks", []):
        analyzed += sum(1 for i in _normalize_iface_list(h.get("interfaces")) if i["analyze"])
    for h in cfg.get("olts", []):
        analyzed += sum(1 for i in _normalize_iface_list(h.get("ge_interfaces")) if i["analyze"])
    return {
        "enabled": enabled,
        "status": "active" if enabled else "paused",
        "mikrotiks_configured": mk_count,
        "olts_configured": olt_count,
        "schedules": schedules,
        "analyzed_interfaces": analyzed,
    }


@router.post("/generate")
async def generate_report_now():
    if _generating_lock.locked():
        raise HTTPException(status_code=409, detail="Ya hay un reporte en generacion")
    try:
        from ...storage.influx_client import InfluxClient
        from ...config_ia import load_config_ia
        from ...reporte.reporter import DailyReporter

        config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
        ia_config = load_config_ia(config_path)
        influx = InfluxClient(
            url=os.environ.get("INFLUXDB_URL", ia_config.influxdb.url),
            token=os.environ.get("INFLUXDB_TOKEN", ia_config.influxdb.token),
            org=os.environ.get("INFLUXDB_ORG", ia_config.influxdb.org),
            bucket=os.environ.get("INFLUXDB_BUCKET", ia_config.influxdb.bucket),
        )
        if not influx.connect():
            raise HTTPException(status_code=503, detail="No se pudo conectar a InfluxDB")

        reporter = DailyReporter(
            influx=influx,
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ia_config.telegram.bot_token),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ia_config.telegram.chat_id),
        )
        async with _generating_lock:
            ok = await asyncio.to_thread(reporter.run)
        if ok:
            return {"status": "ok", "message": "Reporte enviado a Telegram correctamente"}
        raise HTTPException(status_code=502, detail="Fallo al enviar el reporte a Telegram")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Generate report failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error generando reporte: {e}")


@router.get("/discover")
async def discover():
    result = {"mikrotiks": [], "olts": []}

    try:
        from ...storage.influx_client import InfluxClient
        from ...config_ia import load_config_ia

        config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
        ia_config = load_config_ia(config_path)
        influx = InfluxClient(
            url=os.environ.get("INFLUXDB_URL", ia_config.influxdb.url),
            token=os.environ.get("INFLUXDB_TOKEN", ia_config.influxdb.token),
            org=os.environ.get("INFLUXDB_ORG", ia_config.influxdb.org),
            bucket=os.environ.get("INFLUXDB_BUCKET", ia_config.influxdb.bucket),
        )
        if not influx.connect():
            return {"mikrotiks": [], "olts": [], "error": "cannot connect to InfluxDB"}

        # Discover OLTs
        olt_results = influx.query(f'''
            from(bucket: "{influx.bucket}")
                |> range(start: -1h)
                |> filter(fn: (r) => r["_measurement"] == "olt_system")
                |> last()
        ''')
        olt_names = set()
        for r in olt_results:
            name = r.get("olt_name", "")
            if name:
                olt_names.add(name)

        # Discover MikroTiks
        mk_results = influx.query(f'''
            from(bucket: "{influx.bucket}")
                |> range(start: -1h)
                |> filter(fn: (r) => r["_measurement"] == "mikrotik_system")
                |> last()
        ''')
        mk_names = set()
        for r in mk_results:
            name = r.get("device_name", "")
            if name:
                mk_names.add(name)

        # Merge with the infrastructure source of truth (nodes.yaml) so that
        # configured hosts without recent SNMP data (e.g. NARANJILLOS-VIDANET)
        # still appear in the dashboard selection list.
        known_mk_names: List[str] = []
        known_olt_names: List[str] = []
        try:
            from ...config_ia import load_nodes_config
            nodes = load_nodes_config(str(PROJECT_ROOT / "config" / "nodes.yaml"))
            known_mk_names = [mt.hostname for node in nodes for mt in node.mikrotiks if mt.hostname]
            known_olt_names = [olt.hostname for node in nodes for olt in node.olts if olt.hostname]
        except Exception as e:
            logger.warning(f"No se pudieron cargar hosts de nodes.yaml: {e}")

        # Get interfaces for each MikroTik
        for name in _merge_discovered_hosts(sorted(mk_names), known_mk_names):
            iface_results = influx.query(f'''
                from(bucket: "{influx.bucket}")
                    |> range(start: -1h)
                    |> filter(fn: (r) => r["_measurement"] == "mikrotik_interface")
                    |> filter(fn: (r) => r["device_name"] == "{name}")
                    |> last()
            ''')
            ifaces = set()
            for r in iface_results:
                iface = r.get("interface_name", "")
                if iface:
                    ifaces.add(iface)
            result["mikrotiks"].append({
                "name": name,
                "interfaces": sorted(ifaces),
            })

        # Get interfaces for each OLT (separate GE vs PON)
        for name in _merge_discovered_hosts(sorted(olt_names), known_olt_names):
            iface_results = influx.query(f'''
                from(bucket: "{influx.bucket}")
                    |> range(start: -1h)
                    |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
                    |> filter(fn: (r) => r["device_name"] == "{name}")
                    |> last()
            ''')
            ge_ifaces = set()
            pon_ifaces = set()
            other_ifaces = set()
            for r in iface_results:
                iface = r.get("interface_name", "")
                if not iface:
                    continue
                if iface.startswith("GE"):
                    ge_ifaces.add(iface)
                elif iface.startswith("GPON") or iface.startswith("PON"):
                    pon_ifaces.add(iface)
                else:
                    other_ifaces.add(iface)
            result["olts"].append({
                "name": name,
                "ge_interfaces": sorted(ge_ifaces),
                "pon_interfaces": sorted(pon_ifaces),
                "other_interfaces": sorted(other_ifaces),
            })

    except Exception as e:
        logger.warning(f"Discover failed: {e}")
        return {"mikrotiks": [], "olts": [], "error": str(e)}

    return result
