# src/ai/memory_ext.py
"""
Memoria multinivel para el agente WISP.

Tres niveles:
- Working memory: deque en memoria (últimos K eventos)
- Session memory: Redis (resumen última hora)
- Long-term memory: InfluxDB/JSON (resúmenes diarios)

Reutiliza EventStore/AlertState existentes del llm_agent.
"""
import os
import json
import time
import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class WorkingMemory:
    """Buffer circular en memoria para los últimos N eventos."""

    def __init__(self, max_size: int = 100):
        self._buffer: deque = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def add(self, event: Dict[str, Any], reasoning: Optional[Dict] = None) -> None:
        entry = {
            "event": event,
            "reasoning": reasoning,
            "timestamp": time.time(),
        }
        with self._lock:
            self._buffer.append(entry)

    def get_recent(self, n: int = 20) -> List[Dict]:
        with self._lock:
            items = list(self._buffer)
        return items[-n:]

    def get_all(self) -> List[Dict]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


class SessionMemory:
    """Memoria de sesión en Redis (última hora resumida)."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 prefix: str = "wisp:session:"):
        self.prefix = prefix
        self._redis = None
        self._connected = False
        self._local_cache: Dict[str, Any] = {}
        self._try_connect(redis_url)

    def _try_connect(self, url: str) -> None:
        try:
            import redis
            self._redis = redis.Redis.from_url(
                url, decode_responses=True,
                socket_timeout=3, socket_connect_timeout=2
            )
            self._redis.ping()
            self._connected = True
        except Exception:
            self._connected = False
            logger.debug("SessionMemory: Redis unavailable, using local cache")

    def add(self, key: str, value: Any, ttl: int = 3600) -> None:
        if self._connected and self._redis:
            try:
                self._redis.setex(f"{self.prefix}{key}", ttl, json.dumps(value, default=str))
            except Exception as e:
                logger.debug(f"SessionMemory set failed: {e}")
                self._local_cache[key] = value
        else:
            self._local_cache[key] = value

    def get(self, key: str) -> Optional[Any]:
        if self._connected and self._redis:
            try:
                data = self._redis.get(f"{self.prefix}{key}")
                if data:
                    return json.loads(data)
            except Exception:
                pass
        return self._local_cache.get(key)

    def get_keys(self, pattern: str = "*") -> List[str]:
        if self._connected and self._redis:
            try:
                keys = self._redis.keys(f"{self.prefix}{pattern}")
                return [k.replace(self.prefix, "") for k in keys]
            except Exception:
                pass
        return list(self._local_cache.keys())

    def delete(self, key: str) -> bool:
        if self._connected and self._redis:
            try:
                self._redis.delete(f"{self.prefix}{key}")
                return True
            except Exception:
                pass
        self._local_cache.pop(key, None)
        return True

    def summarize_window(self, start_ts: float, end_ts: float) -> Dict[str, Any]:
        """Resumen de eventos en una ventana de tiempo."""
        keys = self.get_keys()
        events_in_window = []
        for key in keys:
            val = self.get(key)
            if isinstance(val, dict):
                ts = val.get("timestamp", 0)
                if isinstance(ts, (int, float)) and start_ts <= ts <= end_ts:
                    events_in_window.append(val)

        return {
            "total_events": len(events_in_window),
            "by_type": self._count_by_field(events_in_window, "event_type"),
            "by_severity": self._count_by_field(events_in_window, "severity"),
            "window_start": start_ts,
            "window_end": end_ts,
        }

    @staticmethod
    def _count_by_field(items: List[Dict], field: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in items:
            val = str(item.get(field, "unknown"))
            counts[val] = counts.get(val, 0) + 1
        return counts


class LongTermMemory:
    """Memoria de largo plazo en archivo JSON (resúmenes diarios)."""

    def __init__(self, data_dir: str = "data/memory"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def _daily_file(self, date_str: str) -> str:
        return os.path.join(self.data_dir, f"memory_{date_str}.json")

    def save_daily_summary(self, date_str: str, summary: Dict[str, Any]) -> None:
        path = self._daily_file(date_str)
        existing = self.load_daily(date_str)
        existing.update(summary)
        existing["updated_at"] = time.time()

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            logger.error(f"LongTermMemory save failed: {e}")

    def load_daily(self, date_str: str) -> Dict[str, Any]:
        path = self._daily_file(date_str)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def search(self, query: str, since: Optional[str] = None) -> List[Dict]:
        results = []
        for filename in sorted(os.listdir(self.data_dir)):
            if not filename.startswith("memory_"):
                continue
            date_str = filename.replace("memory_", "").replace(".json", "")
            if since and date_str < since:
                continue
            data = self.load_daily(date_str)
            if self._matches_query(data, query):
                results.append({"date": date_str, "data": data})
        return results

    @staticmethod
    def _matches_query(data: Dict, query: str) -> bool:
        query_lower = query.lower()
        for key, val in data.items():
            if query_lower in str(key).lower() or query_lower in str(val).lower():
                return True
        return False

    def cleanup(self, retention_days: int = 90) -> int:
        cutoff = time.time() - (retention_days * 86400)
        removed = 0
        for filename in os.listdir(self.data_dir):
            if not filename.startswith("memory_"):
                continue
            path = os.path.join(self.data_dir, filename)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except Exception:
                pass
        return removed


class WispMemory:
    """API unificada de memoria multinivel."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 working_size: int = 100, data_dir: str = "data/memory"):
        self.working = WorkingMemory(max_size=working_size)
        self.session = SessionMemory(redis_url=redis_url)
        self.longterm = LongTermMemory(data_dir=data_dir)

    def add(self, event: Dict[str, Any], reasoning: Optional[Dict] = None) -> None:
        self.working.add(event, reasoning)

        event_id = event.get("id", str(time.time()))
        self.session.add(
            f"event:{event_id}",
            {
                "event": event,
                "reasoning": reasoning,
                "timestamp": time.time(),
            },
        )

    def get_recent(self, n: int = 20) -> List[Dict]:
        return self.working.get_recent(n)

    def search(self, query: str, since: Optional[str] = None) -> List[Dict]:
        session_results = []
        for key in self.session.get_keys("event:*"):
            val = self.session.get(key)
            if val and query.lower() in json.dumps(val, default=str).lower():
                session_results.append(val)

        longterm_results = self.longterm.search(query, since)
        return session_results + longterm_results

    def summarize_window(self, start: float, end: float) -> Dict[str, Any]:
        return self.session.summarize_window(start, end)

    def save_daily_summary(self, summary: Dict[str, Any]) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.longterm.save_daily_summary(today, summary)

    def cleanup(self, retention_days: int = 90) -> int:
        return self.longterm.cleanup(retention_days)

    def stats(self) -> Dict[str, Any]:
        return {
            "working_memory_size": len(self.working.get_all()),
            "session_keys": len(self.session.get_keys()),
            "longterm_files": len(os.listdir(self.longterm.data_dir))
            if os.path.exists(self.longterm.data_dir) else 0,
        }
