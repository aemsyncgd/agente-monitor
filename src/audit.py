# src/audit.py
"""
Auditoría estructurada JSON para el sistema de monitoreo WISP.

Registra cada acción significativa del sistema: alertas enviadas,
acciones ejecutadas, cambios de configuración, eventos procesados.
"""
import os
import json
import time
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class AuditLogger:
    """Logger de auditoría con append-only JSONL y rotación."""

    def __init__(self, log_file: str = "logs/audit.jsonl",
                 retention_days: int = 90,
                 enabled: bool = True):
        self.enabled = enabled
        self.log_file = log_file
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._buffer: list = []
        self._buffer_size = 50
        self._flush_interval = 10.0
        self._last_flush = time.time()

        if enabled:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    def log(self, actor: str, action: str, target: str = "",
            parameters: Optional[Dict[str, Any]] = None,
            result: str = "success",
            details: Optional[str] = None,
            severity: str = "info") -> None:
        if not self.enabled:
            return

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "parameters": parameters or {},
            "result": result,
            "severity": severity,
        }
        if details:
            entry["details"] = details

        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) >= self._buffer_size:
                self._flush_locked()

    def log_event(self, event_id: str, event_type: str, source: str,
                  device_id: str = "", severity: str = "info",
                  details: str = "") -> None:
        self.log(
            actor=source,
            action=f"event:{event_type}",
            target=device_id,
            parameters={"event_id": event_id},
            result="processed",
            details=details,
            severity=severity,
        )

    def log_action(self, action_name: str, actor: str, target: str,
                   parameters: Optional[Dict] = None,
                   result: str = "pending") -> None:
        self.log(
            actor=actor,
            action=f"action:{action_name}",
            target=target,
            parameters=parameters or {},
            result=result,
        )

    def log_alert(self, alert_title: str, channel: str, severity: str,
                  recipient: str = "", result: str = "sent") -> None:
        self.log(
            actor="agent",
            action="alert:send",
            target=channel,
            parameters={"title": alert_title, "recipient": recipient},
            result=result,
            severity=severity,
        )

    def log_config_change(self, module: str, key: str, old_value: Any,
                          new_value: Any, actor: str = "dashboard") -> None:
        self.log(
            actor=actor,
            action="config:change",
            target=module,
            parameters={"key": key, "old": str(old_value), "new": str(new_value)},
            result="applied",
        )

    def log_approval(self, action_name: str, target: str,
                     approved: bool, approver: str = "") -> None:
        self.log(
            actor=approver or "system",
            action=f"approval:{action_name}",
            target=target,
            parameters={"approved": approved},
            result="approved" if approved else "rejected",
        )

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                for entry in self._buffer:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._buffer.clear()
            self._last_flush = time.time()
        except Exception as e:
            logger.error(f"Audit flush failed: {e}")

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def query(self, since: Optional[str] = None,
              action_filter: Optional[str] = None,
              limit: int = 100) -> list:
        results = []
        if not os.path.exists(self.log_file):
            return results

        try:
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if since and entry.get("timestamp", "") < since:
                        continue
                    if action_filter and action_filter not in entry.get("action", ""):
                        continue

                    results.append(entry)
                    if len(results) >= limit:
                        break
        except Exception as e:
            logger.error(f"Audit query failed: {e}")

        return results

    def cleanup_old_entries(self) -> int:
        if not os.path.exists(self.log_file):
            return 0

        cutoff = datetime.now(timezone.utc).isoformat()
        kept = 0
        removed = 0

        try:
            from datetime import timedelta
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
            cutoff = cutoff_dt.isoformat()

            temp_file = self.log_file + ".tmp"
            with open(self.log_file, "r", encoding="utf-8") as f_in, \
                 open(temp_file, "w", encoding="utf-8") as f_out:
                for line in f_in:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("timestamp", "") >= cutoff:
                            f_out.write(line + "\n")
                            kept += 1
                        else:
                            removed += 1
                    except json.JSONDecodeError:
                        f_out.write(line + "\n")
                        kept += 1

            os.replace(temp_file, self.log_file)
            logger.info(f"Audit cleanup: kept {kept}, removed {removed}")
        except Exception as e:
            logger.error(f"Audit cleanup failed: {e}")

        return removed


_instance: Optional[AuditLogger] = None
_lock = threading.Lock()


def get_audit_logger(**kwargs) -> AuditLogger:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                from .config_ia import load_config_ia
                config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
                try:
                    config = load_config_ia(config_path)
                    _instance = AuditLogger(
                        log_file=config.audit.log_file,
                        retention_days=config.audit.retention_days,
                        enabled=config.audit.enabled,
                    )
                except Exception:
                    _instance = AuditLogger(**kwargs)
    return _instance
