# src/alert_state.py
"""Estado persistente de la última alerta enviada por equipo/dispositivo.

Permite deduplicar alertas: si la condición a notificar no cambió respecto a la
última alerta enviada, no se vuelve a enviar. Solo un cambio de condición
dispara una nueva alerta. El estado sobrevive reinicios del agente.
"""
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class LastStateStore:
    """Almacén persistente del último estado notificado por clave de equipo.

    Cada entrada tiene la forma::

        {key: {"state": <fingerprint>, "ts": <unix>, **extra}}

    El ``fingerprint`` determina si una alerta es idéntica a la anterior.
    """

    DEFAULT_PATH = os.path.join("data", "alert_state.json")

    def __init__(self, path: Optional[str] = None):
        self.path = path or self.DEFAULT_PATH
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = raw
        except (OSError, ValueError) as e:
            logger.warning(
                "Alert state unreadable (%s), starting empty: %s", self.path, e
            )

    def _save(self) -> None:
        parent = os.path.dirname(self.path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=parent or ".", suffix=".tmp", delete=False
            ) as tmp:
                tmp_name = tmp.name
                json.dump(self._data, tmp, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.path)
        except OSError as e:
            logger.error("Failed to persist alert state to %s: %s", self.path, e)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Devuelve la entrada completa almacenada para la clave (o None)."""
        return self._data.get(key)

    def state(self, key: str) -> Optional[Dict[str, Any]]:
        """Devuelve el fingerprint del último estado notificado (o None)."""
        entry = self._data.get(key)
        return entry.get("state") if entry else None

    def should_send(self, key: str, fingerprint: Dict[str, Any]) -> bool:
        """True si el estado no existe o difiere del último notificado."""
        return self.state(key) != fingerprint

    def record(self, key: str, fingerprint: Dict[str, Any], **extra: Any) -> None:
        """Guarda el estado notificado para la clave y persiste en disco."""
        entry: Dict[str, Any] = {"state": fingerprint, "ts": time.time()}
        entry.update(extra)
        self._data[key] = entry
        self._save()

    def clear(self, key: str) -> None:
        """Elimina el estado de una clave (re-armar alertas desde cero)."""
        if key in self._data:
            del self._data[key]
            self._save()