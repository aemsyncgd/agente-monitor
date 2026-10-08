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
from pydantic import BaseModel, Field
from typing import List, Optional, Union

from ..auth import Role

import os
import threading
from ..influx_helper import (
    get_enriched_clients,
    get_pending_clients,
    get_pending_by_documento,
    get_pending_filtered,
    get_clientes_stats,
    get_client_by_documento,
    search_clientes,
)

router = APIRouter(prefix="/api/clientes", tags=["clientes"])

_LOS_DOWN = [1, 4, 6]  # estados "sin señal" de la ONU


def _get_store():
    """ClientStore (cache Redis). Separado para poder mockearlo en tests."""
    from ...redis_store import get_client_store
    return get_client_store()


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


class GrupoDetalle(BaseModel):
    clave: str
    total: int
    los: int


class BulkRequest(BaseModel):
    documentos: List[str]


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


# === Búsqueda vital (WISP vivo + Influx) ===

_client_search = None
_client_search_lock = threading.Lock()


def _get_client_search():
    """ClientSearchService compartido (lazy, singleton)."""
    global _client_search
    if _client_search is not None:
        return _client_search
    with _client_search_lock:
        if _client_search is None:
            from ...client_search import ClientSearchService
            from ...wisp_client import WispClient
            from ...storage.influx_client import InfluxClient
            from ...config_manager import get_config_manager

            cm = get_config_manager()
            influx = InfluxClient(
                url=os.environ.get("INFLUXDB_URL", cm.config.influxdb.url),
                token=os.environ.get("INFLUXDB_TOKEN", cm.config.influxdb.token),
                org=os.environ.get("INFLUXDB_ORG", cm.config.influxdb.org),
                bucket=os.environ.get("INFLUXDB_BUCKET", cm.config.influxdb.bucket),
            )
            influx.connect()
            _client_search = ClientSearchService(
                wisp=WispClient(cm.config.wisp),
                influx=influx,
            )
    return _client_search


class VitalRequest(BaseModel):
    serial: Optional[str] = None
    documento: Optional[str] = None
    mac: Optional[str] = None
    nombre: Optional[str] = None


class VitalBatchRequest(BaseModel):
    queries: List[str] = Field(default_factory=list)
    include_history: bool = True
    include_infra: bool = True


class GrupoAnalizarRequest(BaseModel):
    serials: Optional[List[str]] = None
    olt_name: Optional[str] = None
    pon_port: Optional[str] = None
    nap: Optional[str] = None


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
    rows: Optional[List[dict]] = None
    try:
        store = _get_store()
        if store.count() > 0:
            docs = store.search(q, limit=limit, nap=nap, zona=zona, olt=olt)
            rows = store.get_fichas(docs)
    except Exception:
        rows = None
    if rows is None:
        rows = search_clientes(search=q, olt=olt, nap=nap, zona=zona, limit=limit)
    elif len(rows) < limit:
        rows = rows + get_pending_filtered(
            search=q, nap=nap, zona=zona, limit=limit - len(rows),
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
    rows: Optional[List[dict]] = None
    try:
        store = _get_store()
        if store.count() > 0:
            docs = store.search("", limit=limit, nap=nap, zona=zona, olt=olt)
            rows = store.get_fichas(docs)
            rows.sort(key=lambda r: (str(r.get("olt") or ""),
                                     str(r.get("nap") or "")))
    except Exception:
        rows = None
    if rows is None:
        rows = get_enriched_clients(olt=olt, nap=nap, zona=zona, limit=limit)
    return [
        _to_lite(_apply_masking(r, role)) for r in rows
    ]


@router.get("/los", response_model=List[ClienteLite])
async def en_los(
    request: Request,
    limit: int = Query(500, ge=1, le=2000),
):
    """Clientes actualmente en LOS (estado_onu en 1/4/6) desde el cache Redis."""
    role = _role_from_request(request)
    rows = _try_store(lambda s: s.get_fichas(s.los(_LOS_DOWN)[:limit]))
    return [_to_lite(_apply_masking(r, role)) for r in rows]


@router.get("/grupo", response_model=List[GrupoDetalle])
async def grupo(by: str = Query("zona", pattern="^(zona|nap)$")):
    """Agregados por zona/NAP: total de clientes y cuantos en LOS."""
    grupos = _try_store(lambda s: s.group_los(_LOS_DOWN, by=by))
    return [GrupoDetalle(**g) for g in grupos]


@router.post("/bulk", response_model=List[ClienteLite])
async def bulk(fichas: BulkRequest, request: Request):
    """Fichas de varios documentos en una sola consulta (cache Redis)."""
    role = _role_from_request(request)
    docs = fichas.documentos[:500]
    rows = _try_store(lambda s: s.get_fichas(docs))
    return [_to_lite(_apply_masking(r, role)) for r in rows]


def _try_store(fn):
    """Ejecuta fn(store); si Redis falla devuelve [] (rama degradada)."""
    try:
        return fn(_get_store())
    except Exception:
        return []


@router.post("/vital/batch", response_model=dict)
async def cliente_vital_batch(
    body: Union[VitalBatchRequest, List[str]],
    request: Request,
):
    """Fichas vitales de varios clientes en un solo request.

    Acepta `{"queries": [...], "include_history": bool, "include_infra": bool}`
    o directamente una lista de cadenas. El numero de consultas a Influx es
    constante respecto al numero de clientes. Respuesta enmascarada por rol.
    """
    if isinstance(body, list):
        queries, include_history, include_infra = body, True, True
    else:
        queries = list(body.queries or [])
        include_history = bool(body.include_history)
        include_infra = bool(body.include_infra)
    queries = [str(q).strip() for q in queries if str(q or "").strip()]
    if not queries:
        raise HTTPException(status_code=400,
                            detail="Debe indicar al menos una consulta")
    role = _role_from_request(request)
    res = _get_client_search().find_many(
        queries, include_history=include_history, include_infra=include_infra,
    )
    for r in res.get("results", []):
        if r.get("ficha"):
            r["ficha"] = _apply_masking(r["ficha"], role)
    return res


@router.post("/vital", response_model=dict)
async def cliente_vital(body: VitalRequest, request: Request):
    """Ficha vital de un cliente (WISP vivo + Influx). serial/documento/mac
    resuelven una ficha; nombre devuelve la primera coincidencia. Enmascarada
    segun rol."""
    role = _role_from_request(request)
    if not any([body.serial, body.documento, body.mac, body.nombre]):
        raise HTTPException(status_code=400,
                            detail="Debe indicar serial, documento, mac o nombre")
    res = _get_client_search().find(
        serial=body.serial, documento=body.documento,
        mac=body.mac, nombre=body.nombre,
    )
    if not res.get("found") or not res.get("ficha"):
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return {"found": True, "ficha": _apply_masking(res["ficha"], role)}


@router.post("/grupo/analizar", response_model=dict)
async def grupo_analizar(body: GrupoAnalizarRequest, request: Request):
    """Análisis de un grupo de clientes afectados. entrada: seriales explicitos
    y/o OLT/PON y/o NAP. Devuelve total, offline, loss, low_power y afectados
    con direccion (enmascarada segun rol)."""
    role = _role_from_request(request)
    if not any([body.serials, body.olt_name, body.pon_port, body.nap]):
        raise HTTPException(status_code=400,
                            detail="Indique serials, olt_name+pon_port o nap")
    res = _get_client_search().group_analysis(
        serials=body.serials or None,
        olt_name=body.olt_name, pon_port=body.pon_port, nap=body.nap,
    )
    res["afectados"] = [
        _apply_masking(a, role) for a in res["afectados"]
    ]
    return res


@router.get("/{documento}", response_model=ClienteDetalle)
async def ficha(request: Request, documento: str):
    """Ficha completa por documento. Consulta el cache Redis (instantaneo)
    y cae a InfluxDB si Redis no tiene la ficha o no esta disponible.
    Tambien cae a la ficha pendiente si el cliente no tiene cruce ONU aun.
    Respuesta enmascarada segun rol."""
    role = _role_from_request(request)
    row = _try_store(lambda s: s.get_ficha(documento)) or {}
    if not row:
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