# src/client_lookup.py
"""
ClientLookup: Fast serial-to-Client lookup for AI agent alerting.

Loads grid_servicios.csv and builds in-memory indexes for O(1) lookups
by serial number and by OLT:PON port. Normalizes serials to lowercase
for case-insensitive matching with InfluxDB SNMP data.
"""
import os
import logging
import threading
import time
from typing import Dict, List, Optional

from .grouper import _parse_csv_robust
from .models import Client

logger = logging.getLogger(__name__)


class ClientLookup:
    """Thread-safe, fast lookup of clients by serial number or PON port."""

    def __init__(self):
        self._by_serial: Dict[str, Client] = {}
        self._by_pon: Dict[str, List[Client]] = {}
        self._all: List[Client] = []
        self._lock = threading.RLock()
        self._last_load: float = 0
        self._last_path: str = ""

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._all)

    @property
    def last_load(self) -> float:
        return self._last_load

    def load_from_csv(self, path: str) -> int:
        """
        Load clients from grid_servicios.csv.

        Returns number of clients loaded.
        """
        if not path or not os.path.exists(path):
            logger.warning(f"CSV not found: {path}")
            return 0

        df = _parse_csv_robust(path)
        df.columns = df.columns.str.strip()

        by_serial: Dict[str, Client] = {}
        by_pon: Dict[str, List[Client]] = {}
        all_clients: List[Client] = []

        for _, row in df.iterrows():
            serial_raw = str(row.get("Serial Onu", "")).strip()
            if not serial_raw or serial_raw == "nan":
                continue

            client = Client(
                nombre=str(row.get("Nombre Cliente", "")).strip(),
                serial_onu=serial_raw,
                olt="",
                nodo=str(row.get("Router", "")).strip(),
                puerto_pon=str(row.get("Puerto PON", row.get("Puerto PON ", ""))).strip(),
                direccion=str(row.get("Direccion Servicio", "")).strip(),
                documento=str(row.get("Documento", "")).strip(),
                ip_servicio=str(row.get("Ip Servicio", "")).strip(),
                estado=str(row.get("Estado", "")).strip(),
            )

            serial_key = serial_raw.lower()
            by_serial[serial_key] = client
            all_clients.append(client)

            # Index by OLT hostname + PON port (e.g. "NARANJILLOS1:PON5")
            pon_key = self._pon_key(client)
            if pon_key:
                if pon_key not in by_pon:
                    by_pon[pon_key] = []
                by_pon[pon_key].append(client)

        with self._lock:
            self._by_serial = by_serial
            self._by_pon = by_pon
            self._all = all_clients
            self._last_load = time.time()
            self._last_path = path

        logger.info(f"Loaded {len(all_clients)} clients from CSV ({path})")
        return len(all_clients)

    def lookup_by_serial(self, serial: str) -> Optional[Client]:
        """O(1) lookup by ONU serial (case-insensitive)."""
        if not serial:
            return None
        with self._lock:
            return self._by_serial.get(serial.lower())

    def clients_on_pon(self, olt_hostname: str, pon_port: str) -> List[Client]:
        """All clients on a specific OLT:PON port."""
        key = self._make_pon_key(olt_hostname, pon_port)
        with self._lock:
            return list(self._by_pon.get(key, []))

    def get_all(self) -> List[Client]:
        """Return all loaded clients."""
        with self._lock:
            return list(self._all)

    def search_by_name(self, name: str) -> List[Client]:
        """Partial match on client name (case-insensitive)."""
        if not name:
            return []
        q = name.lower()
        with self._lock:
            return [c for c in self._all if q in c.nombre.lower()]

    def search_by_address(self, address: str) -> List[Client]:
        """Partial match on address (case-insensitive)."""
        if not address:
            return []
        q = address.lower()
        with self._lock:
            return [c for c in self._all if q in c.direccion.lower()]

    def needs_reload(self, check_interval_hours: float = 24) -> bool:
        """Check if CSV needs periodic reload."""
        if self._last_load == 0:
            return True
        return (time.time() - self._last_load) > check_interval_hours * 3600

    def stats(self) -> Dict:
        """Return stats about loaded data."""
        with self._lock:
            onus = len(self._by_serial)
            pons = len(self._by_pon)
            active = sum(1 for c in self._all if c.estado.lower() == "activo")
            return {
                "total_clients": onus,
                "active_clients": active,
                "total_pons": pons,
                "last_load": self._last_load,
                "source": self._last_path,
            }

    @staticmethod
    def _make_pon_key(olt_hostname: str, pon_port: str) -> str:
        """Normalize PON key for indexing. E.g. 'OLT-NARANJILLOS-1:GPON0/5'."""
        return f"{olt_hostname}:{pon_port}".upper()

    @staticmethod
    def _pon_key(client: Client) -> str:
        """Extract PON key from client's puerto_pon field."""
        # CSV format: "NARANJILLOS1-PON5" or "GPON0/5"
        pp = client.puerto_pon
        if not pp:
            return ""
        # Normalize: remove hyphens, uppercase
        return pp.replace("-", "").upper()
