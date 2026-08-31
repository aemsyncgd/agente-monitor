# src/verifier.py
import os
import warnings
import subprocess
import logging
from typing import Optional
from .models import Event, FaultVerification, VerificationResult
from .zabbix_client import ZabbixClient
from .config import Config

logger = logging.getLogger(__name__)

warnings.warn(
    "src.verifier uses ZabbixClient which is DEPRECATED. "
    "Zabbix integration will be removed in a future version.",
    DeprecationWarning,
    stacklevel=2,
)

STATUS_MAP = {"1": "LOS", "3": "Online", "4": "Dying Gaps", "6": "Offline"}

class Verifier:
    def __init__(self, config: Config, client: ZabbixClient):
        self.config = config
        self.client = client
    
    def _verify_zabbix(self, olt_ip: str, puerto_pon: str) -> VerificationResult:
        try:
            host = None
            hosts = self.client.get_hosts()
            for h in hosts:
                if h.get("interfaces", [{}])[0].get("ip") == olt_ip:
                    host = h
                    break
            
            if not host:
                return VerificationResult(
                    source="zabbix",
                    success=False,
                    error="Host not found"
                )
            
            items = self.client.get_items(host["hostid"], search_name=puerto_pon)
            
            for item in items:
                if puerto_pon in item.get("name", ""):
                    value = item.get("lastvalue", "")
                    status = STATUS_MAP.get(value, f"Unknown({value})")
                    return VerificationResult(
                        source="zabbix",
                        success=True,
                        status=status,
                        details={"itemid": item["itemid"], "value": value}
                    )
            
            return VerificationResult(
                source="zabbix",
                success=False,
                error="Item not found"
            )
        
        except Exception as e:
            logger.error(f"Zabbix verification failed: {e}")
            return VerificationResult(
                source="zabbix",
                success=False,
                error=str(e)
            )
    
    def _verify_ping(self, ip: str) -> VerificationResult:
        if not self.config.verification.ping_enabled:
            return VerificationResult(source="ping", success=False, error="Disabled")
        
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(self.config.verification.ping_timeout_seconds), ip],
                capture_output=True,
                timeout=self.config.verification.ping_timeout_seconds + 2
            )
            return VerificationResult(
                source="ping",
                success=result.returncode == 0,
                details={"returncode": result.returncode}
            )
        except subprocess.TimeoutExpired:
            return VerificationResult(source="ping", success=False, error="Timeout")
        except Exception as e:
            return VerificationResult(source="ping", success=False, error=str(e))
    
    def _verify_snmp(self, ip: str, puerto_pon: str) -> VerificationResult:
        if not self.config.verification.snmp_enabled:
            return VerificationResult(source="snmp", success=False, error="Disabled")
        
        try:
            from pysnmp.hlapi import getCmd, SnmpEngine, CommunityData, UdpTransportTarget, ContextData, ObjectType, ObjectIdentity
            
            error_indication, error_status, error_index, var_binds = next(
                getCmd(
                    SnmpEngine(),
                    CommunityData('public'),
                    UdpTransportTarget((ip, 161), timeout=5),
                    ContextData(),
                    ObjectType(ObjectIdentity('SNMPv2-MIB', 'sysDescr', 0))
                )
            )
            
            if error_indication:
                return VerificationResult(source="snmp", success=False, error=str(error_indication))
            elif error_status:
                return VerificationResult(source="snmp", success=False, error=str(error_status))
            else:
                return VerificationResult(
                    source="snmp",
                    success=True,
                    details={"sysDescr": str(var_binds[0][1])}
                )
        
        except ImportError:
            return VerificationResult(source="snmp", success=False, error="pysnmp not installed")
        except Exception as e:
            return VerificationResult(source="snmp", success=False, error=str(e))
    
    def _verify_ssh(self, ip: str, puerto_pon: str) -> VerificationResult:
        if not self.config.verification.ssh_enabled:
            return VerificationResult(source="ssh", success=False, error="Disabled")
        
        ssh_user = os.environ.get("SSH_USERNAME", "")
        ssh_pass = os.environ.get("SSH_PASSWORD", "")
        if not ssh_user or not ssh_pass:
            return VerificationResult(source="ssh", success=False, error="SSH_USERNAME/SSH_PASSWORD not configured")
        
        try:
            import paramiko
            
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.WarningPolicy())
            client.connect(ip, username=ssh_user, password=ssh_pass, timeout=5)
            
            stdin, stdout, stderr = client.exec_command(f"show interface {puerto_pon}")
            output = stdout.read().decode()
            
            client.close()
            
            return VerificationResult(
                source="ssh",
                success=True,
                details={"output": output[:500]}
            )
        
        except ImportError:
            return VerificationResult(source="ssh", success=False, error="paramiko not installed")
        except Exception as e:
            return VerificationResult(source="ssh", success=False, error=str(e))
    
    def verify_fault(self, event: Event) -> FaultVerification:
        zabbix_result = self._verify_zabbix(event.olt_ip, event.puerto_pon)
        ping_result = self._verify_ping(event.olt_ip)
        snmp_result = self._verify_snmp(event.olt_ip, event.puerto_pon)
        ssh_result = self._verify_ssh(event.olt_ip, event.puerto_pon)
        
        confirmed = zabbix_result.success and zabbix_result.status in ["LOS", "Dying Gaps"]
        confirmed = confirmed and (ping_result.success or snmp_result.success or ssh_result.success)
        
        return FaultVerification(
            olt_name=event.olt_name,
            olt_ip=event.olt_ip,
            puerto_pon=event.puerto_pon,
            confirmed=confirmed,
            zabbix_result=zabbix_result,
            ping_result=ping_result,
            snmp_result=snmp_result,
            ssh_result=ssh_result,
            timestamp=event.timestamp,
            nodo=event.nodo
        )
