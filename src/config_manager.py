# src/config_manager.py
"""
Unified Config Manager - Gestiona configuración YAML + overrides de API.
Carga configuración de nodos desde YAML y permite cambios runtime via API.
"""

import os
import yaml
import json
import threading
from typing import List, Optional, Dict, Any
from datetime import datetime
from pathlib import Path

from .config_ia import (
    ConfigIA, Node, MikroTikDevice, OLTDevice, InterfaceInfo,
    load_config_ia, load_nodes_config
)


class ConfigManager:
    """
    Manager unificado de configuración.
    
    - YAML es la fuente de verdad persistente
    - API overrides se aplican en memoria y se sincronizan a YAML
    - Los collectors leen de este manager
    """
    
    def __init__(self, config_path: str, nodes_path: str):
        self.config_path = config_path
        self.nodes_path = nodes_path
        self._lock = threading.RLock()
        self._config: ConfigIA = None
        self._nodes: List[Node] = []
        self._last_modified: float = 0
        self._api_overrides: Dict[str, Any] = {}
        
        self._load_initial()
    
    def _load_initial(self):
        """Carga inicial de configuración."""
        with self._lock:
            self._config = load_config_ia(self.config_path)
            self._nodes = load_nodes_config(self.nodes_path)
            self._update_last_modified()
    
    def _update_last_modified(self):
        """Actualiza timestamp de última modificación."""
        try:
            if os.path.exists(self.nodes_path):
                self._last_modified = os.path.getmtime(self.nodes_path)
        except OSError:
            pass
    
    def _save_nodes_yaml(self):
        """Guarda nodos a YAML."""
        existing = {}
        if os.path.exists(self.nodes_path):
            try:
                with open(self.nodes_path, 'r') as f:
                    existing = yaml.safe_load(f) or {}
            except (yaml.YAMLError, OSError):
                pass

        data = {
            "nodes": [],
            "monitoring": existing.get("monitoring", {
                "default_interval": 60,
                "node_intervals": {},
                "default_snmp_timeout": 15,
                "device_timeouts": {
                    "mikrotik": 10,
                    "olt": 15,
                    "olt_g2b": 20
                }
            })
        }
        
        for node in self._nodes:
            node_dict = {
                "name": node.name,
                "description": node.description,
                "interval_seconds": node.interval_seconds,
                "mikrotiks": [],
                "olts": []
            }
            
            for mt in node.mikrotiks:
                mikrotik_dict = {
                    "ip": mt.ip,
                    "hostname": mt.hostname,
                    "community": mt.community,
                    "role": mt.role,
                    "modelo": mt.modelo,
                    "interfaces": [i.to_dict() for i in mt.interfaces],
                    "conectado_a": mt.conectado_a
                }
                node_dict["mikrotiks"].append(mikrotik_dict)
            
            for olt in node.olts:
                olt_dict = {
                    "ip": olt.ip,
                    "hostname": olt.hostname,
                    "community": olt.community,
                    "modelo": olt.modelo,
                    "pon_count": olt.pon_count,
                    "descripcion": olt.descripcion,
                    "conectado_a": olt.conectado_a,
                    "capturar_mac": olt.capturar_mac
                }
                node_dict["olts"].append(olt_dict)
            
            data["nodes"].append(node_dict)
            
            # Agregar intervalo por nodo
            data["monitoring"]["node_intervals"][node.name] = node.interval_seconds
        
        # Crear directorio si no existe
        os.makedirs(os.path.dirname(self.nodes_path), exist_ok=True)
        
        with open(self.nodes_path, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        
        self._update_last_modified()
    
    # === Operaciones de lectura ===
    
    @property
    def config(self) -> ConfigIA:
        """Retorna configuración global."""
        return self._config
    
    def get_nodes(self) -> List[Node]:
        """Retorna todos los nodos."""
        with self._lock:
            return self._nodes.copy()
    
    def get_node(self, name: str) -> Optional[Node]:
        """Retorna un nodo por nombre."""
        with self._lock:
            for node in self._nodes:
                if node.name.upper() == name.upper():
                    return node
            return None
    
    def get_all_mikrotiks(self) -> List[MikroTikDevice]:
        """Retorna todos los MikroTik de todos los nodos."""
        with self._lock:
            result = []
            for node in self._nodes:
                result.extend(node.mikrotiks)
            return result
    
    def get_all_olts(self) -> List[OLTDevice]:
        """Retorna todas las OLTs de todos los nodos."""
        with self._lock:
            result = []
            for node in self._nodes:
                result.extend(node.olts)
            return result
    
    def get_mikrotik_by_ip(self, ip: str) -> Optional[MikroTikDevice]:
        """Retorna un MikroTik por IP."""
        with self._lock:
            for node in self._nodes:
                mt = node.get_mikrotik_by_ip(ip)
                if mt:
                    return mt
            return None
    
    def get_olt_by_ip(self, ip: str) -> Optional[OLTDevice]:
        """Retorna una OLT por IP."""
        with self._lock:
            for node in self._nodes:
                olt = node.get_olt_by_ip(ip)
                if olt:
                    return olt
            return None
    
    def get_node_for_mikrotik(self, ip: str) -> Optional[Node]:
        """Retorna el nodo que contiene un MikroTik."""
        with self._lock:
            for node in self._nodes:
                if node.get_mikrotik_by_ip(ip):
                    return node
            return None
    
    def get_node_for_olt(self, ip: str) -> Optional[Node]:
        """Retorna el nodo que contiene una OLT."""
        with self._lock:
            for node in self._nodes:
                if node.get_olt_by_ip(ip):
                    return node
            return None
    
    # === Operaciones de escritura ===
    
    def add_node(self, node: Node, save: bool = True) -> bool:
        """Agrega un nuevo nodo."""
        with self._lock:
            if self.get_node(node.name):
                return False
            
            self._nodes.append(node)
            
            if save:
                self._save_nodes_yaml()
            
            return True
    
    def update_node(self, name: str, updates: Dict[str, Any], save: bool = True) -> bool:
        """Actualiza un nodo existente."""
        with self._lock:
            node = self.get_node(name)
            if not node:
                return False
            
            if "description" in updates:
                node.description = updates["description"]
            if "interval_seconds" in updates:
                node.interval_seconds = updates["interval_seconds"]
            
            if save:
                self._save_nodes_yaml()
            
            return True
    
    def delete_node(self, name: str, save: bool = True) -> bool:
        """Elimina un nodo."""
        with self._lock:
            for i, node in enumerate(self._nodes):
                if node.name.upper() == name.upper():
                    self._nodes.pop(i)
                    if save:
                        self._save_nodes_yaml()
                    return True
            return False
    
    # === Operaciones de dispositivos ===
    
    def add_mikrotik_to_node(self, node_name: str, mikrotik: MikroTikDevice, save: bool = True) -> bool:
        """Agrega un MikroTik a un nodo."""
        with self._lock:
            node = self.get_node(node_name)
            if not node:
                return False
            
            result = node.add_mikrotik(mikrotik)
            if result and save:
                self._save_nodes_yaml()
            
            return result
    
    def update_mikrotik(self, ip: str, updates: Dict[str, Any], save: bool = True) -> bool:
        """Actualiza un MikroTik por IP."""
        with self._lock:
            for node in self._nodes:
                mt = node.get_mikrotik_by_ip(ip)
                if mt:
                    if "hostname" in updates:
                        mt.hostname = updates["hostname"]
                    if "community" in updates:
                        mt.community = updates["community"]
                    if "role" in updates:
                        mt.role = updates["role"]
                    if "modelo" in updates:
                        mt.modelo = updates["modelo"]
                    if "interfaces" in updates:
                        mt.interfaces = [
                            InterfaceInfo(**i) if isinstance(i, dict) else i
                            for i in updates["interfaces"]
                        ]
                    if "conectado_a" in updates:
                        mt.conectado_a = updates["conectado_a"]
                    
                    if save:
                        self._save_nodes_yaml()
                    
                    return True
            return False
    
    def delete_mikrotik(self, ip: str, save: bool = True) -> bool:
        """Elimina un MikroTik por IP."""
        with self._lock:
            for node in self._nodes:
                if node.remove_mikrotik(ip):
                    if save:
                        self._save_nodes_yaml()
                    return True
            return False
    
    def move_mikrotik(self, ip: str, target_node: str, save: bool = True) -> bool:
        """Mueve un MikroTik de un nodo a otro."""
        with self._lock:
            # Buscar y remover del nodo actual
            source_node = None
            mikrotik = None
            for node in self._nodes:
                mt = node.get_mikrotik_by_ip(ip)
                if mt:
                    source_node = node
                    mikrotik = mt
                    break
            
            if not source_node or not mikrotik:
                return False
            
            target = self.get_node(target_node)
            if not target:
                return False
            
            # Remover del origen y agregar al destino
            source_node.remove_mikrotik(ip)
            target.add_mikrotik(mikrotik)
            
            if save:
                self._save_nodes_yaml()
            
            return True
    
    def add_olt_to_node(self, node_name: str, olt: OLTDevice, save: bool = True) -> bool:
        """Agrega una OLT a un nodo."""
        with self._lock:
            node = self.get_node(node_name)
            if not node:
                return False
            
            result = node.add_olt(olt)
            if result and save:
                self._save_nodes_yaml()
            
            return result
    
    def update_olt(self, ip: str, updates: Dict[str, Any], save: bool = True) -> bool:
        """Actualiza una OLT por IP."""
        with self._lock:
            for node in self._nodes:
                olt = node.get_olt_by_ip(ip)
                if olt:
                    if "hostname" in updates:
                        olt.hostname = updates["hostname"]
                    if "community" in updates:
                        olt.community = updates["community"]
                    if "modelo" in updates:
                        olt.modelo = updates["modelo"]
                    if "pon_count" in updates:
                        olt.pon_count = updates["pon_count"]
                    if "descripcion" in updates:
                        olt.descripcion = updates["descripcion"]
                    if "conectado_a" in updates:
                        olt.conectado_a = updates["conectado_a"]
                    if "capturar_mac" in updates:
                        olt.capturar_mac = bool(updates["capturar_mac"])
                    
                    if save:
                        self._save_nodes_yaml()
                    
                    return True
            return False
    
    def delete_olt(self, ip: str, save: bool = True) -> bool:
        """Elimina una OLT por IP."""
        with self._lock:
            for node in self._nodes:
                if node.remove_olt(ip):
                    if save:
                        self._save_nodes_yaml()
                    return True
            return False
    
    def move_olt(self, ip: str, target_node: str, save: bool = True) -> bool:
        """Mueve una OLT de un nodo a otro."""
        with self._lock:
            source_node = None
            olt = None
            for node in self._nodes:
                o = node.get_olt_by_ip(ip)
                if o:
                    source_node = node
                    olt = o
                    break
            
            if not source_node or not olt:
                return False
            
            target = self.get_node(target_node)
            if not target:
                return False
            
            source_node.remove_olt(ip)
            target.add_olt(olt)
            
            if save:
                self._save_nodes_yaml()
            
            return True
    
    # === Utilidades ===
    
    def reload_config(self):
        """Recarga configuración desde archivos."""
        self._load_initial()
    
    def get_summary(self) -> Dict[str, Any]:
        """Retorna resumen de la configuración."""
        with self._lock:
            total_mikrotiks = sum(len(n.mikrotiks) for n in self._nodes)
            total_olts = sum(len(n.olts) for n in self._nodes)
            
            return {
                "nodes_count": len(self._nodes),
                "total_mikrotiks": total_mikrotiks,
                "total_olts": total_olts,
                "last_modified": datetime.fromtimestamp(self._last_modified).isoformat() if self._last_modified else None,
                "nodes": [n.name for n in self._nodes]
            }
    
    def get_flat_device_list(self) -> Dict[str, List[Dict[str, Any]]]:
        """Retorna lista plana de todos los dispositivos agrupados por tipo."""
        with self._lock:
            mikrotiks = []
            olts = []
            
            for node in self._nodes:
                for mt in node.mikrotiks:
                    device_dict = mt.to_dict()
                    device_dict["node"] = node.name
                    mikrotiks.append(device_dict)
                
                for olt in node.olts:
                    device_dict = olt.to_dict()
                    device_dict["node"] = node.name
                    olts.append(device_dict)
            
            return {
                "mikrotiks": mikrotiks,
                "olts": olts
            }
    
    def validate_config(self) -> List[str]:
        """Valida la configuración actual."""
        errors = []
        mikrotik_ips = set()
        olt_ips = set()
        
        with self._lock:
            for node in self._nodes:
                # Verificar IPs duplicadas de MikroTik
                for mt in node.mikrotiks:
                    if mt.ip in mikrotik_ips:
                        errors.append(f"IP duplicada de MikroTik: {mt.ip} en {node.name}")
                    mikrotik_ips.add(mt.ip)
                
                # Verificar IPs duplicadas de OLT
                for olt in node.olts:
                    if olt.ip in olt_ips:
                        errors.append(f"IP duplicada de OLT: {olt.ip} en {node.name}")
                    olt_ips.add(olt.ip)
                
                # Verificar modelo de OLT
                for olt in node.olts:
                    if olt.modelo not in ["V1600G0B", "V1600G1", "V1600G2B"]:
                        errors.append(f"Modelo de OLT desconocido: {olt.modelo} en {olt.hostname}")
        
        return errors


# Singleton global
_config_manager: Optional[ConfigManager] = None


def get_config_manager() -> ConfigManager:
    """Retorna la instancia singleton del ConfigManager."""
    global _config_manager
    if _config_manager is None:
        raise RuntimeError("ConfigManager no inicializado. Llamar init_config_manager() primero.")
    return _config_manager


def init_config_manager(config_path: str, nodes_path: str) -> ConfigManager:
    """Inicializa el ConfigManager global."""
    global _config_manager
    _config_manager = ConfigManager(config_path, nodes_path)
    return _config_manager
