# src/telegram_bot.py
"""
Bot Telegram interactivo del Agente WISP.

Comandos disponibles:
  /status      Estado del agente y event bus
  /alertas     Últimas alertas generadas
  /reporte     Resumen/reporte del período
  /topologia   Estado de nodos (up/down)
  /accion      Ejecutar una acción registrada (requiere aprobación si aplica)
  /silenciar   Silenciar alertas de una zona por N minutos
  /ayuda       Lista de comandos

Además procesa los callbacks inline (approve/deny) para las solicitudes
de aprobación de acciones de nivel 2/3.
"""
import os
import json
import time
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

CALLBACK_PREFIX_APPROVE = "approve:"
CALLBACK_PREFIX_DENY = "deny:"

DEFAULT_HELP = """🤖 *Agente WISP - Comandos*

<code>/status</code> - Estado del agente y event bus
<code>/alertas</code> - Últimas alertas generadas
<code>/reporte</code> - Resumen del período
<code>/topologia</code> - Estado de nodos
<code>/accion &lt;nombre&gt; [device]</code> - Ejecutar acción
<code>/silenciar &lt;zona&gt; &lt;minutos&gt;</code> - Silenciar alertas de zona
<code>/ayuda</code> - Esta ayuda

Estando ligado a un ApprovalService, también responderás por botones inline a las solicitudes de aprobación.
"""


class TelegramBot:
    def __init__(self, bot_token: str, chat_id: str,
                 approval_service: Optional[Any] = None,
                 actions_registry: Optional[Any] = None,
                 agent: Optional[Any] = None,
                 policy: Optional[Any] = None,
                 notifier: Optional[Any] = None,
                 watchdog: Optional[Callable[[], Dict]] = None,
                 poll_timeout: int = 30):
        self.token = bot_token
        self.chat_id = str(chat_id)
        self.approval = approval_service
        self.actions = actions_registry
        self.agent = agent
        self.policy = policy
        self.notifier = notifier
        self.watchdog = watchdog or self._default_watchdog

        self._offset: Optional[int] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_message_by_command: Dict[str, float] = {}
        self._command_cooldown: float = 5.0

    # ── API helpers ──────────────────────────────────────────────────────────

    def _call(self, method: str, **params) -> Optional[Dict]:
        try:
            resp = requests.post(API.format(token=self.token, method=method),
                                 json=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data if data.get("ok") else None
        except Exception as e:
            logger.error(f"Telegram API {method} failed: {e}")
            return None

    def send_message(self, text: str, reply_markup: Optional[Dict] = None,
                     chat_id: Optional[str] = None) -> Optional[Dict]:
        params = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        }
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self._call("sendMessage", **params)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery",
                   callback_query_id=callback_id, text=text)

    # ── Comandos ─────────────────────────────────────────────────────────────

    def _default_watchdog(self) -> Dict:
        """Estado básico del agente vía archivos de health."""
        import subprocess
        status = {"running": False, "uptime": 0}
        try:
            result = subprocess.run(
                ["bash", "scripts/manage-services.sh", "status", "wisp-agent"],
                capture_output=True, text=True, timeout=15,
            )
            status["output"] = result.stdout.strip()
            if "RUNNING" in result.stdout:
                status["running"] = True
        except Exception as e:
            status["error"] = str(e)

        health_file = os.path.join(
            os.getcwd(), "logs", "wisp_agent_health.json")
        if os.path.exists(health_file):
            try:
                with open(health_file, "r") as f:
                    status.update(json.load(f))
            except Exception:
                pass
        return status

    def _cmd_status(self, args: List[str]) -> str:
        lines = ["📊 *Estado del Agente*"]
        status = self.watchdog() if self.watchdog else {}
        running = status.get("running", False)
        lines.append(f"🟢 Agente: {'ACTIVO' if running else 'INACTIVO'}")
        lines.append(f"  Estado: {status.get('output', 'n/a')}")
        if status.get("in_degraded_mode"):
            lines.append("⚠️ Modo degradado (LLM no disponible)")

        bus_status = "no disponible"
        if self.agent and getattr(self.agent, "bus", None):
            try:
                stats = self.agent.bus.stats()
                bus_status = f"stream={stats.get('stream')} msgs={stats.get('total_messages', '?')}"
            except Exception:
                bus_status = "sin conexión"
        lines.append(f"🔌 Event bus: {bus_status}")

        if self.agent:
            health = self.agent.get_health()
            lines.append(f"📈 Procesados: {health.get('processed_events', 0)}")
            lines.append(f"🤖 Fallos LLM: {health.get('llm_failures', 0)}")
        return "\n".join(lines)

    def _cmd_alertas(self, args: List[str]) -> str:
        alertas_file = os.path.join(os.getcwd(), "logs", "alertas_telegram.json")
        if not os.path.exists(alertas_file):
            return "📭 No hay alertas registradas todavía."
        try:
            with open(alertas_file, "r") as f:
                alertas = json.load(f)
        except Exception as e:
            return f"⚠️ Error leyendo alertas: {e}"
        if not alertas:
            return "📭 No hay alertas registradas todavía."
        lines = ["🚨 *Últimas alertas*"]
        for a in list(alertas)[-10:]:
            sev = a.get("severity", "info")
            icon = {"critical": "🔴", "warning": "🟠", "info": "ℹ️"}.get(sev, "•")
            ts = time.strftime("%d/%m %H:%M", time.localtime(a.get("ts", 0)))
            lines.append(f"{icon} [{ts}] {a.get('title', a.get('message', ''))}")
        return "\n".join(lines)

    def _cmd_reporte(self, args: List[str]) -> str:
        resumen_file = os.path.join(os.getcwd(), "logs", "resumen_diario.json")
        if not os.path.exists(resumen_file):
            return "📊 No hay reporte disponible todavía (se genera a las 06:00)."
        try:
            with open(resumen_file, "r") as f:
                resumen = json.load(f)
        except Exception as e:
            return f"⚠️ Error leyendo reporte: {e}"
        lines = ["📊 *Resumen del día*"]
        lines.append(f"📅 Fecha: {resumen.get('fecha', 'n/a')}")
        lines.append(f"🔴 Eventos críticos: {resumen.get('critical', 0)}")
        lines.append(f"🟠 Alertas warning: {resumen.get('warning', 0)}")
        lines.append(f"📈 Procesados: {resumen.get('processed', 0)}")
        lines.append(f"⚙️ Acciones: {resumen.get('acciones', 0)}")
        return "\n".join(lines)

    def _cmd_topologia(self, args: List[str]) -> str:
        nodes_file = os.path.join(os.getcwd(), "data", "topologia.json")
        if not os.path.exists(nodes_file):
            nodes_file = os.path.join(os.getcwd(), "models", "topologia.json")
        if not os.path.exists(nodes_file):
            return "🌐 No hay datos de topología disponibles."
        try:
            with open(nodes_file, "r") as f:
                data = json.load(f)
        except Exception as e:
            return f"⚠️ Error leyendo topología: {e}"

        nodes = data.get("nodes", data if isinstance(data, list) else [])
        if not nodes:
            return "🌐 Topología vacía."
        total = len(nodes)
        up = 0
        down = 0
        for n in nodes:
            state = n.get("status", n.get("state", ""))
            if state in ("up", "online", "running", "ok", "active"):
                up += 1
            elif state in ("down", "offline", "error"):
                down += 1
        lines = [f"🌐 *Topología* ({total} nodos)"]
        lines.append(f"🟢 Up: {up}")
        lines.append(f"🔴 Down: {down}")
        if down:
            lines.append("\n*Nodos caídos:*")
            for n in nodes:
                if n.get("status", n.get("state", "")) in ("down", "offline", "error"):
                    lines.append(f"  - {n.get('name', n.get('address', '?'))}")
        return "\n".join(lines)

    def _cmd_accion(self, args: List[str]) -> str:
        if not self.actions:
            return "⚠️ Registro de acciones no disponible."
        if not args:
            available = ", ".join(a["name"] for a in self.actions.list_actions())
            return f"Uso: <code>/accion &lt;nombre&gt; [device]</code>\nDisponibles: {available}"

        name = args[0]
        device = args[1] if len(args) > 1 else ""
        try:
            result = self.actions.execute(name, device_id=device)
            return self._format_action_result(result)
        except Exception as e:
            return f"❌ Error ejecutando acción: {e}"

    def _format_action_result(self, result: Any) -> str:
        if isinstance(result, dict):
            if result.get("ok"):
                return f"✅ {result.get('message', 'Acción ejecutada')}"
            if result.get("status") == "approval_required":
                return (
                    f"🔒 Se requiere aprobación (nivel {result.get('level')}).\n"
                    f"ID: <code>{result.get('approval_id')}</code> — "
                    "revisa los botones en el chat."
                )
            return f"❌ {result.get('error', result)}"
        return f"✅ {result}"

    def _cmd_silenciar(self, args: List[str]) -> str:
        if not self.policy:
            return "⚠️ Política de alertas no disponible."
        if len(args) < 2:
            return "Uso: <code>/silenciar &lt;zona&gt; &lt;minutos&gt;</code>"
        zona = args[0]
        try:
            minutos = int(args[1])
        except ValueError:
            return "⚠️ Los minutos deben ser un número."
        self.policy.silence_zone(zona, minutos)
        return f"🔕 Alertas de *{zona}* silenciadas por {minutos} min."

    def _cmd_ayuda(self, args: List[str]) -> str:
        return DEFAULT_HELP

    COMMANDS = {
        "/status": _cmd_status,
        "/alertas": _cmd_alertas,
        "/reporte": _cmd_reporte,
        "/topologia": _cmd_topologia,
        "/accion": _cmd_accion,
        "/silenciar": _cmd_silenciar,
        "/ayuda": _cmd_ayuda,
        "/help": _cmd_ayuda,
    }

    def handle_command(self, text: str, chat_id: str) -> Optional[str]:
        if not text.startswith("/"):
            return None
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        handler = self.COMMANDS.get(cmd)
        if not handler:
            return f"🤔 Comando desconocido: <code>{cmd}</code>\n\n{DEFAULT_HELP}"

        now = time.time()
        if cmd in self._last_message_by_command:
            if now - self._last_message_by_command[cmd] < self._command_cooldown:
                return None
        self._last_message_by_command[cmd] = now
        return handler(self, args)

    # ── Polling ──────────────────────────────────────────────────────────────

    def _process_update(self, update: Dict) -> None:
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if text:
                reply = self.handle_command(text, str(chat_id))
                if reply:
                    self.send_message(reply, chat_id=str(chat_id))
            return

        if "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb.get("id")
            data = cb.get("data", "")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            user = cb.get("from", {}).get("username") or "telegram"

            if data.startswith(CALLBACK_PREFIX_APPROVE):
                action_id = data[len(CALLBACK_PREFIX_APPROVE):]
                ok = self._resolve_approval(action_id, True, user)
                self.answer_callback(cb_id,
                                     "✅ Aprobado" if ok else "❌ No se pudo aprobar")
                if chat_id:
                    self.send_message(f"🔓 Aprobaste la acción `{action_id}`",
                                      chat_id=str(chat_id))
            elif data.startswith(CALLBACK_PREFIX_DENY):
                action_id = data[len(CALLBACK_PREFIX_DENY):]
                ok = self._resolve_approval(action_id, False, user)
                self.answer_callback(cb_id,
                                     "❌ Rechazado" if ok else "⚠️ Ya no está pendiente")
                if chat_id:
                    self.send_message(f"🔒 Rechazaste la acción `{action_id}`",
                                      chat_id=str(chat_id))

    def _resolve_approval(self, action_id: str, approved: bool, approver: str) -> bool:
        if not self.approval:
            return False
        return self.approval.resolve(action_id, approved, approver=approver)

    def _poll_once(self) -> None:
        params: Dict[str, Any] = {"timeout": self.poll_timeout if hasattr(self, "poll_timeout") else 30}
        if self._offset:
            params["offset"] = self._offset
        data = self._call("getUpdates", **params)
        if not data or not data.get("result"):
            return
        for update in data["result"]:
            self._offset = update["update_id"] + 1
            try:
                self._process_update(update)
            except Exception as e:
                logger.error(f"Update processing failed: {e}")

    def run_sync(self) -> None:
        self._running = True
        logger.info("TelegramBot polling started")
        while self._running:
            try:
                self._poll_once()
                time.sleep(1)
            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)
                time.sleep(5)
        logger.info("TelegramBot polling stopped")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_sync, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def get_me(self) -> Optional[Dict]:
        data = self._call("getMe")
        return data.get("result") if data else None


def create_bot(config_path: str = "config/config_ia.yaml") -> TelegramBot:
    """Construye el bot con los singletons configurados."""
    from .config_ia import load_config_ia
    from .actions import ApprovalService
    from .ai.agent_loop import create_agent_sync

    config = load_config_ia(config_path)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id)

    agent = None
    try:
        agent = create_agent_sync(config_path)
    except Exception as e:
        logger.warning(f"Agent not available for bot: {e}")

    bot = TelegramBot(
        bot_token=bot_token,
        chat_id=chat_id,
        approval_service=agent.actions_registry.approval_service if agent else None,
        actions_registry=agent.actions_registry if agent else None,
        agent=agent,
        policy=agent.policy if agent else None,
        notifier=agent.notifier if agent else None,
    )
    return bot


def main():
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no configurados")
        sys.exit(1)

    bot = create_bot(config_path)

    import signal
    shutdown = threading.Event()

    def signal_handler(signum, frame):
        logger.info(f"Signal {signum}, stopping bot...")
        shutdown.set()
        bot.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    bot.run_sync()


if __name__ == "__main__":
    main()