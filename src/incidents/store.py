# src/incidents/store.py
"""Modelo y almacén de incidencias persistentes.

Basado en el patrón de `src/models.py` y las necesidades de la Fase 1.

Ejemplo de uso:

    incident = Incident(
        id="INC-2026-11-05-0001",
        olt="OLT-SISAL2-PON2",
        pon="GPON0/9",
        nap="SISAL2",
        causa="Pérdida de energía en rack principal",
        confianza=85.0,
        evidencia=[
            {"tipo": "clientes_down", "valor": 82},
            {"tipo": "syslog", "mensaje": "link down"},
        ],
        impacto={"clientes": 82, "onus_los": 11, "predicciones": 0},
    )
"""
import time
import json
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

# Estado posible de una incidencia
INCIDENT_OPEN = "open"
INCIDENT_RESOLVED = "resolved"

# Severidad posible
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_RESOLVED = "resolved"


@dataclass
class Evidence:
    """Evidencia que sustenta una incidencia."""
    tipo: str          # "clientes_down", "syslog", "power", etc.
    valor: Any         # Datos específicos de la evidencia
    mensaje: str = ""  # Mensaje descriptivo opcional

    def to_dict(self) -> Dict[str, Any]:
        return {"tipo": self.tipo, "valor": self.valor, "mensaje": self.mensaje}


@dataclass
class Incident:
    """Incidencia de red con ciclo de vida completo.

    Campos según manual de incidencias_docs.md:
    - id: identificador único (INC-AAAA-MM-DD-NNNN). Auto-generado si no se proporciona.
    - estado: open/resolved
    - severidad: critical/warning/resolved
    - inicio: timestamp cuando se detectó la caída
    - detectado: timestamp de detección por el sistema
    - fin: timestamp de resolución (None mientras abierta)
    - olt: nombre de la OLT afectada
    - pon: puerto PON (ej: GPON0/9)
    - nap: nombre del NAP/caja
    - causa: descripción de la causa raíz
    - confianza: porcentaje 0-100
    - evidencia: lista de Evidencia
    - impacto: dict con clientes, ONUs, PONs, etc.
    - afectados: lista de clientes/clientes IDs
    - fuentes: dict con disponibilidad de cada fuente
    - reconocida: si fue reconocida por operador
    - silenciada_hasta: timestamp de fin de silencio
    """
    # Identificación (id es opcional: se genera automáticamente)
    id: str = ""
    olt: str = ""

    # Estado y tiempo
    estado: str = INCIDENT_OPEN
    severidad: str = SEVERITY_CRITICAL
    inicio: float = 0.0
    detectado: float = 0.0
    fin: Optional[float] = None

    # Detalles técnicos
    pon: str = ""
    nap: str = ""
    causa: str = ""
    confianza: float = 0.0

    # Evidencia y afectación
    evidencia: List[Dict[str, Any]] = field(default_factory=list)
    impacto: Dict[str, int] = field(default_factory=dict)
    afectados: List[Dict[str, Any]] = field(default_factory=list)

    # Fuentes consultadas
    fuentes: Dict[str, bool] = field(default_factory=dict)

    # Acciones del operador
    reconocida: bool = False
    silenciada_hasta: Optional[float] = None

    # Metadatos
    creado: float = field(default_factory=time.time)
    actualizado: float = field(default_factory=time.time)

    def __post_init__(self):
        """Genera ID si no se proporcionó y valida campos."""
        if not self.id:
            self.id = self._generate_id()

    @staticmethod
    def _generate_id() -> str:
        """Genera ID en formato INC-AAAA-MM-DD-NNNN."""
        ts = time.time()
        date_part = time.strftime("%Y-%m-%d", time.localtime(ts))
        seq = int(ts % 10000)
        return f"INC-{date_part}-{seq:04d}"

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a diccionario para Redis/JSON."""
        return {
            "id": self.id,
            "olt": self.olt,
            "pon": self.pon,
            "nap": self.nap,
            "estado": self.estado,
            "severidad": self.severidad,
            "inicio": self.inicio,
            "detectado": self.detectado,
            "fin": self.fin,
            "causa": self.causa,
            "confianza": self.confianza,
            "evidencia": self.evidencia,
            "impacto": self.impacto,
            "afectados": self.afectados,
            "fuentes": self.fuentes,
            "reconocida": self.reconocida,
            "silenciada_hasta": self.silenciada_hasta,
            "creado": self.creado,
            "actualizado": self.actualizado,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Incident":
        """Crea Incident desde diccionario (desde Redis o JSON)."""
        
        def _parse_json_field(value, default):
            """Parse JSON field, handling strings and actual dicts/lists."""
            if value is None:
                return default
            if isinstance(value, (dict, list)):
                return value
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return default
            return default
        
        def _parse_float(value, default=0.0):
            """Parse float field."""
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    return default
            return default
        
        def _parse_bool(value, default=False):
            """Parse boolean field."""
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ('1', 'true', 'yes')
            return bool(value)
        
        def _parse_optional_float(value, default=None):
            """Parse optional float field."""
            if value is None or value == '':
                return default
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    return default
            return default
        
        return cls(
            id=data.get("id", ""),
            olt=data.get("olt", ""),
            pon=data.get("pon", ""),
            nap=data.get("nap", ""),
            estado=data.get("estado", INCIDENT_OPEN),
            severidad=data.get("severidad", SEVERITY_CRITICAL),
            inicio=_parse_float(data.get("inicio"), 0.0),
            detectado=_parse_float(data.get("detectado"), 0.0),
            fin=_parse_optional_float(data.get("fin")),
            causa=data.get("causa", ""),
            confianza=_parse_float(data.get("confianza"), 0.0),
            evidencia=_parse_json_field(data.get("evidencia"), []),
            impacto=_parse_json_field(data.get("impacto"), {}),
            afectados=_parse_json_field(data.get("afectados"), []),
            fuentes=_parse_json_field(data.get("fuentes"), {}),
            reconocida=_parse_bool(data.get("reconocida"), False),
            silenciada_hasta=_parse_optional_float(data.get("silenciada_hasta")),
        )

    def actualizar(self, **kwargs):
        """Actualiza campos y timestamp."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        self.actualizado = time.time()

    def marcar_reconocida(self):
        """Marca la incidencia como reconocida."""
        self.reconocida = True
        self.actualizado = time.time()

    def silenciar(self, horas: int = 1):
        """Silencia notificaciones por N horas."""
        self.silenciada_hasta = time.time() + (horas * 3600)
        self.actualizado = time.time()

    def resolver(self, fin_ts: Optional[float] = None):
        """Cierra la incidencia."""
        self.estado = INCIDENT_RESOLVED
        self.fin = fin_ts or time.time()
        self.severidad = SEVERITY_RESOLVED
        self.actualizado = time.time()


class IncidentStore:
    """Almacén en Redis para incidencias.

    Uso:
        store = IncidentStore(redis_client)
        store.save(incident)
        store.get("INC-2026-11-05-0001")
    """

    def __init__(self, redis_client=None):
        self.redis = redis_client
        self.key_index = "inc:index"  # ZSET por timestamp de inicio

    def _key(self, incident_id: str) -> str:
        return f"inc:{incident_id}"

    def save(self, incident: Incident) -> bool:
        """Guarda incidencia en Redis."""
        if not self.redis:
            return False
        try:
            data = incident.to_dict()
            # Convert types for Redis storage
            redis_data = {}
            for k, v in data.items():
                if v is None:
                    continue
                if isinstance(v, bool):
                    redis_data[k] = '1' if v else '0'
                elif isinstance(v, (dict, list)):
                    redis_data[k] = json.dumps(v)
                else:
                    redis_data[k] = str(v)
            self.redis.hset(self._key(incident.id), mapping=redis_data)
            if incident.inicio > 0:
                self.redis.zadd(self.key_index, {incident.id: incident.inicio})
            return True
        except Exception:
            return False

    def get(self, incident_id: str) -> Optional[Incident]:
        """Recupera incidencia por ID."""
        if not self.redis:
            return None
        try:
            data = self.redis.hgetall(self._key(incident_id))
            if not data:
                return None
            if isinstance(data, dict):
                data = {k.decode() if isinstance(k, bytes) else k:
                        v.decode() if isinstance(v, bytes) else v
                        for k, v in data.items()}
            return Incident.from_dict(data)
        except Exception:
            return None

    def list_all(self, limit: int = 100) -> List[Incident]:
        """Lista todas las incidencias (más recientes primero)."""
        if not self.redis:
            return []
        try:
            ids = self.redis.zrevrange(self.key_index, 0, limit - 1)
            incidents = []
            for inc_id in ids:
                inc_id_str = inc_id if isinstance(inc_id, str) else inc_id.decode()
                inc = self.get(inc_id_str)
                if inc:
                    incidents.append(inc)
            return incidents
        except Exception:
            return []

    def delete(self, incident_id: str) -> bool:
        """Elimina incidencia."""
        if not self.redis:
            return False
        try:
            self.redis.delete(self._key(incident_id))
            self.redis.zrem(self.key_index, incident_id)
            return True
        except Exception:
            return False