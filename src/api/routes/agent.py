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

logger = logging.getLogger(__name__)
router = APIRouter()

# Lazy-loaded managers (initialized on first request)
_instruction_manager = None
_client_lookup = None
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


def _get_client_lookup():
    global _client_lookup
    if _client_lookup is None:
        from src.client_lookup import ClientLookup
        _client_lookup = ClientLookup()
        csv_path = os.environ.get(
            "CSV_PATH",
            os.path.join(_project_root, "grid_servicios.csv"),
        )
        if os.path.exists(csv_path):
            _client_lookup.load_from_csv(csv_path)
    return _client_lookup


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
    """Get AI agent status and stats."""
    from ...config_manager import get_config_manager
    cm = get_config_manager()
    config = cm.config

    mgr = _get_instruction_manager()
    lookup = _get_client_lookup()

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
        "clients": lookup.stats(),
        "csv_enabled": config.csv.enabled,
        "csv_reload_hours": config.csv.reload_hours,
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


# === Client lookup ===

@router.get("/api/agent/clients/lookup")
async def lookup_client(serial: Optional[str] = Query(None), name: Optional[str] = Query(None)):
    """Look up a client by serial or name."""
    lookup = _get_client_lookup()

    if serial:
        client = lookup.lookup_by_serial(serial)
        if client:
            return {
                "found": True,
                "client": {
                    "nombre": client.nombre,
                    "serial_onu": client.serial_onu,
                    "direccion": client.direccion,
                    "nodo": client.nodo,
                    "puerto_pon": client.puerto_pon,
                    "ip_servicio": client.ip_servicio,
                    "estado": client.estado,
                },
            }
        return {"found": False, "serial": serial}

    if name:
        results = lookup.search_by_name(name)
        return {
            "found": len(results) > 0,
            "count": len(results),
            "clients": [
                {
                    "nombre": c.nombre,
                    "serial_onu": c.serial_onu,
                    "direccion": c.direccion,
                    "nodo": c.nodo,
                    "puerto_pon": c.puerto_pon,
                    "estado": c.estado,
                }
                for c in results[:20]
            ],
        }

    return {"found": False, "message": "Provide 'serial' or 'name' query parameter"}


@router.get("/api/agent/clients/stats")
async def client_stats():
    """Get client database statistics."""
    lookup = _get_client_lookup()
    return lookup.stats()


@router.get("/api/agent/clients/pon/{olt_hostname}/{pon_port}")
async def clients_on_pon(olt_hostname: str, pon_port: str):
    """Get all clients on a specific OLT:PON port."""
    lookup = _get_client_lookup()
    clients = lookup.clients_on_pon(olt_hostname, pon_port)
    return {
        "olt": olt_hostname,
        "pon_port": pon_port,
        "count": len(clients),
        "clients": [
            {"nombre": c.nombre, "serial_onu": c.serial_onu, "direccion": c.direccion}
            for c in clients
        ],
    }
