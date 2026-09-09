# src/ai/reasoner.py
"""
Reasoner: Motor de razonamiento del agente WISP.

Recibe evento enriquecido → recupera contexto RAG → construye prompt →
llama LLM → parsea respuesta estructurada.
"""
import json
import time
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ReasoningResult:
    """Resultado estructurado del razonamiento del LLM."""

    def __init__(self, raw_text: str = "", model: str = ""):
        self.raw_text = raw_text
        self.model = model
        self.diagnostico = ""
        self.severidad_recalculada = "info"
        self.acciones_recomendadas: List[Dict] = []
        self.causa_raiz_probable = ""
        self.confidence = 0.0
        self.tokens_used = 0
        self.latency_ms = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "diagnostico": self.diagnostico,
            "severidad_recalculada": self.severidad_recalculada,
            "acciones_recomendadas": self.acciones_recomendadas,
            "causa_raiz_probable": self.causa_raiz_probable,
            "confidence": self.confidence,
            "model": self.model,
            "tokens_used": self.tokens_used,
            "latency_ms": self.latency_ms,
        }

    def has_actions(self) -> bool:
        return bool(self.acciones_recomendadas)


class WispReasoner:
    """Razonador que integra RAG + LLM para analizar eventos."""

    SYSTEM_PROMPT = """Eres el agente de razonamiento de VidaNet, proveedor de internet FTTH/GPON.

Tu tarea es analizar eventos de red y devolver UN JSON válido y CONCISO con esta estructura:
{
    "diagnostico": "Análisis técnico en 1-2 frases",
    "severidad": "info|warning|critical",
    "causa_raiz_probable": "Causa más probable en 1 frase",
    "confianza": 0.0-1.0,
    "acciones": [
        {
            "nombre": "NombreAcción",
            "nivel": 0-3,
            "descripcion": "Qué hacer",
            "parametros": {}
        }
    ]
}

REGLAS:
1. Responde SOLO con el JSON, sin texto adicional ni markdown.
2. Mantén diagnostico y causa en máximo 2 frases cada uno (respuesta breve).
3. Nivel 0: lectura/diagnóstico. Nivel 1: alertas. Nivel 2: reversibles. Nivel 3: críticas.
4. Severidad: critical=caída masiva/host down; warning=degradación/aislada; info=informativo.
5. Basa tu análisis en los datos proporcionados, no inventes.
6. confianza debe ser un NÚMERO entre 0 y 1.
7. Si no hay evidencia suficiente, indica confianza baja.
8. Responde en español."""

    def __init__(self, provider=None, rag_store=None, influx_client=None):
        self.provider = provider
        self.rag = rag_store
        self.influx = influx_client

    def reason(self, event_dict: Dict[str, Any]) -> ReasoningResult:
        start_time = time.time()
        result = ReasoningResult()

        try:
            context_parts = self._build_context(event_dict)
            user_prompt = self._build_prompt(event_dict, context_parts)

            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]

            if self.provider:
                response = self._call_llm(messages)
                result.raw_text = response.get("text", "")
                result.model = response.get("model") or ""
                result.tokens_used = response.get("tokens", 0)
            else:
                result.raw_text = self._fallback_reasoning(event_dict)
                result.model = "fallback-rules"

            # Si el LLM no produjo un JSON útil (timeout/fallo/vacío), degradar a reglas
            used_llm = bool(self.provider and response.get("text") and
                            self._parse_response(result.raw_text).get("diagnostico"))
            if not used_llm and self.provider:
                logger.warning(
                    f"LLM no produjo diagnóstico válido (model={result.model!r}); "
                    "degradando a fallback-rules"
                )
                result.raw_text = self._fallback_reasoning(event_dict)
                result.model = "fallback-rules"
                parsed = self._parse_response(result.raw_text)
            else:
                parsed = self._parse_response(result.raw_text)

            result.diagnostico = parsed.get("diagnostico", result.raw_text[:500])
            result.severidad_recalculada = parsed.get("severidad", event_dict.get("severity", "info"))
            result.acciones_recomendadas = parsed.get("acciones", [])
            result.causa_raiz_probable = parsed.get("causa_raiz_probable", "")
            result.confidence = parsed.get("confianza", 0.5)

        except Exception as e:
            logger.error(f"Reasoning failed: {e}")
            result.diagnostico = f"Error en razonamiento: {e}"
            result.severidad_recalculada = event_dict.get("severity", "info")

        result.latency_ms = int((time.time() - start_time) * 1000)
        return result

    def _build_context(self, event_dict: Dict[str, Any]) -> str:
        parts = []

        parts.append(f"EVENTO: {event_dict.get('event_type', 'desconocido')}")
        parts.append(f"SEVERIDAD: {event_dict.get('severity', 'info')}")
        parts.append(f"DISPOSITIVO: {event_dict.get('device_id', 'N/A')} "
                     f"({event_dict.get('device_type', 'N/A')})")

        context = event_dict.get("context", {})
        if context.get("client_name"):
            parts.append(f"CLIENTE: {context['client_name']}")
        if context.get("zone"):
            parts.append(f"ZONA: {context['zone']}")
        if context.get("clientes_afectados"):
            parts.append(f"CLIENTES AFECTADOS: {context['clientes_afectados']}")
        if context.get("historic_avg_power"):
            parts.append(f"POTENCIA PROMEDIO: {context['historic_avg_power']} dBm")
        if context.get("topology"):
            topo = context["topology"]
            parts.append(f"ESTADO TOPOLOGÍA: {topo.get('status', 'unknown')}")

        payload = event_dict.get("payload", {})
        if payload:
            for k, v in payload.items():
                if k not in ("onu_serial", "olt_name", "pon_port") and v:
                    parts.append(f"{k}: {v}")

        if self.rag and self.rag.ready:
            rag_context = self._retrieve_rag(event_dict)
            if rag_context:
                parts.append(f"\nCONOCIMIENTO RELEVANTE:\n{rag_context}")

        return "\n".join(parts)

    def _retrieve_rag(self, event_dict: Dict[str, Any]) -> str:
        query_parts = [
            event_dict.get("event_type", ""),
            event_dict.get("device_type", ""),
            event_dict.get("device_id", ""),
        ]
        query = " ".join(filter(None, query_parts))

        if not query.strip():
            return ""

        results = self.rag.retrieve(query, n_results=3)
        if not results:
            return ""

        context_lines = []
        for r in results:
            if r.get("score", 0) > 0.3:
                content = r.get("content", "")[:300]
                context_lines.append(f"- {content}")

        return "\n".join(context_lines)

    def _build_prompt(self, event_dict: Dict[str, Any], context: str) -> str:
        return f"""Analiza el siguiente evento de red y responde con el JSON estructurado (breve).

CONTEXTO:
{context}

Genera el diagnóstico, causa raíz probable, severidad recalculada y acciones recomendadas. Sé conciso."""

    def _call_llm(self, messages: List[Dict]) -> Dict[str, Any]:
        try:
            from .llm_agent.providers import LLMResult
            result = self.provider.chat(
                messages,
                max_tokens=256,
                temperature=0.0,
            )
            if isinstance(result, dict):
                text = result.get("content") or result.get("text") or ""
                model = result.get("model") or ""
                tokens = result.get("tokens", 0)
            else:
                text = getattr(result, "text", "") or ""
                model = getattr(result, "model", "") or ""
                tokens = (getattr(result, "prompt_tokens", 0) or 0) + (getattr(result, "completion_tokens", 0) or 0)
            return {"text": text, "model": model, "tokens": tokens}
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return {"text": "", "model": "", "tokens": 0, "error": str(e)}

    def _fallback_reasoning(self, event_dict: Dict[str, Any]) -> str:
        severity = event_dict.get("severity", "info")
        event_type = event_dict.get("event_type", "unknown")
        device_id = event_dict.get("device_id", "desconocido")
        device_type = event_dict.get("device_type", "")
        context = event_dict.get("context", {}) or {}
        payload = event_dict.get("payload", {}) or {}
        affected = int(context.get("clientes_afectados", payload.get("clientes_afectados", 0)) or 0)
        client_name = context.get("client_name") or payload.get("client_name") or ""
        zone = context.get("zone") or payload.get("zone") or ""
        power = context.get("historic_avg_power") or payload.get("power") or payload.get("rx_power") or ""
        olt = payload.get("olt_name") or context.get("olt_name") or ""
        pon = payload.get("pon_port") or context.get("pon_port") or ""

        def _sev():
            sev = severity
            if sev == "critical":
                return "critical"
            if severity == "warning" or affected > 0:
                return "warning"
            return "info"

        location = f"{zone}{(' / ' + client_name) if client_name else ''}".strip(" /")

        if event_type in ("link_down", "host_down"):
            causa = "El enlace o equipo dejó de responder; posible caída de enlace, corte de energía o fallo de hardware"
            act = [{"nombre": "DiagnosticarAction", "nivel": 0,
                    "descripcion": "Verificar estado del enlace/equipo",
                    "parametros": {"device_id": device_id}},
                   {"nombre": "EnviarAlertaTelegramAction", "nivel": 1,
                    "descripcion": "Notificar caída del enlace",
                    "parametros": {"title": f"Caída en {device_id}"}}]
            sev = "critical"
            conf = 0.7
        elif event_type in ("low_signal", "anomaly"):
            if power:
                causa = f"Señal óptica degradada (potencia {power} dBm); revisar empalmes, splitter o conectorización"
            else:
                causa = "Degradación de señal óptica; revisar presupuesto óptico del enlace"
            act = [{"nombre": "PingTestAction", "nivel": 2,
                    "descripcion": "Prueba de conectividad del cliente",
                    "parametros": {"device_id": device_id}},
                   {"nombre": "DiagnosticarAction", "nivel": 0,
                    "descripcion": "Medir potencia óptica en OLT y ONU",
                    "parametros": {"device_id": device_id, "olt_name": olt, "pon_port": pon}}]
            sev = _sev()
            conf = 0.6
        elif event_type in ("cascade", "mass_down"):
            causa = (f"Falla masiva ({affected} clientes afectados); probable caída de OLT, "
                     f"fibra troncal o alimentación y afecta redundancia")
            act = [{"nombre": "EnviarAlertaTelegramAction", "nivel": 1,
                    "descripcion": "Notificar falla masiva",
                    "parametros": {"title": f"Cascada en {device_id} ({affected} clientes)"}},
                   {"nombre": "DiagnosticarAction", "nivel": 0,
                    "descripcion": "Verificar estado del nodo/OLT afectado",
                    "parametros": {"device_id": device_id}}]
            sev = "critical"
            conf = 0.8
        elif event_type in ("recurrence",):
            causa = (f"Eventos repetidos en {device_id}; posible problema intermitente, "
                     f"hasta {affected or 'varios'} reportes en la ventana")
            act = [{"nombre": "DiagnosticarAction", "nivel": 0,
                    "descripcion": "Investigar patrón intermitente y correlacionar con la(s) zona(s) afectada(s)",
                    "parametros": {"device_id": device_id, "zone": zone}}]
            sev = "warning"
            conf = 0.6
        elif event_type in ("heartbeat", "status"):
            causa = "Estado de salud reportado por el componente; sin anomalía si se mantiene 'healthy'"
            act = [{"nombre": "DiagnosticarAction", "nivel": 0,
                    "descripcion": "Revisar métricas de salud del componente",
                    "parametros": {"device_id": device_id}}]
            sev = "info"
            conf = 0.5
        else:
            causa = "Evento detectado; se recomienda diagnóstico del dispositivo afectado"
            act = [{"nombre": "DiagnosticarAction", "nivel": 0,
                    "descripcion": "Diagnosticar el dispositivo afectado",
                    "parametros": {"device_id": device_id}}]
            sev = _sev()
            conf = 0.4

        diag = f"Evento '{event_type}' en {device_id} ({device_type or 'dispositivo'})"
        if location:
            diag += f" · {location}"
        if power:
            diag += f" · potencia {power} dBm"
        if affected:
            diag += f" · {affected} cliente(s) afectado(s)"

        result = {
            "diagnostico": diag,
            "severidad": sev,
            "causa_raiz_probable": causa,
            "confianza": conf,
            "acciones": act,
        }
        return json.dumps(result, ensure_ascii=False)

    def _normalize_result(self, parsed: Dict) -> None:
        """Normaliza tipos del JSON del LLM (strings → números, dicts → listas)."""
        # confianza puede venir como string ("alta"/"0.8")
        conf = parsed.get("confianza")
        if isinstance(conf, str):
            low = conf.strip().lower()
            scale = {"alta": 0.8, "alta ": 0.8, "media": 0.6, "baja": 0.4, "alta": 0.9}
            num = scale.get(low)
            if num is None:
                try:
                    num = float(low)
                except (ValueError, TypeError):
                    num = 0.5
            parsed["confianza"] = min(1.0, max(0.0, num))

        # acciones puede venir como string o dict individual → forzar lista
        acc = parsed.get("acciones")
        if isinstance(acc, str):
            parsed["acciones"] = [{"nombre": acc, "nivel": 0, "descripcion": acc}]
        elif isinstance(acc, dict):
            parsed["acciones"] = [acc]
        elif not isinstance(acc, list):
            parsed["acciones"] = []

    def _parse_response(self, text: str) -> Dict[str, Any]:
        if not text:
            return {}

        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                self._normalize_result(parsed)
                return parsed
        except json.JSONDecodeError:
            pass

        import re
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            candidate = json_match.group()
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    self._normalize_result(parsed)
                    return parsed
            except json.JSONDecodeError:
                # JSON truncado (llave sin cerrar): intentar reparar
                repaired = self._repair_truncated_json(candidate)
                if repaired is not None:
                    self._normalize_result(repaired)
                    return repaired
                return {}

        return {}

    @staticmethod
    def _repair_truncated_json(text: str) -> Optional[Dict]:
        """Intenta reparar un JSON truncado a medio camino (útil en respuestas cortadas)."""
        increments = 0
        in_string = False
        escaped = False
        for ch in text:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    increments += 1
                elif ch == "}":
                    increments -= 1
        if increments <= 0:
            return None
        candidate = text + ("}" * increments)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None
