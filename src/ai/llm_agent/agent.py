# src/ai/llm_agent/agent.py
"""
LLMAgent — active loop that orchestrates the LLM + tools.

Modes:
  autonomous  each cycle the LLM inspects the network (tools) and decides
              whether to alert/analyze.
  hybrid      deterministic rules (existing agent) keep emitting; this agent
              also cycles and enriches, but the deterministic agent remains
              the primary alert source.
  analyst     on-demand chat + scheduled daily analysis. The loop mostly
              sleeps; chat is served by the API calling chat().
"""
import os
import time
import json
import logging
from typing import Dict, List, Optional, Any, Tuple

from .config import RuntimeConfigStore
from .providers import LLMProvider, LLMResult
from .tools import ToolRegistry, InfluxReader
from .memory import EventStore, AlertState, CostTracker

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6
ALERT_COOLDOWN_SECONDS = 1800  # 30 min
TOOL_OUTPUT_MAX_CHARS = 2000   # truncate tool JSON to avoid context inflation


class LLMAgent:
    def __init__(self, config_store: RuntimeConfigStore,
                 provider: LLMProvider, registry: ToolRegistry,
                 events: EventStore, alert_state: AlertState,
                 cost_tracker: CostTracker):
        self.config_store = config_store
        self.provider = provider
        self.registry = registry
        self.events = events
        self.alert_state = alert_state
        self.cost = cost_tracker

        self.last_cycle: float = 0
        self.last_cycle_result: str = ""
        self.last_cycle_tool_count: int = 0

        registry.alert_sender = self._dispatch_alert

    # ------------------------------------------------------------------ prompt

    def system_prompt(self, config) -> str:
        if config.system_prompt.strip():
            return config.system_prompt.strip()

        return (
            "Eres el Agente IA de monitoreo de red FTTH de VidaNet. "
            "SOLO puedes usar herramientas de SOLO LECTURA para consultar datos. "
            "NO generes contenido creativo, narrativo ni explicaciones extensas.\n\n"
            "FORMATO DE RESPUESTA OBLIGATORIO (máximo 150 palabras):\n"
            "ESTADO: [resumen 1 línea del estado general]\n"
            "HALLAZGOS: [lista concisa de hallazgos concretos]\n"
            "RIESGOS: [riesgos identificados o vacío si no hay]\n"
            "ACCIÓN: [acción sugerida o 'Ninguna']\n\n"
            "REGLAS:\n"
            "1. Consulta get_network_summary y get_recent_events primero.\n"
            "2. Investiga solo si hay evidencia de problemas.\n"
            "3. send_alert SOLO con evidencia real y concreta.\n"
            "4. Severidad: critical=ONU caída/host down/potencia<-32dBm; "
            "warning=degradación; info=novedades.\n"
            "5. Responde en español, máximo 150 palabras.\n"
            "6. NO ejecutes acciones sobre equipos.\n"
            "7. Si no hay nada relevante: ESTADO OK, sin hallazgos."
        )

    def build_context(self) -> str:
        """Compact context injected each cycle to reduce token usage."""
        try:
            stats = self.registry.reader.overall_stats()
        except Exception as e:
            logger.error(f"Context build failed: {e}")
            return "No se pudo obtener el estado de la red."
        return (
            f"Red: {stats['olts']} OLTs | ONUs online {stats['onus_online']}/"
            f"{stats['onus_total']} (offline {stats['onus_offline']}) | "
            f"potencia baja: {stats['low_power_alerts']} | hosts caidos: "
            f"{stats['hosts_down']}."
        )

    # ------------------------------------------------------------------ tools

    def current_tools(self, config) -> Tuple[List[Dict], Dict]:
        """Rebuild tools from the enabled rules each cycle."""
        enabled_ids = []
        try:
            from ..instructions import InstructionManager
            mgr = InstructionManager(config.instructions_path)
            enabled_ids = [i.id for i in mgr.get_enabled()]
        except Exception as e:
            logger.warning(f"Failed to load instructions: {e}")
        return self.registry.build(enabled_ids, config.enabled_tools or None)

    def _dispatch_alert(self, title: str, severity: str, message: str,
                        channel: str) -> bool:
        """Callback used by the send_alert tool."""
        if not self.alert_state.should_alert(
                key=f"llm_alert:{title.lower()[:60]}",
                cooldown_seconds=ALERT_COOLDOWN_SECONDS):
            logger.info(f"Alert deduped: {title}")
            return True  # treated as handled (already alerted recently)

        ok = False
        if channel in ("telegram", "both"):
            ok = self._telegram(title, severity, message)
        self.events.add(kind="alert", title=title, detail=message,
                        severity=severity, extra={"telegram": ok})
        return ok

    def _telegram(self, title: str, severity: str, message: str) -> bool:
        try:
            from ..notifier_ai import TelegramNotifierAI, TelegramConfig
            import os
            tg = TelegramNotifierAI(TelegramConfig(
                bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            ))
            return tg.send_llm_alert(title, severity, message)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    # ------------------------------------------------------------------ loop

    def run_cycle(self, reason: str = "cron") -> str:
        """One agentic cycle. Returns the final agent text."""
        config = self.config_store.effective()

        if not config.enabled:
            logger.debug("LLM agent disabled, skipping cycle")
            return ""

        if not self.provider.api_key and config.provider == "openrouter":
            logger.warning("No API key configured, skipping cycle")
            self.last_cycle_result = "no API key"
            return ""

        if not self.cost.under_budget():
            logger.warning(f"Daily budget exceeded: {self.cost.estimate_usd():.4f} USD")
            self.last_cycle_result = "budget exceeded"
            return ""

        tools, dispatch = self.current_tools(config)

        messages: List[Dict] = [
            {"role": "system", "content": self.system_prompt(config)},
            {"role": "user", "content": (
                f"Ciclo {reason}. Estado actual: {self.build_context()}\n"
                f"Investiga con las herramientas disponibles y decide si "
                f"corresponde alertar o generar un analisis."
            )},
        ]

        final_text = ""
        tool_count = 0
        for _ in range(MAX_TOOL_ITERATIONS):
            result = self.provider.chat(
                messages, tools=tools,
                max_tokens=config.max_tokens,
                temperature=config.temperature,
            )
            self.cost.record(result.prompt_tokens, result.completion_tokens)
            if not result.ok:
                logger.error(f"LLM error: {result.error}")
                self.last_cycle_result = f"LLM error: {result.error}"
                return ""

            if result.tool_calls:
                messages.append({"role": "assistant", "content": result.text,
                                 "tool_calls": [
                                     {"id": tc.id, "type": "function",
                                      "function": {"name": tc.name,
                                                   "arguments": json.dumps(tc.arguments)}}
                                     for tc in result.tool_calls
                                 ]})
                for tc in result.tool_calls:
                    tool_count += 1
                    output = self._execute_tool(tc.name, tc.arguments, dispatch)
                    truncated = self._truncate_output(output)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": truncated,
                    })
                continue

            final_text = result.text or ""
            break

        self.last_cycle = time.time()
        self.last_cycle_tool_count = tool_count
        self.last_cycle_result = final_text[:200]

        if final_text:
            self.events.add(kind="analysis", title=f"Análisis de ciclo ({reason})",
                            detail=final_text[:1500], severity="info")
        logger.info(f"Cycle done: tools={tool_count} text_len={len(final_text)}")
        return final_text

    def _execute_tool(self, name: str, args: Dict, dispatch: Dict) -> Any:
        handler = dispatch.get(name)
        if not handler:
            return {"error": f"Tool '{name}' not available"}
        try:
            result = handler(**args)
            return result if isinstance(result, (dict, list, str, int, float, bool)) else str(result)
        except TypeError as e:
            return {"error": f"Invalid arguments for {name}: {e}"}
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return {"error": f"{name}: {e}"}

    @staticmethod
    def _truncate_output(output: Any) -> str:
        """Truncate tool output JSON to prevent context inflation."""
        raw = json.dumps(output, ensure_ascii=False, default=str)
        if len(raw) <= TOOL_OUTPUT_MAX_CHARS:
            return raw
        return raw[:TOOL_OUTPUT_MAX_CHARS] + '...[truncated]'

    # ------------------------------------------------------------------ chat

    def chat(self, question: str, history: Optional[List[Dict]] = None) -> Dict:
        """On-demand analysis/answer for the dashboard (any mode)."""
        config = self.config_store.effective()
        tools, dispatch = self.current_tools(config)

        messages: List[Dict] = [
            {"role": "system", "content": self.system_prompt(config)},
        ]
        if history:
            messages.extend(history[-10:])
        messages.append({"role": "user", "content": (
            f"Consulta del operador: {question}\n\n"
            f"Contexto actual: {self.build_context()}\n"
            f"Usa las herramientas si necesitas datos de la red."
        )})

        for _ in range(MAX_TOOL_ITERATIONS):
            result = self.provider.chat(messages, tools=tools,
                                        max_tokens=config.max_tokens,
                                        temperature=config.temperature)
            self.cost.record(result.prompt_tokens, result.completion_tokens)
            if not result.ok:
                return {"ok": False, "error": result.error}

            if result.tool_calls:
                messages.append({"role": "assistant", "content": result.text,
                                 "tool_calls": [
                                     {"id": tc.id, "type": "function",
                                      "function": {"name": tc.name,
                                                   "arguments": json.dumps(tc.arguments)}}
                                     for tc in result.tool_calls
                                 ]})
                for tc in result.tool_calls:
                    output = self._execute_tool(tc.name, tc.arguments, dispatch)
                    truncated = self._truncate_output(output)
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": truncated,
                    })
                continue

            answer = result.text or ""
            self.events.add(kind="analysis", title="Respuesta a consulta",
                            detail=answer[:1500], severity="info")
            return {"ok": True, "answer": answer, "model": result.model}

        return {"ok": False, "error": "max tool iterations reached"}

    # ------------------------------------------------------------------ modes

    def run_autonomous(self):
        while True:
            cfg = self.config_store.effective()
            if cfg.changed:
                self.config_store.reload()
            if cfg.enabled:
                self.run_cycle(reason="autonomous")
            time.sleep(max(60, cfg.check_interval_seconds))

    def run_hybrid(self):
        """Lighter cycle: check for new events and analyze, deterministic agent
        remains the primary alert source."""
        last_check = 0
        while True:
            cfg = self.config_store.effective()
            if cfg.changed:
                self.config_store.reload()
            if cfg.enabled:
                if time.time() - last_check > cfg.check_interval_seconds:
                    last_check = time.time()
                    self.run_cycle(reason="hybrid")
            time.sleep(30)

    def run_analyst(self):
        """Scheduled daily analysis + on-demand chat served by the API."""
        last_report = ""
        while True:
            cfg = self.config_store.effective()
            if cfg.changed:
                self.config_store.reload()
            if cfg.enabled:
                now = time.strftime("%H:%M")
                if now == cfg.analyst_report_hour and last_report != time.strftime("%Y-%m-%d"):
                    last_report = time.strftime("%Y-%m-%d")
                    self.run_cycle(reason="analyst")
            time.sleep(60)
