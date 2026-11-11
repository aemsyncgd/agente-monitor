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

### "Logs" (id: anomalies) — IMPLEMENTADO EN PRODUCCIÓN
- Ya no es un placeholder: la pestaña Logs del dashboard Vue (`src/api/static/index.html`)
  replica el boceto aprobado (`/logs`) con datos reales del backend
- Tarjetas por evento importante (no por cliente): una por incidencia de
  `/api/v1/incidents/`, con causa, impacto, equipo/PON y hora
- Paneles de equipos en 3 categorías (Noticias / Advertencias / Alertas) desde
  `GET /api/v1/syslog/panels?minutes=<rango>&per_bucket=20`
- Rango 1 / 6 / 24 h y selector de equipo (Todos / MikroTik / OLTs) operativos
- Filtro por gravedad desde las tarjetas de resumen (Todos / Críticas / Altas / Advertencias)
- Indicadores en el menú Logs con los conteos crudos por bucket:
  OLT alertas (rojo), OLT advertencias (ámbar), OLT noticias (celeste), MikroTik noticias (gris)
- Modal de detalle por evento (incidencia o línea de syslog) con:
  causa, confianza, evidencia, impacto, fuentes consultadas, puertos PON involucrados,
  clientes involucrados, logs relacionados y acciones operativas
- Acciones de incidencia (solo incidencias): `POST /{id}/ack`, `POST /{id}/mute?hours=1`,
  `POST /{id}/resolve`; el modal y los indicadores se refrescan al terminar

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
- Datos de ejemplo; queda como referencia visual de la sección ya implementada
  en el dashboard (el boceto no consume la API)

### "Configuración" (id: config)
- Placeholder: "Módulo de configuración desactivado temporalmente"

## Notas técnicas
- Los bloques comentados se envolvieron en `<!-- ... -->` limpiando antes los
  comentarios HTML internos (los comentarios anidados no son válidos).
- `setup()` debe devolver TODA función usada en el template: si falta una,
  el render falla y Vue deja `#app` con `<!---->` (página vacía).
- El archivo tiene un desbalance de 1 `<div>` preexistente (también en HEAD).
- `normDev()` normaliza `OLT-SISAL2-PON2` → `oltsisal2` y `OLT-SISAL-2` → `oltsisal2`
  para casar la OLT de las incidencias con la de Influx y el syslog
- Los mensajes de syslog de OLT llegan escapados (`PON 0\/9`); `cleanLogMsg()`
  los normaliza a `PON 0/9` antes de parsear el puerto PON y de mostrarlos
- Los "logs relacionados" del modal agrupan por familia de mensaje (`patternKey()`:
  MAC / IP / usuario / serial / números enmascarados), no por dispositivo completo
- El contenido de syslog e incidencias se interpola como texto plano (nunca `v-html`);
  `v-html` solo se usa para los SVG estáticos de los iconos
- Backend de paneles: `src/api/routes/syslog.py` (`/api/v1/syslog/panels`) agrupa por
  familia de mensaje, reordena OLT (viene todo como `severity=err`) y ordena por
  `processed_at` (la hora del equipo en las OLT no es confiable)

## Correcciones de datos detectadas al verificar
- `src/api/routes/incidents.py`: `_calculate_duration()` dividía segundos entre
  60000 (ms) en vez de 60 → una incidencia de 5 días se mostraba como "7 min"
- Frontend: los logs relacionados del modal de syslog incluían todos los eventos
  del equipo (el `||` de "mismo dispositivo + mismo tag" dominaba el filtro por familia)
- Frontend: el puerto PON no se detectaba en los mensajes de OLT por las barras
  invertidas del colector (`0\/9`)

## Verificación realizada (navegador real, datos reales)
- Sidebar "Logs" con 4 indicadores reales (OLT alertas / advertencias / noticias,
  MikroTik noticias) y tooltips correctos
- Las 3 tarjetas de incidencias renderizan causa, impacto, equipo y hora reales
- Modal de incidencia: duración "5 d 1 h", tabla de puertos PON (GPON0/9 con 98 ONUs,
  69 online / 29 offline), tabla de clientes afectados (2 filas), logs relacionados
  filtrados por el mismo PON (incluye "ONU Dying Gasp" en GPON0/9, coherente con
  la causa de pérdida de energía) y fuentes consultadas
- Modal de syslog OLT: título mapeado ("ONU Dying Gasp (corte de energía)"),
  evidencia (repeticiones, primera vez, tag, origen, facility, hora del equipo,
  serial, puerto), tabla PON (GPON0/11) y cliente real encontrado por serial ONU
  (nombre, documento, MAC, Rx dBm, estado)
- Modal de syslog MikroTik: "Host recuperado (netwatch)", 1262 repeticiones y
  logs relacionados de la misma familia
- Acciones probadas end-to-end: `Reconocer` (backend `reconocida=1`, modal RECONOCIDA),
  `Silenciar 1 h` (`silenciada_hasta` = +1 h) y `Resolver` (estado/severidad actualizados,
  la tarjeta pasa a NOTICIA y el resumen se recalcula). El estado de Redis de la
  incidencia probada se restauró desde un respaldo previo
- Rango 1/6/24 h, selector de equipo y filtro por gravedad verificados
- Regresión: las 7 pestañas renderizan sin errores de consola
- `node --check` del bloque `<script>` y balance de etiquetas HTML idéntico a HEAD

## Backups
- `src/api/static/index.html.bak` (creado antes de los cambios)
