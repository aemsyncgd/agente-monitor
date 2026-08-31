# src/zabbix_client.py
# DEPRECATED: Zabbix integration is no longer used.
# This module will be removed in a future version.
# Kept temporarily for backward compatibility during migration.
import warnings
import requests
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

warnings.warn(
    "src.zabbix_client is DEPRECATED and will be removed. "
    "Zabbix integration is no longer used.",
    DeprecationWarning,
    stacklevel=2,
)


class ZabbixClient:
    def __init__(self, url: str, username: str, password: str, timeout: int = 30):
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.auth: Optional[str] = None
        self._request_id = 0
    
    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id
    
    def _call(self, method: str, params: Dict[str, Any], auth: bool = True) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self._next_id()
        }
        headers = {"Content-Type": "application/json-rpc"}
        if auth and self.auth:
            headers["Authorization"] = f"Bearer {self.auth}"
        
        try:
            resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            
            if "error" in data:
                error_msg = data["error"].get("data", data["error"].get("message", "Unknown"))
                logger.error(f"Zabbix API error on {method}: {error_msg}")
                return None
            
            return data.get("result")
        except requests.exceptions.RequestException as e:
            logger.error(f"Zabbix API request failed: {e}")
            return None
    
    def login(self) -> Optional[str]:
        params = {"username": self.username, "password": self.password}
        result = self._call("user.login", params, auth=False)
        if result:
            self.auth = result
            logger.info("Zabbix login successful")
        else:
            logger.error("Zabbix login failed")
        return result
    
    def get_problems(self, time_from: int, names: Optional[List[str]] = None) -> List[Dict]:
        params = {
            "output": ["eventid", "name", "severity", "clock", "acknowledged"],
            "time_from": time_from,
            "sortfield": ["eventid"],
            "sortorder": "DESC",
            "limit": 100
        }
        if names:
            params["filter"] = {"name": names}
        
        result = self._call("problem.get", params)
        return result if result else []
    
    def get_hosts(self, filter_status: str = "0") -> List[Dict]:
        params = {
            "output": ["hostid", "name", "status"],
            "selectInterfaces": ["ip"],
            "filter": {"status": filter_status}
        }
        result = self._call("host.get", params)
        return result if result else []
    
    def get_host_by_name(self, name: str) -> Optional[Dict]:
        hosts = self.get_hosts()
        for host in hosts:
            if host.get("name") == name:
                return host
        return None
    
    def get_items(self, hostid: str, search_name: Optional[str] = None, limit: int = 500) -> List[Dict]:
        params = {
            "output": ["itemid", "name", "key_", "lastvalue", "lastclock", "units", "value_type"],
            "hostids": [str(hostid)],
            "limit": limit
        }
        if search_name:
            params["search"] = {"name": search_name}
        
        result = self._call("item.get", params)
        return result if result else []
    
    def get_history(self, itemid: str, hours: int = 24) -> List[Dict]:
        import time
        from datetime import timedelta
        
        t_from = int((time.time() - timedelta(hours=hours).total_seconds()))
        t_till = int(time.time())
        
        for h_type in [0, 3]:
            params = {
                "history": h_type,
                "itemids": [str(itemid)],
                "time_from": t_from,
                "time_till": t_till,
                "output": "extend",
                "sortfield": "clock",
                "sortorder": "ASC"
            }
            result = self._call("history.get", params)
            if result:
                return result
        return []
    
    def get_item_current_value(self, hostid: str, item_name_pattern: str) -> Optional[Dict]:
        items = self.get_items(hostid, search_name=item_name_pattern)
        for item in items:
            if item_name_pattern.lower() in item.get("name", "").lower():
                return item
        return items[0] if items else None
    
    def get_event_hosts(self, eventid: str) -> List[Dict]:
        result = self._call("event.get", {
            "output": ["eventid"],
            "selectHosts": ["hostid", "name"],
            "eventids": [eventid]
        })
        if result and len(result) > 0:
            hosts = result[0].get("hosts", [])
            for h in hosts:
                hostid = h.get("hostid")
                if hostid:
                    interfaces = self._call("hostinterface.get", {
                        "output": ["ip"],
                        "hostids": [hostid]
                    })
                    h["interfaces"] = interfaces if interfaces else []
            return hosts
        return []
