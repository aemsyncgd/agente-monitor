# src/notifier_ai.py
import time
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass
import requests

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    cooldown_minutes: int = 15


class TelegramNotifierAI:
    def __init__(self, config: TelegramConfig):
        self.config = config
        self._last_sent: Dict[str, float] = {}

    def _check_cooldown(self, key: str) -> bool:
        if key in self._last_sent:
            elapsed = time.time() - self._last_sent[key]
            if elapsed < self.config.cooldown_minutes * 60:
                return False
        return True

    def _send_message(self, message: str, key: str = "default") -> bool:
        if not self._check_cooldown(key):
            logger.debug(f"Cooldown active for {key}")
            return False

        try:
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
            payload = {
                "chat_id": self.config.chat_id,
                "text": message,
                "disable_web_page_preview": True,
            }

            resp = requests.post(url, json=payload, timeout=30)
            resp.raise_for_status()

            self._last_sent[key] = time.time()
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

        lines = []

        # Header with zone if available
        if zone:
            lines.append(f"{emoji} FALLA ZONA {zone}")
            lines.append("━" * 28)
        else:
            lines.append(f"{emoji} ANOMALIA DETECTADA - {anomaly_type.upper()}")
            lines.append("")

        # Client info
        if client_name:
            lines.append(f"Cliente: {client_name}")
        if client_address:
            lines.append(f"Direccion: {client_address}")
        if node:
            lines.append(f"Nodo: {node}")
        if client_name or client_address or node:
            lines.append("")

        # Equipment info
        lines.append(f"OLT: {olt_name} | PON: {pon_port}")
        lines.append(f"ONU: {onu_serial}")
        lines.append("")

        # Power metrics
        lines.append(f"Potencia actual: {current_power:.1f} dBm")
        lines.append(f"Potencia promedio: {avg_power:.1f} dBm")
        lines.append(f"Tendencia: {trend:.2f} dB/h")

        if predicted_time:
            lines.append(f"Falla estimada en: ~{predicted_time:.0f} horas")

        # Affected clients in same PON
        if affected_count > 0:
            lines.append("")
            lines.append(f"Clientes afectados en PON: {affected_count}")
            if affected_names:
                for name in affected_names[:10]:
                    lines.append(f"  - {name}")
                if affected_count > 10:
                    lines.append(f"  ... y {affected_count - 10} mas")

        if message:
            lines.append(f"\n{message}")

        lines.append(f"\nHora: {time.strftime('%H:%M')}")

        return self._send_message(
            "\n".join(lines),
            key=f"anomaly_{onu_index}",
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

        lines = []
        if zone:
            lines.append(f"🤖 PREDICCION IA - Zona {zone}")
        else:
            lines.append("🤖 PREDICCION IA")
        lines.append("")

        if client_name:
            lines.append(f"Cliente: {client_name}")
        if client_address:
            lines.append(f"Direccion: {client_address}")
        if client_name or client_address:
            lines.append("")

        lines.append(f"ONU: {onu_serial} en {olt_name}:{pon_port}")
        lines.append(f"Potencia actual: {current_power:.1f} dBm")
        lines.append(f"Confianza: {confidence * 100:.0f}%")
        lines.append(f"Tiempo estimado hasta falla: ~{hours_to_failure:.0f} horas")
        lines.append("")
        lines.append("Accion recomendada: Revisar fibra optica")
        lines.append(f"\nHora: {time.strftime('%H:%M')}")

        return self._send_message("\n".join(lines), key=f"prediction_{onu_index}")

    def send_ping_alert(
        self,
        host_name: str,
        host_ip: str,
        host_type: str,
        status: str,
        downtime_seconds: float,
        avg_latency: float = 0,
    ) -> bool:
        if status == "down":
            minutes = int(downtime_seconds // 60)
            lines = [
                "HOST CAIDO",
                "",
                f"Nombre: {host_name}",
                f"IP: {host_ip}",
                f"Tipo: {host_type}",
                "Estado: SIN RESPUESTA",
                f"Tiempo caido: {minutes} min",
                "",
                "Verificar conectividad",
            ]
        else:
            lines = [
                "HOST RECUPERADO",
                "",
                f"Nombre: {host_name}",
                f"IP: {host_ip}",
                f"Tipo: {host_type}",
                "Estado: RESPONDIENDO",
                f"Latencia: {avg_latency:.1f} ms",
            ]

        message = "\n".join(lines)
        return self._send_message(message, key=f"ping_{host_name}_{status}")

    def send_daily_summary(self, stats: Dict) -> bool:

        lines = [
            f"RESUMEN DIARIO - {time.strftime('%d/%m/%Y')}",
            "",
            f"OLTs monitoreadas: {stats.get('olt_count', 0)}",
            f"ONUs activas: {stats.get('onu_count', 0)}",
            f"Clientes en base: {stats.get('client_count', 0)}",
            f"Anomalias detectadas: {stats.get('anomaly_count', 0)}",
            f"Predicciones IA: {stats.get('prediction_count', 0)}",
            "",
            "Por tipo:",
        ]

        by_type = stats.get("by_type", {})
        for atype, count in by_type.items():
            lines.append(f"  - {atype}: {count}")

        if stats.get("zones_affected"):
            lines.append("")
            lines.append("Zonas afectadas:")
            for z in stats["zones_affected"][:5]:
                lines.append(f"  - {z}")

        lines.append(f"\nHora: {time.strftime('%H:%M')}")

        return self._send_message("\n".join(lines), key="daily_summary")

    def send_system_status(self, status: str, details: str = "") -> bool:

        emoji = "OK" if status == "healthy" else "ADVERTENCIA"
        lines = [
            f"{emoji} ESTADO DEL SISTEMA",
            "",
            f"Estado: {status}",
        ]

        if details:
            lines.append(f"Detalles: {details}")

        lines.append(f"\nHora: {time.strftime('%H:%M')}")

        return self._send_message("\n".join(lines), key="system_status")

    def send_llm_alert(self, title: str, severity: str = "warning",
                       message: str = "") -> bool:
        """Generic alert produced by the LLM agent."""
        emoji = {
            "critical": "🔴",
            "warning": "🟠",
            "info": "ℹ️",
        }.get(severity, "🤖")

        lines = [f"{emoji} {title}", ""]
        if message:
            lines.append(message)
        lines.append(f"\nHora: {time.strftime('%H:%M')}")
        lines.append("🤖 Generado por el agente IA")

        return self._send_message("\n".join(lines), key=f"llm_{title[:40]}_{severity}")
