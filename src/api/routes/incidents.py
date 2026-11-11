# src/api/routes/incidents.py
"""Endpoints para gestión de incidencias persistes.

API v1 - agente-monitor
"""
import redis as _redis
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends, Query

from ...incidents.store import Incident, IncidentStore

router = APIRouter()

# Store de incidencias (se inicializa con Redis)
_incident_store: Optional[IncidentStore] = None


def _redis_url() -> str:
    """URL de Redis desde config o variable de entorno."""
    import os
    from ...config_ia import load_config_ia
    
    url = os.environ.get("REDIS_URL")
    if url:
        return url
    try:
        return load_config_ia(
            os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
        ).redis.url
    except Exception:
        # Misma base que el resto del sistema (clientes, grupos, wisp).
        # Usar otra DB aquí dejaba las incidencias aisladas del resto.
        return "redis://localhost:6379/0"


def get_incident_store() -> IncidentStore:
    """Obtiene o inicializa el store de incidencias."""
    global _incident_store
    if _incident_store is None:
        try:
            _incident_store = IncidentStore(
                _redis.Redis.from_url(_redis_url(), socket_timeout=3, decode_responses=True)
            )
        except Exception:
            _incident_store = IncidentStore(None)
    return _incident_store


@router.get("/")
async def list_incidents(
    limit: int = Query(50, description="Máximo número de incidencias"),
    estado: Optional[str] = Query(None, description="Filtrar por estado (open/resolved)"),
    severidad: Optional[str] = Query(None, description="Filtrar por severidad (critical/warning/resolved)"),
    olt: Optional[str] = Query(None, description="Filtrar por nombre de OLT"),
    store: IncidentStore = Depends(get_incident_store)
) -> dict:
    """
    Listar incidencias con filtros.
    
    Orden: automáticamente por orden de llegada (timestamp de inicio, descendente).
    
    Returns:
        dict: Objeto con lista de incidencias y metadatos
    """
    incidents: List[Incident] = store.list_all(limit=limit)
    
    # Aplicar filtros
    if estado:
        incidents = [i for i in incidents if i.estado == estado]
    if severidad:
        incidents = [i for i in incidents if i.severidad == severidad]
    if olt:
        incidents = [i for i in incidents if i.olt == olt]
    
    return {
        "incidents": [i.to_dict() for i in incidents],
        "count": len(incidents),
        "filters": {
            "estado": estado,
            "severidad": severidad,
            "olt": olt,
            "limit": limit
        }
    }


@router.get("/{incident_id}")
async def get_incident(
    incident_id: str,
    store: IncidentStore = Depends(get_incident_store)
) -> dict:
    """
    Obtener detalles de una incidencia específica.
    
    Args:
        incident_id: ID de la incidencia (ej: INC-2026-11-05-0001)
    
    Returns:
        dict: Detalles completos de la incidencia
    """
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incidencia {incident_id} no encontrada")
    
    result = incident.to_dict()
    # Añadir campos calculados
    result["duracion"] = _calculate_duration(incident)
    return result


@router.post("/{incident_id}/ack")
async def acknowledge_incident(
    incident_id: str,
    store: IncidentStore = Depends(get_incident_store)
) -> dict:
    """
    Reconocer una incidencia (marcar como vista por operador).
    
    Args:
        incident_id: ID de la incidencia
    
    Returns:
        dict: Estado actualizado
    """
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incidencia {incident_id} no encontrada")
    
    incident.marcar_reconocida()
    store.save(incident)
    
    return {
        "success": True,
        "incident_id": incident_id,
        "reconocida": incident.reconocida
    }


@router.post("/{incident_id}/mute")
async def mute_incident(
    incident_id: str,
    hours: int = Query(1, ge=1, le=24, description="Horas a silenciar"),
    store: IncidentStore = Depends(get_incident_store)
) -> dict:
    """
    Silenciar notificaciones de una incidencia por N horas.
    
    Args:
        incident_id: ID de la incidencia
        hours: Número de horas (1-24)
    
    Returns:
        dict: Estado actualizado
    """
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incidencia {incident_id} no encontrada")
    
    incident.silenciar(horas=hours)
    store.save(incident)
    
    return {
        "success": True,
        "incident_id": incident_id,
        "silenciada_hasta": incident.silenciada_hasta
    }


@router.post("/{incident_id}/resolve")
async def resolve_incident(
    incident_id: str,
    store: IncidentStore = Depends(get_incident_store)
) -> dict:
    """
    Cerrar/resolver una incidencia (marcando como recuperada).
    
    Args:
        incident_id: ID de la incidencia
    
    Returns:
        dict: Estado actualizado
    """
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incidencia {incident_id} no encontrada")
    
    import time
    incident.resolver(fin_ts=time.time())
    store.save(incident)
    
    return {
        "success": True,
        "incident_id": incident_id,
        "estado": incident.estado,
        "severidad": incident.severidad,
        "fin": incident.fin
    }


@router.get("/{incident_id}/affected")
async def get_affected_customers(
    incident_id: str,
    store: IncidentStore = Depends(get_incident_store)
) -> dict:
    """
    Obtener lista de clientes afectados por una incidencia.
    
    Incluye datos enriquecidos: nombre, dirección, PPPoE, MAC, serial ONU, Rx power actual.
    
    Args:
        incident_id: ID de la incidencia
    
    Returns:
        dict: Lista de clientes afectados con sus datos
    """
    incident = store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail=f"Incidencia {incident_id} no encontrada")
    
    # Los clientes afectados vienen en el campo 'afectados' del incidente
    # Cada cliente tiene: cliente, direccion, pppoe, mac, serial_onu, rx_power, estado_onu
    return {
        "incident_id": incident_id,
        "clientes": incident.afectados,
        "count": len(incident.afectados)
    }


def _calculate_duration(incident: Incident) -> str:
    """Calcula la duración formateada de una incidencia."""
    if not incident.inicio:
        return "-"
    
    start = incident.inicio
    end = incident.fin if incident.fin else __import__('time').time()
    
    # inicio/fin están en segundos epoch (no milisegundos).
    duration_s = end - start
    total_min = int(duration_s / 60)
    
    if total_min < 60:
        return f"{total_min} min"
    elif total_min < 1440:  # menos de 24h
        h = total_min // 60
        m = total_min % 60
        return f"{h} h {m} min"
    else:
        d = total_min // 1440
        h = (total_min % 1440) // 60
        return f"{d} d {h} h"