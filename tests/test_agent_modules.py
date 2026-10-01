# tests/test_agent_modules.py
"""
Tests para los módulos de las Fases 4-6:
event_bus, policy, correlator, reasoner, audit, enricher, actions.
"""
import time
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.event_bus import EventBus, WispEvent
from src.policy import AlertPolicy
from src.audit import AuditLogger
from src.enricher import EventEnricher
from src.models import Client

# ── Event Bus (modo degradado / memoria) ─────────────────────────────────────

def test_event_bus_degraded_publish_consume():
    """Sin Redis, publica a memoria y consume localmente."""
    bus = EventBus(url="redis://127.0.0.1:1/0", stream="wisp:test")
    assert not bus._connected

    ev = WispEvent(source="test", event_type="host_down", severity="critical",
                   device_id="NODE-1", payload={"ip": "10.0.0.1"})
    assert bus.publish(ev) is True

    messages = bus.consume(count=10, timeout_ms=100)
    assert len(messages) == 1
    assert messages[0]["event_type"] == "host_down"
    assert messages[0]["device_id"] == "NODE-1"
    # El ACK por memoria no falla
    assert bus.ack(messages[0]["_msg_id"]) is True
    bus.close()


def test_event_bus_subscriber_notified():
    bus = EventBus(url="redis://127.0.0.1:1/0", stream="wisp:test2")
    seen = []
    bus.subscribe(lambda e: seen.append(e))
    bus.publish(WispEvent(source="t", event_type="info"))
    assert len(seen) == 1
    bus.close()


# ── Policy Engine ────────────────────────────────────────────────────────────

def test_policy_critical_always_immediate():
    policy = AlertPolicy(critical_always_immediate=True)
    ok, reason = policy.should_alert_now("critical", zone="Z-1")
    assert ok is True
    assert reason == "critical_immediate"


def test_policy_silence_zone():
    policy = AlertPolicy(critical_always_immediate=True,
                         max_alerts_per_hour=100)
    policy.silence_zone("Z-SIL", minutes=60)

    ok, reason = policy.should_alert_now("warning", zone="Z-SIL")
    assert ok is False
    assert reason == "zone_silenced"

    # Las críticas pasan aunque la zona esté silenciada
    ok, reason = policy.should_alert_now("critical", zone="Z-SIL")
    assert ok is True

    stats = policy.stats()
    assert "Z-SIL" in stats["silenced_zones"]


def test_policy_rate_limit():
    policy = AlertPolicy(critical_always_immediate=False,
                         max_alerts_per_hour=2,
                         work_hours_start=0, work_hours_end=24,
                         work_days=[0, 1, 2, 3, 4, 5, 6])
    for _ in range(2):
        ok, reason = policy.should_alert_now("info", zone="Z-RATE")
        assert ok is True
        policy.record_alert_sent("Z-RATE")

    ok, reason = policy.should_alert_now("info", zone="Z-RATE")
    assert ok is False
    assert reason == "rate_limited"


# ── Correlator ───────────────────────────────────────────────────────────────

def _iso(minutes_ago: int) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return ts.isoformat()


def _event(etype: str, device: str, severity: str = "warning",
           payload=None, ts: str = ""):
    return {
        "event_type": etype,
        "device_id": device,
        "device_type": "olt",
        "severity": severity,
        "payload": payload or {},
        "context": {},
        "timestamp": ts or _iso(0),
    }


def test_correlator_cascade():
    from src.ai.correlator import WispCorrelator
    corr = WispCorrelator(cascade_window=300, cascade_min_nodes=2,
                          mass_threshold=50)

    assert corr.process_event(_event("host_down", "NODE-1", "critical")) is None
    pattern = corr.process_event(_event("host_down", "NODE-2", "critical"))
    assert pattern is not None
    assert pattern.pattern_type == "cascade"
    assert pattern.severity == "critical"
    assert set(pattern.devices) == {"NODE-1", "NODE-2"}


def test_correlator_mass():
    from src.ai.correlator import WispCorrelator
    corr = WispCorrelator(mass_threshold=3, cascade_min_nodes=50)

    corr.process_event(_event("host_down", "NODE-A", "critical"))
    corr.process_event(_event("host_down", "NODE-B", "critical"))
    pattern = corr.process_event(_event("host_down", "NODE-C", "critical"))
    assert pattern is not None
    assert pattern.pattern_type == "mass_down"


def test_correlator_recurrence():
    from src.ai.correlator import WispCorrelator
    corr = WispCorrelator(mass_threshold=50, cascade_min_nodes=50,
                          recurrence_min_count=3)

    corr.process_event(_event("anomaly", "ONU-123", "warning", ts=_iso(120)))
    corr.process_event(_event("anomaly", "ONU-123", "warning", ts=_iso(60)))
    pattern = corr.process_event(_event("anomaly", "ONU-123", "warning", ts=_iso(0)))
    assert pattern is not None
    assert pattern.pattern_type == "recurrence"
    assert pattern.devices == ["ONU-123"]


def test_correlator_degradation():
    from src.ai.correlator import WispCorrelator
    corr = WispCorrelator(mass_threshold=50, cascade_min_nodes=50,
                          recurrence_min_count=50,
                          degradation_window=21600)

    corr.process_event(_event("low_signal", "ONU-9", "warning",
                              payload={"rx_power": -27.0}, ts=_iso(120)))
    corr.process_event(_event("low_signal", "ONU-9", "warning",
                              payload={"rx_power": -29.0}, ts=_iso(60)))
    pattern = corr.process_event(_event("low_signal", "ONU-9", "warning",
                                        payload={"rx_power": -31.0}, ts=_iso(0)))
    assert pattern is not None
    assert pattern.pattern_type == "degradation"


# ── Reasoner (fallback determinístico) ───────────────────────────────────────

def test_reasoner_fallback_host_down():
    from src.ai.reasoner import WispReasoner
    reasoner = WispReasoner(provider=None)
    result = reasoner.reason({
        "event_type": "host_down",
        "device_id": "OLT-PRINCIPAL",
        "device_type": "olt",
        "severity": "critical",
        "context": {"zone": "NORTE", "clientes_afectados": 25},
        "payload": {},
    })
    assert result.model == "fallback-rules"
    assert result.severidad_recalculada == "critical"
    assert any(
        a.get("nombre") == "DiagnosticarAction"
        for a in result.acciones_recomendadas
    )
    assert result.latency_ms >= 0


def test_reasoner_parse_markdown_json():
    from src.ai.reasoner import WispReasoner
    r = WispReasoner()
    parsed = r._parse_response('```json\n{"diagnostico": "prueba", "severidad": "warning", "confianza": 0.9}\n```')
    assert parsed["diagnostico"] == "prueba"
    assert parsed["severidad"] == "warning"


# ── Audit Logger ─────────────────────────────────────────────────────────────

def test_audit_logger(tmp_path):
    log_file = str(tmp_path / "audit.jsonl")
    audit = AuditLogger(log_file=log_file, enabled=True)
    audit.log(actor="system", action="event:host_down", target="NODE-1")
    audit.flush()

    entries = audit.query(limit=1)
    assert len(entries) >= 1
    assert entries[0]["action"] == "event:host_down"
    assert entries[0]["target"] == "NODE-1"

    # el archivo existe en disco
    assert Path(log_file).exists()


# ── Enricher ─────────────────────────────────────────────────────────────────

def _fake_lookup():
    lookup = MagicMock()
    client = Client(
        nombre="JUAN PEREZ", serial_onu="abcd1234", olt="OLT-NORTE-1",
        nodo="NORTE-1", puerto_pon="GPON0/1", direccion="Barrio Las Flores",
    )
    lookup.lookup_by_serial.return_value = client
    lookup.clients_on_pon.return_value = [client, client]
    return lookup


def test_enricher_adds_client_and_zone():
    enricher = EventEnricher(_fake_lookup(), influx_client=None)
    ev = WispEvent(source="test", event_type="low_signal", severity="warning",
                   device_id="ONU-1",
                   payload={"onu_serial": "abcd1234",
                            "olt_name": "OLT-NORTE-1",
                            "pon_port": "GPON0/1"})
    enriched = enricher.enrich(ev)

    assert enriched._enriched is True
    assert enriched.context["client_name"] == "JUAN PEREZ"
    assert enriched.context["zone"] == "Las Flores"
    assert enriched.context["clientes_afectados"] == 2


def test_extract_zone():
    assert EventEnricher._extract_zone("Barrio La Merced Central") == "La Merced Central"
    assert EventEnricher._extract_zone("") == ""


# ── Actions registry / approval ──────────────────────────────────────────────

def test_action_registry_build():
    from src.actions import build_registry, BaseAction
    registry = build_registry()
    names = {a["name"] for a in registry.list_actions()}
    assert "DiagnosticarAction" in names
    assert "RebootOLTAction" in names
    assert "BloquearClienteAction" in names

    action = registry.get_action("DiagnosticarAction")
    assert action.level == 0
    assert action.requires_approval() is False


def test_approval_service_resolve():
    from src.actions import ApprovalService, BaseAction

    class DummyAction(BaseAction):
        name = "Dummy"
        level = 3

        def execute(self, device_id="", **kwargs):
            return {"ok": True}

    svc = ApprovalService(telegram_sender=None, timeout_seconds=60)
    action_id = svc.request_approval(DummyAction())
    assert svc.get_request(action_id).status == "pending"

    assert svc.resolve(action_id, True, approver="telegram-user") is True
    assert svc.get_request(action_id) is None


# ── Anti-eco: el agente NO se alerta a sí mismo ──────────────────────────────

def test_correlator_ignores_agent_heartbeats():
    """Heartbeats publicados por el propio agente no deben disparar patrones."""
    from src.ai.correlator import WispCorrelator
    corr = WispCorrelator(mass_threshold=50, cascade_min_nodes=50,
                          recurrence_min_count=3)
    for i in range(5):
        ev = dict(_event("heartbeat", "wisp-agent", "info", ts=_iso(i)),
                  # replicar source del agente (helper no lo expone)
                  payload={"source": "agent"})
        ev["source"] = "agent"
        pattern = corr.process_event(ev)
        assert pattern is None, "heartbeat del agente no debe generar patrón"
    # Los self-events no se acumulan en el buffer de correlación
    assert len(corr._recent_events) == 0, "self-events no deben acumularse"


def _make_agent():
    from src.ai.agent_loop import WispAgent
    agent = WispAgent(
        bus=MagicMock(), enricher=MagicMock(), reasoner=MagicMock(),
        memory=MagicMock(), correlator=MagicMock(), policy=MagicMock(),
        actions_registry=MagicMock(), notification_service=MagicMock(),
        config=None,
    )
    return agent


def test_anti_echo_filter_returns_none():
    """_process_message es síncrono y descarta eventos propios sin romper el loop."""
    agent = _make_agent()
    agent.bus.ack = MagicMock(return_value=True)

    # Heartbeat del propio agente: se ACK pero NO se procesa ni correlaciona
    result = agent._process_message({
        "_msg_id": "m1",
        "event_type": "heartbeat",
        "source": "agent",
        "device_id": "wisp-agent",
        "severity": "info",
    })
    assert result is None
    agent.bus.ack.assert_called_with("m1")
    agent.correlator.process_event.assert_not_called()


def test_run_sync_processes_message_directly():
    """_process_message es síncrono: no debe invocarse con asyncio.run."""
    import inspect
    from src.ai.agent_loop import WispAgent
    assert not inspect.iscoroutinefunction(WispAgent._process_message), \
        "_process_message no debe ser coroutine (regresión: asyncio.run(None) falla)"
    # Llamada directa sin asyncio: no lanza "a coroutine was expected"
    agent = _make_agent()
    assert agent._process_message({"severity": "info"}) is None


# ── Send alert: dedup por estado (host_down/host_up comparten clave) ────────

def _reasoning(sev="critical"):
    return SimpleNamespace(
        severidad_recalculada=sev,
        diagnostico="Evento 'host_down' en X (mikrotik)",
        causa_raiz_probable="posible caída de enlace",
        acciones_recomendadas=[{"nombre": "DiagnosticarAction", "nivel": 0},
                               {"nombre": "EnviarAlertaTelegramAction", "nivel": 1}],
        model="fallback-rules",
    )


def _evt(etype, device, severity="critical"):
    return SimpleNamespace(
        event_type=etype, device_id=device, device_type="mikrotik",
        severity=severity, context={}, payload={},
    )


def test_send_alert_host_pairing_dedup_keys():
    """host_down y host_up comparten la clave agent:host:<device> para que una
    recuperación o una nueva caída re-activen la alerta."""
    agent = _make_agent()
    agent.notifier = MagicMock()

    agent._send_alert(_evt("host_down", "NODO-CASTORES"), _reasoning("critical"))
    agent._send_alert(_evt("host_up", "NODO-CASTORES"), _reasoning("info"))
    agent._send_alert(_evt("host_down", "NODO-CASTORES"), _reasoning("critical"))

    calls = agent.notifier.send_llm_alert.call_args_list
    assert len(calls) == 3
    assert calls[0].kwargs["dedup_key"] == "agent:host:NODO-CASTORES"
    assert calls[0].kwargs["state_fingerprint"] == {"status": "down"}
    assert calls[1].kwargs["state_fingerprint"] == {"status": "up"}
    assert calls[2].kwargs["state_fingerprint"] == {"status": "down"}
    assert calls[0].kwargs["severity"] == "critical"


def test_send_alert_pattern_dedup_por_severidad():
    """Patrones (cascade/recurrence): clave por evento+device, dedup por severidad."""
    agent = _make_agent()
    agent.notifier = MagicMock()

    agent._send_alert(_evt("cascade", "SWITCH-BACKBONE"), _reasoning("critical"))
    agent._send_alert(_evt("cascade", "SWITCH-BACKBONE"), _reasoning("critical"))

    calls = agent.notifier.send_llm_alert.call_args_list
    assert len(calls) == 2
    for call in calls:
        assert call.kwargs["dedup_key"] == "agent:cascade:SWITCH-BACKBONE"
        assert call.kwargs["state_fingerprint"] == {"severity": "critical"}


def test_send_alert_title_y_fingerprint_incluyen_severidad():
    agent = _make_agent()
    agent.notifier = MagicMock()

    agent._send_alert(_evt("low_signal", "ONU-42"), _reasoning("warning"))
    call = agent.notifier.send_llm_alert.call_args
    assert call.kwargs["title"] == "low_signal — ONU-42"
    assert call.kwargs["state_fingerprint"] == {"severity": "warning"}


# ── Reasoner: degradación LLM→fallback (no debe quedar vacío) ───────────────

def test_reasoner_degradation_to_fallback_on_garbage():
    """LLM que devuelve basura (no JSON) ⇒ degrada a fallback-rules con diagnóstico."""
    from src.ai.reasoner import WispReasoner

    class LLMResult:
        def __init__(self, text="", model="qwen2.5:3b-instruct"):
            self.text, self.model = text, model
            self.prompt_tokens, self.completion_tokens = 0, 0

    class GarbageProvider:
        def chat(self, messages, **kw):
            return LLMResult(text="esto no es JSON")

    r = WispReasoner(provider=GarbageProvider())
    res = r.reason({
        "event_type": "host_down",
        "device_id": "OLT-1",
        "device_type": "olt",
        "severity": "critical",
        "context": {"zone": "N", "clientes_afectados": 5},
        "payload": {},
    })
    assert res.model == "fallback-rules"
    assert res.diagnostico
    assert res.severidad_recalculada == "critical"


def test_reasoner_uses_llm_when_json_valid():
    """LLM que responde JSON válido ⇒ se conserva la respuesta del LLM."""
    from src.ai.reasoner import WispReasoner

    class LLMResult:
        def __init__(self, text="", model="partial-llm"):
            self.text, self.model = text, model
            self.prompt_tokens, self.completion_tokens = 0, 0

    class GoodProvider:
        def chat(self, messages, **kw):
            text = ('{"diagnostico": "caida por falla de fibra", '
                    '"severidad": "critical", "confianza": 0.9, "acciones": []}')
            return LLMResult(text=text)

    r = WispReasoner(provider=GoodProvider())
    res = r.reason({
        "event_type": "host_down", "device_id": "OLT-1",
        "device_type": "olt", "severity": "critical",
        "context": {}, "payload": {},
    })
    assert res.model == "partial-llm"
    assert "fibra" in res.diagnostico