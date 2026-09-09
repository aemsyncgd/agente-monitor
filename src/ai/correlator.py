# src/ai/correlator.py
"""
Correlator: Detecta patrones complejos en eventos WISP.

Patrones:
- Cascada: nodos caen en orden topológico en X minutos.
- Masa: N interfaces caen simultáneamente.
- Recurrencia: mismo evento >M veces en 24h.
- Degradación: métrica empeora sostenidamente.

Salida: eventos compuestos con severidad elevada.
"""
import time
import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class PatternMatch:
    """Resultado de detección de patrón."""

    def __init__(self, pattern_type: str, severity: str,
                 description: str, devices: List[str],
                 event_count: int = 0, metadata: Optional[Dict] = None):
        self.pattern_type = pattern_type
        self.severity = severity
        self.description = description
        self.devices = devices
        self.event_count = event_count
        self.metadata = metadata or {}
        self.timestamp = time.time()

    def to_event_dict(self) -> Dict[str, Any]:
        return {
            "id": f"pattern-{self.pattern_type}-{int(self.timestamp)}",
            "event_type": self.pattern_type,
            "severity": self.severity,
            "device_id": self.devices[0] if self.devices else "",
            "device_type": "pattern",
            "source": "correlator",
            "payload": {
                "description": self.description,
                "devices": self.devices,
                "event_count": self.event_count,
                **self.metadata,
            },
            "context": {},
        }


class WispCorrelator:
    """Detecta patrones de falla en eventos."""

    def __init__(self, cascade_window: int = 300, cascade_min_nodes: int = 2,
                 mass_threshold: int = 3, recurrence_window: int = 86400,
                 recurrence_min_count: int = 3, degradation_window: int = 21600):
        self.cascade_window = cascade_window
        self.cascade_min_nodes = cascade_min_nodes
        self.mass_threshold = mass_threshold
        self.recurrence_window = recurrence_window
        self.recurrence_min_count = recurrence_min_count
        self.degradation_window = degradation_window

        self._recent_events: deque = deque(maxlen=10000)
        self._device_events: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        self._device_status: Dict[str, List[Dict]] = defaultdict(list)

    def process_event(self, event_dict: Dict[str, Any]) -> Optional[PatternMatch]:
        # Excluir eventos del propio agente (heartbeats, auto-generated)
        evt_type = event_dict.get("event_type", "")
        evt_source = event_dict.get("source", "")
        evt_device = event_dict.get("device_id", "")
        if (evt_type == "heartbeat"
                or evt_source == "agent"
                or evt_device == "wisp-agent"):
            return None

        self._recent_events.append(event_dict)

        device_id = event_dict.get("device_id", "")
        if device_id:
            self._device_events[device_id].append(event_dict)
            self._device_status[device_id].append({
                "timestamp": event_dict.get("timestamp", ""),
                "event_type": event_dict.get("event_type", ""),
                "severity": event_dict.get("severity", "info"),
            })

        patterns = []
        patterns.extend(self._detect_cascade(event_dict))
        patterns.extend(self._detect_mass(event_dict))
        patterns.extend(self._detect_recurrence(device_id))
        patterns.extend(self._detect_degradation(device_id))

        if patterns:
            most_severe = max(patterns, key=lambda p: self._severity_rank(p.severity))
            logger.warning(
                f"Pattern detected: {most_severe.pattern_type} "
                f"({most_severe.severity}) - {most_severe.description}"
            )
            return most_severe

        return None

    def _detect_cascade(self, event_dict: Dict[str, Any]) -> List[PatternMatch]:
        now = time.time()
        cutoff = now - self.cascade_window
        patterns = []

        node_down_events = []
        for ev in self._recent_events:
            ts = ev.get("timestamp", "")
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ev_time = dt.timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                ev_time = ts

            if ev_time < cutoff:
                continue
            if ev.get("event_type") in ("link_down", "host_down", "mass_down"):
                node_down_events.append(ev)

        if len(node_down_events) >= self.cascade_min_nodes:
            devices = list(set(
                ev.get("device_id", "") for ev in node_down_events
            ))
            if len(devices) >= self.cascade_min_nodes:
                patterns.append(PatternMatch(
                    pattern_type="cascade",
                    severity="critical",
                    description=(f"Cascada detectada: {len(devices)} dispositivos "
                                f"caídos en {self.cascade_window}s"),
                    devices=devices,
                    event_count=len(node_down_events),
                ))

        return patterns

    def _detect_mass(self, event_dict: Dict[str, Any]) -> List[PatternMatch]:
        now = time.time()
        recent = []
        for ev in self._recent_events:
            ts = ev.get("timestamp", "")
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ev_time = dt.timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                ev_time = ts

            if ev_time >= now - 60:
                recent.append(ev)

        if len(recent) >= self.mass_threshold:
            devices = list(set(ev.get("device_id", "") for ev in recent))
            return [PatternMatch(
                pattern_type="mass_down",
                severity="critical",
                description=(f"Masa: {len(recent)} eventos en 60s "
                            f"({len(devices)} dispositivos)"),
                devices=devices,
                event_count=len(recent),
            )]

        return []

    def _detect_recurrence(self, device_id: str) -> List[PatternMatch]:
        if not device_id:
            return []

        now = time.time()
        cutoff = now - self.recurrence_window

        device_evs = self._device_events.get(device_id, [])
        recent = []
        for ev in device_evs:
            ts = ev.get("timestamp", "")
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ev_time = dt.timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                ev_time = ts

            if ev_time >= cutoff:
                recent.append(ev)

        if len(recent) >= self.recurrence_min_count:
            types = set(ev.get("event_type", "") for ev in recent)
            return [PatternMatch(
                pattern_type="recurrence",
                severity="warning",
                description=(f"Recurrencia: {len(recent)} eventos en "
                            f"dispositivo {device_id} en 24h"),
                devices=[device_id],
                event_count=len(recent),
                metadata={"event_types": list(types)},
            )]

        return []

    def _detect_degradation(self, device_id: str) -> List[PatternMatch]:
        if not device_id:
            return []

        device_evs = self._device_events.get(device_id, [])
        now = time.time()
        cutoff = now - self.degradation_window

        low_signal_events = []
        for ev in device_evs:
            if ev.get("event_type") not in ("low_signal", "anomaly"):
                continue
            ts = ev.get("timestamp", "")
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ev_time = dt.timestamp()
                except (ValueError, TypeError):
                    continue
            else:
                ev_time = ts

            if ev_time >= cutoff:
                low_signal_events.append(ev)

        if len(low_signal_events) >= 3:
            powers = []
            for ev in low_signal_events:
                rx = ev.get("payload", {}).get("rx_power")
                if rx is not None:
                    try:
                        powers.append(float(rx))
                    except (ValueError, TypeError):
                        pass

            if len(powers) >= 3 and powers[-1] < powers[0]:
                return [PatternMatch(
                    pattern_type="degradation",
                    severity="warning",
                    description=(f"Degradación: potencia empeora en {device_id} "
                                f"({powers[0]:.1f} → {powers[-1]:.1f} dBm)"),
                    devices=[device_id],
                    event_count=len(low_signal_events),
                    metadata={"power_trend": powers},
                )]

        return []

    @staticmethod
    def _severity_rank(severity: str) -> int:
        return {"critical": 3, "warning": 2, "info": 1}.get(severity, 0)

    def stats(self) -> Dict[str, Any]:
        return {
            "total_events_tracked": len(self._recent_events),
            "devices_tracked": len(self._device_events),
        }
