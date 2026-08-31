# src/ai/instructions.py
"""
Agent Instruction System — configurable task list for the AI monitoring agent.

Each instruction is a named, toggleable task that the agent loop executes.
Instructions are persisted in config/instructions.json and manageable via API.
"""
import os
import json
import time
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config")
INSTRUCTIONS_FILE = os.path.join(CONFIG_DIR, "instructions.json")


class InstructionType(str, Enum):
    """Types of instructions the agent can execute."""
    DETECT_ANOMALY = "detect_anomaly"
    SEND_TELEGRAM = "send_telegram"
    DAILY_SUMMARY = "daily_summary"
    RETRAIN_MODEL = "retrain_model"
    CHECK_INTERFACE = "check_interface"
    ZONE_GROUPING = "zone_grouping"
    PREDICT_FAILURE = "predict_failure"
    CLIENT_LOOKUP = "client_lookup"
    CHECK_PING_STATUS = "check_ping_status"
    SEND_REPORT = "send_report"
    LLM_ANALYZE = "llm_analyze"


@dataclass
class Instruction:
    """A single agent instruction."""
    id: str
    name: str
    description: str
    instruction_type: InstructionType
    enabled: bool = True
    priority: int = 50  # 0=highest, 100=lowest
    params: Dict = field(default_factory=dict)
    last_run: float = 0.0
    last_result: str = ""
    run_count: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["instruction_type"] = self.instruction_type.value
        return d

    @classmethod
    def from_dict(cls, data: Dict) -> "Instruction":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            instruction_type=InstructionType(data.get("instruction_type", "detect_anomaly")),
            enabled=data.get("enabled", True),
            priority=data.get("priority", 50),
            params=data.get("params", {}),
            last_run=data.get("last_run", 0.0),
            last_result=data.get("last_result", ""),
            run_count=data.get("run_count", 0),
            created_at=data.get("created_at", 0.0),
        )


DEFAULT_INSTRUCTIONS: List[Instruction] = [
    Instruction(
        id="detect_anomaly",
        name="Deteccion de Anomalias",
        description="Analiza datos de potencia optica para detectar fallas automaticamente",
        instruction_type=InstructionType.DETECT_ANOMALY,
        enabled=True,
        priority=10,
        params={"check_interval_seconds": 60, "min_samples": 10},
    ),
    Instruction(
        id="send_telegram",
        name="Alertas Telegram",
        description="Envia notificaciones a Telegram cuando se detectan anomalias o predicciones",
        instruction_type=InstructionType.SEND_TELEGRAM,
        enabled=True,
        priority=20,
        params={"cooldown_minutes": 15},
    ),
    Instruction(
        id="daily_summary",
        name="Resumen Diario",
        description="Genera y envia un resumen diario de estado de la red y anomalias",
        instruction_type=InstructionType.DAILY_SUMMARY,
        enabled=True,
        priority=80,
        params={"send_hour": 8},
    ),
    Instruction(
        id="retrain_model",
        name="Reentrenar Modelo IA",
        description="Reentrena el modelo autoencoder con datos recientes (cada 24h)",
        instruction_type=InstructionType.RETRAIN_MODEL,
        enabled=True,
        priority=60,
        params={"retrain_hours": 24, "min_samples": 1000},
    ),
    Instruction(
        id="check_interface",
        name="Verificacion de Interfaces",
        description="Verifica trafico de interfaces para detectar caidas de enlace",
        instruction_type=InstructionType.CHECK_INTERFACE,
        enabled=True,
        priority=30,
        params={"check_interval_seconds": 900},
    ),
    Instruction(
        id="zone_grouping",
        name="Agrupacion por Zonas",
        description="Agrupa clientes afectados por zona geografica para alertas masivas",
        instruction_type=InstructionType.ZONE_GROUPING,
        enabled=True,
        priority=40,
        params={"method": "simple"},
    ),
    Instruction(
        id="predict_failure",
        name="Prediccion de Fallos",
        description="Predice fallas futuras basado en tendencias de degradacion de potencia",
        instruction_type=InstructionType.PREDICT_FAILURE,
        enabled=True,
        priority=25,
        params={"prediction_horizon_hours": 24, "min_degradation_rate": 0.5},
    ),
    Instruction(
        id="client_lookup",
        name="Lookup de Clientes",
        description="Resuelve serial ONU a nombre y direccion del cliente para enriquecer alertas",
        instruction_type=InstructionType.CLIENT_LOOKUP,
        enabled=True,
        priority=15,
        params={"csv_path": "grid_servicios.csv", "reload_hours": 24},
    ),
    Instruction(
        id="check_ping_status",
        name="Monitoreo ICMP",
        description="Verifica disponibilidad de dispositivos via ping y alerta cuando caen",
        instruction_type=InstructionType.CHECK_PING_STATUS,
        enabled=True,
        priority=12,
        params={"alert_down_seconds": 120, "alert_on_recovery": True},
    ),
    Instruction(
        id="send_report",
        name="Reporte Diario",
        description="Genera y envia reporte detallado de OLTs, MikroTiks, graficos y alertas a Telegram",
        instruction_type=InstructionType.SEND_REPORT,
        enabled=True,
        priority=85,
        params={"send_times": ["08:00", "12:30", "16:50", "20:50"]},
    ),
    Instruction(
        id="llm_analyze",
        name="Analisis LLM",
        description="Permite al agente LLM consultar estado de OLTs y generar analisis descriptivos y predictivos",
        instruction_type=InstructionType.LLM_ANALYZE,
        enabled=True,
        priority=35,
        params={},
    ),
]


class InstructionManager:
    """Manages agent instructions — load, save, toggle, reorder."""

    def __init__(self, path: str = INSTRUCTIONS_FILE):
        self.path = path
        self._instructions: List[Instruction] = []
        self._load()

    def _load(self):
        """Load instructions from file, or use defaults."""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._instructions = [Instruction.from_dict(d) for d in data]
                logger.info(f"Loaded {len(self._instructions)} instructions from {self.path}")
                return
            except Exception as e:
                logger.warning(f"Failed to load instructions: {e}, using defaults")

        # First run: save defaults
        self._instructions = [inst for inst in DEFAULT_INSTRUCTIONS]
        self._save()
        logger.info(f"Initialized {len(self._instructions)} default instructions")

    def _save(self):
        """Persist instructions to file."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump([inst.to_dict() for inst in self._instructions], f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save instructions: {e}")

    def get_all(self) -> List[Instruction]:
        """Return all instructions."""
        return list(self._instructions)

    def get_enabled(self) -> List[Instruction]:
        """Return only enabled instructions, sorted by priority."""
        enabled = [i for i in self._instructions if i.enabled]
        return sorted(enabled, key=lambda x: x.priority)

    def get_by_id(self, inst_id: str) -> Optional[Instruction]:
        """Get instruction by ID."""
        for inst in self._instructions:
            if inst.id == inst_id:
                return inst
        return None

    def set_enabled(self, inst_id: str, enabled: bool) -> bool:
        """Enable or disable an instruction."""
        for inst in self._instructions:
            if inst.id == inst_id:
                inst.enabled = enabled
                self._save()
                logger.info(f"Instruction '{inst_id}' {'enabled' if enabled else 'disabled'}")
                return True
        return False

    def update_params(self, inst_id: str, params: Dict) -> bool:
        """Update parameters for an instruction."""
        for inst in self._instructions:
            if inst.id == inst_id:
                inst.params.update(params)
                self._save()
                logger.info(f"Updated params for '{inst_id}': {params}")
                return True
        return False

    def set_priority(self, inst_id: str, priority: int) -> bool:
        """Update priority (0=highest, 100=lowest)."""
        for inst in self._instructions:
            if inst.id == inst_id:
                inst.priority = max(0, min(100, priority))
                self._save()
                return True
        return False

    def record_run(self, inst_id: str, result: str = "ok"):
        """Record that an instruction was executed."""
        for inst in self._instructions:
            if inst.id == inst_id:
                inst.last_run = time.time()
                inst.last_result = result
                inst.run_count += 1
                self._save()
                return

    def add_custom(self, name: str, description: str, instruction_type: InstructionType,
                   params: Dict = None, priority: int = 50) -> Instruction:
        """Add a custom instruction."""
        inst_id = f"custom_{int(time.time())}"
        inst = Instruction(
            id=inst_id,
            name=name,
            description=description,
            instruction_type=instruction_type,
            enabled=True,
            priority=priority,
            params=params or {},
        )
        self._instructions.append(inst)
        self._save()
        return inst

    def remove_custom(self, inst_id: str) -> bool:
        """Remove a custom instruction (not built-in ones)."""
        built_in = {i.id for i in DEFAULT_INSTRUCTIONS}
        if inst_id in built_in:
            return False
        before = len(self._instructions)
        self._instructions = [i for i in self._instructions if i.id != inst_id]
        if len(self._instructions) < before:
            self._save()
            return True
        return False

    def to_summary(self) -> List[Dict]:
        """Return summary of all instructions for API/UI."""
        return [
            {
                "id": inst.id,
                "name": inst.name,
                "description": inst.description,
                "type": inst.instruction_type.value,
                "enabled": inst.enabled,
                "priority": inst.priority,
                "params": inst.params,
                "last_run": inst.last_run,
                "last_result": inst.last_result,
                "run_count": inst.run_count,
            }
            for inst in self._instructions
        ]
