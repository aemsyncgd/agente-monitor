# src/storage/influx_client.py
import time
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    measurement: str
    tags: Dict[str, str]
    fields: Dict[str, Any]
    timestamp: Optional[int] = None  # nanoseconds, None = now


class InfluxClient:
    def __init__(self, url: str, token: str, org: str, bucket: str):
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket
        self._client = None
        self._write_api = None
        self._query_api = None
    
    def connect(self) -> bool:
        try:
            from influxdb_client import InfluxDBClient
            from influxdb_client.client.write_api import SYNCHRONOUS
            
            self._client = InfluxDBClient(
                url=self.url,
                token=self.token,
                org=self.org,
                timeout=30_000
            )
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            self._query_api = self._client.query_api()
            
            logger.info(f"InfluxDB connected: {self.url}")
            return True
        except Exception as e:
            logger.error(f"InfluxDB connection failed: {e}")
            return False
    
    def write_point(self, point: MetricPoint) -> bool:
        try:
            from influxdb_client import Point as InfluxPoint
            
            p = InfluxPoint(point.measurement)
            for k, v in point.tags.items():
                p = p.tag(k, v)
            for k, v in point.fields.items():
                p = p.field(k, v)
            if point.timestamp:
                p = p.time(point.timestamp)
            
            self._write_api.write(bucket=self.bucket, record=p)
            return True
        except Exception as e:
            logger.error(f"InfluxDB write failed: {e}")
            return False
    
    def write_points(self, points: List[MetricPoint], batch_size: int = 250,
                     retries: int = 1) -> int:
        """Escribe puntos por chunks de batch_size; retorna cuantos se escribieron.

        Los chunks se procesan en orden: si un chunk falla tras retries, se
        retorna lo ya escrito y el resto queda en el buffer del writer.
        """
        if not points:
            return 0
        try:
            from influxdb_client import Point as InfluxPoint

            records = []
            for point in points:
                p = InfluxPoint(point.measurement)
                for k, v in point.tags.items():
                    p = p.tag(k, v)
                for k, v in point.fields.items():
                    if v is not None:
                        p = p.field(k, v)
                if point.timestamp:
                    p = p.time(point.timestamp)
                records.append(p)

            written = 0
            for start in range(0, len(records), batch_size):
                chunk = records[start:start + batch_size]
                for attempt in range(retries + 1):
                    try:
                        self._write_api.write(bucket=self.bucket, record=chunk)
                        written += len(chunk)
                        break
                    except Exception as e:
                        if attempt < retries:
                            time.sleep(1 * (attempt + 1))
                            continue
                        logger.error(
                            f"InfluxDB batch write failed "
                            f"(points {start}..{start + len(chunk)}): {e}"
                        )
                        return written
            return written
        except Exception as e:
            logger.error(f"InfluxDB batch write failed: {e}")
            return 0
    
    def query(self, flux: str) -> List[Dict]:
        try:
            tables = self._query_api.query(flux, org=self.org)
            results = []
            for table in tables:
                for record in table.records:
                    values = getattr(record, "values", {}) or {}
                    result = {
                        "measurement": values.get("_measurement", ""),
                        "field": values.get("_field", ""),
                        "value": values.get("_value"),
                    }
                    if "_time" in values:
                        result["time"] = values["_time"]
                    for k, v in values.items():
                        if k not in ("_time", "_measurement", "_field", "_value", "_result", "_start", "_stop"):
                            result[k] = v
                    results.append(result)
            return results
        except Exception as e:
            logger.error(f"InfluxDB query failed: {e}")
            return []
    
    def get_optical_power(self, olt_name: Optional[str] = None,
                          pon_port: Optional[str] = None,
                          hours: int = 24) -> List[Dict]:
        filters = []
        if olt_name:
            filters.append(f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")')
        if pon_port:
            filters.append(f'|> filter(fn: (r) => r["pon_port"] == "{pon_port}")')
        
        filter_str = "\n    ".join(filters) if filters else ""
        
        flux = f'''
        from(bucket: "{self.bucket}")
            |> range(start: -{hours}h)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            {filter_str}
            |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
            |> yield(name: "optical_power")
        '''
        return self.query(flux)
    
    def get_olt_system(self, olt_name: Optional[str] = None,
                       hours: int = 24) -> List[Dict]:
        filter_str = ""
        if olt_name:
            filter_str = f'|> filter(fn: (r) => r["olt_name"] == "{olt_name}")'
        
        flux = f'''
        from(bucket: "{self.bucket}")
            |> range(start: -{hours}h)
            |> filter(fn: (r) => r["_measurement"] == "olt_system")
            {filter_str}
            |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
            |> yield(name: "olt_system")
        '''
        return self.query(flux)
    
    def get_latest_optical(self, olt_name: str) -> List[Dict]:
        flux = f'''
        from(bucket: "{self.bucket}")
            |> range(start: -5m)
            |> filter(fn: (r) => r["_measurement"] == "optical_power")
            |> filter(fn: (r) => r["olt_name"] == "{olt_name}")
            |> last()
            |> yield(name: "latest")
        '''
        return self.query(flux)
    
    def close(self):
        if self._client:
            self._client.close()
