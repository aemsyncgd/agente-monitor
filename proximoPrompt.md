# ROL Y OBJETIVO

Eres un ingeniero senior de DevOps, redes y sistemas multi-agente. Tu tarea
es COMPLETAR las FASES 4, 5 y 6 de un sistema de monitoreo WISP que ya
está en producción. NO debes reinventar lo existente: debes INTEGRARTE
con la arquitectura actual y añadir únicamente los componentes faltantes.

Debes entregar:
  1. Código Python nuevo, listo para producción, integrado al proyecto.
  2. Modificaciones precisas a archivos existentes cuando sea necesario.
  3. Instrucciones de despliegue.
  4. Plan de migración que NO rompa producción.

================================================================
# ESTADO ACTUAL DEL PROYECTO (NO MODIFICAR, SÍ INTEGRAR)
================================================================

## Arquitectura en producción

*.txt
Plaintext

┌─────────────────────────────────────────────┐
                │                InfluxDB 2.x                 │
                │        (almacén de métricas y eventos)      │
                └───────▲───────────────────▲─────────────────┘
                        │                   │
 SNMP / ICMP            │                   │

┌─────────────────┐ escrituras lecturas
│ Collectors │──────┘ │
│ (OLT + MikroTik)│ │
│ - potencia óptica│ ┌───────────────────┴──────────────────┐
│ - tráfico/iface │ │ API (FastAPI) │
│ - ping targets │ │ /api/metrics, /anomalies, /agent, │
└─────────────────┘ │ /reporte, /nodes, /devices, /ping │
│ └───────▲───────────────▲────────────────┘
▼ │ │ │
┌─────────────────┐ informes estado / eventos
│ AI Engine │ │ │
│ (autoencoder + │ ┌───────┴────────────────┴────────┐
│ predicciones) │ │ Agente LLM (autónomo/hybrid) │
└─────────────────┘ │ Ollama · OpenRouter · Anthropic│
└──────────────▲──────────────────┘
alertas / análisis
│
┌─────────┴──────────┐
│ Telegram Bot │
└────────────────────┘
*.txt
Plaintext

## Estructura actual del proyecto

agente-monitor/
├── src/
│ ├── main.py # Agente base (Zabbix → eventos)
│ ├── config.py / config_ia.py
│ ├── collectors/ # SNMP OLT/MikroTik + escaneo ONU
│ ├── ai/ # Autoencoder, detección, llm_agent/
│ │ └── llm_agent/ # Agente LLM (solo lectura)
│ ├── api/ # FastAPI + dashboard
│ ├── reporte/
│ ├── storage/ # Cliente InfluxDB
│ ├── notifier*.py # Telegram
│ ├── grouper.py
│ ├── client_lookup.py
│ └── buffer.py / verifier.py
├── config/ # YAML, JSON de instrucciones y llm_config
├── tests/
├── scripts/start-all.sh
├── mantenimiento.sh
└── compose.yml (Podman: influxdb-ia, redis-ia)
*.txt
Plaintext

## Stack confirmado

  • Python 3.11+
  • InfluxDB 2.x (métricas y eventos)
  • FastAPI (API + dashboard)
  • LLM: Ollama local + opcionalmente OpenRouter/Anthropic
  • Telegram Bot
  • YAML + JSON para configuración
  • Podman disponible (NO Docker): InfluxDB corre en contenedor `influxdb-ia`; redis-ia se levanta con `podman compose up -d redis-ia`
  • systemd de USUARIO (`systemctl --user`, sin sudo) para los servicios

## Lo que YA funciona y NO debes duplicar

  • Recolección SNMP/ICMP (potencia óptica, tráfico, ping).
  • Autoencoder para detección de anomalías.
  • Agrupación de clientes por zona (grouper).
  • Lookup ONU → cliente.
  • Verificación y buffer de eventos.
  • Generación de reportes y gráficos.
  • Notificaciones Telegram básicas.
  • API REST con autenticación y dashboard web.

================================================================
# FASES A COMPLETAR (ESTE ES TU ÚNICO TRABAJO)
================================================================

## FASE 4 — Activar razonamiento continuo del LLM

### 4.1 Event Bus (NUEVO)

Crear `src/event_bus.py` con Redis Streams como columna vertebral de
comunicación asíncrona entre colectores, AI engine y agente LLM.

  - Productor: colectores y ai_engine publican en stream `wisp:events`.
  - Consumidor: agente LLM se suscribe con consumer group.
  - ACK explícito tras procesar.
  - Reintentos con backoff y dead-letter queue (`wisp:events:dlq`).

Esquema del evento (JSON):
{
  "id": "uuid",
  "timestamp": "ISO-8601",
  "source": "collector|ai_engine|api|manual",
  "device_id": "string",
  "device_type": "mikrotik|olt|onu|ping_target",
  "event_type": "link_down|high_loss|high_latency|low_signal|anomaly|...",
  "severity": "info|warning|critical",
  "payload": { ... datos crudos ... },
  "context": { "zona": "...", "clientes_afectados": N, "topologia": {...} }
}

### 4.2 Integración Collector → Event Bus

Modificar los colectores y el AI engine para que publiquen en el event bus
en vez de (o además de) escribir directamente. Debe ser retrocompatible:
un flag `LEGACY_DIRECT_WRITE` en `config_ia.yaml` controla el modo.

### 4.3 Enriquecimiento automático de eventos

Crear `src/enricher.py` que, antes de que el evento llegue al agente,
lo enriquece con:
  - Clientes afectados (usando `client_lookup` + `grouper`).
  - Zona geográfica.
  - Si es parte de una cascada (consulta topología en InfluxDB).
  - Histórico reciente del mismo dispositivo.

Este módulo REUTILIZA `client_lookup.py` y `grouper.py` existentes.

### 4.4 System Prompt especializado

Crear `config/instructions_v2.json` con un system prompt que combine:
  - Conocimiento de redes WISP, Mikrotik RouterOS, OLTs VSOL.
  - Tu base de conocimiento local (extraída de `instructions.json` actual).
  - Protocolos de escalación.
  - Tono: técnico, conciso, accionable, en español.

Migrar gradualmente desde el `instructions.json` legacy.

### 4.5 RAG (Retrieval-Augmented Generation)

Crear `src/ai/rag.py` con:
  - Vector store: ChromaDB (ligero, embebible) o Qdrant.
  - Documentos a indexar:
      * Topología de red (exportada desde InfluxDB periódicamente).
      * Manuales de equipos Mikrotik/VSOL (PDFs/texto en `docs/`).
      * Histórico de fallas con resolución.
      * Runbooks de operaciones.
  - Endpoint para re-indexar.
  - Función `retrieve_context(query, n_results=5, filters={...})`.
  - Script `scripts/seed_rag.py` para carga inicial.

### 4.6 Reasoner (NUEVO)

Crear `src/ai/reasoner.py` que:
  - Recibe evento enriquecido.
  - Recupera contexto vía RAG.
  - Construye prompt con: system + contexto + evento.
  - Llama al LLM (usando tu `llm_agent` existente como backend).
  - Parsea respuesta estructurada (JSON con: diagnóstico, severidad_recalculada,
    acciones_recomendadas, causa_raíz_probable).
  - Valida y registra el razonamiento.

================================================================
## FASE 5 — Loop autónomo 24/7 con memoria
================================================================

### 5.1 Loop asíncrono principal

Crear `src/ai/agent_loop.py` con `class WispAgent(aiocoagent.BaseAgent)`
que:
  - Consume `wisp:events` continuamente.
  - Para cada evento: enrich → rag → reasoner → actions.
  - Heartbeat cada 30s publicando en `wisp:agent:health`.
  - Modo degradado: si el LLM falla N veces seguidas, sigue solo
    con reglas determinísticas.
  - Métricas Prometheus: eventos_procesados, latencia, errores_llm.

### 5.2 Memoria multinivel

Crear `src/ai/memory.py` con tres niveles:
  - Working memory: deque en memoria con últimos K eventos (configurable).
  - Session memory: Redis con resumen de la última hora.
  - Long-term memory: InfluxDB + tabla `agent_memory` (PostgreSQL si existe,
    si no, bucket en InfluxDB) con resúmenes diarios.

API:
  - `memory.add(event, reasoning)`
  - `memory.get_recent(n=20)`
  - `memory.summarize_window(start, end)`
  - `memory.search(query, since=None)`

### 5.3 Correlación y patrones

Crear `src/ai/correlator.py` que detecta:
  - Cascada: si caen nodos en orden topológico en X minutos.
  - Masa: si caen N interfaces simultáneamente.
  - Recurrencia: si un mismo evento se repite >M veces en 24h.
  - Degradación: si una métrica empeora sostenidamente.

Salida: un "evento compuesto" que se publica al bus con severidad
elevada.

### 5.4 Turnos y políticas de notificación

Modificar `notifier*.py` (o crear `src/policy.py`) con:
  - Horario laboral vs. no laboral.
  - Severidad crítica → inmediata 24/7.
  - Warning/info en horario no laboral → acumular y resumen cada 4h.
  - Anti-spam: máximo N alertas por hora por zona.

### 5.5 Health check del agente

Añadir a la API:
  - `GET /api/agent/health` → estado del agente, última actividad,
    latencia LLM, uso de memoria.
  - `GET /api/agent/stats` → eventos procesados, alertas enviadas,
    acciones ejecutadas.
  - Si la API no recibe heartbeat del agente en X segundos → alerta
    al admin vía Telegram.

================================================================
## FASE 6 — Acciones, reportes programados y operación
================================================================

### 6.1 Acciones con niveles de seguridad

Crear `src/actions.py` con clases que heredan de `BaseAction` y un
decorador `@require_approval(level)`:

  - Nivel 0 (lectura): siempre permitido.
      `DiagnosticarAction`, `ConsultarTopologiaAction`.
  - Nivel 1 (alertas/notificaciones): sin confirmación.
      `EnviarAlertaTelegramAction`, `AbrirTicketAction`.
  - Nivel 2 (acciones reversibles): permitido bajo condiciones.
      `PingTestAction`, `TracerouteAction`, `RebootInterfaceAction`
        (solo si el nodo tiene < N clientes y no es de backbone).
  - Nivel 3 (críticas): SIEMPRE confirmación humana.
      `RebootOLTAction`, `CambiarConfiguracionAction`, `BloquearClienteAction`.

El decorador publica una solicitud al bot de Telegram con botones
inline "Confirmar / Rechazar" con timeout configurable.

### 6.2 Bot de Telegram enriquecido

Ampliar tu bot actual con:
  - Comandos: /status, /alertas, /reporte, /topologia <nodo>,
    /accion <id>, /silenciar <horas>, /ayuda.
  - Botones inline para confirmación de acciones nivel 3.
  - Notificaciones con mini-mapa (PNG estático generado con Folium
    o similar, embebido en el dashboard).
  - Reenvío automático de eventos críticos con su diagnóstico LLM.

### 6.3 Scheduler de reportes

Crear `src/scheduler.py` con APScheduler (o `schedule` + asyncio)
que ejecute:
  - Reporte diario a las 06:00.
  - Reporte semanal los lunes a las 07:00.
  - Reporte mensual el día 1 a las 08:00.
  - Health check del propio agente cada 5 min.
  - Re-indexado RAG semanal.
  - Limpieza de memoria de largo plazo (retención configurable).

Reutilizar el módulo `reporte/` existente. Solo añadir la capa
de scheduling y los formatos (Markdown para Telegram, HTML/PDF para
correo, JSON para integraciones).

### 6.4 Mapa interactivo (ampliación del dashboard)

Extender tu dashboard FastAPI con:
  - WebSocket `/ws/events` que emite eventos en tiempo real.
  - Capa de nodos con color por estado (actualiza en vivo).
  - Capa de zonas de calor de fallas.
  - Reutilizar `grouper.py` para zonas.

### 6.5 Auditoría y observabilidad

Crear `src/audit.py` con:
  - Tabla/log `audit_log`: timestamp, actor, acción, parámetros, resultado.
  - Log estructurado JSON (python-json-logger) en todos los módulos nuevos.
  - Métricas Prometheus: contadores y histogramas.
  - Endpoint `GET /api/audit?since=...&action=...` para consulta.

================================================================
# ENTREGABLES (EN ESTE ORDEN)
================================================================

Mensaje 1 (lo más crítico):
  1. Diagrama de la arquitectura final integrada.
  2. Diagrama de secuencia: evento → agente → acción.
  3. Estructura de archivos nuevos (no toques los existentes salvo
     lo estrictamente necesario).
  4. requirements-ia.txt actualizado (añade: redis, aiocoagent o similar,
     chromadb, folium, apscheduler, python-json-logger, prometheus_client,
     aiogram o python-telegram-bot con handlers async, etc.).
  5. Código completo de:
       src/event_bus.py
       src/enricher.py
       src/ai/agent_loop.py
       src/ai/memory.py
       src/ai/rag.py
       src/ai/reasoner.py
       src/ai/correlator.py
       src/policy.py
       src/actions.py
       src/audit.py
       src/scheduler.py

Mensaje 2:
       Modificaciones a config_ia.yaml y llm_config.json.
       Modificaciones a notifier*.py para el bot enriquecido.
       Modificaciones a api/ para nuevos endpoints.
       Script scripts/seed_rag.py.
       Archivo systemd actualizado.
       compose.yml actualizado (Podman: redis-ia).
       Plan de migración paso a paso (modo dual: legacy + nuevo,
         corte limpio tras validación).
       Lista de riesgos y mitigaciones.
       Tests mínimos para los nuevos módulos.

================================================================
# RESTRICCIONES
================================================================

  • NUNCA duplicar lógica existente: SIEMPRE reutilizar client_lookup,
    grouper, verifier, buffer, notifier, storage, ai_engine, llm_agent.
  • Código 100% async donde sea posible.
  • Configuración solo vía variables de entorno o YAML/JSON; cero
    hardcodeo de credenciales.
  • Retrocompatibilidad: el sistema debe poder correr en modo "legacy"
    (sin event bus, sin LLM en loop) mientras se valida lo nuevo.
  • Documentación en cada módulo + un NUEVO README_AGENT.md.
  • Nada de TODOs ni placeholders. Código listo para producción.
  • Responde en español, estructurado en secciones claras.

🎯 Cómo usar este prompt

    Pégalo completo en tu asistente (recomendado: Claude, GPT-4, o un modelo de razonamiento fuerte). En qwen 2.5 deberás fragmentarlo.

    El prompt respeta tu proyecto: no pide reescribir nada de lo que ya funciona. Solo añade módulos nuevos y especifica qué archivos existentes modificar y por qué.

    Modo dual durante la migración: el prompt exige que el sistema pueda correr en modo legacy mientras validas el nuevo, así no rompes producción.

    Orden de implementación sugerido (lo entregué así en el propio prompt):
        Primero event bus + enricher + loop (la columna vertebral).
        Después reasoner + RAG + memoria (la inteligencia).
        Finalmente actions + scheduler + audit (la operación).

    Sobre el LLM: tu llm_agent ya existe. El prompt lo trata como backend — el nuevo reasoner.py lo invoca con prompts enriquecidos. No reemplaza tu código, lo orquesta.

💡 Próximo paso recomendado

Pega este prompt en tu asistente favorito y pide que empiece por el Mensaje 1. Si tu modelo se queda corto de tokens, fragmenta así:

    Turno A: "Dame solo event_bus.py, enricher.py y agent_loop.py con su explicación."
    Turno B: "Continúa con memory.py, rag.py y reasoner.py."
    Turno C: "Continúa con correlator.py, policy.py y actions.py."
    Turno D: "Continúa con audit.py, scheduler.py y los hooks en notifier/api."
    Turno E: "Dame el plan de migración, systemd de usuario, podman y tests."
