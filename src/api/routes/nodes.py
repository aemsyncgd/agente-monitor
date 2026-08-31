# src/api/routes/nodes.py
"""
API Routes - Gestión de Nodos y dispositivos de red.
CRUD completo para nodos, MikroTik y OLTs.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime

from ...config_manager import get_config_manager
from ...config_ia import Node, MikroTikDevice, OLTDevice, InterfaceInfo
from ..influx_helper import get_active_device_ips


router = APIRouter(prefix="/api/nodes", tags=["nodes"])


# === Pydantic Models ===

class InterfaceResponse(BaseModel):
    name: str
    description: str


class MikroTikResponse(BaseModel):
    ip: str
    hostname: str
    community: str
    role: str
    modelo: str
    interfaces: List[InterfaceResponse]
    status: str
    node: Optional[str] = None
    conectado_a: str = ""


class OLTResponse(BaseModel):
    ip: str
    hostname: str
    community: str
    modelo: str
    pon_count: int
    descripcion: str
    status: str
    node: Optional[str] = None
    conectado_a: str = ""


class NodeResponse(BaseModel):
    name: str
    description: str
    status: str
    interval_seconds: int
    mikrotik_count: int
    olt_count: int
    mikrotiks: List[MikroTikResponse]
    olts: List[OLTResponse]


class NodeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    description: str = ""
    interval_seconds: int = Field(60, ge=10, le=3600)


class NodeUpdate(BaseModel):
    description: Optional[str] = None
    interval_seconds: Optional[int] = Field(None, ge=10, le=3600)


class MikroTikCreate(BaseModel):
    ip: str = Field(..., pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    hostname: str = Field(..., min_length=1)
    community: str = Field(..., min_length=1, description="SNMP community string")
    role: str = Field("principal", pattern=r"^(principal|distribucion|secundario|respaldo)$")
    modelo: str = ""
    interfaces: List[Dict[str, str]] = []
    conectado_a: str = ""


class MikroTikUpdate(BaseModel):
    hostname: Optional[str] = None
    community: Optional[str] = None
    role: Optional[str] = None
    modelo: Optional[str] = None
    interfaces: Optional[List[Dict[str, str]]] = None
    conectado_a: Optional[str] = None


class OLTCreate(BaseModel):
    ip: str = Field(..., pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    hostname: str = Field(..., min_length=1)
    community: str = Field(..., min_length=1, description="SNMP community string")
    modelo: str = Field(..., pattern=r"^(V1600G0B|V1600G1|V1600G2B)$")
    pon_count: int = Field(8, ge=1, le=32)
    descripcion: str = ""
    conectado_a: str = ""


class OLTUpdate(BaseModel):
    hostname: Optional[str] = None
    community: Optional[str] = None
    modelo: Optional[str] = None
    pon_count: Optional[int] = Field(None, ge=1, le=32)
    descripcion: Optional[str] = None
    conectado_a: Optional[str] = None


class MoveDeviceRequest(BaseModel):
    target_node: str = Field(..., min_length=1)


class ConfigSummary(BaseModel):
    nodes_count: int
    total_mikrotiks: int
    total_olts: int
    last_modified: Optional[str]
    nodes: List[str]


class ConfigErrors(BaseModel):
    errors: List[str]
    valid: bool


# === Endpoints de Nodos ===

@router.get("/summary", response_model=ConfigSummary)
async def get_config_summary():
    """Retorna resumen de la configuración."""
    manager = get_config_manager()
    summary = manager.get_summary()
    return ConfigSummary(**summary)


@router.get("/validate", response_model=ConfigErrors)
async def validate_config():
    """Valida la configuración actual."""
    manager = get_config_manager()
    errors = manager.validate_config()
    return ConfigErrors(errors=errors, valid=len(errors) == 0)


@router.get("/", response_model=List[NodeResponse])
async def list_nodes():
    """Lista todos los nodos con sus dispositivos."""
    manager = get_config_manager()
    nodes = manager.get_nodes()
    active_ips = get_active_device_ips()
    
    result = []
    for node in nodes:
        mikrotiks = [
            MikroTikResponse(
                ip=mt.ip,
                hostname=mt.hostname,
                community=mt.community,
                role=mt.role,
                modelo=mt.modelo,
                interfaces=[InterfaceResponse(name=i.name, description=i.description) for i in mt.interfaces],
                status="online" if mt.ip in active_ips else "unknown",
                node=node.name,
                conectado_a=mt.conectado_a
            )
            for mt in node.mikrotiks
        ]
        
        olts = [
            OLTResponse(
                ip=olt.ip,
                hostname=olt.hostname,
                community=olt.community,
                modelo=olt.modelo,
                pon_count=olt.pon_count,
                descripcion=olt.descripcion,
                status="online" if olt.ip in active_ips else "unknown",
                node=node.name,
                conectado_a=olt.conectado_a
            )
            for olt in node.olts
        ]
        
        device_statuses = [d.status for d in mikrotiks + olts]
        if any(s == "online" for s in device_statuses):
            node_status = "online"
        elif not device_statuses:
            node_status = "unknown"
        else:
            node_status = "unknown"
        
        result.append(NodeResponse(
            name=node.name,
            description=node.description,
            status=node_status,
            interval_seconds=node.interval_seconds,
            mikrotik_count=len(mikrotiks),
            olt_count=len(olts),
            mikrotiks=mikrotiks,
            olts=olts
        ))
    
    return result


@router.get("/{node_name}", response_model=NodeResponse)
async def get_node(node_name: str):
    """Retorna un nodo específico."""
    manager = get_config_manager()
    node = manager.get_node(node_name)
    
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    active_ips = get_active_device_ips()
    
    mikrotiks = [
        MikroTikResponse(
            ip=mt.ip,
            hostname=mt.hostname,
            community=mt.community,
            role=mt.role,
            modelo=mt.modelo,
            interfaces=[InterfaceResponse(name=i.name, description=i.description) for i in mt.interfaces],
            status="online" if mt.ip in active_ips else "unknown",
            node=node.name,
            conectado_a=mt.conectado_a
        )
        for mt in node.mikrotiks
    ]
    
    olts = [
        OLTResponse(
            ip=olt.ip,
            hostname=olt.hostname,
            community=olt.community,
            modelo=olt.modelo,
            pon_count=olt.pon_count,
            descripcion=olt.descripcion,
            status="online" if olt.ip in active_ips else "unknown",
            node=node.name,
            conectado_a=olt.conectado_a
        )
        for olt in node.olts
    ]
    
    device_statuses = [d.status for d in mikrotiks + olts]
    node_status = "online" if any(s == "online" for s in device_statuses) else "unknown"
    
    return NodeResponse(
        name=node.name,
        description=node.description,
        status=node_status,
        interval_seconds=node.interval_seconds,
        mikrotik_count=len(mikrotiks),
        olt_count=len(olts),
        mikrotiks=mikrotiks,
        olts=olts
    )


@router.post("/", response_model=NodeResponse, status_code=201)
async def create_node(node_data: NodeCreate):
    """Crea un nuevo nodo."""
    manager = get_config_manager()
    
    # Verificar que no exista
    if manager.get_node(node_data.name):
        raise HTTPException(status_code=409, detail=f"El nodo {node_data.name} ya existe")
    
    node = Node(
        name=node_data.name.upper(),
        description=node_data.description,
        interval_seconds=node_data.interval_seconds
    )
    
    if not manager.add_node(node):
        raise HTTPException(status_code=500, detail="Error al crear nodo")
    
    return NodeResponse(
        name=node.name,
        description=node.description,
        status=node.status,
        interval_seconds=node.interval_seconds,
        mikrotik_count=0,
        olt_count=0,
        mikrotiks=[],
        olts=[]
    )


@router.put("/{node_name}", response_model=NodeResponse)
async def update_node(node_name: str, updates: NodeUpdate):
    """Actualiza un nodo existente."""
    manager = get_config_manager()
    
    update_dict = {}
    if updates.description is not None:
        update_dict["description"] = updates.description
    if updates.interval_seconds is not None:
        update_dict["interval_seconds"] = updates.interval_seconds
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    
    if not manager.update_node(node_name, update_dict):
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    return await get_node(node_name)


@router.delete("/{node_name}")
async def delete_node(node_name: str):
    """Elimina un nodo y todos sus dispositivos."""
    manager = get_config_manager()
    
    node = manager.get_node(node_name)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    # Guardar info antes de eliminar
    mikrotik_count = len(node.mikrotiks)
    olt_count = len(node.olts)
    
    if not manager.delete_node(node_name):
        raise HTTPException(status_code=500, detail="Error al eliminar nodo")
    
    return {
        "message": f"Nodo {node_name} eliminado",
        "mikrotiks_removed": mikrotik_count,
        "olts_removed": olt_count
    }


# === Endpoints de MikroTik ===

@router.get("/{node_name}/mikrotiks", response_model=List[MikroTikResponse])
async def list_node_mikrotiks(node_name: str):
    """Lista los MikroTik de un nodo."""
    manager = get_config_manager()
    node = manager.get_node(node_name)
    
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    return [
        MikroTikResponse(
            ip=mt.ip,
            hostname=mt.hostname,
            community=mt.community,
            role=mt.role,
            modelo=mt.modelo,
            interfaces=[InterfaceResponse(name=i.name, description=i.description) for i in mt.interfaces],
            status=mt.status,
            node=node.name,
            conectado_a=mt.conectado_a
        )
        for mt in node.mikrotiks
    ]


@router.post("/{node_name}/mikrotiks", response_model=MikroTikResponse, status_code=201)
async def add_mikrotik_to_node(node_name: str, mikrotik_data: MikroTikCreate):
    """Agrega un MikroTik a un nodo."""
    manager = get_config_manager()
    
    node = manager.get_node(node_name)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    # Verificar IP no duplicada
    if manager.get_mikrotik_by_ip(mikrotik_data.ip):
        raise HTTPException(status_code=409, detail=f"Ya existe un MikroTik con IP {mikrotik_data.ip}")
    
    interfaces = [InterfaceInfo(name=i.get("name", ""), description=i.get("description", "")) for i in mikrotik_data.interfaces]
    
    mikrotik = MikroTikDevice(
        ip=mikrotik_data.ip,
        hostname=mikrotik_data.hostname,
        community=mikrotik_data.community,
        role=mikrotik_data.role,
        modelo=mikrotik_data.modelo,
        interfaces=interfaces,
        conectado_a=mikrotik_data.conectado_a
    )
    
    if not manager.add_mikrotik_to_node(node_name, mikrotik):
        raise HTTPException(status_code=500, detail="Error al agregar MikroTik")
    
    return MikroTikResponse(
        ip=mikrotik.ip,
        hostname=mikrotik.hostname,
        community=mikrotik.community,
        role=mikrotik.role,
        modelo=mikrotik.modelo,
        interfaces=[InterfaceResponse(name=i.name, description=i.description) for i in mikrotik.interfaces],
        status=mikrotik.status,
        node=node_name,
        conectado_a=mikrotik.conectado_a
    )


@router.put("/mikrotiks/{ip}", response_model=MikroTikResponse)
async def update_mikrotik(ip: str, updates: MikroTikUpdate):
    """Actualiza un MikroTik por IP."""
    manager = get_config_manager()
    
    update_dict = {}
    if updates.hostname is not None:
        update_dict["hostname"] = updates.hostname
    if updates.community is not None:
        update_dict["community"] = updates.community
    if updates.role is not None:
        update_dict["role"] = updates.role
    if updates.modelo is not None:
        update_dict["modelo"] = updates.modelo
    if updates.interfaces is not None:
        update_dict["interfaces"] = [InterfaceInfo(name=i.get("name", ""), description=i.get("description", "")) for i in updates.interfaces]
    if updates.conectado_a is not None:
        update_dict["conectado_a"] = updates.conectado_a
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    
    if not manager.update_mikrotik(ip, update_dict):
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    mt = manager.get_mikrotik_by_ip(ip)
    node = manager.get_node_for_mikrotik(ip)
    
    return MikroTikResponse(
        ip=mt.ip,
        hostname=mt.hostname,
        community=mt.community,
        role=mt.role,
        modelo=mt.modelo,
        interfaces=[InterfaceResponse(name=i.name, description=i.description) for i in mt.interfaces],
        status=mt.status,
        node=node.name if node else None,
        conectado_a=mt.conectado_a
    )


@router.delete("/mikrotiks/{ip}")
async def delete_mikrotik(ip: str):
    """Elimina un MikroTik por IP."""
    manager = get_config_manager()
    
    mt = manager.get_mikrotik_by_ip(ip)
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    hostname = mt.hostname
    
    if not manager.delete_mikrotik(ip):
        raise HTTPException(status_code=500, detail="Error al eliminar MikroTik")
    
    return {"message": f"MikroTik {hostname} ({ip}) eliminado"}


@router.post("/mikrotiks/{ip}/move")
async def move_mikrotik(ip: str, request: MoveDeviceRequest):
    """Mueve un MikroTik a otro nodo."""
    manager = get_config_manager()
    
    mt = manager.get_mikrotik_by_ip(ip)
    if not mt:
        raise HTTPException(status_code=404, detail=f"MikroTik con IP {ip} no encontrado")
    
    hostname = mt.hostname
    
    if not manager.move_mikrotik(ip, request.target_node):
        raise HTTPException(status_code=500, detail=f"Error al mover MikroTik a nodo {request.target_node}")
    
    return {"message": f"MikroTik {hostname} movido a nodo {request.target_node}"}


# === Endpoints de OLTs ===

@router.get("/{node_name}/olts", response_model=List[OLTResponse])
async def list_node_olts(node_name: str):
    """Lista las OLTs de un nodo."""
    manager = get_config_manager()
    node = manager.get_node(node_name)
    
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    return [
        OLTResponse(
            ip=olt.ip,
            hostname=olt.hostname,
            community=olt.community,
            modelo=olt.modelo,
            pon_count=olt.pon_count,
            descripcion=olt.descripcion,
            status=olt.status,
            node=node.name,
            conectado_a=olt.conectado_a
        )
        for olt in node.olts
    ]


@router.post("/{node_name}/olts", response_model=OLTResponse, status_code=201)
async def add_olt_to_node(node_name: str, olt_data: OLTCreate):
    """Agrega una OLT a un nodo."""
    manager = get_config_manager()
    
    node = manager.get_node(node_name)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_name} no encontrado")
    
    if manager.get_olt_by_ip(olt_data.ip):
        raise HTTPException(status_code=409, detail=f"Ya existe una OLT con IP {olt_data.ip}")
    
    olt = OLTDevice(
        ip=olt_data.ip,
        hostname=olt_data.hostname,
        community=olt_data.community,
        modelo=olt_data.modelo,
        pon_count=olt_data.pon_count,
        descripcion=olt_data.descripcion,
        conectado_a=olt_data.conectado_a
    )
    
    if not manager.add_olt_to_node(node_name, olt):
        raise HTTPException(status_code=500, detail="Error al agregar OLT")
    
    return OLTResponse(
        ip=olt.ip,
        hostname=olt.hostname,
        community=olt.community,
        modelo=olt.modelo,
        pon_count=olt.pon_count,
        descripcion=olt.descripcion,
        status=olt.status,
        node=node_name,
        conectado_a=olt.conectado_a
    )


@router.put("/olts/{ip}", response_model=OLTResponse)
async def update_olt(ip: str, updates: OLTUpdate):
    """Actualiza una OLT por IP."""
    manager = get_config_manager()
    
    update_dict = {}
    if updates.hostname is not None:
        update_dict["hostname"] = updates.hostname
    if updates.community is not None:
        update_dict["community"] = updates.community
    if updates.modelo is not None:
        update_dict["modelo"] = updates.modelo
    if updates.pon_count is not None:
        update_dict["pon_count"] = updates.pon_count
    if updates.descripcion is not None:
        update_dict["descripcion"] = updates.descripcion
    if updates.conectado_a is not None:
        update_dict["conectado_a"] = updates.conectado_a
    
    if not update_dict:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    
    if not manager.update_olt(ip, update_dict):
        raise HTTPException(status_code=404, detail=f"OLT con IP {ip} no encontrada")
    
    olt = manager.get_olt_by_ip(ip)
    node = manager.get_node_for_olt(ip)
    
    return OLTResponse(
        ip=olt.ip,
        hostname=olt.hostname,
        community=olt.community,
        modelo=olt.modelo,
        pon_count=olt.pon_count,
        descripcion=olt.descripcion,
        status=olt.status,
        node=node.name if node else None,
        conectado_a=olt.conectado_a
    )


@router.delete("/olts/{ip}")
async def delete_olt(ip: str):
    """Elimina una OLT por IP."""
    manager = get_config_manager()
    
    olt = manager.get_olt_by_ip(ip)
    if not olt:
        raise HTTPException(status_code=404, detail=f"OLT con IP {ip} no encontrada")
    
    hostname = olt.hostname
    
    if not manager.delete_olt(ip):
        raise HTTPException(status_code=500, detail="Error al eliminar OLT")
    
    return {"message": f"OLT {hostname} ({ip}) eliminada"}


@router.post("/olts/{ip}/move")
async def move_olt(ip: str, request: MoveDeviceRequest):
    """Mueve una OLT a otro nodo."""
    manager = get_config_manager()
    
    olt = manager.get_olt_by_ip(ip)
    if not olt:
        raise HTTPException(status_code=404, detail=f"OLT con IP {ip} no encontrada")
    
    hostname = olt.hostname
    
    if not manager.move_olt(ip, request.target_node):
        raise HTTPException(status_code=500, detail=f"Error al mover OLT a nodo {request.target_node}")
    
    return {"message": f"OLT {hostname} movida a nodo {request.target_node}"}
