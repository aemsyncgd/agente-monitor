# tests/test_notifier_ai.py
import time
from unittest.mock import MagicMock, patch

from src.notifier_ai import TelegramNotifierAI, TelegramConfig


def make_notifier(tmp_path):
    cfg = TelegramConfig(
        bot_token="test_token",
        chat_id="-100123456",
        cooldown_minutes=15,
        state_file=str(tmp_path / "alert_state.json"),
    )
    return TelegramNotifierAI(cfg)


def mock_post():
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"ok": True, "result": {"message_id": 1}}
    return patch("src.notifier_ai.requests.post", return_value=resp)


# === Ping alerts ===

def test_ping_down_sent_only_once(tmp_path):
    notifier = make_notifier(tmp_path)
    kwargs = dict(
        host_name="SWITCH-BACKBONE", host_ip="172.16.15.2",
        host_type="mikrotik", status="down", downtime_seconds=120,
    )
    with mock_post() as post:
        assert notifier.send_ping_alert(**kwargs) is True
        assert notifier.send_ping_alert(**kwargs) is False
    assert post.call_count == 1


def test_ping_down_message_html_formatted(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        notifier.send_ping_alert(
            host_name="SWITCH-BACKBONE", host_ip="172.16.15.2",
            host_type="mikrotik", status="down", downtime_seconds=120,
        )
    payload = post.call_args.kwargs["json"]
    assert payload["parse_mode"] == "HTML"
    text = payload["text"]
    assert "<b>HOST CAÍDO</b>" in text
    assert "<b>Nombre:</b> SWITCH-BACKBONE" in text
    assert "<code>172.16.15.2</code>" in text
    assert "SIN RESPUESTA" in text
    assert "Tiempo caído:</b> 2 min" in text


def test_ping_recovery_sent_on_change_and_not_spammed(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        assert notifier.send_ping_alert(
            host_name="H", host_ip="1.1.1.1", host_type="mikrotik",
            status="down", downtime_seconds=120,
        ) is True
        assert notifier.send_ping_alert(
            host_name="H", host_ip="1.1.1.1", host_type="mikrotik",
            status="down", downtime_seconds=500,
        ) is False
        assert notifier.send_ping_alert(
            host_name="H", host_ip="1.1.1.1", host_type="mikrotik",
            status="up", downtime_seconds=300, avg_latency=1.5,
        ) is True
        assert notifier.send_ping_alert(
            host_name="H", host_ip="1.1.1.1", host_type="mikrotik",
            status="up", downtime_seconds=1, avg_latency=1.2,
        ) is False
    assert post.call_count == 2

    payload = post.call_args.kwargs["json"]
    assert "<b>HOST RECUPERADO</b>" in payload["text"]
    assert "Tiempo caído total:</b> 5 min" in payload["text"]


def test_recovery_uses_stored_down_since_after_restart(tmp_path):
    notifier = make_notifier(tmp_path)
    notifier._state_store.record("ping:H", {"status": "down"}, down_since=time.time() - 3600)
    with mock_post() as post:
        assert notifier.send_ping_alert(
            host_name="H", host_ip="1.1.1.1", host_type="mikrotik",
            status="up", downtime_seconds=0,
        ) is True
    text = post.call_args.kwargs["json"]["text"]
    assert "Tiempo caído total:</b> 60 min" in text


def test_ping_dynamic_values_are_html_escaped(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        notifier.send_ping_alert(
            host_name="ROUTER & <CORE>", host_ip="10.0.0.1", host_type="mikrotik",
            status="down", downtime_seconds=120,
        )
    text = post.call_args.kwargs["json"]["text"]
    assert "ROUTER &amp; &lt;CORE&gt;" in text


# === Anomaly alerts ===

def test_anomaly_dedup_by_condition(tmp_path):
    notifier = make_notifier(tmp_path)
    base = dict(olt_name="OLT-1", pon_port="GPON0/1:1", onu_index="1", onu_serial="VSOL1")
    with mock_post() as post:
        assert notifier.send_anomaly_alert(
            anomaly_type="sudden_drop", current_power=-24.0,
            avg_power=-20.0, trend=-2.0, **base,
        ) is True
        assert notifier.send_anomaly_alert(
            anomaly_type="sudden_drop", current_power=-25.0,
            avg_power=-20.0, trend=-3.0, **base,
        ) is False
        assert notifier.send_anomaly_alert(
            anomaly_type="total_failure", current_power=-35.0,
            avg_power=-20.0, trend=-5.0, **base,
        ) is True
        assert notifier.send_anomaly_alert(
            anomaly_type="recovery", current_power=-19.0,
            avg_power=-20.0, trend=1.0, **base,
        ) is True
    assert post.call_count == 3

    payloads = [c.kwargs["json"]["text"] for c in post.call_args_list]
    assert "<b>FALLA ZONA" in payloads[0] or "<b>ANOMALÍA DETECTADA" in payloads[0]
    assert "RECUPERADO" in payloads[2]


# === AI predictions ===

def test_prediction_dedup_by_band(tmp_path):
    notifier = make_notifier(tmp_path)
    base = dict(olt_name="OLT-1", pon_port="GPON0/1:1", onu_index="1", onu_serial="VSOL1")
    with mock_post() as post:
        assert notifier.send_ai_prediction(
            confidence=0.8, hours_to_failure=20.0, current_power=-24.0, **base,
        ) is True                    # banda 3
        assert notifier.send_ai_prediction(
            confidence=0.8, hours_to_failure=19.0, current_power=-25.0, **base,
        ) is False                   # sigue en banda 3
        assert notifier.send_ai_prediction(
            confidence=0.9, hours_to_failure=13.0, current_power=-26.0, **base,
        ) is True                    # baja a banda 2
    assert post.call_count == 2


# === Other alerts (HTML + escape) ===

def test_llm_alert_escapes_and_formats(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        assert notifier.send_llm_alert(
            title="Falla <b>bold</b>", message="Corte & <falla>", severity="critical",
        ) is True
    payload = post.call_args.kwargs["json"]
    assert payload["parse_mode"] == "HTML"
    assert "<b>Falla &lt;b&gt;bold&lt;/b&gt;</b>" in payload["text"]
    assert "Corte &amp; &lt;falla&gt;" in payload["text"]


# === Alertas del agente IA (send_llm_alert + dedup por estado) ===

def test_llm_alert_dedup_state_change(tmp_path):
    """host_down → repetido → host_up → host_down: solo cambia cuando el
    estado del equipo cambia (y una nueva caída tras recuperación re-alerta)."""
    notifier = make_notifier(tmp_path)
    key = "agent:host:SWITCH-BACKBONE"
    with mock_post() as post:
        # caída 1
        assert notifier.send_llm_alert(
            title="host_down — SWITCH-BACKBONE", severity="critical",
            message="detalle", dedup_key=key, state_fingerprint={"status": "down"},
        ) is True
        # misma condición (escaneo siguiente / reinicio): NO se re-envía
        assert notifier.send_llm_alert(
            title="host_down — SWITCH-BACKBONE", severity="critical",
            message="detalle", dedup_key=key, state_fingerprint={"status": "down"},
        ) is False
        # recuperación
        assert notifier.send_llm_alert(
            title="host_up — SWITCH-BACKBONE", severity="info",
            message="rec", dedup_key=key, state_fingerprint={"status": "up"},
        ) is True
        # nueva caída (episodio distinto): vuelve a alertar
        assert notifier.send_llm_alert(
            title="host_down — SWITCH-BACKBONE", severity="critical",
            message="detalle", dedup_key=key, state_fingerprint={"status": "down"},
        ) is True
    assert post.call_count == 3
    texts = [c.kwargs["json"]["text"] for c in post.call_args_list]
    assert texts[0].startswith("🔴 <b>host_down — SWITCH-BACKBONE</b>")
    assert texts[1].startswith("ℹ️ <b>host_up — SWITCH-BACKBONE</b>")
    assert "🤖 Generado por el agente IA" in texts[2]


def test_llm_alert_dedup_por_severidad(tmp_path):
    """Patrones (ej. cascade): dedup por severidad — re-alerta solo si la
    condición de severidad cambia."""
    notifier = make_notifier(tmp_path)
    key = "agent:cascade:SWITCH-BACKBONE"
    with mock_post() as post:
        assert notifier.send_llm_alert(
            title="cascade — SWITCH-BACKBONE", severity="critical",
            dedup_key=key, state_fingerprint={"severity": "critical"},
        ) is True
        assert notifier.send_llm_alert(
            title="cascade — SWITCH-BACKBONE", severity="critical",
            dedup_key=key, state_fingerprint={"severity": "critical"},
        ) is False
        assert notifier.send_llm_alert(
            title="cascade — SWITCH-BACKBONE", severity="warning",
            dedup_key=key, state_fingerprint={"severity": "warning"},
        ) is True
    assert post.call_count == 2


def test_llm_alert_sin_dedup_usa_cooldown(tmp_path):
    """Sin dedup_key/state_fingerprint mantiene el cooldown en memoria (compat)."""
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        assert notifier.send_llm_alert(title="gen", severity="info") is True
        notifier._last_sent["llm_gen_info"] = time.time()  # envío reciente
        assert notifier.send_llm_alert(title="gen", severity="info") is False
    assert post.call_count == 1


def test_daily_summary_html(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        assert notifier.send_daily_summary({
            "olt_count": 5, "onu_count": 100, "client_count": 250,
            "anomaly_count": 3, "prediction_count": 1,
            "by_type": {"sudden_drop": 2}, "zones_affected": ["Zona <A>"],
        }) is True
    payload = post.call_args.kwargs["json"]
    assert payload["parse_mode"] == "HTML"
    assert "<b>RESUMEN DIARIO" in payload["text"]
    assert "Zona &lt;A&gt;" in payload["text"]


# === Alertas agregadas por NAP / OLT (fault_detector) ===

def test_area_alert_nap_down_dedup(tmp_path):
    notifier = make_notifier(tmp_path)
    kwargs = dict(scope="nap", olt="OLT-1", label="NAP-12",
                  down=5, total=5, names=["A", "B", "C"])
    with mock_post() as post:
        assert notifier.send_area_alert(kind="down", **kwargs) is True
        assert notifier.send_area_alert(kind="down", **kwargs) is False
    assert post.call_count == 1
    text = post.call_args.kwargs["json"]["text"]
    assert "<b>FALLA NAP NAP-12</b>" in text
    assert "<b>OLT:</b> OLT-1" in text
    assert "5 / 5" in text
    assert "- A" in text and "- B" in text


def test_area_alert_broadcast_olt(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        assert notifier.send_area_alert(
            scope="olt", olt="OLT-2", label="OLT-2",
            down=8, total=10, kind="down",
        ) is True
    text = post.call_args.kwargs["json"]["text"]
    assert "<b>BROADCAST - OLT OLT-2</b>" in text


def test_area_alert_recovery_uses_duration(tmp_path):
    notifier = make_notifier(tmp_path)
    notifier._state_store.record("fault:nap:OLT-3:NAP-5",
                                 {"status": "down"},
                                 down_since=time.time() - 1800)
    with mock_post() as post:
        assert notifier.send_area_alert(
            scope="nap", olt="OLT-3", label="NAP-5",
            kind="recovery", duration_minutes=0,
        ) is True
    text = post.call_args.kwargs["json"]["text"]
    assert "<b>NAP RECUPERADO - NAP-5</b>" in text
    assert "30 min" in text


def test_area_alert_escapes_labels(tmp_path):
    notifier = make_notifier(tmp_path)
    with mock_post() as post:
        notifier.send_area_alert(
            scope="nap", olt="OLT <1>", label="NAP & 12",
            down=3, total=4, kind="down",
        )
    text = post.call_args.kwargs["json"]["text"]
    assert "OLT &lt;1&gt;" in text
    assert "NAP &amp; 12" in text


def test_area_alert_affected_usa_direccion(tmp_path):
    notifier = make_notifier(tmp_path)
    affected = [
        {"nombre": "Cliente Uno", "direccion": "Calle 1"},
        {"nombre": "Cliente Dos", "direccion": ""},
    ]
    with mock_post() as post:
        assert notifier.send_area_alert(
            scope="nap", olt="OLT-1", label="NAP-12",
            down=2, total=2, kind="down",
            affected=affected, scope_detail="OLT OLT-1 · PON GPON0/2",
        ) is True
    text = post.call_args.kwargs["json"]["text"]
    assert "<b>Alcance:</b> OLT OLT-1 · PON GPON0/2" in text
    assert "- Cliente Uno — Calle 1" in text
    assert "- Cliente Dos" in text
    assert "- A" not in text


def test_area_alert_affected_limita_ocho(tmp_path):
    notifier = make_notifier(tmp_path)
    affected = [{"nombre": f"Cliente {i}", "direccion": "Calle"}
                for i in range(12)]
    with mock_post() as post:
        notifier.send_area_alert(
            scope="nap", olt="OLT-1", label="NAP-B",
            down=12, total=12, kind="down", affected=affected,
        )
    text = post.call_args.kwargs["json"]["text"]
    assert "- Cliente 7" in text
    assert "- Cliente 8" not in text
    assert "4 más" in text