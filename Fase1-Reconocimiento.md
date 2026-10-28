# FASE 1 — Reconocimiento obligatorio

> Sin escribir código. Solo análisis con citas archivo:línea.

## 1) Stack confirmado (con citas)
- FastAPI app principal: `src/api/main.py:1-257` (título `Monitoreo IA - Vidanet`, middlewares Security/NoCache/Auth, CORS, rate limiting opcional, monta `/static` desde `STATIC_DIR`, rutas HTML: `/` y `/newdesign`).
- Estáticos UI actual: `src/api/static/index.html:1-60+` (Vue 3 + Tailwind + ApexCharts + vis-network + Inter), `backbone.html`, `olts.html`.
- Autenticación/roles: `src/api/auth.py:1-160` (Role: admin/operator/viewer/none, keys via env `API_KEY*`, rutas públicas/ADMIN_PATHS/VIEWER_WRITE_PREFIXES, middleware).
- Datos InfluxDB: `src/api/influx_helper.py:1-200` (TTL caché `_DASH_TTL=45s`, single-flight, executor dedicado, `run_influx`, funciones métricas/clientes/anomalías/health).
- Clientes enriquecidos: `src/api/influx_helper.py:513-750` (`get_enriched_clients`, `search_clientes`, `get_client_by_documento`, filtros por tags).
- Store Redis clientes: `src/redis_store.py:1-150` (`ClientStore`, `_collapse_variants`, índices por documento/serial).
- Rutas API: `src/api/routes/*.py` (clientes:n1+, nodes:n1+, metrics:n1+, anomalies:n1+, health:n1+, agent_llm:n1+).
- Config: `src/config.py:1-120`, `src/config_manager.py:1-150`, `src/config_ia.py`, `config/*.yaml`.
- Scheduler/Servicios: `src/scheduler.py`, systemd user units: `agente-monitor*.service`, `influxdb-ia.service`, `redis-ia.service`.
- Otros: `src/wisp_client.py`, `src/models.py`, `src/reporte/`, `src/audit.py` (fases 4-6), `src/ai/*` existentes.

## 2) Tabla de correspondencia detallada

| Sección | Estado | Ruta/Plantilla real | Fuente datos real | Notas con citas |
|---|---|---|---|---|
| Panel NOC (KPIs globales) | Parcial | `src/api/static/index.html` (Vue app) | `src/api/influx_helper.py:get_overall_stats` (referenciado por `routes/health.py:44-57`), `/api/anomalies/summary` (`routes/anomalies.py:118-124`) | KPIs existen (totales/críticas/advertencias/predicciones con %). Necesitan agregados específicos al layout NEXO NOC (ventanas 24h/prev, sparkline). `requiere backend`: endpoint agregado si se desea un único `GET /api/noc/summary` optimizado. |
| Alertas | Parcial | `src/api/routes/anomalies.py:29-124` (endpoints `/anomalies/`, `/recent`, `/summary`, `/history`, `/predictions`) | InfluxDB + autoencoder vía `influx_helper` (`get_anomalies_from_influx`, `predict_failures`, `get_onu_optical_history`) | Listado/summary reales. Migrar a nuevo layout. `requiere backend`: paginación/agrupado por severidad/zona con filtros avanzados. |
| Mapa/Topología | Parcial | `src/api/static/backbone.html`, `src/api/static/index.html` (vis-network). API `src/api/routes/nodes.py:146-210` (`GET /api/nodes/`, `/summary`, `/validate`) | `config/nodes.yaml` vía `config_manager.py`, `conectado_a` (`nodes.py:165,180`), estado `online/unknown` basado en IPs activas (`nodes.py:151`, `influx_helper.get_active_device_ips`) | Topología interactiva existe. Replicar estilo NEXO NOC (colores por estado, ruta al core, impacto). `requiere backend`: exponer subárbol/descendientes + métricas agregadas por nodo para panel flotante en un único endpoint. |
| MikroTik | Existe | `src/api/static/index.html` (tarjetas+modal detalle interfaces) | `src/api/routes/metrics.py` (interfaces/metrics), `influx_helper` consultas SNMP/InfluxDB | Funcional (Sesiones 7-8 documentadas en `interface-dashboard.md`). Datos reales. Migrar UI. Sin cambios backend obligados. |
| OLT VSOL | Existe | `src/api/static/olts.html` + index.html, `src/api/routes/nodes.py:481-600` (CRUD/listado OLTs por nodo) | InfluxDB `olt_*`, potencia óptica, tráfico PON/GE; `influx_helper.get_olt_*` | Completo (tarjetas, modal GE/PON/ONUs, búsqueda). Migrar. Datos reales. |
| ONUs/clientes GPON | Parcial | `src/api/routes/clientes.py:225-450` (`/api/clientes/stats`, `/pendientes`, `/buscar`, `/`, `/los`, `/grupo`, `/bulk`, `/vital*`, `/{documento}`) | `clientes_enriquecidos` (InfluxDB) vía `influx_helper.get_enriched_clients` (513+), `search_clientes` (723+), Redis store (`redis_store.py`) para snapshot | API rica con enmascarado por rol (`_apply_masking`: clientes.py:174-208, roles auth.py:70-87). UI nueva debe respetar roles. `requiere backend`: agregar filtros/columnas específicos ficha-cliente + endpoint ficha única enriquecida con todo contexto. |
| WISP | Parcial | Depende nodos/MikroTik (`routes/nodes.py`, `routes/metrics.py`) | Config + métricas SNMP/InfluxDB | Alcance WISP no expuesto como vista unificada. `requiere backend`: definir vista WISP con datos reales existentes o agregar agregados. |
| Fichas de clientes | No existe (nueva) | — | — | **Nueva sección**. Requisito: mapeo cliente–PPPoE–ONU. Datos disponibles: `clientes_enriquecidos`, Redis store, búsquedas. `requiere backend`: endpoint `/api/clientes/ficha/{id}` con datos completos + historial + eventos + topología. Estimación ver abajo. |
| Trabajos programados | Parcial | `src/scheduler.py`, `agente-monitor-scheduler.service` (systemd user) | Scheduler APScheduler-like existente (fases 4-6: `src/scheduler.py` referencia en proximoPrompt.md) | Existe lógica scheduler (reportes/health/RAG/reindex/limpieza). `requiere backend`: endpoints REST para listar jobs/estado/última ejecución + habilitar/deshabilitar (solo lectura segura o admin). |
| Reportes/SLA | Parcial | `src/reporte/` (reporter.py con templates) | Datos InfluxDB + eventos | Generación existente. UI nueva para consultar/descargar. `requiere backend`: listado histórico reportes + preview/descarga con permisos por rol. |
| Auditoría | Parcial | `src/audit.py` (fases 4-6) | Logs estructurados/audit_log | Existe módulo auditoría. `requiere backend`: `GET /api/audit` con filtros (since/action/actor) + paginación. |
| Configuración | Parcial | `src/api/routes/nodes.py:128-600` (CRUD nodos/MikroTik/OLT + summary/validate), `config_manager.py` | `config/nodes.yaml`, `config/config*.yaml` | CRUD real existe (crear/editar/borrar nodos/MikroTik/OLT). Mejorar UX (grupos). `requiere backend`: endpoints para editar otros parámetros (monitoring/verification/telegram/retención) con validación + backup. |

## 3) Lo que NO puede entregarse hoy (`requiere backend`) — con estimación

| Ítem | Justificación | Archivos a considerar | Estimación | Prioridad |
|---|---|---|---|---|
| NOC Summary agregado (`/api/noc/summary`) | Unificar KPIs (dispositivos/alertas/críticas/advertencias/ONUs afectadas/resumen por zona) en 1 request para evitar N llamadas. Datos existen dispersos (`anomalies/summary`, `nodes/summary`, health). | `src/api/routes/noc.py` (nuevo) + `influx_helper.py` (agregados) | 2–3h | Alta |
| Topología enriquecida (`/api/topology/enriched`) | Panel flotante requiere: subárbol descendientes por `conectado_a`, métricas últimas (CPU/RAM/Temp/RX/TX) + "impacto si cae". Hoy nodos+status; métricas se piden por dispositivo. | `src/api/routes/topology.py` (nuevo) + `influx_helper.py` | 2–3h | Alta |
| Fichas de clientes completas (`/api/clientes/ficha/{id}`) | Nueva sección. Necesita: datos enriquecidos + historial potencia 24–168h (`get_onu_optical_history`), eventos/anomalías recientes, estado PPPoE/WISP, topología (OLT/PON/nodo/nap/zona), notas? | `src/api/routes/clientes.py` (extender) + `influx_helper.py` | 2–3h | Alta |
| Auditoría API UI (`GET /api/audit`) | Exponer `audit.py` vía REST con paginación/filtros (since/action/actor/result). Si ya existe internamente, solo exponer endpoint + auth por rol. | `src/api/routes/audit.py` (nuevo) | 1–2h | Media-Alta |
| Trabajos programados UI API | Listar jobs APScheduler (nombre/trigger/next_run_time/last_run/estado). Solo lectura inicial (seguro). Escritura requiere admin. | `src/api/routes/scheduler.py` (nuevo) + `src/scheduler.py` (exponer registry) | 1–2h | Media |
| Reportes/SLA UI API | Listar reportes generados (histórico) + endpoints descarga con validación permisos. `reporte/` genera; falta listado. | `src/api/routes/reportes.py` (nuevo) | 1–2h | Media |
| Config global editable | CRUD para `config.yaml` secciones (monitoring/verification/telegram/retención) + validación + backup. Hoy solo nodos/MikroTik/OLT editables vía `nodes.py`/`config_manager`. | `src/api/routes/config.py` (nuevo) + `config_manager.py` extender | 2–3h | Media |
| Alertas avanzadas (filtros/paginación) | Paginación servidor + filtros por zona/OLT/severidad/fecha + agrupado. `/anomalies/recent` limitado (`limit`). | `src/api/routes/anomalies.py` extender | 1–2h | Media |

**Total estimado `requiere backend`: ~10–16h** (endpoints nuevos + pequeños agregados). Muchos datos ya existen en InfluxDB/Redis/config; mayor parte es **exposición vía API** + agregados ligeros.

## 4) Veredicto FASE 1
FASE 1 **completa** (sin código). Stack mapeado con citas, tabla detallada, inventario de rutas reales, auth/permisos, fuentes de datos validadas. Listado explícito `requiere backend` con estimación por ítem.

**Siguiente:** Aprobación → **FASE 2 (Plan)** con estructura de archivos para `/newdesign`, orden de entregas incremental (una sección a la vez), criterios "listo", riesgos/mitigantes. No escribir código hasta aprobación.
