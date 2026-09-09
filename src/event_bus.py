# src/event_bus.py
"""
Event Bus basado en Redis Streams para comunicación asíncrona
entre colectores, AI engine y agente LLM.

Patrón: Producer → Redis Stream → Consumer Group → ACK explícito.
Fallback: Si Redis no está disponible, opera en modo degradado (sin cola).
"""
import os
import json
import time
import uuid
import logging
import threading
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timezone
from collections import deque

logger = logging.getLogger(__name__)

# Lazy import de Redis
_redis = None
_redis_available = False


def _get_redis(url: str = "redis://localhost:6379/0"):
    global _redis, _redis_available
    if _redis is not None:
        return _redis
    try:
        import redis
        _redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=3,
            retry_on_timeout=True,
        )
        _redis.ping()
        _redis_available = True
        logger.info(f"Redis connected: {url}")
        return _redis
    except Exception as e:
        _redis_available = False
        logger.warning(f"Redis unavailable ({url}): {e} — operating in degraded mode")
        return None


def is_redis_available() -> bool:
    return _redis_available


class WispEvent:
    """Representa un evento en el sistema WISP."""

    def __init__(self, source: str, event_type: str, severity: str = "info",
                 device_id: str = "", device_type: str = "",
                 payload: Optional[Dict] = None,
                 context: Optional[Dict] = None):
        self.id = str(uuid.uuid4())
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.source = source
        self.device_id = device_id
        self.device_type = device_type
        self.event_type = event_type
        self.severity = severity
        self.payload = payload or {}
        self.context = context or {}
        self._enriched = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "source": self.source,
            "device_id": self.device_id,
            "device_type": self.device_type,
            "event_type": self.event_type,
            "severity": self.severity,
            "payload": self.payload,
            "context": self.context,
            "_enriched": self._enriched,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WispEvent":
        ev = cls(
            source=data.get("source", "unknown"),
            event_type=data.get("event_type", "unknown"),
            severity=data.get("severity", "info"),
            device_id=data.get("device_id", ""),
            device_type=data.get("device_type", ""),
            payload=data.get("payload", {}),
            context=data.get("context", {}),
        )
        ev.id = data.get("id", ev.id)
        ev.timestamp = data.get("timestamp", ev.timestamp)
        ev._enriched = data.get("_enriched", False)
        return ev


class EventBus:
    """
    Event Bus con Redis Streams como backend.

    Modo degradado: si Redis no está disponible, publica a una cola en memoria
    (bounded deque) que puede ser consumida localmente.
    """

    def __init__(self, url: str = "redis://localhost:6379/0",
                 stream: str = "wisp:events",
                 consumer_group: str = "wisp-agent",
                 consumer_name: str = "agent-1",
                 dlq_stream: str = "wisp:events:dlq",
                 max_retries: int = 3,
                 retry_backoff: float = 1.0,
                 max_memory_buffer: int = 5000):
        self.url = url
        self.stream = stream
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name
        self.dlq_stream = dlq_stream
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.max_memory_buffer = max_memory_buffer

        self._redis = None
        self._connected = False
        self._memory_buffer: deque = deque(maxlen=max_memory_buffer)
        self._publish_count = 0
        self._error_count = 0
        self._lock = threading.Lock()
        self._subscribers: List[Callable] = []

        self._try_connect()

    def _try_connect(self) -> bool:
        try:
            self._redis = _get_redis(self.url)
            if self._redis:
                self._connected = True
                self._ensure_consumer_group()
                return True
        except Exception as e:
            logger.warning(f"EventBus connect failed: {e}")
        self._connected = False
        return False

    def _ensure_consumer_group(self) -> None:
        if not self._connected or not self._redis:
            return
        try:
            self._redis.xgroup_create(
                self.stream, self.consumer_group,
                id="0", mkstream=True
            )
            logger.info(f"Consumer group '{self.consumer_group}' ready on '{self.stream}'")
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning(f"Consumer group create: {e}")

    def publish(self, event: WispEvent) -> bool:
        data = json.dumps(event.to_dict(), ensure_ascii=False, default=str)

        with self._lock:
            if self._connected and self._redis:
                try:
                    msg_id = self._redis.xadd(
                        self.stream,
                        {"data": data},
                        maxlen=100000,
                    )
                    self._publish_count += 1
                    logger.debug(f"Published event {event.id} → {msg_id}")
                    self._notify_subscribers(event)
                    return True
                except Exception as e:
                    self._error_count += 1
                    logger.error(f"Stream publish failed: {e}")
                    self._connected = False

            self._memory_buffer.append(data)
            self._publish_count += 1
            self._notify_subscribers(event)
            return True

    def consume(self, count: int = 10, timeout_ms: int = 5000) -> List[Dict]:
        if not self._connected or not self._redis:
            return self._consume_memory(count)

        try:
            results = self._redis.xreadgroup(
                self.consumer_group,
                self.consumer_name,
                {self.stream: ">"},
                count=count,
                block=timeout_ms,
            )
            messages = []
            for stream_name, entries in results:
                for msg_id, fields in entries:
                    data = json.loads(fields.get("data", "{}"))
                    data["_msg_id"] = msg_id
                    messages.append(data)
            return messages
        except Exception as e:
            logger.error(f"Stream consume failed: {e}")
            return self._consume_memory(count)

    def ack(self, msg_id: str) -> bool:
        if not self._connected or not self._redis:
            return True
        try:
            self._redis.xack(self.stream, self.consumer_group, msg_id)
            return True
        except Exception as e:
            logger.error(f"Stream ack failed: {e}")
            return False

    def send_to_dlq(self, event: WispEvent, reason: str = "") -> bool:
        payload = event.to_dict()
        payload["_dlq_reason"] = reason
        data = json.dumps(payload, ensure_ascii=False, default=str)

        if self._connected and self._redis:
            try:
                self._redis.xadd(
                    self.dlq_stream,
                    {"data": data},
                    maxlen=10000,
                )
                logger.warning(f"Event {event.id} sent to DLQ: {reason}")
                return True
            except Exception as e:
                logger.error(f"DLQ publish failed: {e}")
        return False

    def _consume_memory(self, count: int) -> List[Dict]:
        messages = []
        for _ in range(min(count, len(self._memory_buffer))):
            if self._memory_buffer:
                data = self._memory_buffer.popleft()
                entry = json.loads(data)
                entry["_msg_id"] = f"mem-{uuid.uuid4().hex[:8]}"
                entry["_memory"] = True
                messages.append(entry)
        return messages

    def ack_memory(self, msg_id: str) -> bool:
        return True

    def subscribe(self, callback: Callable[[WispEvent], None]) -> None:
        self._subscribers.append(callback)

    def _notify_subscribers(self, event: WispEvent) -> None:
        for cb in self._subscribers:
            try:
                cb(event)
            except Exception as e:
                logger.error(f"Subscriber callback failed: {e}")

    def pending_count(self) -> int:
        if not self._connected or not self._redis:
            return len(self._memory_buffer)
        try:
            info = self._redis.xpending(self.stream, self.consumer_group)
            return info.get("pending", 0) if isinstance(info, dict) else 0
        except Exception:
            return 0

    def stats(self) -> Dict[str, Any]:
        return {
            "connected": self._connected,
            "publish_count": self._publish_count,
            "error_count": self._error_count,
            "memory_buffer_size": len(self._memory_buffer),
            "pending_count": self.pending_count(),
            "stream": self.stream,
            "consumer_group": self.consumer_group,
        }

    def close(self) -> None:
        if self._redis:
            try:
                self._redis.close()
            except Exception:
                pass
        self._connected = False


_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def get_event_bus(**kwargs) -> EventBus:
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                from .config_ia import load_config_ia
                config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
                try:
                    config = load_config_ia(config_path)
                    _bus = EventBus(
                        url=config.redis.url,
                        stream=config.event_bus.stream,
                        consumer_group=config.event_bus.consumer_group,
                        consumer_name=config.event_bus.consumer_name,
                        dlq_stream=config.event_bus.dlq_stream,
                        max_retries=config.event_bus.max_retries,
                        retry_backoff=config.event_bus.retry_backoff_seconds,
                    )
                except Exception:
                    _bus = EventBus(**kwargs)
    return _bus
