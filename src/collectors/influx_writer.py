# src/collectors/influx_writer.py
import logging
import threading
from collections import deque
from typing import List, Dict, Any, Optional
from ..storage.influx_client import InfluxClient, MetricPoint
from .base import CollectorResult

logger = logging.getLogger(__name__)


class CollectorInfluxWriter:
    """Writer thread-safe: buffer + escrituras accesibles desde varios threads
    (workers de MikroTik/Ping y escaneo OLT paralelo).

    Las metricas se escriben aunque result.success sea False (los errors son
    avisos parciales, p.ej. FDB vacia); no descartar datos por eso.
    """
    def __init__(self, influx_client: InfluxClient, max_buffer_size: int = 10000):
        self.influx = influx_client
        self._write_count = 0
        self._error_count = 0
        self._buffer: deque[MetricPoint] = deque(maxlen=max_buffer_size)
        self._lock = threading.RLock()

    def _flush_buffer_locked(self) -> int:
        """Vacia el buffer a InfluxDB. Debe llamarse con el lock adquirido."""
        if not self._buffer:
            return 0

        batch = list(self._buffer)
        written = self.influx.write_points(batch)
        if written > 0:
            for _ in range(written):
                self._buffer.popleft()
            if written < len(batch):
                logger.warning(
                    f"Flush parcial: {written}/{len(batch)} puntos; "
                    f"{len(batch) - written} quedan en buffer"
                )
            self._write_count += written
            logger.info(f"Flushed {written} buffered points to InfluxDB")
            return written
        logger.warning(f"Flush fallo: {len(batch)} puntos siguen en buffer")
        return 0

    def write_collector_result(self, result: CollectorResult) -> int:
        if not result.metrics:
            return 0

        points = []
        for metric in result.metrics:
            point = MetricPoint(
                measurement=metric["measurement"],
                tags=metric.get("tags", {}),
                fields=metric.get("fields", {})
            )
            points.append(point)

        if not points:
            return 0

        with self._lock:
            # Primero intentar vaciar el buffer si tiene puntos guardados
            if self._buffer:
                self._flush_buffer_locked()

            written = self.influx.write_points(points)
            if written > 0:
                self._write_count += written
                logger.info(
                    f"Wrote {written}/{len(points)} points for {result.device_name}"
                )
                if written < len(points):
                    # Solo quedan en buffer los no escritos (chunks secuenciales)
                    self._buffer.extend(points[written:])
            else:
                self._error_count += 1
                logger.warning(
                    f"Failed to write {len(points)} points for {result.device_name}, "
                    f"buffering..."
                )
                self._buffer.extend(points)
            return written

    def write_metrics(self, measurement: str, tags: Dict[str, str],
                      fields: Dict[str, Any], timestamp: Optional[int] = None) -> bool:
        point = MetricPoint(
            measurement=measurement,
            tags=tags,
            fields=fields,
            timestamp=timestamp
        )
        with self._lock:
            if self._buffer:
                self._flush_buffer_locked()

            success = self.influx.write_point(point)
            if not success:
                self._buffer.append(point)
                logger.warning(f"Failed to write metric {measurement}, buffering point...")
        return success

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "write_count": self._write_count,
                "error_count": self._error_count,
                "buffered_points": len(self._buffer)
            }
