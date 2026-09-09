# src/api/routes/agent_ops.py
"""
Endpoints de operación del agente: estado, control de servicios,
estadísticas, events bus y auditoría.
"""
import os
import time
import logging
from typing import Dict, Optional

from fastapi import APIRouter, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends

logger = logging.getLogger(__name__)

router = APIRouter()

MANAGE_SCRIPT = os.environ.get(
    "MANAGE_SCRIPT", "scripts/manage-services.sh"
)
PROJECT_DIR = os.environ.get("PROJECT_DIR", "/home/server/agente-monitor")

ALLOWED_PING_KEYS = {
    "enabled": bool,
    "targets_path": str,
    "interval_seconds": int,
    "alert_down_seconds": int,
    "alert_on_recovery": bool,
    "status_window_minutes": int,
    "high_latency_threshold_ms": float,
}

# Conexiones WebSocket activas
_ws_clients: set = set()


def _run_manage(command: str, service: str) -> Dict:
    """Ejecuta manage-services.sh y captura salida."""
    import subprocess
    try:
        result = subprocess.run(
            ["bash", MANAGE_SCRIPT, command, service],
            capture_output=True, text=True, timeout=30,
            cwd=PROJECT_DIR,
        )
        return {
            "ok": result.returncode == 0,
            "command": f"{command} {service}",
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "exit_code": result.returncode,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_agent_instance():
    """Devuelve la instancia del WispAgent si está en el mismo proceso."""
    return None


def _read_agent_status() -> Dict:
    """Lee el estado del agente desde el sistema de archivos / heartbeat."""
    health_file = os.path.join(PROJECT_DIR, "logs", "wisp_agent_health.json")
    status = {
        "running": False,
        "status": "offline",
        "last_heartbeat": 0,
    }
    if os.path.exists(health_file):
        try:
            import json
            with open(health_file, "r") as f:
                status.update(json.load(f))
        except Exception:
            pass

    # Verificar proceso
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "src.ai.agent_loop"],
            capture_output=True, text=True,
        )
        if result.stdout.strip():
            status["running"] = True
    except Exception:
        pass

    # Último heartbeat: si es viejo, marcar offline
    last_heartbeat = status.get("last_heartbeat", 0)
    if last_heartbeat and (time.time() - last_heartbeat) < 120:
        status["status"] = "healthy"
    elif last_heartbeat:
        status["status"] = "stale"
    return status


# ── Estado del agente ────────────────────────────────────────────────────────

@router.get("/api/agent/health")
async def agent_health(request: Request):
    """Estado del agente autónomo."""
    status = _read_agent_status()
    return status


@router.get("/api/agent/stats")
async def agent_stats(request: Request):
    """Estadísticas del agente autónomo."""
    stats_file = os.path.join(PROJECT_DIR, "logs", "wisp_agent_stats.json")
    if os.path.exists(stats_file):
        try:
            import json
            with open(stats_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "processed_events": 0,
        "alerts_sent": 0,
        "actions_executed": 0,
        "llm_failures": 0,
        "degraded_mode": False,
    }


# ── Control de servicios/módulos ─────────────────────────────────────────────

@router.get("/api/services")
async def list_services(request: Request):
    """Lista el estado de todos los servicios/módulos."""
    import subprocess
    try:
        result = subprocess.run(
            ["bash", MANAGE_SCRIPT, "status"],
            capture_output=True, text=True, timeout=30,
            cwd=PROJECT_DIR,
        )
        lines = result.stdout.strip().splitlines()
        services = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("RUNNING", "STOPPED"):
                services.append({
                    "name": parts[1],
                    "status": "running" if parts[0] == "RUNNING" else "stopped",
                    "pid": parts[2].replace("pid=", "") if len(parts) > 2 else None,
                })
        return {"services": services}
    except Exception as e:
        return {"services": [], "error": str(e)}


@router.post("/api/services/{service}/action")
async def service_action(service: str, action: str, request: Request):
    """Ejecuta una acción sobre un servicio (start, stop, restart, enable, disable)."""
    valid_actions = {"start", "stop", "restart", "enable", "disable"}
    valid_services = {"collector", "api", "ai-agent", "llm-agent", "wisp-agent", "scheduler", "bot"}

    if service not in valid_services:
        raise HTTPException(status_code=400, detail=f"Servicio inválido: {service}")
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"Acción inválida: {action}")

    if action in ("start", "stop", "restart"):
        from src.audit import get_audit_logger
        get_audit_logger().log(
            actor=getattr(request.state, "role", "unknown"),
            action=f"service:{action}",
            target=service,
        )

    result = _run_manage(action, service)
    return result


@router.get("/api/services/{service}/status")
async def service_status(service: str, request: Request):
    result = _run_manage("status", service)
    return result


# ── Config Manager ───────────────────────────────────────────────────────────

@router.get("/api/config")
async def get_config(request: Request):
    """Devuelve la configuración actual del sistema (sin secretos)."""
    import yaml
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    try:
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}
        # Sanitizar secretos
        for section in ("telegram", "influxdb"):
            if section in raw:
                for key in list(raw[section].keys()):
                    if "token" in key.lower() or "password" in key.lower() or "key" in key.lower():
                        raw[section][key] = "***REDACTED***"
        return raw
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo config: {e}")


@router.get("/api/agent/ping-config")
async def get_ping_config(request: Request):
    """Devuelve la configuración actual del monitor de ping."""
    import yaml
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    try:
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("ping", {})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error leyendo config: {e}")


@router.put("/api/agent/ping-config")
async def update_ping_config(request: Request):
    """Actualiza la configuración del monitor de ping."""
    import yaml
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body debe ser un objeto")

    try:
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f) or {}
        ping = raw.setdefault("ping", {})

        for key, value in payload.items():
            if key not in ALLOWED_PING_KEYS:
                raise HTTPException(status_code=400, detail=f"Clave no permitida: {key}")
            if key == "status_window_minutes":
                v = int(value)
                if v < 1:
                    raise HTTPException(status_code=400, detail="status_window_minutes debe ser >= 1")
                ping[key] = v
            elif key == "high_latency_threshold_ms":
                v = float(value)
                if v < 0:
                    raise HTTPException(status_code=400, detail="high_latency_threshold_ms debe ser >= 0")
                ping[key] = v
            elif key == "interval_seconds":
                v = int(value)
                if v < 10:
                    raise HTTPException(status_code=400, detail="interval_seconds debe ser >= 10")
                ping[key] = v
            elif key == "alert_down_seconds":
                v = int(value)
                if v < 10:
                    raise HTTPException(status_code=400, detail="alert_down_seconds debe ser >= 10")
                ping[key] = v
            elif key == "alert_on_recovery":
                ping[key] = bool(value)
            else:
                ping[key] = value

        with open(config_path, "w") as f:
            yaml.safe_dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        if "status_window_minutes" in payload:
            from ..influx_helper import set_status_window
            set_status_window(ping["status_window_minutes"])

        logger.info(f"Ping config updated: {ping}")
        return {"ok": True, **ping}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error actualizando config: {e}")


@router.get("/api/modules")
async def list_modules(request: Request):
    """Lista los módulos del sistema y su capacidad de control."""
    return {
        "modules": [
            {"name": "collector", "controls": ["start", "stop", "restart", "enable", "disable"]},
            {"name": "api", "controls": ["start", "stop", "restart", "enable", "disable"]},
            {"name": "ai-agent", "controls": ["start", "stop", "restart", "enable", "disable"]},
            {"name": "llm-agent", "controls": ["start", "stop", "restart", "enable", "disable"]},
            {"name": "wisp-agent", "controls": ["start", "stop", "restart", "enable", "disable"]},
            {"name": "scheduler", "controls": ["start", "stop", "restart", "enable", "disable"]},
            {"name": "bot", "controls": ["start", "stop", "restart", "enable", "disable"]},
        ]
    }


# ── Event Bus ────────────────────────────────────────────────────────────────

@router.get("/api/eventbus")
async def eventbus_status(request: Request):
    """Estado del event bus y cola de eventos."""
    try:
        from src.event_bus import get_event_bus
        bus = get_event_bus()
        return bus.stats()
    except Exception as e:
        return {"error": str(e), "connected": False}


@router.post("/api/eventbus/replay/{count}")
async def eventbus_replay(count: int, request: Request):
    """Re-procesa eventos sin ACK (útil para recuperación)."""
    try:
        from src.event_bus import get_event_bus
        bus = get_event_bus()
        messages = bus.consume(count=min(count, 100), timeout_ms=1000)
        return {"replayed": len(messages), "event_ids": [m.get("id") for m in messages]}
    except Exception as e:
        return {"error": str(e)}


# ── WebSocket de eventos en tiempo real ──────────────────────────────────────

@router.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong", "ts": time.time()})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


async def broadcast_event(event_dict: Dict):
    """Envía un evento a todas las conexiones WebSocket activas."""
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(event_dict)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


# ── Auditoría ────────────────────────────────────────────────────────────────

@router.get("/api/audit")
async def audit_query(request: Request, since: Optional[str] = None,
                      action_contains: Optional[str] = None, limit: int = 100):
    """Consulta el log de auditoría."""
    try:
        from src.audit import get_audit_logger
        audit = get_audit_logger()
        results = audit.query(
            since=since,
            action_filter=action_contains,
            limit=min(limit, 500),
        )
        return {"count": len(results), "entries": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Audit query failed: {e}")


# ── RAG ──────────────────────────────────────────────────────────────────────

@router.get("/api/rag")
async def rag_status(request: Request):
    """Estado del vector store RAG."""
    try:
        from src.ai.rag import get_rag_store
        rag = get_rag_store()
        return rag.stats()
    except Exception as e:
        return {"ready": False, "error": str(e)}


@router.post("/api/rag/reindex")
async def rag_reindex(request: Request):
    """Re-indexa el RAG."""
    try:
        from src.ai.rag import get_rag_store
        from src.config_ia import load_config_ia
        rag = get_rag_store()
        if not rag.ready:
            return {"ok": False, "error": "RAG no disponible"}
        config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
        config = load_config_ia(config_path)
        count = rag.index_directory(config.rag.docs_directory)
        return {"ok": True, "chunks_indexed": count, "total": rag.count()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
