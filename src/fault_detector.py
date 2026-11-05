# src/fault_detector.py
"""
FaultDetector: detección de fallas agregadas por NAP / OLT (broadcast).

Línea base: estado de cada cliente enriquecido proviene de `clientes_enriquecidos`
(enriquecido previamente desde `onu_location`, que ya incluye `estado_onu`,
donde 3 = Online). Cuando un NAP (o una OLT entera) supera el umbral de clientes
con estado != 3, se dispara UNA alerta agregada en lugar de alertas individuales.

Si `refresh_after_grouping` está activo, tras agrupar se hace un *pull* puntual
de `estado_onu` por SNMP sobre las OLTs de los grupos candidatos (patrón del
antiguo verifier.py) para confirmar antes de alertar. Si el pull falla, se
mantiene la línea base.

Recuperaciones: cuando un grupo vuelve a estar bajo el umbral se envía una
alerta de recuperación con la duración de la caída (LastStateStore dedicado).
"""
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field

from .alert_state import LastStateStore
from .config_ia import FaultDetectionConfig

logger = logging.getLogger(__name__)

# OID estado ONU (3 = Online) - igual que OltCollector
OID_ONU_STATUS = "1.3.6.1.4.1.37950.1.1.6.1.1.1.1.5"

# Campos de clientes_enriquecidos que consume el detector. Un `last()` plano
# devuelve UNA fila (1 campo aleatorio) por serie porque los campos son
# registros separados en Flux; se consulta `last()` por campo y las filas se
# fusionan por tags en _clients_from_rows.
_CLIENT_FIELDS = ("estado_onu", "nombre", "onu_index")


def _norm_state(estado) -> Optional[int]:
    """Normaliza un estado a int, o None si es desconocido (-1 / vacío)."""
    if estado is None:
        return None
    try:
        v = int(estado)
    except (ValueError, TypeError):
        return None
    if v in (-1, 0):
        return None
    return v


# ---------------------------------------------------------------------------
# Lógica pura de agrupación (sin I/O, testeable)
# ---------------------------------------------------------------------------

@dataclass
class NAPGroup:
    key: str
    olt: str
    label: str
    clients: List[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.clients)

    @property
    def down(self) -> int:
        return sum(1 for c in self.clients if c.get("_down"))

    @property
    def unknown(self) -> int:
        return sum(1 for c in self.clients if c.get("_unknown"))

    def names(self, limit: int = 10) -> List[str]:
        known = [c.get("nombre") or c.get("documento") or "" for c in self.clients
                 if c.get("_down") and (c.get("nombre") or c.get("documento"))]
        return [n for n in known if n][:limit]


@dataclass
class OLTGroup:
    olt: str
    clients: List[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.clients)

    @property
    def down(self) -> int:
        return sum(1 for c in self.clients if c.get("_down"))

    @property
    def unknown(self) -> int:
        return sum(1 for c in self.clients if c.get("_unknown"))


def _is_broadcast(olt_group: OLTGroup, config: FaultDetectionConfig) -> bool:
    known = max(olt_group.total - olt_group.unknown, 0)
    if known == 0 or olt_group.down < config.threshold_clients_los:
        return False
    return (olt_group.down / known) >= config.broadcast_down_ratio


def analyze_network(clients: List[dict],
                    config: FaultDetectionConfig) -> (Dict[str, NAPGroup], Dict[str, OLTGroup]):
    """Agrupa clientes por NAP (caja) y por OLT. Marca _down/_unknown por cliente."""
    nap_groups: Dict[str, NAPGroup] = {}
    olt_groups: Dict[str, OLTGroup] = {}

    for c in clients:
        estado = _norm_state(c.get("estado_onu"))
        c["_down"] = estado in (config.status_down_values or [])
        c["_unknown"] = estado is None

        olt = (c.get("olt") or c.get("nodo") or "").strip()
        if not olt:
            continue

        nap = (c.get("nap") or "").strip()
        pon = (c.get("pon_port") or "").strip()
        label = nap if nap else f"PON {pon}".strip()
        group_key = f"{olt}::{label}"

        ng = nap_groups.setdefault(group_key, NAPGroup(key=group_key, olt=olt, label=label))
        ng.clients.append(c)

        og = olt_groups.setdefault(olt, OLTGroup(olt=olt))
        og.clients.append(c)

    return nap_groups, olt_groups


def candidate_groups(nap_groups: Dict[str, NAPGroup],
                     olt_groups: Dict[str, OLTGroup],
                     config: FaultDetectionConfig) -> (List[NAPGroup], Set[str]):
    """Grupos NAP que superan el umbral, excluyendo los de OLTs en broadcast.

    Devuelve (candidatos NAP, OLTs en broadcast). Los candidatos de NAPs en una
    OLT en broadcast se suprimen (la alerta es una sola, agregada a nivel OLT).
    """
    broadcast: Set[str] = {
        olt for olt, og in olt_groups.items() if _is_broadcast(og, config)
    }
    candidates = [
        g for g in nap_groups.values()
        if g.olt not in broadcast and g.down >= config.threshold_clients_los
    ]
    candidates.sort(key=lambda g: g.down, reverse=True)
    return candidates, broadcast


# ---------------------------------------------------------------------------
# Pull puntual de estado ONU por SNMP (confirmación tras agrupar)
# ---------------------------------------------------------------------------

class OltStatusRefresher:
    """snmpbulkwalk de estado ONU (OID) sobre una OLT -> {idx_dot: int}."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def fetch(self, ip: str, community: str) -> Dict[str, int]:
        if not shutil.which("snmpbulkwalk"):
            return {}
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".snmp", delete=False)
            tmp.close()
            proc = subprocess.run(
                ["snmpbulkwalk", "-v2c", "-c", community,
                 "-Cr100", "-t", "10", "-r", "0", "-On",
                 ip, OID_ONU_STATUS],
                stdout=open(tmp.name, "w"),
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
            )
            result: Dict[str, int] = {}
            with open(tmp.name, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    oid_part, _, val_part = line.partition("=")
                    oid_str = oid_part.strip().lstrip(".")
                    if not oid_str.startswith(OID_ONU_STATUS):
                        continue
                    suffix = oid_str[len(OID_ONU_STATUS) + 1:]
                    value = val_part.split(":", 1)[-1].strip().strip('"')
                    try:
                        result[suffix] = int(value)
                    except (ValueError, TypeError):
                        continue
            return result
        except Exception as e:
            logger.debug(f"SNMP status refresh failed for {ip}: {e}")
            return {}
        finally:
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

    @staticmethod
    def onu_suffix(onu_index: str) -> str:
        """'3_01' -> '3.01' (formato idx_dot del walk de estado ONU)."""
        parts = onu_index.split("_")
        if len(parts) == 2:
            return f"{parts[0].strip()}.{parts[1].strip()}"
        return onu_index.strip()


# ---------------------------------------------------------------------------
# Detector (orquestador)
# ---------------------------------------------------------------------------

@dataclass
class FaultStats:
    clients: int = 0
    nap_candidates: int = 0
    broadcast_olts: int = 0
    alerts_down: int = 0
    alerts_recovered: int = 0
    refresh_confirms: int = 0
    refresh_fail: int = 0


class FaultDetector:
    def __init__(self, influx, notifier, config: FaultDetectionConfig,
                 olt_lookup: Dict[str, tuple] = None,
                 refresher: Optional[OltStatusRefresher] = None,
                 group_analyzer: Optional[Callable] = None):
        """olt_lookup: {olt_name: (ip, community)}. El detector es síncrono.
        group_analyzer: callable(serials) -> resultado de group_analysis
        (afectados con dirección) para enriquecer la alerta agregada."""
        self.influx = influx
        self.notifier = notifier
        self.config = config
        self.olt_lookup = olt_lookup or {}
        self.refresher = refresher or OltStatusRefresher(config.refresh_timeout_seconds)
        self.group_analyzer = group_analyzer
        self._state = LastStateStore(config.state_file)
        self._notify = notifier is not None
        self._refresh_rotation = 0

    # ----- lectura de clientes enriquecidos -----
    def _load_clients(self) -> List[dict]:
        results = []
        for field in _CLIENT_FIELDS:
            flux = (
                f'from(bucket: "{self.influx.bucket}")\n'
                f'  |> range(start: -{self.config.onu_lookback_minutes}m)\n'
                f'  |> filter(fn: (r) => r["_measurement"] == "clientes_enriquecidos")\n'
                f'  |> filter(fn: (r) => r["_field"] == "{field}")\n'
                f'  |> last()\n'
                f'  |> yield(name: "fault")'
            )
            results.extend(self.influx.query(flux) or [])
        return _clients_from_rows(results)

    # ----- ejecución -----
    def run(self) -> FaultStats:
        stats = FaultStats()
        if not self.config.enabled:
            return stats
        if not self.influx._query_api and not self.influx.connect():
            logger.error("InfluxDB no disponible; detección de fallas omitida")
            return stats

        clients = self._load_clients()
        stats.clients = len(clients)
        if not clients:
            logger.info("Sin clientes enriquecidos; no se evalúan fallas")
            return stats

        nap_groups, olt_groups = analyze_network(clients, self.config)
        candidates, broadcast = candidate_groups(nap_groups, olt_groups, self.config)
        stats.nap_candidates = len(candidates)
        stats.broadcast_olts = len(broadcast)

        # Refresh puntual tras agrupar (solo sobre OLTs de candidatos/broadcast)
        if self.config.refresh_after_grouping and (candidates or broadcast):
            refreshed = self._refresh_groups(candidates, broadcast)
            if refreshed:
                stats.refresh_confirms += 1
                nap_groups, olt_groups = analyze_network(clients, self.config)
                candidates, broadcast = candidate_groups(nap_groups, olt_groups, self.config)
                stats.nap_candidates = len(candidates)
                stats.broadcast_olts = len(broadcast)
            else:
                stats.refresh_fail += 1

        # Estado en el ciclo actual por clave de alerta
        down_keys: Dict[str, dict] = {}
        for g in candidates:
            down_keys[f"fault:nap:{g.olt}:{g.label}"] = {
                "olt": g.olt, "label": g.label, "down": g.down, "total": g.total,
                "names": g.names(), "scope": "nap",
                "serials": [c.get("serial_onu") for c in g.clients
                            if c.get("serial_onu")],
            }
        for olt in broadcast:
            og = olt_groups[olt]
            down_keys[f"fault:olt:{olt}"] = {
                "olt": olt, "label": olt, "down": og.down, "total": og.total,
                "names": _olt_names(og, 8), "scope": "olt",
                "serials": [c.get("serial_onu") for c in og.clients
                            if c.get("serial_onu")],
                "clients": og.clients,
            }

        stats.alerts_down = self._emit_down(down_keys)
        stats.alerts_recovered = self._emit_recoveries(down_keys)
        return stats

    def _refresh_groups(self, candidates: List[NAPGroup],
                        broadcast: Set[str]) -> bool:
        """Pull SNMP de estado ONU sobre OLTs candidatas y re-marca clientes.

        Devuelve True si al menos una OLT respondió.
        """
        olt_to_refresh = sorted({g.olt for g in candidates}.union(broadcast))

        # Techo por corrida con rotación: cada OLT refrescada cuesta 30-90 s
        # de SNMP secuencial; sin techo, 8 OLTs candidatas = ~10 min que
        # desbordan la ventana del job. Rotando el inicio, todas las OLTs se
        # cubren en ceil(N/max) corridas consecutivas.
        cap = max(1, int(getattr(self.config, "max_olts_refresh", 3) or 3))
        if len(olt_to_refresh) > cap:
            start = self._refresh_rotation % len(olt_to_refresh)
            olt_to_refresh = (olt_to_refresh[start:] +
                              olt_to_refresh[:start])[:cap]
            self._refresh_rotation = (start + cap) % max(1, len(olt_to_refresh))

        status_map: Dict[str, Dict[str, int]] = {}
        for olt in olt_to_refresh:
            lookup = self.olt_lookup.get(olt)
            if not lookup:
                logger.warning(f"Refresh SNMP: sin IP/community para OLT {olt}")
                continue
            ip, community = lookup
            status = self.refresher.fetch(ip, community)
            if status:
                status_map[olt] = status
                logger.info(f"Refresh SNMP {olt}: {len(status)} ONUs")
            else:
                logger.warning(f"Refresh SNMP {olt}: sin respuesta")

        if not status_map:
            return False

        def _reapply(client: dict):
            idx = self.refresher.onu_suffix((client.get("onu_index") or ""))
            if not idx:
                return
            st = status_map.get(client.get("olt") or client.get("nodo") or "")
            if not st or idx not in st:
                return
            estado = _norm_state(st[idx])
            client["estado_onu"] = estado
            client["_down"] = estado in (self.config.status_down_values or [])
            client["_unknown"] = estado is None

        for group in candidates:
            for client in group.clients:
                _reapply(client)

        return True

    # ----- alertas -----
    def _emit_down(self, down_keys: Dict[str, dict]) -> int:
        sent = 0
        for key, info in down_keys.items():
            if not self._state.should_send(key, {"status": "down"}):
                continue
            if not self._notify:
                continue
            extra = {}
            # `serials`/`clients` son para el analyzer, no para el notifier:
            # despacharlos via **info rompia send_area_alert (kw inesperado).
            serials = info.get("serials")
            if self.group_analyzer and serials:
                try:
                    res = self.group_analyzer(list(serials))
                    if res:
                        extra["affected"] = res.get("afectados", [])
                        extra["scope_detail"] = res.get("scope") or ""
                except Exception as e:
                    logger.warning(f"group_analyzer falló: {e}")
            notify_kwargs = {k: v for k, v in info.items()
                             if k not in ("serials", "clients")}
            if self.notifier.send_area_alert(
                **notify_kwargs, kind="down", duration_minutes=0, **extra,
            ):
                self._state.record(key, {"status": "down"},
                                   down_since=time.time())
                sent += 1
        return sent

    def _emit_recoveries(self, down_keys: Dict[str, dict]) -> int:
        sent = 0
        current = set(down_keys.keys())
        entries = self._state._data
        for key, entry in list(entries.items()):
            if key not in current and entries.get(key, {}).get("state", {}).get("status") == "down":
                down_since = entries[key].get("down_since")
                duration = int((time.time() - down_since) // 60) if down_since else 0
                if self._notify:
                    info = _recovery_info(key, down_since=down_since)
                    if self.notifier.send_area_alert(
                        **info, kind="recovery", duration_minutes=duration,
                    ):
                        sent += 1
                        self._state.record(key, {"status": "up"})
                else:
                    self._state.record(key, {"status": "up"})
        return sent


def _olt_names(og: OLTGroup, limit: int = 8) -> List[str]:
    names = []
    for c in og.clients:
        if c.get("_down"):
            n = c.get("nombre") or c.get("documento") or ""
            if n:
                names.append(n)
    return names[:limit]


def _recovery_info(key: str, down_since: Optional[float]) -> dict:
    """Parseo de claves de fault_state para generar información de recuperación.

    Formatos soportados:
    - fault:olt:<olt_name>      → scope="olt", label=<olt_name>
    - fault:nap:<olt>:<label>   → scope="nap", label=<label>
    """
    parts = key.split(":")

    # Validar formato básico: debe empezar con "fault"
    if len(parts) < 3 or parts[0] != "fault":
        return {"olt": "unknown", "label": "unknown", "scope": "unknown",
                "down": 0, "total": 0, "names": []}

    # Detectar tipo de clave por el segundo componente
    if parts[1] == "olt":
        # Formato: fault:olt:<olt_name>
        olt_name = parts[2] if len(parts) > 2 else "unknown"
        return {"olt": olt_name, "label": olt_name, "scope": "olt",
                "down": 0, "total": 0, "names": []}
    elif parts[1] == "nap" and len(parts) >= 4:
        # Formato: fault:nap:<olt_name>:<label>
        return {"olt": parts[2], "label": parts[3], "scope": "nap",
                "down": 0, "total": 0, "names": []}
    else:
        # Formato desconocido o malformado
        return {"olt": parts[2] if len(parts) > 2 else "unknown",
                "label": "unknown", "scope": "unknown",
                "down": 0, "total": 0, "names": []}


def _clients_from_rows(results: List[dict]) -> List[dict]:
    """Convierte filas de InfluxClient.query() en clientes fusionados por MAC."""
    meta = {"measurement", "field", "value", "time"}
    rows: Dict[str, dict] = {}
    order: Dict[str, float] = {}
    for r in results:
        tags = {k: v for k, v in r.items() if k not in meta}
        key = str(tags.get("mac") or f"doc:{tags.get('documento')}").strip()
        ts = r.get("time")
        ts_key = 0.0
        if hasattr(ts, "timestamp"):
            ts_key = float(ts.timestamp())
        elif ts is not None:
            try:
                ts_key = float(ts)
            except (ValueError, TypeError):
                ts_key = 0.0
        if key in order and ts_key < order[key]:
            continue
        row = rows.setdefault(key, dict(tags))
        row[r.get("field", "")] = r.get("value")
        order[key] = ts_key
    return list(rows.values())