# src/collectors/olt_traffic_sampler.py
import logging
import time
from typing import List

from .olt_collector import OltCollector

logger = logging.getLogger("olt_traffic_sampler")


class OltTrafficSampler:
    """Muestrea trafico por interfaz de OLT (`olt_traffic_history`) en un thread
    propio, con cadencia independiente del ciclo completo de recoleccion (~15-20
    min). Una pasada recorre las OLTs de forma SECUENCIAL: las VSOL V1600 son
    fragiles al SNMP, 4 walks por OLT con pausas (walk_delay_seconds).

    Reconstruye los collectors cuando el ConfigManager recarga la config
    (mismo patron que `mikrotik_ping_worker`).
    """

    def __init__(self, config_manager, writer, interval_seconds: int = 300,
                 walk_delay_seconds: int = 3, olt_timeout: int = 90):
        self.config_manager = config_manager
        self.writer = writer
        self.interval_seconds = interval_seconds
        self.walk_delay_seconds = walk_delay_seconds
        self.olt_timeout = olt_timeout
        self._collectors: List[OltCollector] = []

    def rebuild_collectors(self) -> int:
        from .main import create_collectors_from_config
        new_olt, _ = create_collectors_from_config(
            self.config_manager, self.olt_timeout
        )
        for c in new_olt:
            c.mac_walk_delay = self.walk_delay_seconds
        self._collectors = new_olt
        logger.info(f"OltTrafficSampler: {len(self._collectors)} collectors")
        return len(self._collectors)

    def sample_once(self) -> None:
        """Una pasada completa de muestreo por todas las OLTs (secuencial)."""
        for collector in self._collectors:
            try:
                result = collector.sample_traffic()
                if result.metrics:
                    written = self.writer.write_collector_result(result)
                    logger.info(
                        f"{result.device_name}: {len(result.metrics)} samples "
                        f"({written} escritos) in {result.duration_seconds:.1f}s"
                    )
                if result.errors:
                    logger.warning(f"{result.device_name}: {result.errors}")
            except Exception as e:
                logger.error(f"Traffic sample failed {collector.config.hostname}: {e}")

    def run(self, running) -> None:
        """Loop principal: muestreo cada interval_seconds + reload de config."""
        try:
            n = self.rebuild_collectors()
            if n == 0:
                logger.warning("OltTrafficSampler sin OLTs en config")
        except Exception as e:
            logger.error(f"OltTrafficSampler init failed: {e}")

        last_sample = 0.0
        last_reload = time.time()

        while running.is_set():
            now = time.time()

            # Reload de config cada 5 min (entre pasadas)
            if now - last_reload > 300:
                try:
                    self.config_manager.reload_config()
                    self.rebuild_collectors()
                except Exception as e:
                    logger.error(f"OltTrafficSampler config reload failed: {e}")
                last_reload = time.time()
                now = time.time()

            if now - last_sample >= self.interval_seconds:
                self.sample_once()
                last_sample = time.time()

            time.sleep(min(5, max(1, self.interval_seconds)))