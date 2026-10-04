# src/ai/agent_loop.py
"""
Agent Loop: Loop autónomo 24/7 con memoria para el agente WISP.

Consume eventos del bus → enriquece → correlaciona → razona → ejecuta acciones.
Modo degradado: si el LLM falla N veces seguidas, sigue con reglas determinísticas.
Publishes heartbeat cada 30s en wisp:agent:health.
"""
import os
import json
import time
import signal
import asyncio
import logging
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Histogram, Gauge, start_http_server
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

try:
    from prometheus_client import Counter as PCounter, Histogram as PHistogram, Gauge as PGauge
except ImportError:
    Counter = Histogram = Gauge = None


class WispAgent:
    """Loop principal del agente autónomo."""

    def __init__(self, bus, enricher, reasoner, memory, correlator,
                 policy, actions_registry, notification_service,
                 config=None, influx_client=None,
                 client_lookup=None, rag_store=None):
        self.bus = bus
        self.enricher = enricher
        self.reasoner = reasoner
        self.memory = memory
        self.correlator = correlator
        self.policy = policy
        self.actions_registry = actions_registry
        self.notifier = notification_service
        self.config = config
        self.influx = influx_client
        self.client_lookup = client_lookup
        self.rag = rag_store

        self._running = False
        self._degraded_mode = False
        self._llm_failure_count = 0
        self._processed_events = 0
        self._alert_count = 0
        self._action_count = 0
        self._last_event_ts = 0
        self._last_heartbeat_ts = 0
        self._llm_failures = 0

        self._max_llm_failures = 5
        self._heartbeat_interval = 30

        if self.config:
            self._max_llm_failures = config.agent_loop.max_llm_failures
            self._heartbeat_interval = config.agent_loop.heartbeat_interval_seconds

        self._setup_metrics()

    _METRIC_CACHE = {}

    def _setup_metrics(self) -> None:
        if not PROMETHEUS_AVAILABLE:
            return
        if not WispAgent._METRIC_CACHE:
            WispAgent._METRIC_CACHE["events"] = PCounter(
                "wisp_events_processed", "Eventos procesados")
            WispAgent._METRIC_CACHE["latency"] = PHistogram(
                "wisp_event_latency_seconds", "Latencia de procesamiento")
            WispAgent._METRIC_CACHE["llm_errors"] = PCounter(
                "wisp_llm_errors", "Errores del LLM")
            WispAgent._METRIC_CACHE["heartbeat"] = PGauge(
                "wisp_agent_heartbeat", "Último heartbeat del agente")
        self._metric_events = WispAgent._METRIC_CACHE["events"]
        self._metric_latency = WispAgent._METRIC_CACHE["latency"]
        self._metric_llm_errors = WispAgent._METRIC_CACHE["llm_errors"]
        self._metric_heartbeat = WispAgent._METRIC_CACHE["heartbeat"]

    def start_prometheus(self, port: int = 9100) -> None:
        if PROMETHEUS_AVAILABLE:
            try:
                start_http_server(port)
                logger.info(f"Prometheus metrics on :{port}")
            except Exception as e:
                logger.error(f"Prometheus start failed: {e}")

    async def run(self) -> None:
        self._running = True
        logger.info("WispAgent loop started")

        self.bus.subscribe(self._on_event_local)

        while self._running:
            try:
                messages = self.bus.consume(count=10, timeout_ms=3000)
                for msg in messages:
                    self._process_message(msg)

                await self._heartbeat()
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"Agent loop error: {e}", exc_info=True)
                await asyncio.sleep(1)

        logger.info("WispAgent loop stopped")

    def run_sync(self) -> None:
        """Wrapper síncrono para poder correr en un thread."""
        self._running = True
        logger.info("WispAgent loop started (sync)")

        self.bus.subscribe(self._on_event_local)

        while self._running:
            try:
                messages = self.bus.consume(count=10, timeout_ms=3000)
                for msg in messages:
                    try:
                        self._process_message(msg)
                    except RuntimeError:
                        pass

                self._heartbeat_sync()

                # Check approval timeouts
                if self.actions_registry and self.actions_registry.approval_service:
                    self.actions_registry.approval_service.check_timeouts()

                time.sleep(0.1)
            except Exception as e:
                logger.error(f"Agent loop error: {e}", exc_info=True)
                time.sleep(1)

        logger.info("WispAgent loop stopped (sync)")

    def stop(self) -> None:
        self._running = False
        logger.info("Stop signal received")

    def _on_event_local(self, event) -> None:
        self._processed_events += 1

    def _process_message(self, msg: Dict) -> None:
        from ..event_bus import WispEvent
        event = WispEvent.from_dict({
            k: v for k, v in msg.items()
            if k not in ("_msg_id",)
        })

        self._last_event_ts = time.time()

        # ACK siempre (evita re-delivery infinito)
        msg_id = msg.get("_msg_id")
        if msg_id:
            self.bus.ack(msg_id)

        # Anti-eco: ignorar eventos del propio agente (heartbeats, self-published)
        evt_type = getattr(event, "event_type", "")
        evt_source = getattr(event, "source", "")
        evt_device = getattr(event, "device_id", "")
        if (evt_type == "heartbeat"
                or evt_source == "agent"
                or evt_device == "wisp-agent"):
            return

        try:
            enriched = self.enricher.enrich(event)
            self.memory.add(event.to_dict())

            pattern = self.correlator.process_event(event.to_dict())
            if pattern:
                self._handle_pattern(pattern)

            reasoning = self.reasoner.reason(enriched.to_dict())
            self.memory.add(enriched.to_dict(), reasoning.to_dict())

            self._processed_events += 1
            self._last_event_ts = time.time()

            if reasoning.severidad_recalculada == "critical" or \
               event.severity == "critical":
                self._handle_critical(event, reasoning)
            elif reasoning.severidad_recalculada in ("warning", "info"):
                maybe, reason = self.policy.should_alert_now(
                    reasoning.severidad_recalculada,
                    zone=event.context.get("zone", ""),
                )
                if maybe:
                    self._send_alert(event, reasoning, reason)

        except Exception as e:
            logger.error(f"Event processing failed: {e}", exc_info=True)
            if msg_id:
                self.bus.send_to_dlq(event, reason=str(e))

    def _handle_pattern(self, pattern) -> None:
        pattern_event = pattern.to_event_dict()
        enriched = self.enricher.enrich(self._event_from_pattern(pattern_event))
        reasoning = self.reasoner.reason(enriched.to_dict())

        self._send_alert(enriched, reasoning, "pattern_detected")
        pattern_id = pattern_event["id"]
        logger.warning(f"Patrón manejado: {pattern.pattern_type} ({pattern_id})")

        if self.correlator:
            from .correlator import PatternMatch
            if isinstance(pattern, PatternMatch):
                self.actions_registry.execute(
                    "EnviarAlertaTelegramAction",
                    title=f"Patrón: {pattern.pattern_type}",
                    message=pattern.description,
                    severity=pattern.severity,
                    device_id=pattern.devices[0] if pattern.devices else "",
                )

    def _event_from_pattern(self, pattern_dict: Dict):
        from ..event_bus import WispEvent
        return WispEvent(
            source=pattern_dict.get("source", "correlator"),
            event_type=pattern_dict.get("event_type", "pattern"),
            severity=pattern_dict.get("severity", "warning"),
            device_id=pattern_dict.get("device_id", ""),
            device_type=pattern_dict.get("device_type", "pattern"),
            payload=pattern_dict.get("payload", {}),
            context=pattern_dict.get("context", {}),
        )

    def _handle_critical(self, event, reasoning) -> None:
        self._send_alert(event, reasoning, "critical")
        self._alert_count += 1

    def _send_alert(self, event, reasoning, reason: str = "") -> None:
        if not self.notifier:
            logger.warning("No notification service; alert skipped")
            return

        zone = event.context.get("zone", "")
        self.policy.record_alert_sent(zone)

        title = f"{event.event_type} — {event.device_id}"
        message_parts = []
        if reasoning.diagnostico:
            message_parts.append(f"🔍 {reasoning.diagnostico}")
        if reasoning.causa_raiz_probable:
            message_parts.append(f"🧠 Causa probable: {reasoning.causa_raiz_probable}")
        # Acciones: omitidas según solicitud (no incluir en formato unificado)
        if reasoning.model:
            message_parts.append(f"🤖 Modelo: {reasoning.model}")

        severity = reasoning.severidad_recalculada or event.severity or "warning"

        # Dedup por estado persistente: host_down/host_up comparten clave para
        # que una recuperación re-active una nueva alerta de caída (y una nueva
        # caída re-active tras una recuperación). Otros eventos dedup por
        # severidad (solo re-alertan si la condición cambia).
        event_type = event.event_type or "event"
        device_id = event.device_id or "unknown"
        if event_type in ("host_down", "host_up"):
            dedup_key = f"agent:host:{device_id}"
            state_fingerprint = {
                "status": "down" if event_type == "host_down" else "up"
            }
        else:
            dedup_key = f"agent:{event_type}:{device_id}"
            state_fingerprint = {"severity": severity}

        try:
            self.notifier.send_llm_alert(
                title=title,
                severity=severity,
                message="\n".join(message_parts),
                dedup_key=dedup_key,
                state_fingerprint=state_fingerprint,
            )
            self._alert_count += 1
        except Exception as e:
            logger.error(f"Alert send failed: {e}")

    async def _heartbeat(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_ts < self._heartbeat_interval:
            return

        self._last_heartbeat_ts = now
        health = self.get_health()
        try:
            self.bus.publish(self._health_event(health))
        except Exception as e:
            logger.error(f"Heartbeat publish failed: {e}")

        if PROMETHEUS_AVAILABLE:
            try:
                self._metric_heartbeat.set(now)
            except Exception:
                pass

    def _heartbeat_sync(self) -> None:
        now = time.time()
        if now - self._last_heartbeat_ts < self._heartbeat_interval:
            return

        self._last_heartbeat_ts = now
        health = self.get_health()
        try:
            self.bus.publish(self._health_event(health))
            self._persist_health(health)
        except Exception as e:
            logger.error(f"Heartbeat publish failed: {e}")

        if PROMETHEUS_AVAILABLE:
            try:
                self._metric_heartbeat.set(now)
            except Exception:
                pass

    def _persist_health(self, health: Dict) -> None:
        """Persiste health/stats a archivos que la API del dashboard pueda leer."""
        try:
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            logs_dir = os.path.join(root, "logs")
            os.makedirs(logs_dir, exist_ok=True)

            health_file = os.path.join(logs_dir, "wisp_agent_health.json")
            with open(health_file, "w") as f:
                json.dump(health, f, indent=2)

            stats_file = os.path.join(logs_dir, "wisp_agent_stats.json")
            with open(stats_file, "w") as f:
                json.dump(self.get_stats(), f, indent=2)
        except Exception as e:
            logger.error(f"Health persist failed: {e}")

    def _health_event(self, health: Dict):
        from ..event_bus import WispEvent
        return WispEvent(
            source="agent",
            event_type="heartbeat",
            severity="info",
            device_id="wisp-agent",
            device_type="agent",
            payload=health,
        )

    def get_health(self) -> Dict:
        return {
            "status": "degraded" if self._degraded_mode else "healthy",
            "running": self._running,
            "last_activity": self._last_event_ts,
            "last_heartbeat": self._last_heartbeat_ts,
            "processed_events": self._processed_events,
            "llm_failures": self._llm_failure_count,
            "in_degraded_mode": self._degraded_mode,
            "uptime": time.time() - getattr(self, "_start_time", time.time()),
        }

    def get_stats(self) -> Dict:
        return {
            "processed_events": self._processed_events,
            "alerts_sent": self._alert_count,
            "actions_executed": self._action_count,
            "llm_failures": self._llm_failure_count,
            "degraded_mode": self._degraded_mode,
            "memory": self.memory.stats() if self.memory else {},
            "correlator": self.correlator.stats() if self.correlator else {},
        }

    def process_event_directly(self, event) -> Dict:
        """Procesa un evento directamente (sin bus). Útil para test/API."""
        try:
            enriched = self.enricher.enrich(event)
            reasoning = self.reasoner.reason(enriched.to_dict())
            self.memory.add(enriched.to_dict(), reasoning.to_dict())
            self._processed_events += 1
            return {
                "ok": True,
                "event_id": event.id,
                "reasoning": reasoning.to_dict(),
            }
        except Exception as e:
            logger.error(f"Direct event processing failed: {e}")
            return {"ok": False, "error": str(e)}


def create_agent_sync(config_path: str = "config/config_ia.yaml") -> WispAgent:
    """Construye un WispAgent a partir de la configuración."""
    from ..config_ia import load_config_ia
    from ..event_bus import get_event_bus
    from ..enricher import EventEnricher
    from ..storage.influx_client import InfluxClient
    from .llm_agent.tools import InfluxReader
    from .reasoner import WispReasoner
    from .rag import get_rag_store
    from .memory_ext import WispMemory
    from .correlator import WispCorrelator
    from ..policy import AlertPolicy
    from ..actions import build_registry, ApprovalService
    from ..notifier_ai import TelegramNotifierAI, TelegramConfig

    config = load_config_ia(config_path)

    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
        token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
        org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
        bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
    )
    influx.connect()

    reader = InfluxReader(influx)

    bus = get_event_bus()

    rag = get_rag_store()

    # Crear provider LLM para el reasoner (Ollama local, Anthropic, o OpenRouter)
    llm_provider = None
    if config.llm.enabled:
        try:
            from .llm_agent.providers import create_provider
            llm_provider = create_provider(config=config.llm)
            logger.info(f"Reasoner LLM provider: {config.llm.provider} ({config.llm.model})")
        except Exception as e:
            logger.warning(f"LLM provider unavailable, using fallback-rules: {e}")

    reasoner = WispReasoner(provider=llm_provider, rag_store=rag, influx_client=influx)
    enricher = EventEnricher(None, influx_client=influx, reader=reader)
    memory = WispMemory(redis_url=config.redis.url)
    correlator = WispCorrelator(
        cascade_window=config.correlator.cascade_window_seconds,
        cascade_min_nodes=config.correlator.cascade_min_nodes,
        mass_threshold=config.correlator.mass_threshold,
        recurrence_window=config.correlator.recurrence_window_hours * 3600,
        recurrence_min_count=config.correlator.recurrence_min_count,
        degradation_window=config.correlator.degradation_window_hours * 3600,
    )
    policy = AlertPolicy(
        work_hours_start=config.policy.work_hours_start,
        work_hours_end=config.policy.work_hours_end,
        work_days=config.policy.work_days,
        max_alerts_per_hour=config.policy.max_alerts_per_hour_per_zone,
        critical_always_immediate=config.policy.critical_always_immediate,
        quiet_summary_interval=config.policy.quiet_hours_summary_interval_minutes,
    )

    notifier = TelegramNotifierAI(TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id),
        cooldown_minutes=config.telegram.cooldown_minutes,
        state_file=config.telegram.state_file,
    ))

    approval = ApprovalService(
        telegram_sender=notifier,
        timeout_seconds=config.actions.approval_timeout_seconds,
        max_concurrent=config.actions.max_concurrent_actions,
    )
    registry = build_registry(approval_service=approval)

    agent = WispAgent(
        bus=bus,
        enricher=enricher,
        reasoner=reasoner,
        memory=memory,
        correlator=correlator,
        policy=policy,
        actions_registry=registry,
        notification_service=notifier,
        config=config,
        influx_client=influx,
        rag_store=rag,
    )
    return agent


def main():
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
    )

    agent = create_agent_sync(config_path)

    shutdown = threading.Event()

    def signal_handler(signum, frame):
        logger.info(f"Signal {signum}, stopping agent...")
        shutdown.set()
        agent.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if PROMETHEUS_AVAILABLE:
        agent.start_prometheus(port=int(os.environ.get("PROMETHEUS_PORT", "9100")))

    logger.info("WispAgent starting...")
    agent.run_sync()


if __name__ == "__main__":
    main()
