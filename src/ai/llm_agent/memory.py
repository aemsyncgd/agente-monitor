# src/ai/llm_agent/memory.py
"""
Event store + alert dedup state, persisted as JSON.

- events: ring buffer of agent outputs (alerts / analyses) for the dashboard.
- state:  dedup map (key -> last_ts) so the agent does not re-alert the same
          situation within a cooldown window.
"""
import os
import json
import time
import logging
from typing import Dict, List, Any, Optional
from threading import RLock

logger = logging.getLogger(__name__)

MAX_EVENTS = 500


class EventStore:
    def __init__(self, path: str, max_events: int = MAX_EVENTS):
        self.path = path
        self.max_events = max_events
        self._lock = RLock()
        self._events: List[Dict] = []
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._events = data[-self.max_events:]
        except Exception as e:
            logger.warning(f"Failed to load events {self.path}: {e}")

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._events, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save events: {e}")

    def add(self, kind: str, title: str, detail: str = "",
            severity: str = "info", extra: Optional[Dict] = None) -> Dict:
        event = {
            "ts": time.time(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "kind": kind,          # alert | analysis | info
            "title": title,
            "detail": detail,
            "severity": severity,
        }
        if extra:
            event["extra"] = extra
        with self._lock:
            self._events.append(event)
            self._events = self._events[-self.max_events:]
            self._save()
        return event

    def recent(self, limit: int = 10, since_ts: float = 0) -> List[Dict]:
        with self._lock:
            items = [e for e in self._events if e["ts"] >= since_ts]
            return list(reversed(items[-limit:]))

    def count(self) -> int:
        with self._lock:
            return len(self._events)


class AlertState:
    """Dedup: key -> timestamp of last alert, with per-key cooldown."""

    def __init__(self, path: str):
        self.path = path
        self._lock = RLock()
        self._state: Dict[str, float] = {}
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._state = {k: float(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"Failed to load state {self.path}: {e}")

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def should_alert(self, key: str, cooldown_seconds: float) -> bool:
        """True if an alert for `key` is allowed now (cooldown elapsed)."""
        with self._lock:
            last = self._state.get(key, 0)
            if time.time() - last < cooldown_seconds:
                return False
            self._state[key] = time.time()
            self._save()
            return True


class CostTracker:
    """Daily token/cost budget. Persists to JSON so restarts don't reset it."""

    # Conservative blended estimates (USD per 1M tokens)
    EST_INPUT_PER_M = 0.30
    EST_OUTPUT_PER_M = 1.00

    def __init__(self, path: str, daily_budget_usd: float = 2.0):
        self.path = path
        self.daily_budget_usd = daily_budget_usd
        self._lock = RLock()
        self._day = time.strftime("%Y-%m-%d")
        self._in = 0
        self._out = 0
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("day") == self._day:
                self._in = int(data.get("in", 0))
                self._out = int(data.get("out", 0))
        except Exception as e:
            logger.warning(f"Failed to load cost {self.path}: {e}")

    def _save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"day": self._day, "in": self._in, "out": self._out},
                          f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save cost: {e}")

    def record(self, prompt_tokens: int, completion_tokens: int):
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            if today != self._day:
                self._day = today
                self._in = 0
                self._out = 0
            self._in += int(prompt_tokens or 0)
            self._out += int(completion_tokens or 0)
            self._save()

    def estimate_usd(self) -> float:
        with self._lock:
            return (
                self._in * self.EST_INPUT_PER_M / 1_000_000
                + self._out * self.EST_OUTPUT_PER_M / 1_000_000
            )

    def under_budget(self) -> bool:
        return self.estimate_usd() < self.daily_budget_usd

    def stats(self) -> Dict:
        return {
            "day": self._day,
            "tokens_in": self._in,
            "tokens_out": self._out,
            "estimate_usd": round(self.estimate_usd(), 4),
            "budget_usd": self.daily_budget_usd,
        }
