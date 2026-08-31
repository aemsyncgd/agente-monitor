import os
import re
import subprocess
import logging
import time
import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from .base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)


@dataclass
class PingTarget:
    name: str
    ip: str
    target_type: str  # mikrotik, olt, dns
    enabled: bool = True
    timeout_ms: int = 3000
    count: int = 3


class PingCollector(BaseCollector):
    def __init__(self, targets: List[PingTarget] = None, config_path: str = None):
        super().__init__("ping_collector")
        self.targets = targets or []
        self._previous_status: Dict[str, bool] = {}
        self._downtime_start: Dict[str, float] = {}
        self._downtime_threshold = 120  # 2 minutes to trigger alert

        if config_path and not targets:
            self.targets = self._load_targets(config_path)

    def _load_targets(self, config_path: str) -> List[PingTarget]:
        path = Path(config_path)
        if not path.exists():
            logger.warning(f"Ping targets config not found: {config_path}")
            return []

        with open(path) as f:
            data = yaml.safe_load(f)

        targets = []
        for t in data.get("targets", []):
            if t.get("enabled", True):
                targets.append(PingTarget(
                    name=t["name"],
                    ip=t["ip"],
                    target_type=t.get("type", "unknown"),
                    timeout_ms=t.get("timeout_ms", 3000),
                    count=t.get("count", 3),
                ))

        logger.info(f"Loaded {len(targets)} ping targets")
        return targets

    def collect(self) -> CollectorResult:
        if not self.targets:
            return CollectorResult(
                success=False, metrics=[], errors=["No ping targets configured"],
                duration_seconds=0, device_name="ping_collector", device_ip=""
            )

        start = time.time()
        errors = []
        metrics = []

        enabled_targets = [t for t in self.targets if t.enabled]
        if not enabled_targets:
            return CollectorResult(
                success=False, metrics=[], errors=["No enabled targets"],
                duration_seconds=0, device_name="ping_collector", device_ip=""
            )

        for target in enabled_targets:
            try:
                result = self._ping_target(target)
                if result:
                    metrics.append(result)
                else:
                    errors.append(f"{target.name}: no result")
            except Exception as e:
                logger.error(f"Ping failed for {target.name}: {e}")
                errors.append(f"{target.name}: {e}")

        self._record_success(time.time() - start)

        return CollectorResult(
            success=True,
            metrics=metrics,
            errors=errors,
            duration_seconds=time.time() - start,
            device_name="ping_collector",
            device_ip=""
        )

    def _ping_target(self, target: PingTarget) -> Optional[Dict[str, Any]]:
        try:
            output = subprocess.run(
                ["fping", "-c", str(target.count), "-t", str(target.timeout_ms),
                 "-q", "--timeout", str(target.timeout_ms), target.ip],
                capture_output=True, text=True,
                timeout=(target.timeout_ms * target.count / 1000) + 10
            )

            stdout = output.stdout.strip()
            stderr = output.stderr.strip()

            if not stdout and not stderr:
                return self._build_down_metric(target, target.timeout_ms)

            stats_line = stdout if stdout else stderr
            return self._parse_fping_output(target, stats_line)

        except subprocess.TimeoutExpired:
            logger.warning(f"Ping timeout for {target.name}")
            return self._build_down_metric(target, target.timeout_ms)

        except FileNotFoundError:
            logger.error("fping not found")
            return None

        except Exception as e:
            logger.error(f"Ping error for {target.name}: {e}")
            return None

    def _parse_fping_output(self, target: PingTarget, stats_line: str) -> Dict[str, Any]:
        loss_match = re.search(r'xmt/rcv/%loss = (\d+)/(\d+)/(\d+)%', stats_line)
        latency_match = re.search(r'min/avg/max = ([\d.]+)/([\d.]+)/([\d.]+)', stats_line)

        if not loss_match:
            return self._build_down_metric(target, 0)

        xmt = int(loss_match.group(1))
        rcv = int(loss_match.group(2))
        loss = int(loss_match.group(3))

        if latency_match:
            min_ms = float(latency_match.group(1))
            avg_ms = float(latency_match.group(2))
            max_ms = float(latency_match.group(3))
        else:
            min_ms = avg_ms = max_ms = 0.0

        is_up = rcv > 0
        now = time.time()

        # Track status transitions
        prev = self._previous_status.get(target.name)
        self._previous_status[target.name] = is_up

        status_changed = False
        if prev is not None and prev != is_up:
            status_changed = True

        if is_up:
            self._downtime_start.pop(target.name, None)
            was_down = False
            downtime_seconds = 0.0
        else:
            if target.name not in self._downtime_start:
                self._downtime_start[target.name] = now
            was_down = True
            downtime_seconds = now - self._downtime_start[target.name]

        return self._build_metric(
            target, is_up, min_ms, avg_ms, max_ms,
            loss, xmt, rcv,
            status_changed=status_changed,
            was_down=was_down,
            downtime_seconds=downtime_seconds,
        )

    def _build_down_metric(self, target: PingTarget, timeout_ms: int) -> Dict[str, Any]:
        now = time.time()
        is_up = False

        prev = self._previous_status.get(target.name)
        self._previous_status[target.name] = is_up
        status_changed = prev is not None and prev != is_up

        if target.name not in self._downtime_start:
            self._downtime_start[target.name] = now

        downtime_seconds = now - self._downtime_start[target.name]

        return self._build_metric(
            target, is_up, 0.0, 0.0, 0.0,
            100, 1, 0,
            status_changed=status_changed,
            was_down=True,
            downtime_seconds=downtime_seconds,
        )

    def _build_metric(self, target: PingTarget,
                      is_up: bool, min_ms: float, avg_ms: float, max_ms: float,
                      loss_pct: int, xmt: int, rcv: int,
                      status_changed: bool = False,
                      was_down: bool = False,
                      downtime_seconds: float = 0.0) -> Dict[str, Any]:
        now = int(time.time())

        return {
            "measurement": "ping_check",
            "tags": {
                "name": target.name,
                "ip": target.ip,
                "type": target.target_type,
            },
            "fields": {
                "status": 1 if is_up else 0,
                "latency_ms_min": min_ms,
                "latency_ms_avg": avg_ms,
                "latency_ms_max": max_ms,
                "packet_loss_pct": loss_pct,
                "packets_sent": xmt,
                "packets_received": rcv,
                "status_changed": status_changed,
                "was_down": was_down,
                "downtime_seconds": round(downtime_seconds, 1),
            },
            "timestamp": now,
        }

    def get_status_summary(self) -> Dict[str, Any]:
        up = sum(1 for s in self._previous_status.values() if s)
        down = sum(1 for s in self._previous_status.values() if not s)
        return {
            "total": len(self._previous_status),
            "up": up,
            "down": down,
            "targets": dict(self._previous_status),
            "downtime_threshold": self._downtime_threshold,
        }
