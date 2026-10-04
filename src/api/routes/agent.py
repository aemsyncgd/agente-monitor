# src/api/routes/agent.py
"""
Agent API routes — manages the AI monitoring agent and its instructions.

Provides endpoints for:
- Listing/toggling/reordering instructions
- Getting agent status and stats
- Client lookup by serial/name
- Managing custom instructions
"""
import os
import time
import logging
from typing import Optional, List, Dict
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from ..influx_helper import run_influx

logger = logging.getLogger(__name__)
router = APIRouter()

# Lazy-loaded managers (initialized on first request)
_instruction_manager = None
_project_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _get_instruction_manager():
    global _instruction_manager
    if _instruction_manager is None:
        # Import directly to avoid triggering ai/__init__.py torch dependency
        import importlib
        mod = importlib.import_module("src.ai.instructions")
        InstructionManager = mod.InstructionManager
        instr_path = os.environ.get(
            "INSTRUCTIONS_PATH",
            os.path.join(_project_root, "config", "instructions.json"),
        )
        _instruction_manager = InstructionManager(instr_path)
    return _instruction_manager


# === Request models ===

class ToggleInstruction(BaseModel):
    enabled: bool


class UpdateParams(BaseModel):
    params: Dict


class UpdatePriority(BaseModel):
    priority: int


class NewInstruction(BaseModel):
    name: str
    description: str
    instruction_type: str
    params: Dict = {}
    priority: int = 50


# === Agent status ===

@router.get("/api/agent/status")
async def get_agent_status():
    """Get AI agent status and stats (clientes desde la base enriquecida)."""
    from ...config_manager import get_config_manager
    from ..influx_helper import get_clientes_stats
    cm = get_config_manager()
    config = cm.config

    mgr = _get_instruction_manager()

    enabled = mgr.get_enabled()

    # Check if the ai_engine process is running
    ai_running = False
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "src.ai.main"],
            capture_output=True, text=True, timeout=3
        )
        ai_running = result.returncode == 0
    except Exception:
        pass

    cstats = await run_influx(get_clientes_stats)
    return {
        "status": "running" if ai_running else "stopped",
        "enabled": config.agent.enabled,
        "check_interval": config.agent.check_interval_seconds,
        "model_lightweight": config.agent.model_lightweight,
        "instructions": {
            "total": len(mgr.get_all()),
            "enabled": len(enabled),
            "disabled": len(mgr.get_all()) - len(enabled),
        },
        "clients": {
            "total_clients": cstats["enriquecidos"],
            "active_clients": cstats["enriquecidos"] - cstats["descuadrados"],
            "pendientes": cstats["pendientes"],
            "source": "clientes_enriquecidos (InfluxDB)",
        },
        "influxdb_url": config.influxdb.url,
    }


# === Instructions CRUD ===

@router.get("/api/agent/instructions")
async def list_instructions():
    """List all agent instructions."""
    mgr = _get_instruction_manager()
    return {"instructions": mgr.to_summary()}


@router.get("/api/agent/instructions/enabled")
async def list_enabled_instructions():
    """List only enabled instructions, sorted by priority."""
    mgr = _get_instruction_manager()
    enabled = mgr.get_enabled()
    return {
        "instructions": [inst.to_dict() for inst in enabled],
        "count": len(enabled),
    }


@router.get("/api/agent/instructions/{inst_id}")
async def get_instruction(inst_id: str):
    """Get a single instruction by ID."""
    mgr = _get_instruction_manager()
    inst = mgr.get_by_id(inst_id)
    if not inst:
        raise HTTPException(status_code=404, detail=f"Instruction '{inst_id}' not found")
    return inst.to_dict()


@router.put("/api/agent/instructions/{inst_id}/toggle")
async def toggle_instruction(inst_id: str, body: ToggleInstruction):
    """Enable or disable an instruction."""
    mgr = _get_instruction_manager()
    success = mgr.set_enabled(inst_id, body.enabled)
    if not success:
        raise HTTPException(status_code=404, detail=f"Instruction '{inst_id}' not found")
    inst = mgr.get_by_id(inst_id)
    return {"ok": True, "instruction": inst.to_dict()}


@router.put("/api/agent/instructions/{inst_id}/params")
async def update_instruction_params(inst_id: str, body: UpdateParams):
    """Update parameters for an instruction."""
    mgr = _get_instruction_manager()
    success = mgr.update_params(inst_id, body.params)
    if not success:
        raise HTTPException(status_code=404, detail=f"Instruction '{inst_id}' not found")
    inst = mgr.get_by_id(inst_id)
    return {"ok": True, "instruction": inst.to_dict()}


@router.put("/api/agent/instructions/{inst_id}/priority")
async def update_instruction_priority(inst_id: str, body: UpdatePriority):
    """Update priority for an instruction (0=highest)."""
    mgr = _get_instruction_manager()
    success = mgr.set_priority(inst_id, body.priority)
    if not success:
        raise HTTPException(status_code=404, detail=f"Instruction '{inst_id}' not found")
    inst = mgr.get_by_id(inst_id)
    return {"ok": True, "instruction": inst.to_dict()}


@router.post("/api/agent/instructions")
async def create_instruction(body: NewInstruction):
    """Create a new custom instruction."""
    mgr = _get_instruction_manager()
    import importlib
    mod = importlib.import_module("src.ai.instructions")
    InstructionType = mod.InstructionType
    try:
        itype = InstructionType(body.instruction_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type: {body.instruction_type}. Valid: {[t.value for t in InstructionType]}",
        )

    inst = mgr.add_custom(
        name=body.name,
        description=body.description,
        instruction_type=itype,
        params=body.params,
        priority=body.priority,
    )
    return {"ok": True, "instruction": inst.to_dict()}


@router.delete("/api/agent/instructions/{inst_id}")
async def delete_instruction(inst_id: str):
    """Delete a custom instruction (built-in ones cannot be deleted)."""
    mgr = _get_instruction_manager()
    success = mgr.remove_custom(inst_id)
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete '{inst_id}': not found or is built-in",
        )
    return {"ok": True}


# === Client lookup (base enriquecida, sin grid_servicios.csv) ===

def _estado_label(row: Dict) -> str:
    """Estado legible de la ONU para compatibilidad con el shape legacy."""
    try:
        estado = int(row.get("estado_onu", -1))
    except (TypeError, ValueError):
        estado = -1
    if estado == 3:
        return "Activo"
    if estado in (1, 4, 6):
        return "Caído"
    return "N/A"


def _client_brief(c: Dict) -> Dict:
    return {
        "nombre": c.get("nombre", ""),
        "serial_onu": c.get("serial_onu", ""),
        "direccion": c.get("direccion", ""),
        "nodo": c.get("nodo", ""),
        "puerto_pon": c.get("pon_port", ""),
        "estado": _estado_label(c),
    }


@router.get("/api/agent/clients/lookup")
async def lookup_client(serial: Optional[str] = Query(None), name: Optional[str] = Query(None)):
    """Look up a client by serial or name (base enriquecida)."""
    from ..influx_helper import get_enriched_clients

    if serial:
        rows = await run_influx(get_enriched_clients, serial, None, None, None, None, 10)
        serial_u = str(serial).strip().upper()
        exact = [c for c in rows
                 if (c.get("serial_onu") or "").upper() == serial_u]
        if not exact and rows:
            exact = rows
        if not exact:
            return {"found": False, "serial": serial}
        return {"found": True, "client": _client_brief(exact[0])}

    if name:
        results = await run_influx(get_enriched_clients, name, None, None, None, None, 20)
        return {
            "found": len(results) > 0,
            "count": len(results),
            "clients": [_client_brief(c) for c in results],
        }

    return {"found": False, "message": "Provide 'serial' or 'name' query parameter"}


@router.get("/api/agent/clients/stats")
async def client_stats():
    """Get client database statistics (base enriquecida)."""
    from ..influx_helper import get_clientes_stats
    return await run_influx(get_clientes_stats)


@router.get("/api/agent/clients/pon/{olt_hostname}/{pon_port}")
async def clients_on_pon(olt_hostname: str, pon_port: str):
    """Get all clients on a specific OLT:PON port (base enriquecida)."""
    from ..influx_helper import get_enriched_clients
    clients = await run_influx(
        get_enriched_clients, None, olt_hostname, None, None, pon_port, 50
    )
    return {
        "olt": olt_hostname,
        "pon_port": pon_port,
        "count": len(clients),
        "clients": [_client_brief(c) for c in clients],
    }
