# src/collectors/mikrotik_templates.py
"""Plantillas de coleccion SNMP por modelo de MikroTik (estilo LibreNMS).

Cada plantilla define una matriz de estrategias SNMP en orden de prioridad
para CPU, memoria y temperatura. Se resuelve a partir del campo `modelo` del
nodo en nodes.yaml (o del sysDescr si el modelo no viene configurado).

Estrategias soportadas:
  cpu:
    - {"strategy": "mtxr_get", "oid": ...}  -> GET clasico (mtxrSystemCPU)
    - {"strategy": "hr_avg"}                -> walk HOST-RESOURCES, promedio de nucleos
  memory:
    - {"strategy": "mtxr_get", "oid_total": ..., "oid_free": ...}  -> GET clasico (bytes -> KB)
    - {"strategy": "hr_storage"}            -> walk hrStorage, entrada "main memory" (bytes -> KB)
  temperature:
    - {"strategy": "mtxr_health"}           -> tabla de sensores RouterOS v7 (mtxrHealth)
    - {"strategy": "mtxr_legacy"}           -> OIDs clasicos de temperatura
  iface_exclude_patterns:
    - regex (minusculas) de nombres de interfaz a OMITIR. Por defecto se
      excluyen las interfaces dinamicas "<pppoe-...>" (sesiones PPPoE de
      clientes), que son ruido y disparan el rate-limit del RouterOS en
      equipos con miles de sesiones.
"""

# Classic RouterOS MIB (mtxrSystemCPU)
MTXR_CPU_LOAD = "1.3.6.1.4.1.14988.1.1.3.1.0"
MTXR_TOTAL_MEM = "1.3.6.1.4.1.14988.1.1.3.2.0"
MTXR_FREE_MEM = "1.3.6.1.4.1.14988.1.1.3.3.0"

# HOST-RESOURCES MIB (siempre disponibles en RouterOS)
HR_PROCESSOR_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_TOTAL = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"

# RouterOS v7 sensor table (mtxrHealth)
MTXR_SENSOR_NAME = "1.3.6.1.4.1.14988.1.1.3.100.1.2"
MTXR_SENSOR_VALUE = "1.3.6.1.4.1.14988.1.1.3.100.1.3"

# Legacy temperature OIDs
MTXR_CPU_TEMP = "1.3.6.1.4.1.14988.1.1.10.1.1.2"
MTXR_DEVICE_TEMP = "1.3.6.1.4.1.14988.1.1.10.1.2.1.3"


CPU_MATRIX = [
    {"strategy": "mtxr_get", "oid": MTXR_CPU_LOAD},
    {"strategy": "hr_avg"},
]

MEM_MATRIX = [
    {
        "strategy": "mtxr_get",
        "oid_total": MTXR_TOTAL_MEM,
        "oid_free": MTXR_FREE_MEM,
    },
    {"strategy": "hr_storage"},
]

TEMP_MATRIX = [
    {"strategy": "mtxr_health"},
    {"strategy": "mtxr_legacy"},
]


DEFAULT_TEMPLATE = {
    "cpu": CPU_MATRIX,
    "memory": MEM_MATRIX,
    "temperature": TEMP_MATRIX,
    # Interfaces dinamicas (sesiones PPPoE de clientes) -> ruido + rate-limit.
    "iface_exclude_patterns": [
        r"^<",            # "<pppoe-..." (dinamicas)
        r"^pppoe-in",     # interfaces pppoe-in de servidor
    ],
}

# Registro por familia de modelo (prefijo en mayusculas). El campo `modelo`
# del nodo suele ser algo como "RB4011iGS+RM" o "CCR2116-12G-4S+".
MIKROTIK_TEMPLATES = {
    "CCR2116": DEFAULT_TEMPLATE,
    "CCR2004": DEFAULT_TEMPLATE,
    "CCR1036": DEFAULT_TEMPLATE,
    "RB4011": DEFAULT_TEMPLATE,
    "RB750": DEFAULT_TEMPLATE,
    "RB960": DEFAULT_TEMPLATE,
    "CRS305": DEFAULT_TEMPLATE,
    "default": DEFAULT_TEMPLATE,
}


def resolve_mikrotik_template(model: str) -> dict:
    """Resuelve la plantilla a usar segun la familia del modelo."""
    if not model:
        return MIKROTIK_TEMPLATES["default"]
    m = model.strip().upper()
    for family, template in MIKROTIK_TEMPLATES.items():
        if family == "default":
            continue
        if m.startswith(family):
            return template
    return MIKROTIK_TEMPLATES["default"]
