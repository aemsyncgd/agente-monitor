# Plan de Implementación: Mini-tarjetas de Incidencias

## Resumen Ejecutivo

**Objetivo**: Reemplazar la vista actual de UNA incidencia con una lista de mini-tarjetas coloridas que muestren múltiples incidencias críticas, con modal de detalles al hacer click.

**Estado actual**: Fase 0 (diseño visual) completada. Vista actual muestra `currentIncident` como un solo objeto derivado de anomalías.

**Tareas principales**:
1. Crear endpoint API `/api/v1/incidents` 
2. Modificar el frontend para mostrar lista de mini-tarjetas
3. Implementar modal de detalles con botón "Actualizar"
4. Integrar con el store.py existente

---

## Fase 1: Backend - Endpoint de Incidencias

### Archivo a crear
- `src/api/routes/incidents.py`

### Endpoints necesarios (según F1.3 de incidencias_docs.md)
- `GET /api/v1/incidents` - Listar incidencias con filtros
- `GET /api/v1/incidents/{id}` - Detalle de una incidencia
- `POST /api/v1/incidents/{id}/ack` - Reconocer incidencia
- `POST /api/v1/incidents/{id}/mute` - Silenciar incidencia

### Diseño del endpoint

```python
# GET /api/v1/incidents?limit=10
{
  "incidents": [
    {
      "id": "INC-2026-11-05-0001",
      "olt": "OLT-SISAL-1",
      "pon": "GPON0/9",
      "nap": "SISAL1",
      "estado": "open",
      "severidad": "critical",  // or "warning", "resolved"
      "inicio": 1730810527,
      "detectado": 1730810539,
      "fin": null,
      "causa": "Corte de fibra troncal",
      "confianza": 87.5,
      "evidencia": [...],
      "impacto": {
        "clientes": 100,
        "onus": 96,
        "pons": 1,
        "routers": 3
      },
      "afectados": [...],
      "fuentes": {...},
      "duracion": "27 min"
    }
  ]
}
```

---

## Fase 2: Frontend - Mini-tarjetas

### Cambios en `src/api/static/index.html`

#### 2.1 Variables reactivas nuevas
```javascript
// Lista de incidencias para mostrar en mini-tarjetas
const incidentList = ref([]);
const incidentListLoading = ref(false);
const selectedIncident = ref(null); // Para el modal
const showIncidentModal = ref(false);
```

#### 2.2 Computed para mini-tarjetas
```javascript
// Lista de incidencias con severity calculada
const incidentCards = computed(() => {
  return incidentList.value.map(inc => ({
    id: inc.id,
    title: inc.causa || 'Incidencia',
    olt: inc.olt,
    pon: inc.pon,
    severityLabel: inc.severidad?.toUpperCase() || 'OK',
    severityClass: getSeverityClass(inc.severidad),
    estadoLabel: getEstadoLabel(inc.estado),
    estadoClass: getEstadoClass(inc.estado),
    inicio: formatTime(inc.inicio),
    duracion: formatDuration(inc.inicio, inc.fin),
    clientes: inc.impacto?.clientes || 0,
    onus: inc.impacto?.onus || 0,
    confianza: inc.confianza || 0
  }));
});
```

#### 2.3 Funciones de estilo por severidad
```javascript
function getSeverityClass(severity) {
  switch(severity) {
    case 'critical': return 'bg-red-500/20 text-red-400 border-red-700/60';
    case 'warning': return 'bg-amber-500/20 text-amber-400 border-amber-700/60';
    case 'resolved': return 'bg-emerald-500/20 text-emerald-400 border-emerald-700/60';
    default: return 'bg-emerald-500/20 text-emerald-400';
  }
}
```

#### 2.4 Template de mini-tarjetas

```html
<!-- Mini-tarjetas de Incidencias -->
<div v-if="incidentCards.length" class="space-y-3">
  <div 
    v-for="inc in incidentCards" 
    :key="inc.id"
    @click="openIncidentModal(inc.id)"
    class="group cursor-pointer bg-surface-900/40 rounded-xl border p-4 transition-all hover:scale-[1.01] hover:shadow-lg"
    :class="inc.severityClass"
  >
    <div class="flex items-start justify-between gap-3">
      <!-- Badge de severidad -->
      <span class="text-[10px] uppercase px-2 py-0.5 rounded-full font-semibold"
            :class="inc.severityClass">
        {{ inc.severityLabel }}
      </span>
      
      <!-- Título -->
      <h3 class="text-sm font-semibold text-surface-100 line-clamp-2 flex-1">
        {{ inc.title }}
      </h3>
      
      <!-- Badge de estado -->
      <span class="text-[10px] px-2 py-0.5 rounded-full font-semibold"
            :class="inc.estadoClass">
        {{ inc.estadoLabel }}
      </span>
    </div>
    
    <!-- Meta -->
    <div class="mt-2 text-xs text-surface-400 space-y-1">
      <div class="flex items-center gap-2">
        <span class="font-mono">{{ inc.olt }}</span>
        <span>|</span>
        <span class="font-mono">{{ inc.pon }}</span>
      </div>
      <div class="flex items-center gap-3">
        <span>Inicio: {{ inc.inicio }}</span>
        <span>Duración: {{ inc.duracion }}</span>
        <span class="text-surface-500">·</span>
        <span>Clientes: {{ inc.clientes }}</span>
        <span class="text-surface-500">·</span>
        <span>ONUs: {{ inc.onus }}</span>
      </div>
    </div>
    
    <!-- Footer con confianza -->
    <div class="mt-3 pt-2 border-t border-surface-700/40 flex items-center justify-between">
      <span class="text-[10px] text-surface-500">Confianza: {{ inc.confianza }}%</span>
      <svg class="w-4 h-4 text-surface-500 group-hover:text-surface-300 transition-colors" 
           fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"/>
      </svg>
    </div>
  </div>
</div>
```

#### 2.5 Modal de detalles

```html
<!-- Modal de Detalle de Incidencia -->
<div v-if="showIncidentModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm">
  <div class="relative w-full max-w-4xl mx-4 bg-surface-900 rounded-2xl border border-surface-700 overflow-hidden">
    <!-- Header -->
    <div class="flex items-start justify-between p-5 border-b border-surface-700/40">
      <div class="space-y-1">
        <h3 class="text-xl font-bold text-surface-100">{{ modalIncident?.title }}</h3>
        <div class="flex items-center gap-2 text-sm text-surface-400">
          <span class="text-xs font-mono">{{ modalIncident?.id }}</span>
          <span>·</span>
          <span>Inicio: {{ modalIncident?.start }}</span>
          <span class="text-surface-500">·</span>
          <span>Duración: {{ modalIncident?.duration }}</span>
        </div>
      </div>
      <button @click="showIncidentModal = false" class="p-2 rounded-lg hover:bg-surface-800 transition-colors">
        <svg class="w-5 h-5 text-surface-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/>
        </svg>
      </button>
    </div>
    
    <!-- Body -->
    <div class="p-5 max-h-[70vh] overflow-y-auto">
      <!-- Severity, Estado, Cause, Confidence -->
      <div class="flex items-center gap-3 mb-4">
        <span class="text-xs font-semibold px-2 py-0.5 rounded-full uppercase"
              :class="modalIncident?.severityClass">{{ modalIncident?.severityLabel }}</span>
        <span class="text-xs font-semibold px-2 py-0.5 rounded-full" 
              :class="modalIncident?.estadoClass">{{ modalIncident?.estadoLabel }}</span>
      </div>
      
      <!-- Causa y Confianza -->
      <div class="mb-4">
        <h4 class="text-xs uppercase tracking-wider text-surface-500 mb-1">Causa probable</h4>
        <p class="text-sm text-surface-300 leading-relaxed">{{ modalIncident?.causa }}</p>
        <div class="mt-2 flex items-center gap-2">
          <span class="text-xs text-surface-400">Confianza <b class="text-surface-200">{{ modalIncident?.confidence }}</b></span>
          <span class="h-1.5 flex-1 max-w-[180px] rounded-full bg-surface-800 overflow-hidden">
            <span class="block h-full bg-brand-500 transition-all" 
                  :style="{ width: (modalIncident?.confidencePct || 0) + '%' }"></span>
          </span>
        </div>
      </div>
      
      <!-- Botón Actualizar -->
      <div class="mb-4">
        <button @click="updateIncidentData()" 
                class="text-xs px-3 py-1.5 rounded-lg bg-brand-600/40 border border-brand-500/40 text-brand-200 hover:bg-brand-600/50 transition-colors">
          Actualizar
        </button>
      </div>
      
      <!-- Tabla de clientes afectados -->
      <div>
        <h4 class="text-xs uppercase tracking-wider text-surface-500 mb-2">Clientes afectados</h4>
        <div class="rounded-lg border border-surface-700/50 overflow-hidden">
          <table class="w-full text-left">
            <thead class="bg-surface-800/60">
              <tr class="text-[10px] uppercase tracking-wider text-surface-500">
                <th class="px-3 py-2">#</th>
                <th class="px-3 py-2">Cliente</th>
                <th class="px-3 py-2">Dirección</th>
                <th class="px-3 py-2">PPPoE</th>
                <th class="px-3 py-2">MAC</th>
                <th class="px-3 py-2">Serial ONU</th>
                <th class="px-3 py-2">Rx</th>
                <th class="px-3 py-2">Estado</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-surface-800/60">
              <tr v-for="(r, i) in modalIncident?.afectados || []" :key="i" class="hover:bg-surface-800/30">
                <td class="px-3 py-2 text-xs font-mono text-surface-500">{{ i + 1 }}</td>
                <td class="px-3 py-2 text-sm text-surface-200">{{ r.cliente || '-' }}</td>
                <td class="px-3 py-2 text-xs text-surface-400">{{ r.direccion || '-' }}</td>
                <td class="px-3 py-2 text-xs font-mono text-surface-400">{{ r.pppoe || '-' }}</td>
                <td class="px-3 py-2 text-xs font-mono text-surface-400">{{ r.mac || '-' }}</td>
                <td class="px-3 py-2 text-xs font-mono text-surface-200">{{ r.serial || '-' }}</td>
                <td class="px-3 py-2 text-xs font-mono" :class="r.rxClass">{{ r.rx || '-' }}</td>
                <td class="px-3 py-2"><span class="text-[10px] px-2 py-0.5 rounded-full" :class="r.stateClass">{{ r.state || '-' }}</span></td>
              </tr>
              <tr v-if="(modalIncident?.afectados || []).length === 0">
                <td colspan="8" class="px-3 py-6 text-center text-xs text-surface-500">Sin clientes afectados identificados</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</div>
```

---

## Fase 3: Integración con API Existente

### Usar endpoint actual de anomalías

Como `/api/v1/incidents` aún no existe, usaremos los endpoints existentes:
- `/api/v1/anomalies/recent` - Para obtener anomalías recientes
- `/api/v1/anomalies/summary` - Para resumen
- `/api/v1/syslog/important` - Para syslog correlacionado

### Adaptar el flujo actual

El código actual `currentIncident` deriva de `incidentGroup`. Podemos convertir esto a una lista creando múltiples grupos por tiempo/OLT.

```javascript
// Nueva función para obtener lista de incidentes
async function fetchIncidentList() {
  incidentListLoading.value = true;
  try {
    // Usar datos existentes y agrupar por tiempo
    const anomalies = recentAnomalies.value || [];
    const predictions = predictions.value || [];
    const syslog = syslogAlerts.value || [];
    
    // Agrupar por ventana de tiempo (ej: 15 minutos)
    const incidentList = groupAnomaliesByTime(anomalies, predictions, syslog);
    incidentList.value = incidentList;
  } finally {
    incidentListLoading.value = false;
  }
}
```

---

## Fase 4: Colores por Severidad

| Severidad | Color de fondo | Color texto | Border | Badge |
|-----------|----------------|-------------|--------|-------|
| Critical | `bg-red-950/10` | `text-red-400` | `border-red-700/60` | `bg-red-500/20 text-red-400` |
| Warning | `bg-amber-950/10` | `text-amber-400` | `border-amber-700/60` | `bg-amber-500/20 text-amber-400` |
| OK/Resolved | `bg-emerald-950/10` | `text-emerald-400` | `border-emerald-700/60` | `bg-emerald-500/20 text-emerald-400` |

---

## Archivos a Modificar

1. **`src/api/static/index.html`** - Añadir mini-tarjetas y modal
2. **`src/incidents/store.py`** - Ya existe, usar para persistencia
3. **`src/api/main.py`** - Añadir router de incidents (cuando exista)

---

## Checklist de Implementación

### Backend
- [ ] Crear `src/api/routes/incidents.py` con endpoints básicos
- [ ] Registrar router en `src/api/main.py`
- [ ] Conectar con `src/incidents/store.py`

### Frontend
- [ ] Añadir variables reactivas para lista de incidentes
- [ ] Implementar computed `incidentCards`
- [ ] Crear template de mini-tarjetas
- [ ] Crear modal de detalles
- [ ] Añadir botón "Actualizar" al modal
- [ ] Implementar colores por severidad
- [ ] Manejar estado vacío (sin incidencias)

### Pruebas
- [ ] Verificar colores correctos
- [ ] Verificar funcionalidad de click
- [ ] Verificar actualización de datos
- [ ] Verificar en móviles

---

## Cronología Estimada

- **Día 1**: Crear endpoint API básico
- **Día 2**: Implementar mini-tarjetas en frontend
- **Día 3**: Implementar modal y botón actualizar
- **Día 4**: Pruebas y ajustes finales