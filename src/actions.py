# src/actions.py
"""
Framework de acciones con niveles de seguridad.

Nivel 0 (lectura): siempre permitido. DiagnosticarAction, ConsultarTopologiaAction.
Nivel 1 (alertas): sin confirmación. EnviarAlertaTelegramAction, AbrirTicketAction.
Nivel 2 (reversibles): bajo condiciones. PingTestAction, TracerouteAction, RebootInterfaceAction.
Nivel 3 (críticas): SIEMPRE confirmación humana. RebootOLTAction, CambiarConfiguracionAction, BloquearClienteAction.

El decorador @require_approval publica una solicitud al bot de Telegram con
botones inline "Confirmar / Rechazar".
"""
import os
import json
import time
import uuid
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


class ApprovalRequiredError(Exception):
    """Excepción lanzada cuando una acción requiere aprobación."""

    def __init__(self, action_id: str, level: int, message: str = ""):
        self.action_id = action_id
        self.level = level
        self.message = message or f"Acción requiere aprobación (nivel {level})"
        super().__init__(self.message)


@dataclass
class ApprovalRequest:
    action_id: str
    action_name: str
    level: int
    target: str
    parameters: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | rejected | timeout
    approver: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "action_name": self.action_name,
            "level": self.level,
            "target": self.target,
            "parameters": self.parameters,
            "timestamp": self.timestamp,
            "status": self.status,
            "approver": self.approver,
        }


class BaseAction(ABC):
    """Clase base para todas las acciones."""

    name: str = "base"
    level: int = 0
    description: str = ""

    def __init__(self, executor=None):
        self.executor = executor

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        pass

    def validate(self, **kwargs) -> Optional[str]:
        """Valida los parámetros antes de ejecutar. Devuelve error o None."""
        return None

    def requires_approval(self) -> bool:
        return self.level == 3


class ActionError(Exception):
    pass


class ActionRegistry:
    """Registro de acciones disponibles."""

    def __init__(self, approval_service=None):
        self._actions: Dict[str, Type[BaseAction]] = {}
        self.approval_service = approval_service

    def register(self, action_cls: Type[BaseAction]) -> Type[BaseAction]:
        self._actions[action_cls.name] = action_cls
        return action_cls

    def register_instance(self, action_cls: Type[BaseAction]) -> None:
        self._actions[action_cls.name] = action_cls

    def get_action(self, name: str) -> Optional[BaseAction]:
        action_cls = self._actions.get(name)
        if not action_cls:
            return None
        return action_cls(executor=self)

    def has_action(self, name: str) -> bool:
        return name in self._actions

    def list_actions(self) -> List[Dict]:
        return [
            {
                "name": cls.name,
                "level": cls.level,
                "description": cls.description,
            }
            for cls in sorted(self._actions.values(), key=lambda c: c.level)
        ]

    def execute(self, name: str, approval_service=None, **kwargs) -> Any:
        action = self.get_action(name)
        if not action:
            raise ActionError(f"Acción '{name}' no existe")

        error = action.validate(**kwargs)
        if error:
            raise ActionError(error)

        svc = approval_service or self.approval_service

        if action.requires_approval():
            if not svc:
                raise ApprovalRequiredError(
                    uuid.uuid4().hex, action.level,
                    f"Acción '{name}' requiere aprobación pero no hay servicio",
                )
            return svc.request_approval(action, **kwargs)

        perm_error = self._check_permissions(action, kwargs)
        if perm_error:
            raise ActionError(perm_error)

        return action.execute(**kwargs)

    def _check_permissions(self, action: BaseAction, kwargs: Dict) -> Optional[str]:
        if action.level == 2:
            target = kwargs.get("device_id", "")
            meta = kwargs.get("_node_meta", {})
            client_count = meta.get("client_count", 0)
            is_backbone = meta.get("is_backbone", False)

            if is_backbone:
                return f"Acción '{action.name}' no permitida en nodo backbone"
            if client_count and client_count >= 10:
                return (f"Acción '{action.name}' no permitida: nodo tiene "
                        f"{client_count} clientes (máx 10)")
        return None


class ApprovalService:
    """Servicio de aprobación con botones inline de Telegram."""

    def __init__(self, telegram_sender=None, timeout_seconds: int = 300,
                 max_concurrent: int = 3):
        self.telegram = telegram_sender
        self.timeout_seconds = timeout_seconds
        self.max_concurrent = max_concurrent
        self._pending: Dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def request_approval(self, action: BaseAction, **kwargs) -> str:
        request = ApprovalRequest(
            action_id=uuid.uuid4().hex,
            action_name=action.name,
            level=action.level,
            target=kwargs.get("device_id", kwargs.get("target", "")),
            parameters=kwargs,
        )

        with self._lock:
            if len(self._pending) >= self.max_concurrent:
                raise ApprovalRequiredError(
                    request.action_id, action.level,
                    "Máximo de solicitudes de aprobación concurrentes alcanzado",
                )
            self._pending[request.action_id] = request

        self._send_approval_notification(request)
        logger.info(f"Approval requested: {request.action_name} ({request.action_id})")
        return request.action_id

    def _send_approval_notification(self, request: ApprovalRequest) -> None:
        if not self.telegram:
            logger.warning(
                f"No Telegram sender configured; approval {request.action_id} pending locally"
            )
            return

        message = (
            f"🔒 APROBACIÓN REQUERIDA (Nivel {request.level})\n\n"
            f"Acción: {request.action_name}\n"
            f"Dispositivo: {request.target}\n"
            f"ID: {request.action_id}\n\n"
            f"Presiona el botón para aprobar o rechazar."
        )
        try:
            self.telegram.send_action_approval(
                request.action_id, message, self.timeout_seconds
            )
        except Exception as e:
            logger.error(f"Approval notification failed: {e}")

    def resolve(self, action_id: str, approved: bool, approver: str = "") -> bool:
        """Resuelve una solicitud de aprobación."""
        with self._lock:
            request = self._pending.get(action_id)
            if not request or request.status != "pending":
                logger.warning(f"Approval {action_id} not found or already resolved")
                return False

            request.status = "approved" if approved else "rejected"
            request.approver = approver
            self._pending.pop(action_id, None)

        logger.info(
            f"Approval '{action_id}' for {request.action_name}: "
            f"{request.status} by {approver or 'system'}"
        )
        return True

    def get_pending(self) -> List[Dict]:
        with self._lock:
            return [r.to_dict() for r in self._pending.values()]

    def get_request(self, action_id: str) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._pending.get(action_id)

    def check_timeouts(self) -> List[str]:
        """Marca como timeout las solicitudes vencidas."""
        expired = []
        now = time.time()
        with self._lock:
            for action_id in list(self._pending.keys()):
                req = self._pending[action_id]
                if now - req.timestamp > self.timeout_seconds:
                    req.status = "timeout"
                    self._pending.pop(action_id, None)
                    expired.append(action_id)
        return expired


# ── Implementaciones de acciones ───────────────────────────────────────────

@dataclass
class _Registry:
    actions: Dict[str, Type[BaseAction]] = field(default_factory=dict)


_registry = _Registry()


def register_action(cls):
    _registry.actions[cls.name] = cls
    return cls


def get_action_instances() -> Dict[str, Type[BaseAction]]:
    return dict(_registry.actions)


# Nivel 0: Lectura
@register_action
class DiagnosticarAction(BaseAction):
    name = "DiagnosticarAction"
    level = 0
    description = "Ejecuta un diagnóstico de red para un dispositivo."

    def execute(self, device_id: str = "", **kwargs):
        logger.info(f"Diagnóstico de {device_id}")
        return {"ok": True, "action": "diagnostico", "device_id": device_id}


@register_action
class ConsultarTopologiaAction(BaseAction):
    name = "ConsultarTopologiaAction"
    level = 0
    description = "Consulta la topología de red de un nodo."

    def execute(self, node: str = "", **kwargs):
        logger.info(f"Consulta topología de {node}")
        return {"ok": True, "action": "topologia", "node": node}


# Nivel 1: Alertas / Notificaciones
@register_action
class EnviarAlertaTelegramAction(BaseAction):
    name = "EnviarAlertaTelegramAction"
    level = 1
    description = "Envía una alerta de Telegram."

    def execute(self, title: str = "", message: str = "", severity: str = "warning",
                **kwargs):
        return {"ok": True, "action": "telegram",
                "title": title, "severity": severity, "message": message}


@register_action
class AbrirTicketAction(BaseAction):
    name = "AbrirTicketAction"
    level = 1
    description = "Abre un ticket de soporte."

    def execute(self, title: str = "", description: str = "", **kwargs):
        return {"ok": True, "action": "ticket", "title": title}


# Nivel 2: Reversibles
@register_action
class PingTestAction(BaseAction):
    name = "PingTestAction"
    level = 2
    description = "Realiza una prueba de ping a un dispositivo."

    def execute(self, device_id: str = "", **kwargs):
        return {"ok": True, "action": "ping", "device_id": device_id}


@register_action
class TracerouteAction(BaseAction):
    name = "TracerouteAction"
    level = 2
    description = "Realiza un traceroute hacia un dispositivo."

    def execute(self, device_id: str = "", **kwargs):
        return {"ok": True, "action": "traceroute", "device_id": device_id}


@register_action
class RebootInterfaceAction(BaseAction):
    name = "RebootInterfaceAction"
    level = 2
    description = "Reinicia una interfaz (solo nodos con < N clientes, no backbone)."

    def execute(self, device_id: str = "", interface: str = "", **kwargs):
        return {"ok": True, "action": "reboot_interface",
                "device_id": device_id, "interface": interface}


# Nivel 3: Críticas
@register_action
class RebootOLTAction(BaseAction):
    name = "RebootOLTAction"
    level = 3
    description = "Reinicia una OLT completa. SIEMPRE requiere confirmación."

    def execute(self, device_id: str = "", **kwargs):
        return {"ok": True, "action": "reboot_olt", "device_id": device_id}


@register_action
class CambiarConfiguracionAction(BaseAction):
    name = "CambiarConfiguracionAction"
    level = 3
    description = "Cambia configuración de un dispositivo. SIEMPRE requiere confirmación."

    def execute(self, device_id: str = "", changes=None, **kwargs):
        return {"ok": True, "action": "change_config",
                "device_id": device_id, "changes": changes}


@register_action
class BloquearClienteAction(BaseAction):
    name = "BloquearClienteAction"
    level = 3
    description = "Bloquea a un cliente. SIEMPRE requiere confirmación."

    def execute(self, client_id: str = "", reason: str = "", **kwargs):
        return {"ok": True, "action": "block_client",
                "client_id": client_id, "reason": reason}


def build_registry(approval_service: Optional[ApprovalService] = None) -> ActionRegistry:
    registry = ActionRegistry(approval_service=approval_service)
    for name, cls in _registry.actions.items():
        registry.register(cls)
    return registry
