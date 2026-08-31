# src/collectors/base.py
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    success: bool
    metrics: List[Dict[str, Any]]
    errors: List[str]
    duration_seconds: float
    device_name: str
    device_ip: str


class BaseCollector(ABC):
    def __init__(self, name: str):
        self.name = name
        self._last_collect = 0
        self._collect_count = 0
        self._error_count = 0
    
    @abstractmethod
    def collect(self) -> CollectorResult:
        pass
    
    def _measure_time(self, func, *args, **kwargs):
        start = time.time()
        try:
            result = func(*args, **kwargs)
            duration = time.time() - start
            return result, duration
        except Exception as e:
            duration = time.time() - start
            logger.error(f"{self.name}: Error after {duration:.2f}s: {e}")
            raise
    
    def _record_success(self, duration: float):
        self._last_collect = time.time()
        self._collect_count += 1
        logger.debug(f"{self.name}: Collected in {duration:.2f}s")
    
    def _record_error(self):
        self._error_count += 1
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "collect_count": self._collect_count,
            "error_count": self._error_count,
            "last_collect": self._last_collect,
            "uptime": time.time() - self._last_collect if self._last_collect else 0
        }
