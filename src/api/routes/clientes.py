# src/api/routes/clientes.py
"""
API Routes - Base de clientes enriquecida.

Busqueda full-text sobre `clientes_enriquecidos` (cruce OLT + RouterOS +
WISP) con enmascarado de datos sensibles segun el rol del API key.

Enmascarado por rol:
  - admin/operator: pppoe enmascarado (ab****12), documento/telefono visibles.
  - viewer: documento y telefono ocultos (se conservan los ultimos 2 digitos).
"""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from typing import List, Optional

from ..auth import Role
from ..influx_helper import (
    get_enriched_clients,
    get_pending_clients,
    get_pending_by_documento,
    get_clientes_stats,
    get_client_by_documento,
    search_clientes,
)

router = APIRouter(prefix="/api/clientes", tags=["clientes"])


# === Enmascarado por rol ===

def _mask(v, keep: int = 2) -> str:
    s = str(v or "")
    if not s:
        return ""
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]


def _mask_pppoe(v) -> str:
    """Enmascara usuario pppoe tipo ab****12 para todos los roles."""
    s = str(v or "")
    if not s or len(s) <= 4:
        return s
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _apply_masking(row: dict, role: str) -> dict:
    out = dict(row)
    out["usuario_pppoe"] = _mask_pppoe(row.get("usuario_pppoe"))
    if role == Role.VIEWER:
        out["documento"] = _mask(row.get("documento"))
        out["telefono"] = _mask(row.get("telefono"))
    return out


# === Pydantic Models ===

class ClienteLite(BaseModel):
    documento: str
    usuario_pppoe: str
    serial_onu: str
    mac: str
    nombre: str
    direccion: str
    olt: str
    nodo: str
    nap: str
    zona: str
    estado_onu: int
    coincidencia: str
    ubicacion_descuadrada: bool
    telefono: str = ""
    time: Optional[str] = None
    pon_port: str = ""
    pendiente: bool = False
    estado_servicio: str = ""
    pppoe_status: str = ""


class ClienteDetalle(ClienteLite):
    onu_index: str = ""
    puerto: str = ""
    plan: str = ""
    ip_servicio: str = ""
    causa: str = ""


class Pendiente(BaseModel):
    documento: str
    cause: str
    nombre: str
    nap: str = ""
    zona: str = ""
    detalle: str = ""
    time: Optional[str] = None


class ClientesStats(BaseModel):
    enriquecidos: int
    descuadrados: int
    pendientes: int


def _role_from_request(request: Request) -> str:
    return getattr(request.state, "role", Role.NONE) or Role.NONE


def _to_lite(row: dict) -> ClienteLite:
    return ClienteLite(
        documento=str(row.get("documento") or ""),
        usuario_pppoe=str(row.get("usuario_pppoe") or ""),
        serial_onu=str(row.get("serial_onu") or ""),
        mac=str(row.get("mac") or ""),
        nombre=str(row.get("nombre") or ""),
        direccion=str(row.get("direccion") or ""),
        olt=str(row.get("olt") or ""),
        nodo=str(row.get("nodo") or ""),
        nap=str(row.get("nap") or ""),
        zona=str(row.get("zona") or ""),
        estado_onu=_estado_int(row),
        coincidencia=str(row.get("coincidencia") or ""),
        ubicacion_descuadrada=row.get("ubicacion_descuadrada") == "true",
        telefono=str(row.get("telefono") or ""),
        time=row.get("time"),
        pon_port=str(row.get("pon_port") or ""),
        pendiente=(row.get("pendiente") == "true"),
        estado_servicio=str(row.get("estado_servicio") or ""),
        pppoe_status=str(row.get("pppoe_status") or ""),
    )


def _estado_int(row: dict) -> int:
    try:
        return int(row.get("estado_onu", -1))
    except (TypeError, ValueError):
        return -1


# === Endpoints ===

@router.get("/stats", response_model=ClientesStats)
async def stats():
    """Totales de enriquecidos / descuadrados / pendientes."""
    return ClientesStats(**get_clientes_stats())


@router.get("/pendientes", response_model=List[Pendiente])
async def pendientes(limit: int = Query(200, ge=1, le=1000)):
    """Clientes sin cruce, con causa (sin_olt / sin_pppoe / sin_cruce)."""
    rows = get_pending_clients(limit=limit)
    return [
        Pendiente(
            documento=str(r.get("documento") or ""),
            cause=str(r.get("causa") or ""),
            nombre=str(r.get("nombre") or ""),
            nap=str(r.get("nap") or ""),
            zona=str(r.get("zona") or ""),
            detalle=str(r.get("detalle") or ""),
            time=r.get("time"),
        )
        for r in rows
    ]


@router.get("/buscar", response_model=List[ClienteLite])
async def buscar(
    request: Request,
    q: str = Query("", min_length=1),
    olt: Optional[str] = None,
    nap: Optional[str] = None,
    zona: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
):
    """Busqueda full-text unificada: enriquecidos + pendientes (marcados).
    Nombre, documento, direccion, serial, pppoe, mac, olt, nap, zona.
    Respuesta enmascarada segun rol."""
    role = _role_from_request(request)
    rows = search_clientes(
        search=q, olt=olt, nap=nap, zona=zona, limit=limit
    )
    return [
        _to_lite(_apply_masking(r, role)) for r in rows
    ]


@router.get("/", response_model=List[ClienteLite])
async def listar(
    request: Request,
    olt: Optional[str] = None,
    nap: Optional[str] = None,
    zona: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
):
    """Lista filtrada por olt/nap/zona. Respuesta enmascarada segun rol."""
    role = _role_from_request(request)
    rows = get_enriched_clients(olt=olt, nap=nap, zona=zona, limit=limit)
    return [
        _to_lite(_apply_masking(r, role)) for r in rows
    ]


@router.get("/{documento}", response_model=ClienteDetalle)
async def ficha(request: Request, documento: str):
    """Ficha completa por documento. Cae a la ficha pendiente si el cliente
    no tiene cruce ONU aun. Respuesta enmascarada segun rol."""
    role = _role_from_request(request)
    row = get_client_by_documento(documento)
    if not row:
        row = get_pending_by_documento(documento)
        if not row:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        row = {**row, "pendiente": "true", "estado_onu": -1}
    lite = _to_lite(_apply_masking(row, role))
    return ClienteDetalle(
        **lite.model_dump(),
        onu_index=str(row.get("onu_index") or ""),
        puerto=str(row.get("puerto") or ""),
        plan=str(row.get("plan") or ""),
        ip_servicio=str(row.get("ip_servicio") or ""),
        causa=str(row.get("causa") or ""),
    )