# src/monitor.py
import re
import time
import logging
from typing import Optional, List, Dict, Tuple
from .models import Event
from .buffer import EventBuffer
from .zabbix_client import ZabbixClient
from .config import Config

logger = logging.getLogger(__name__)

STATUS_PATTERN = re.compile(r'Status\s+([\w\-\/]+):(\d+)\s+-\s+(\w+)\s+-\s+(\w+)')

class Monitor:
    def __init__(self, config: Config, client: ZabbixClient, buffer: EventBuffer):
        self.config = config
        self.client = client
        self.buffer = buffer
        self._hosts_cache: Dict[str, Dict] = {}
        self._last_cache_refresh = 0
    
    def _refresh_hosts_cache(self) -> None:
        now = time.time()
        if now - self._last_cache_refresh < 300:
            return
        
        hosts = self.client.get_hosts()
        self._hosts_cache = {h["name"]: h for h in hosts}
        self._last_cache_refresh = now
        logger.debug(f"Hosts cache refreshed: {len(hosts)} hosts")
    
    def _parse_item_name(self, item_name: str) -> Optional[Tuple[str, str, str, str]]:
        match = STATUS_PATTERN.search(item_name)
        if not match:
            return None
        return match.groups()
    
    def _is_fault_value(self, value: str) -> bool:
        return value in ("1", "4")
    
    def check_events(self) -> Optional[List[Event]]:
        self._refresh_hosts_cache()
        
        time_from = int(time.time() - self.config.monitoring.threshold_window_minutes * 60)
        problems = self.client.get_problems(time_from=time_from)
        
        triggered_events = []
        
        for problem in problems:
            event_name = problem.get("name", "")
            
            hosts = self.client.get_event_hosts(problem["eventid"])
            
            for host in hosts:
                host_name = host.get("name", "")
                
                parsed = self._parse_item_name(event_name)
                if not parsed:
                    continue
                
                pon_port, onu_num, serial, modelo = parsed
                
                event = Event(
                    olt_name=host_name,
                    olt_ip=host.get("interfaces", [{}])[0].get("ip", ""),
                    puerto_pon=f"{pon_port}",
                    timestamp=float(problem.get("clock", time.time())),
                    tipo=1 if "LOS" in event_name else 4,
                    item_id=problem["eventid"],
                    serial_onu=serial,
                    modelo_onu=modelo
                )
                
                self.buffer.add_event(event)
                triggered_events.append(event)
        
        if not triggered_events:
            return None
        
        groups = self.buffer.group_by_pon()
        for (olt_name, pon), events in groups.items():
            if len(events) >= self.config.monitoring.threshold_events:
                logger.warning(f"Threshold exceeded: {olt_name}:{pon} has {len(events)} events")
                return triggered_events
        
        return None
    
    def run_once(self) -> Optional[List[Event]]:
        self.buffer.cleanup()
        return self.check_events()
