# src/notifier_ai.py
import html
import time
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass
import requests

from .alert_state import LastStateStore

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    cooldown_minutes: int = 15
    state_file: str = ""


class TelegramNotifierAI:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self._last_sent: Dict[str, float] = {}
        self._state_store = LastStateStore(config.state_file or None)

    @staticmethod
    def _esc(value) -> str:
        """Escapa texto dinámico para parse_mode HTML de Telegram."""
        return html.escape(str(value), quote=False)

    def _check_cooldown(self, key: str) -> bool:
        if key in self._last_sent:
            elapsed = time.time() - self._last_sent[key]
            if elapsed < self.config.cooldown_minutes * 60:
                return False
        return True

    def _send_message(
        self,
        message: str,
        key: str = "default",
        state_fingerprint: Optional[Dict] = None,
        state_extra: Optional[Dict] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        # Dedup por estado: si la condición no cambió, no se re-envía.
        if state_fingerprint is not None:
            if not self._state_store.should_send(key, state_fingerprint):
                logger.debug(f"State unchanged for {key}, skipping duplicate alert")
                return False
        elif not self._check_cooldown(key):
            logger.debug(f"Cooldown active for {key}")
            return False

        try:
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.chat_id,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }

            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()

            self._last_sent[key] = time.time()
            if state_fingerprint is not None:
                self._state_store.record(key, state_fingerprint, **(state_extra or {}))
            logger.info(f"Telegram message sent: {key}")
            return True

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def send_anomaly_alert(
        self,
        olt_name: str,
        pon_port: str,
        onu_index: str,
        onu_serial: str,
        anomaly_type: str,
        current_power: float,
        avg_power: float,
        trend: float,
        predicted_time: Optional[float] = None,
        message: str = "",
        # Enriched fields
        client_name: str = "",
        client_address: str = "",
        zone: str = "",
        node: str = "",
        affected_count: int = 0,
        affected_names: Optional[List[str]] = None,
    ) -> bool:

        emoji_map = {
            "total_failure": "🔴",
            "sudden_drop": "🟠",
            "gradual_degradation": "🟡",
            "recovery": "🟢",
        }
        emoji = emoji_map.get(anomaly_type, "⚪")
        is_recovery = anomaly_type == "recovery"
        key = f"anomaly:{onu_serial or onu_index}"
        # Solo cambia la alerta si cambia la condición del equipo.
        fingerprint = {"anomaly_type": anomaly_type}

        separator = "━━━━━━━━━━━━━━━━"
        lines = []

        # Header with zone if available
        if zone:
            if is_recovery:
                lines.append(f"🟢 <b>RECUPERACIÓN ZONA {self._esc(zone)}</b>")
            else:
                lines.append(f"{emoji} <b>FALLA ZONA {self._esc(zone)}</b>")
            lines.append(separator)
        else:
            if is_recovery:
                lines.append(f"✅ <b>EQUIPO RECUPERADO</b>")
            else:
                lines.append(
                    f"{emoji} <b>ANOMALÍA DETECTADA - {self._esc(anomaly_type.upper())}</b>"
                )
            lines.append(separator)

        # Client info
        if client_name:
            lines.append(f"<b>Cliente:</b> {self._esc(client_name)}")
        if client_address:
            lines.append(f"<b>Dirección:</b> {self._esc(client_address)}")
        if node:
            lines.append(f"<b>Nodo:</b> {self._esc(node)}")
        if client_name or client_address or node:
            lines.append("")

        # Equipment info
        lines.append(f"<b>OLT:</b> {self._esc(olt_name)} | <b>PON:</b> {self._esc(pon_port)}")
        lines.append(f"<b>ONU:</b> <code>{self._esc(onu_serial)}</code>")
        lines.append("")

        # Power metrics
        lines.append(f"<b>Potencia actual:</b> <code>{current_power:.1f} dBm</code>")
        lines.append(f"<b>Potencia promedio:</b> <code>{avg_power:.1f} dBm</code>")
        lines.append(f"<b>Tendencia:</b> <code>{trend:.2f} dB/h</code>")

        if predicted_time:
            lines.append(f"<b>Falla estimada en:</b> ~{predicted_time:.0f} horas")

        # Affected clients in same PON
        if affected_count > 0:
            lines.append("")
            lines.append(f"<b>Clientes afectados en PON:</b> {affected_count}")
            if affected_names:
                for name in affected_names[:10]:
                    lines.append(f"  - {self._esc(name)}")
                if affected_count > 10:
                    lines.append(f"  ... y {affected_count - 10} más")

        if message:
            lines.append(f"\n{self._esc(message)}")

        lines.append(f"\n⏰ <code>{time.strftime('%H:%M')}</code>")

        return self._send_message(
            "\n".join(lines),
            key=key,
            state_fingerprint=fingerprint,
        )

    def send_ai_prediction(
        self,
        olt_name: str,
        pon_port: str,
        onu_index: str,
        onu_serial: str,
        confidence: float,
        hours_to_failure: float,
        current_power: float,
        client_name: str = "",
        client_address: str = "",
        zone: str = "",
    ) -> bool:

        key = f"prediction:{onu_serial or onu_index}"
        # Bandas de 6h: solo re-alerta cuando la falla estimada empeora de forma
        # significativa (ej. ~18h -> ~12h -> ~6h), evitando spam por micro-cambios.
        band = int(hours_to_failure // 6)
        fingerprint = {"hours_band": band}

        separator = "━━━━━━━━━━━━━━━━"
        lines = []
        if zone:
            lines.append(f"🤖 <b>PREDICCIÓN IA - Zona {self._esc(zone)}</b>")
        else:
            lines.append("🤖 <b>PREDICCIÓN IA</b>")
        lines.append(separator)

        if client_name:
            lines.append(f"<b>Cliente:</b> {self._esc(client_name)}")
        if client_address:
            lines.append(f"<b>Dirección:</b> {self._esc(client_address)}")
        if client_name or client_address:
            lines.append("")

        lines.append(f"<b>ONU:</b> <code>{self._esc(onu_serial)}</code> en {self._esc(olt_name)}:{self._esc(pon_port)}")
        lines.append(f"<b>Potencia actual:</b> <code>{current_power:.1f} dBm</code>")
        lines.append(f"<b>Confianza:</b> <code>{confidence * 100:.0f}%</code>")
        lines.append(f"<b>Tiempo estimado hasta falla:</b> <code>~{hours_to_failure:.0f} horas</code>")
        lines.append("")
        lines.append("Acción recomendada: Revisar fibra óptica")
        lines.append(f"\n⏰ <code>{time.strftime('%H:%M')}</code>")

        return self._send_message(
            "\n".join(lines),
            key=key,
            state_fingerprint=fingerprint,
        )

    def send_ping_alert(
        self,
        host_name: str,
        host_ip: str,
        host_type: str,
        status: str,
        downtime_seconds: float,
        avg_latency: float = 0,
    ) -> bool:
        key = f"ping:{host_name}"
        separator = "━━━━━━━━━━━━━━━━"

        if status == "down":
            fingerprint = {"status": "down"}
            if not self._state_store.should_send(key, fingerprint):
                logger.debug(f"Host {host_name} already reported down, skipping")
                return False

            minutes = max(1, int(downtime_seconds // 60))
            lines = [
                "🔴 <b>HOST CAÍDO</b>",
                separator,
                f"<b>Nombre:</b> {self._esc(host_name)}",
                f"<b>IP:</b> <code>{self._esc(host_ip)}</code>",
                f"<b>Tipo:</b> {self._esc(host_type)}",
                "<b>Estado:</b> ⛔ SIN RESPUESTA",
                f"<b>Tiempo caído:</b> {minutes} min",
                separator,
                "⚠️ <i>Verificar conectividad</i>",
                f"⏰ <code>{time.strftime('%H:%M')}</code>",
            ]
            message = "\n".join(lines)
            # down_since se guarda para poder reportar el tiempo caído total el día
            # de la recuperación, incluso si el agente reinició en el medio.
            return self._send_message(
                message,
                key=key,
                state_fingerprint=fingerprint,
                state_extra={"down_since": time.time()},
            )

        fingerprint = {"status": "up"}
        if not self._state_store.should_send(key, fingerprint):
            logger.debug(f"Host {host_name} already reported up, skipping")
            return False

        entry = self._state_store.get(key)
        down_since = (entry or {}).get("down_since")
        if downtime_seconds <= 0 and down_since:
            downtime_seconds = time.time() - down_since

        minutes = max(1, int(downtime_seconds // 60))
        lines = [
            "🟢 <b>HOST RECUPERADO</b>",
            separator,
            f"<b>Nombre:</b> {self._esc(host_name)}",
            f"<b>IP:</b> <code>{self._esc(host_ip)}</code>",
            f"<b>Tipo:</b> {self._esc(host_type)}",
            "<b>Estado:</b> ✅ RESPONDIENDO",
            f"<b>Latencia:</b> <code>{avg_latency:.1f} ms</code>",
            f"<b>Tiempo caído total:</b> {minutes} min",
            f"⏰ <code>{time.strftime('%H:%M')}</code>",
        ]
        message = "\n".join(lines)
        return self._send_message(message, key=key, state_fingerprint=fingerprint)

    def send_daily_summary(self, stats: Dict) -> bool:

        lines = [
            f"📊 <b>RESUMEN DIARIO - {time.strftime('%d/%m/%Y')}</b>",
            "",
            f"<b>OLTs monitoreadas:</b> {self._esc(stats.get('olt_count', 0))}",
            f"<b>ONUs activas:</b> {self._esc(stats.get('onu_count', 0))}",
            f"<b>Clientes en base:</b> {self._esc(stats.get('client_count', 0))}",
            f"<b>Anomalías detectadas:</b> {self._esc(stats.get('anomaly_count', 0))}",
            f"<b>Predicciones IA:</b> {self._esc(stats.get('prediction_count', 0))}",
            "",
            "Por tipo:",
        ]

        by_type = stats.get("by_type", {})
        for atype, count in by_type.items():
            lines.append(f"  - {self._esc(atype)}: {self._esc(count)}")

        if stats.get("zones_affected"):
            lines.append("")
            lines.append("Zonas afectadas:")
            for z in stats["zones_affected"][:5]:
                lines.append(f"  - {self._esc(z)}")

        lines.append(f"\n⏰ <code>{time.strftime('%H:%M')}</code>")

        return self._send_message("\n".join(lines), key="daily_summary")

    def send_system_status(self, status: str, details: str = "") -> bool:

        if status == "healthy":
            title = "✅ <b>ESTADO DEL SISTEMA</b>"
        else:
            title = "⚠️ <b>ADVERTENCIA - ESTADO DEL SISTEMA</b>"

        lines = [
            title,
            "",
            f"<b>Estado:</b> {self._esc(status)}",
        ]

        if details:
            lines.append(f"<b>Detalles:</b> {self._esc(details)}")

        lines.append(f"\n⏰ <code>{time.strftime('%H:%M')}</code>")

        return self._send_message("\n".join(lines), key="system_status")

    def send_llm_alert(self, title: str, severity: str = "warning",
                       message: str = "") -> bool:
        """Generic alert produced by the LLM agent."""
        emoji = {
            "critical": "🔴",
            "warning": "🟠",
            "info": "ℹ️",
        }.get(severity, "🤖")

        lines = [f"{emoji} <b>{self._esc(title)}</b>", ""]
        if message:
            lines.append(self._esc(message))
        lines.append(f"\n⏰ <code>{time.strftime('%H:%M')}</code>")
        lines.append("🤖 Generado por el agente IA")

        return self._send_message("\n".join(lines), key=f"llm_{title[:40]}_{severity}")

    def send_action_approval(self, action_id: str, message: str,
                             timeout_seconds: int = 300) -> Optional[int]:
        """Envía solicitud de aprobación con botones inline (Confirmar/Rechazar).

        Devuelve el message_id o None si falla.
        """
        try:
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.chat_id,
                "text": message,
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [[
                        {
                            "text": "✅ Confirmar",
                            "callback_data": f"approve:{action_id}",
                        },
                        {
                            "text": "❌ Rechazar",
                            "callback_data": f"deny:{action_id}",
                        },
                    ]]
                },
            }
            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok") and data.get("result"):
                self._last_sent[f"approval_{action_id}"] = time.time()
                return data["result"].get("message_id")
            return None
        except Exception as e:
            logger.error(f"Failed to send approval request: {e}")
            return None