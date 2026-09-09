# src/policy.py
"""
Policy Engine: Políticas de horarios, anti-spam y escalación.

Controla cuándo y cómo se envían alertas:
- Horario laboral vs. no laboral.
- Severidad crítica → inmediata 24/7.
- Warning/info en horario no laboral → acumular y resumen cada 4h.
- Anti-spam: máximo N alertas por hora por zona.
"""
import time
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class AlertPolicy:
    """Políticas de notificación y anti-spam."""

    def __init__(self, work_hours_start: int = 8, work_hours_end: int = 18,
                 work_days: Optional[List[int]] = None,
                 max_alerts_per_hour: int = 5,
                 critical_always_immediate: bool = True,
                 quiet_summary_interval: int = 240):
        self.work_hours_start = work_hours_start
        self.work_hours_end = work_hours_end
        self.work_days = work_days or [0, 1, 2, 3, 4]
        self.max_alerts_per_hour = max_alerts_per_hour
        self.critical_always_immediate = critical_always_immediate
        self.quiet_summary_interval = quiet_summary_interval

        self._alert_history: Dict[str, List[float]] = defaultdict(list)
        self._pending_summary: Dict[str, List[Dict]] = defaultdict(list)
        self._last_summary: Dict[str, float] = {}
        self._silenced_zones: Dict[str, float] = {}

    def should_alert_now(self, severity: str, zone: str = "") -> Tuple[bool, str]:
        now = datetime.now(timezone.utc)
        is_work = self._is_work_hours(now)
        is_critical = severity == "critical"

        if self._is_silenced(zone, severity):
            return False, "zone_silenced"

        if self.critical_always_immediate and is_critical:
            return True, "critical_immediate"

        if is_work:
            if self._check_rate_limit(zone):
                return True, "work_hours"
            return False, "rate_limited"

        if is_critical:
            return True, "non_work_critical"

        self._pending_summary[zone].append({
            "severity": severity,
            "timestamp": time.time(),
        })

        last_summary = self._last_summary.get(zone, 0)
        if time.time() - last_summary > self.quiet_summary_interval * 60:
            self._last_summary[zone] = time.time()
            return True, "quiet_hours_summary"

        return False, "quiet_hours_accumulated"

    def _is_silenced(self, zone: str, severity: str) -> bool:
        """Una zona silenciada bloquea alertas warning/info (las críticas pasan)."""
        if severity == "critical":
            return False
        until = self._silenced_zones.get(zone, 0)
        if until and time.time() < until:
            return True
        if until and time.time() >= until:
            self._silenced_zones.pop(zone, None)
        return False

    def silence_zone(self, zone: str, minutes: int = 60) -> None:
        """Silencia alertas warning/info de una zona por N minutos."""
        self._silenced_zones[zone] = time.time() + minutes * 60
        logger.info(f"Zona '{zone}' silenciada por {minutes} min")

    def unsilence_zone(self, zone: str) -> None:
        self._silenced_zones.pop(zone, None)
        logger.info(f"Zona '{zone}' des-silenciada")

    def is_work_hours(self) -> bool:
        return self._is_work_hours(datetime.now(timezone.utc))

    def get_pending_summary(self, zone: str) -> List[Dict]:
        pending = self._pending_summary.get(zone, [])
        self._pending_summary[zone] = []
        return pending

    def _is_work_hours(self, now: datetime) -> bool:
        if now.weekday() not in self.work_days:
            return False
        return self.work_hours_start <= now.hour < self.work_hours_end

    def _check_rate_limit(self, zone: str) -> bool:
        now = time.time()
        cutoff = now - 3600
        history = self._alert_history[zone]
        self._alert_history[zone] = [t for t in history if t > cutoff]
        return len(self._alert_history[zone]) < self.max_alerts_per_hour

    def record_alert_sent(self, zone: str) -> None:
        self._alert_history[zone].append(time.time())

    def stats(self) -> Dict:
        return {
            "is_work_hours": self.is_work_hours(),
            "zones_tracked": len(self._alert_history),
            "pending_zones": len(self._pending_summary),
            "total_pending": sum(len(v) for v in self._pending_summary.values()),
            "silenced_zones": {
                z: int(until - time.time())
                for z, until in self._silenced_zones.items()
                if until > time.time()
            },
        }
