# src/collectors/zabbix_sender.py
import json
import logging
import os
import tempfile
import subprocess
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
from .base import CollectorResult

logger = logging.getLogger(__name__)


class ZabbixBridge:
    """
    Envía datos del collector a Zabbix Server via zabbix_sender.

    Desacoplado del collector: se puede activar/desactivar desde config,
    y eliminar completamente borrando este archivo + 3 líneas en main.py.
    """

    SENDER = "/usr/bin/zabbix_sender"

    def __init__(self, server: str = "127.0.0.1", port: int = 10051):
        self.server = server
        self.port = port
        self._send_count = 0
        self._error_count = 0

    def send_olt_result(self, result: CollectorResult) -> bool:
        """Envía datos de una OLT a Zabbix, exactamente como espera la template VSOL-OLT."""
        hostname = result.device_name
        if not hostname:
            logger.warning("ZabbixBridge: no hostname in result, skipping")
            return False

        if result.errors and not result.metrics:
            return self._send_olt_down(hostname)

        metrics_by_measurement = self._index_metrics(result.metrics)

        batch_lines: List[str] = []
        discovery_ge: List[Dict] = []
        discovery_pon: List[Dict] = []
        discovery_onu: List[Dict] = []
        pon_counts: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"online": 0, "offline": 0, "dying": 0, "los": 0}
        )
        total_onus = 0
        online_onus = 0

        olt_system = metrics_by_measurement.get("olt_system", [{}])[0] if "olt_system" in metrics_by_measurement else {}
        sys_fields = olt_system.get("fields", {})
        sys_tags = olt_system.get("tags", {})

        batch_lines.append(self._line(hostname, "vsOLTAlive", "1"))
        batch_lines.append(self._line(hostname, "vsSystemName", str(sys_tags.get("olt_name", hostname))))
        batch_lines.append(self._line(hostname, "vsSystemDesc", str(sys_fields.get("sys_desc", sys_tags.get("modelo", "")))))
        batch_lines.append(self._line(hostname, "vsSystemCPU", self._fmt(sys_fields.get("cpu", 0.0))))
        batch_lines.append(self._line(hostname, "vsSystemMemory", self._fmt(sys_fields.get("memory", 0.0))))
        batch_lines.append(self._line(hostname, "vsSystemTemp", self._fmt(sys_fields.get("temperature", 0.0))))
        batch_lines.append(self._line(hostname, "system.uptime", str(int(sys_fields.get("uptime", 0)))))

        if result.duration_seconds is not None:
            batch_lines.append(self._line(hostname, "vsScriptExecTime", str(int(result.duration_seconds))))

        iface_metrics = metrics_by_measurement.get("interface_traffic", [])
        for m in iface_metrics:
            tags = m.get("tags", {})
            fields = m.get("fields", {})
            iface_name = tags.get("interface_name", "")
            iface_type = tags.get("interface_type", "")
            if not iface_name:
                continue

            if iface_type == "GE":
                discovery_ge.append({"{#IFNAME}": iface_name, "{#IFTYPE}": "GE"})
                if "ifHCInOctets" in fields:
                    batch_lines.append(self._line(hostname, f"ge.ifHCInOctets[{iface_name}]", str(fields["ifHCInOctets"])))
                if "ifHCOutOctets" in fields:
                    batch_lines.append(self._line(hostname, f"ge.ifHCOutOctets[{iface_name}]", str(fields["ifHCOutOctets"])))
                if "ifOperStatus" in fields:
                    batch_lines.append(self._line(hostname, f"ge.ifOperStatus[{iface_name}]", str(fields["ifOperStatus"])))
            elif iface_type == "PON":
                pon_clean = iface_name.split("/")[0] + "/" + iface_name.split("/")[1] if "/" in iface_name else iface_name
                discovery_pon.append({"{#IFNAME}": pon_clean, "{#IFTYPE}": "PON"})
                if "ifHCInOctets" in fields:
                    batch_lines.append(self._line(hostname, f"pon.ifHCInOctets[{pon_clean}]", str(fields["ifHCInOctets"])))
                if "ifHCOutOctets" in fields:
                    batch_lines.append(self._line(hostname, f"pon.ifHCOutOctets[{pon_clean}]", str(fields["ifHCOutOctets"])))
                if "ifOperStatus" in fields:
                    batch_lines.append(self._line(hostname, f"pon.ifOperStatus[{pon_clean}]", str(fields["ifOperStatus"])))

        optical_metrics = metrics_by_measurement.get("optical_power", [])
        for m in optical_metrics:
            tags = m.get("tags", {})
            fields = m.get("fields", {})
            onu_index = tags.get("onu_index", "")
            pon_port = tags.get("pon_port", "")
            status = fields.get("status")

            if not onu_index:
                continue

            total_onus += 1
            if status == 3:
                online_onus += 1
                pon_counts[pon_port]["online"] += 1
            elif status == 1:
                pon_counts[pon_port]["los"] += 1
            elif status == 4:
                pon_counts[pon_port]["dying"] += 1
            else:
                pon_counts[pon_port]["offline"] += 1

            discovery_onu.append({
                "{#ONU_INDEX}": onu_index,
                "{#LOCATION}": f"{pon_port}:{onu_index.split('_')[1]}" if "_" in onu_index else onu_index,
                "{#ONU_DESC}": tags.get("onu_serial", f"ONU_{onu_index}"),
                "{#ONU_SERIAL}": str(tags.get("onu_serial", f"ONU_{onu_index}")),
                "{#ONU_MODEL}": str(tags.get("onu_model", "unknown"))
            })

            batch_lines.append(self._line(hostname, f"onu.status[{onu_index}]", str(status)))

            rx = fields.get("rx_power")
            tx = fields.get("tx_power")
            if rx is not None:
                batch_lines.append(self._line(hostname, f"onu.rx.power[{onu_index}]", self._fmt(rx)))
            if tx is not None:
                batch_lines.append(self._line(hostname, f"onu.tx.power[{onu_index}]", self._fmt(tx)))

        onu_iface_metrics = [m for m in iface_metrics if m.get("tags", {}).get("interface_type") == "ONU"]
        for m in onu_iface_metrics:
            tags = m.get("tags", {})
            fields = m.get("fields", {})
            iface_name = tags.get("interface_name", "")
            onu_idx = self._onu_index_from_iface(iface_name)
            if not onu_idx:
                continue
            if "ifHCInOctets" in fields:
                batch_lines.append(self._line(hostname, f"onu.ifHCInOctets[{onu_idx}]", str(fields["ifHCInOctets"])))
            if "ifHCOutOctets" in fields:
                batch_lines.append(self._line(hostname, f"onu.ifHCOutOctets[{onu_idx}]", str(fields["ifHCOutOctets"])))

        batch_lines.append(self._line(hostname, "vsTotalONUs", str(total_onus)))

        for pon_name, counts in pon_counts.items():
            batch_lines.append(self._line(hostname, f"pon.onu.online[{pon_name}]", str(counts["online"])))
            batch_lines.append(self._line(hostname, f"pon.onu.offline[{pon_name}]", str(counts["offline"])))
            batch_lines.append(self._line(hostname, f"pon.onu.dying[{pon_name}]", str(counts["dying"])))
            batch_lines.append(self._line(hostname, f"pon.onu.los[{pon_name}]", str(counts["los"])))

        sent_ok = True
        if batch_lines:
            sent_ok = self._send_batch_file(hostname, batch_lines)

        self._send_discovery(hostname, "net.if.ge.discovery", discovery_ge)
        self._send_discovery(hostname, "net.if.pon.discovery", discovery_pon)
        self._send_discovery(hostname, "onu.discovery", discovery_onu)

        if sent_ok:
            self._send_count += 1
        else:
            self._error_count += 1

        return sent_ok

    def send_ping_result(self, result: CollectorResult) -> bool:
        """Envía datos de ping a Zabbix. Mapea por tipo de target."""
        sent_ok = True
        for metric in result.metrics:
            tags = metric.get("tags", {})
            fields = metric.get("fields", {})
            target_name = tags.get("name", "")
            target_type = tags.get("type", "")
            target_ip = tags.get("ip", "")
            is_up = fields.get("status", 0)
            latency_avg = fields.get("latency_ms_avg", 0.0)

            if target_type == "dns":
                dns_key = self._dns_key_for_ip(target_ip)
                if dns_key and is_up:
                    latency_sec = f"{latency_avg / 1000:.6f}" if latency_avg else "null"
                    if latency_sec != "null":
                        self._send_single("VIDANET BACKBONE", dns_key, latency_sec)
            else:
                hostname = target_name
                if is_up and latency_avg:
                    latency_sec = f"{latency_avg / 1000:.6f}"
                    self._send_single(hostname, "ping.latency", latency_sec)

        return sent_ok

    def get_stats(self) -> Dict[str, Any]:
        return {"send_count": self._send_count, "error_count": self._error_count}

    def _send_olt_down(self, hostname: str) -> bool:
        """Envía indicadores de caída cuando la OLT no responde."""
        batch = [
            self._line(hostname, "vsOLTAlive", "0"),
            self._line(hostname, "vsTotalONUs", "0"),
            self._line(hostname, "vsScriptExecTime", "0"),
            self._line(hostname, "vsSystemCPU", "0"),
            self._line(hostname, "vsSystemMemory", "0"),
            self._line(hostname, "vsSystemTemp", "0"),
            self._line(hostname, "system.uptime", "0"),
        ]
        ok = self._send_batch_file(hostname, batch)
        self._send_discovery(hostname, "net.if.ge.discovery", [])
        self._send_discovery(hostname, "net.if.pon.discovery", [])
        self._send_discovery(hostname, "onu.discovery", [])
        return ok

    def _send_batch_file(self, hostname: str, lines: List[str]) -> bool:
        """Escribe líneas a un archivo temporal y ejecuta zabbix_sender -i."""
        if not lines:
            return True

        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".zbx", prefix=f"{hostname}_", delete=False
            )
            tmp.write("\n".join(lines) + "\n")
            tmp.close()

            proc = subprocess.run(
                [self.SENDER, "-z", self.server, "-p", str(self.port),
                 "-s", hostname, "-i", tmp.name],
                capture_output=True, text=True, timeout=30
            )

            if proc.returncode != 0:
                logger.warning(f"zabbix_sender -i failed for {hostname}: {proc.stderr[:200]}")
                return False

            logger.debug(f"Zabbix: sent {len(lines)} metrics to {hostname}")
            return True

        except subprocess.TimeoutExpired:
            logger.warning(f"zabbix_sender timeout for {hostname}")
            return False
        except FileNotFoundError:
            logger.error(f"zabbix_sender not found at {self.SENDER}")
            return False
        except Exception as e:
            logger.error(f"Zabbix send error for {hostname}: {e}")
            return False
        finally:
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

    def _send_discovery(self, hostname: str, key: str, data: List[Dict]) -> bool:
        """Envía un discovery JSON via zabbix_sender -k -o."""
        payload = json.dumps({"data": data}, separators=(",", ":"))
        try:
            proc = subprocess.run(
                [self.SENDER, "-z", self.server, "-p", str(self.port),
                 "-s", hostname, "-k", key, "-o", payload],
                capture_output=True, text=True, timeout=15
            )
            if proc.returncode != 0:
                logger.warning(f"Discovery send failed ({key}) for {hostname}: {proc.stderr[:150]}")
                return False
            logger.debug(f"Zabbix: discovery {key} sent to {hostname} ({len(data)} items)")
            return True
        except Exception as e:
            logger.warning(f"Discovery send error ({key}) for {hostname}: {e}")
            return False

    def _send_single(self, hostname: str, key: str, value: str) -> bool:
        """Envía un solo item via zabbix_sender -k -o."""
        try:
            proc = subprocess.run(
                [self.SENDER, "-z", self.server, "-p", str(self.port),
                 "-s", hostname, "-k", key, "-o", value],
                capture_output=True, text=True, timeout=10
            )
            return proc.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _index_metrics(metrics: List[Dict]) -> Dict[str, List[Dict]]:
        """Agrupa métricas por measurement."""
        result: Dict[str, List[Dict]] = defaultdict(list)
        for m in metrics:
            result[m.get("measurement", "unknown")].append(m)
        return result

    @staticmethod
    def _line(hostname: str, key: str, value: str) -> str:
        """Genera una línea para zabbix_sender -i: HOST key value"""
        return f"{hostname} {key} {value}"

    @staticmethod
    def _fmt(value: Any) -> str:
        if value is None:
            return "0"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    @staticmethod
    def _onu_index_from_iface(iface_name: str) -> Optional[str]:
        """
        Convierte 'GPON01ONU32' a '1_32' (formato Z_INDEX que espera Zabbix).
        """
        import re
        m = re.match(r'GPON(\d+)ONU(\d+)', iface_name, re.IGNORECASE)
        if m:
            return f"{int(m.group(1))}_{int(m.group(2))}"
        return None

    @staticmethod
    def _dns_key_for_ip(ip: str) -> Optional[str]:
        """Mapea IP de DNS a key de Zabbix: 8.8.8.8 → icmppingsec.dns1[8.8.8.8]"""
        mapping = {
            "8.8.8.8": "icmppingsec.dns1[8.8.8.8]",
            "9.9.9.9": "icmppingsec.dns2[9.9.9.9]",
            "1.1.1.1": "icmppingsec.dns3[1.1.1.1]",
        }
        return mapping.get(ip)
