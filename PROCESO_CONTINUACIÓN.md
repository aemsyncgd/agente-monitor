# Proceso de reorganización del dashboard — Estado

## Completado
- [x] Menú "Agente" → "Clientes | Servicios"
- [x] Título pestaña "Agente IA" → "Clientes | Servicios"
- [x] Menú "Anomalias" → "Logs"
- [x] Título pestaña "Anomalias" → "Logs"
- [x] Comentario "TAB: Anomalias" → "TAB: Logs"
- [x] Panel "Agente LLM - Monitor" comentado (chat, eventos, tarjetas LLM)
- [x] Modal "Configuración LLM" comentado
- [x] Modal "Detalle de Evento LLM" comentado
- [x] Sección "Configuración" completa comentada + placeholder visible
- [x] LLM desactivado:
  - `config/config_ia.yaml` → `llm.enabled: false`
  - `config/llm_config.json` → `"enabled": false`
  - `scripts/start-all.sh` → línea `llm-agent` comentada
  - Sin procesos `llm_agent` ni unidad systemd `agente-monitor-llm-agent`
  - Guard en backend: `src/api/routes/agent_llm.py` rechaza chat si `enabled=false`
  - Frontend: `llmConfig.enabled` por defecto en `false`
- [x] "Análisis Predictivo" + "Anomalías detectadas" movidos a "Clientes | Servicios"
  (debajo de "Buscar Cliente"), con las 4 tarjetas KPI, filtros y tabla
- [x] Las 4 tarjetas KPI (Total / Críticas / Advertencias / Predicciones) funcionan
  como filtros rápidos (`applyAnomalyKpiFilter` + `anomalyKpiActive`, con anillo de
  selección); "Total" limpia los filtros
- [x] `watch(activeTab)` ahora carga datos y gráficos de anomalías en `agent`
- [x] Validado en navegador real: pestañas renderizan, tarjetas filtran, sin errores

## Estructura actual
### "Clientes | Servicios" (id: agent)
1. Tarjetas: Estado / Instrucciones / Clientes / Intervalo
2. Buscar Cliente + resultados
3. Modal Detalle del Cliente
4. Análisis Predictivo
5. "Anomalías detectadas": 4 tarjetas KPI (clicables) + filtros + tabla
6. (Panel LLM comentado)

### "Logs" (id: anomalies)
- Placeholder: "Módulo de Logs en preparación" (dentro del dashboard Vue)

### Boceto de Logs (página aparte, servida en `/logs`)
- Archivo: `src/api/static/logs-mockup.html`; ruta en `src/api/main.py` (`/logs`),
  mismo patrón que `/rediseno` (lee el archivo en cada request, sin caché)
- 4 contadores filtrables por gravedad (Todos / Críticas / Altas / Advertencias)
- Tarjetas de "Eventos importantes" por evento (no por cliente), con `×N` de repeticiones
- Paneles MikroTik y OLT en 3 columnas: Noticias / Advertencias / Alertas (high + danger)
- Indicadores en el menú (ítem Logs): OLT alertas (rojo), OLT advertencias (ámbar),
  OLT noticias (celeste), MikroTik noticias (gris); tooltips con el detalle
- Clic en una tarjeta abre modal de detalle: causa probable + confianza, evidencia,
  impacto, fuentes consultadas, interfaces/puertos con errores, clientes involucrados
  (tabla con PPPoE / serial ONU / Rx / estado) y logs relacionados, más acciones
- Cierre por botón, backdrop y Escape; scroll del body bloqueado mientras está abierto
- Datos de ejemplo; fuentes reales disponibles: `/api/v1/syslog/important`,
  `/api/v1/syslog/summary` y `/api/v1/incidents/`

### "Configuración" (id: config)
- Placeholder: "Módulo de configuración desactivado temporalmente"

## Notas técnicas
- Los bloques comentados se envolvieron en `<!-- ... -->` limpiando antes los
  comentarios HTML internos (los comentarios anidados no son válidos).
- `setup()` debe devolver TODA función usada en el template: si falta una,
  el render falla y Vue deja `#app` con `<!---->` (página vacía).
- El archivo tiene un desbalance de 1 `<div>` preexistente (también en HEAD).

## Backups
- `src/api/static/index.html.bak` (creado antes de los cambios)
