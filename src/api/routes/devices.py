# src/api/routes/devices.py
"""
API Routes - Gestión de dispositivos individuales.
Operaciones específicas por tipo de dispositivo.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

from ...config_manager import get_config_manager
from ...config_ia import MikroTikDevice, OLTDevice, InterfaceInfo
from ..influx_helper import get_active_device_ips


router = APIRouter(prefix="/api/devices", tags=["devices"])


# === Pydantic Models ===

class DeviceSearchResult(BaseModel):
    type: str  # "mikrotik" or "olt"
    ip: str
    hostname: str
    node: str
    details: Dict[str, Any]


class BulkStatusUpdate(BaseModel):
    devices: List[Dict[str, str]]  # [{"ip": "...", "status": "online"}, ...]


class MikroTikInterface(BaseModel):
    name: str
    description: str = ""


class MikroTikBulkUpdate(BaseModel):
    community: Optional[str] = None
    role: Optional[str] = None


class OLTBulkUpdate(BaseModel):
    community: Optional[str] = None
    modelo: Optional[str] = None


class DeviceTestRequest(BaseModel):
    ip: str
    community: str = Field(..., min_length=1, description="SNMP community string")
    type: str = "mikrotik"  # "mikrotik" or "olt"
    hostname: Optional[str] = None


class DeviceTestResponse(BaseModel):
    success: bool
    ip: str
    logs: List[str]
    details: Dict[str, Any] = {}


# === Endpoint de Test / Diagnóstico de Conexión ===

@router.post("/test", response_model=DeviceTestResponse)
async def test_device_connection(req: DeviceTestRequest):
    """Realiza una prueba en vivo de conectividad ICMP (Ping) y consulta SNMP contra un dispositivo."""
    import asyncio
    import subprocess

    ip = req.ip.strip()
    community = req.community.strip()
    dev_type = req.type.lower().strip()
    logs = []
    details = {}

    logs.append(f"[INIT] Iniciando diagnóstico para {ip} (tipo: {dev_type.upper()}, comunidad: '{community}')...")

    # 1. Test ICMP Ping
    logs.append(f"[STEP 1] Probando conectividad ICMP (Ping) a {ip}...")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "2", "-W", "2", ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logs.append(f"  --> ICMP Ping Exitoso [OK]")
            details["ping"] = True
        else:
            logs.append(f"  --> ICMP Ping Fallido (sin respuesta del host) [FAIL]")
            details["ping"] = False
    except Exception as e:
        logs.append(f"  --> Error ejecutando ping: {e}")
        details["ping"] = False

    # 2. Test SNMP (sysDescr 1.3.6.1.2.1.1.1.0 y sysUptime 1.3.6.1.2.1.1.3.0)
    logs.append(f"[STEP 2] Probando consulta SNMP (UDP/161) v2c con comunidad '{community}'...")
    snmp_ok = False
    try:
        try:
            from pysnmp.hlapi.v3arch.asyncio import get_cmd, SnmpEngine, CommunityData, UdpTransportTarget, ContextData, ObjectType, ObjectIdentity
            target = await UdpTransportTarget.create((ip, 161), timeout=3.0, retries=1)
            errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
                SnmpEngine(),
                CommunityData(community, mpModel=1),
                target,
                ContextData(),
                ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
                ObjectType(ObjectIdentity("1.3.6.1.2.1.1.3.0")),
            )
        except (ImportError, AttributeError):
            from pysnmp.hlapi import getCmd, SnmpEngine, CommunityData, UdpTransportTarget, ContextData, ObjectType, ObjectIdentity
            def _snmp_query():
                return next(
                    getCmd(
                        SnmpEngine(),
                        CommunityData(community, mpModel=1),
                        UdpTransportTarget((ip, 161), timeout=3.0, retries=1),
                        ContextData(),
                        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.1.0")),
                        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.3.0")),
                    )
                )
            errorIndication, errorStatus, errorIndex, varBinds = await asyncio.to_thread(_snmp_query)

        if errorIndication:
            logs.append(f"  --> Fallo de respuesta SNMP: {errorIndication} [FAIL]")
            logs.append(f"  --> [DIAGNÓSTICO] El equipo responde a Ping pero NO a SNMP. Verifique SNMP activado y firewall UDP 161.")
        elif errorStatus:
            logs.append(f"  --> Error SNMP status: {errorStatus.prettyPrint()} en el índice {errorIndex} [FAIL]")
        else:
            snmp_ok = True
            logs.append(f"  --> Consulta SNMP Exitosa! [OK]")
            for varBind in varBinds:
                oid_str = str(varBind[0])
                val_str = str(varBind[1])
                if "1.1.1.0" in oid_str:
                    logs.append(f"      - sysDescr: {val_str}")
                    details["sysDescr"] = val_str
                elif "1.1.3.0" in oid_str:
                    logs.append(f"      - sysUptime: {val_str}")
                    details["sysUptime"] = val_str
    except Exception as e:
        logs.append(f"  --> Excepción al realizar consulta SNMP: {e} [FAIL]")

    details["snmp"] = snmp_ok
    overall_success = details.get("ping", False) and snmp_ok

    if overall_success:
        logs.append(f"[RESULTADO] STATUS OK - La conexión es totalmente correcta y las métricas fluirán.")
    elif details.get("ping", False) and not snmp_ok:
        logs.append(f"[RESULTADO] STATUS PARCIAL - El equipo responde a Ping pero el servicio SNMP está bloqueado o deshabilitado.")
    else:
        logs.append(f"[RESULTADO] STATUS ERROR - El equipo no está accesible por IP/Ping ni por SNMP.")

    return DeviceTestResponse(
        success=overall_success,
        ip=ip,
        logs=logs,
        details=details
    )


# === Endpoints de búsqueda ===

@router.get("/search", response_model=List[DeviceSearchResult])
async def search_devices(
    q: str = Query(..., min_length=1, description="Término de búsqueda (IP, hostname, etc.)"),
    type: Optional[str] = Query(None, pattern=r"^(mikrotik|olt)$", description="Filtrar por tipo")
):
    """Busca dispositivos por IP, hostname o descripción."""
    manager = get_config_manager()
    results = []
    query = q.lower()
    
    # Buscar en MikroTik
    if type is None or type == "mikrotik":
        for mt in manager.get_all_mikrotiks():
            node = manager.get_node_for_mikrotik(mt.ip)
            if (query in mt.ip or 
                query in mt.hostname.lower() or 
                query in mt.modelo.lower()):
                results.append(DeviceSearchResult(
                    type="mikrotik",
                    ip=mt.ip,
                    hostname=mt.hostname,
                    node=node.name if node else "",
                    details=mt.to_dict()
                ))
    
    # Buscar en OLTs
    if type is None or type == "olt":
        for olt in manager.get_all_olts():
            node = manager.get_node_for_olt(olt.ip)
            if (query in olt.ip or 
                query in olt.hostname.lower() or 
                query in olt.modelo.lower() or
                query in olt.descripcion.lower()):
                results.append(DeviceSearchResult(
                    type="olt",
                    ip=olt.ip,
                    hostname=olt.hostname,
                    node=node.name if node else "",
                    details=olt.to_dict()
                ))
    
    return results


@router.get("/all")
async def list_all_devices():
    """Lista todos los dispositivos agrupados por tipo."""
    manager = get_config_manager()
    devices = manager.get_flat_device_list()
    try:
        active_ips = get_active_device_ips()
    except Exception:
        active_ips = {}
    for group in ("mikrotiks", "olts"):
        for device in devices.get(group, []):
            if device.get("ip") in active_ips:
                device["status"] = "online"
    return devices


# === Endpoints de MikroTik específicos ===

@router.get("/mikrotik/{ip}")
async def get_mikrotik_details(ip: str):
    """Retorna detalles completos de un MikroTik."""
    manager = get_config_manager()
    mt = manager.get_mikrotik_by_ip(ip)
    
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    node = manager.get_node_for_mikrotik(ip)
    
    return {
        "device": mt.to_dict(),
        "node": node.name if node else None,
        "interfaces": [i.to_dict() for i in mt.interfaces]
    }


@router.get("/mikrotik/{ip}/interfaces")
async def get_mikrotik_interfaces(ip: str):
    """Retorna las interfaces de un MikroTik."""
    manager = get_config_manager()
    mt = manager.get_mikrotik_by_ip(ip)
    
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    return [i.to_dict() for i in mt.interfaces]


@router.put("/mikrotik/{ip}/interfaces")
async def update_mikrotik_interfaces(ip: str, interfaces: List[MikroTikInterface]):
    """Actualiza las interfaces de un MikroTik."""
    manager = get_config_manager()
    
    mt = manager.get_mikrotik_by_ip(ip)
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    iface_dicts = [{"name": i.name, "description": i.description} for i in interfaces]
    
    if not manager.update_mikrotik(ip, {"interfaces": iface_dicts}):
        raise HTTPException(status_code=500, detail="Error al actualizar interfaces")
    
    return {"message": "Interfaces actualizadas", "count": len(interfaces)}


@router.post("/mikrotik/{ip}/interfaces")
async def add_mikrotik_interface(ip: str, interface: MikroTikInterface):
    """Agrega una interfaz a un MikroTik."""
    manager = get_config_manager()
    
    mt = manager.get_mikrotik_by_ip(ip)
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    # Verificar que no exista
    for existing in mt.interfaces:
        if existing.name == interface.name:
            raise HTTPException(status_code=409, detail=f"La interfaz {interface.name} ya existe")
    
    # Agregar
    interfaces = [i.to_dict() for i in mt.interfaces]
    interfaces.append({"name": interface.name, "description": interface.description})
    
    if not manager.update_mikrotik(ip, {"interfaces": interfaces}):
        raise HTTPException(status_code=500, detail="Error al agregar interfaz")
    
    return {"message": f"Interfaz {interface.name} agregada"}


@router.delete("/mikrotik/{ip}/interfaces/{interface_name}")
async def delete_mikrotik_interface(ip: str, interface_name: str):
    """Elimina una interfaz de un MikroTik."""
    manager = get_config_manager()
    
    mt = manager.get_mikrotik_by_ip(ip)
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    # Filtrar la interfaz
    interfaces = [i.to_dict() for i in mt.interfaces if i.name != interface_name]
    
    if len(interfaces) == len(mt.interfaces):
        raise HTTPException(status_code=404, detail=f"Interfaz {interface_name} no encontrada")
    
    if not manager.update_mikrotik(ip, {"interfaces": interfaces}):
        raise HTTPException(status_code=500, detail="Error al eliminar interfaz")
    
    return {"message": f"Interfaz {interface_name} eliminada"}


@router.put("/mikrotik/bulk")
async def bulk_update_mikrotiks(
    updates: MikroTikBulkUpdate,
    ips: List[str] = Query(..., description="Lista de IPs")
):
    """Actualiza múltiples MikroTik a la vez."""
    manager = get_config_manager()
    updated = 0
    errors = []
    
    update_dict = {}
    if updates.community is not None:
        update_dict["community"] = updates.community
    if updates.role is not None:
        update_dict["role"] = updates.role
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    
    for ip in ips:
        if manager.update_mikrotik(ip, update_dict, save=False):
            updated += 1
        else:
            errors.append(ip)
    
    # Guardar una sola vez
    if updated > 0:
        manager._save_nodes_yaml()
    
    return {"updated": updated, "errors": errors}


# === Endpoints de OLT específicos ===

@router.get("/olt/{ip}")
async def get_olt_details(ip: str):
    """Retorna detalles completos de una OLT."""
    manager = get_config_manager()
    olt = manager.get_olt_by_ip(ip)
    
    if not olt:
        raise HTTPException(status_code=404, detail=f"OLT con IP {ip} no encontrada")
    
    node = manager.get_node_for_olt(ip)
    
    return {
        "device": olt.to_dict(),
        "node": node.name if node else None
    }


@router.put("/olt/bulk")
async def bulk_update_olts(
    updates: OLTBulkUpdate,
    ips: List[str] = Query(..., description="Lista de IPs")
):
    """Actualiza múltiples OLTs a la vez."""
    manager = get_config_manager()
    updated = 0
    errors = []
    
    update_dict = {}
    if updates.community is not None:
        update_dict["community"] = updates.community
    if updates.modelo is not None:
        update_dict["modelo"] = updates.modelo
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    
    for ip in ips:
        if manager.update_olt(ip, update_dict, save=False):
            updated += 1
        else:
            errors.append(ip)
    
    if updated > 0:
        manager._save_nodes_yaml()
    
    return {"updated": updated, "errors": errors}


# === Endpoints de estadísticas ===

@router.get("/stats")
async def get_device_stats():
    """Retorna estadísticas de dispositivos."""
    manager = get_config_manager()
    nodes = manager.get_nodes()
    
    mikrotik_by_role = {}
    olt_by_model = {}
    devices_by_node = {}
    
    for node in nodes:
        devices_by_node[node.name] = {
            "mikrotiks": len(node.mikrotiks),
            "olts": len(node.olts)
        }
        
        for mt in node.mikrotiks:
            mikrotik_by_role[mt.role] = mikrotik_by_role.get(mt.role, 0) + 1
        
        for olt in node.olts:
            olt_by_model[olt.modelo] = olt_by_model.get(olt.modelo, 0) + 1
    
    return {
        "total_nodes": len(nodes),
        "total_mikrotiks": sum(len(n.mikrotiks) for n in nodes),
        "total_olts": sum(len(n.olts) for n in nodes),
        "mikrotik_by_role": mikrotik_by_role,
        "olt_by_model": olt_by_model,
        "devices_by_node": devices_by_node
    }
