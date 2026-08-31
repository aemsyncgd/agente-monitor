# src/reporter.py
# Reporte diario de OLTs y MikroTiks para Telegram - datos desde InfluxDB
# Estructura basada en: zabbix-daily-report-telegram.py

import os
import re
import sys
import time
import copy
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, Set

import requests

from ..storage.influx_client import InfluxClient

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent.parent
REPORTE_CONFIG_DIR = SCRIPT_DIR.parent.parent / "config"
CHART_DIR = BASE_DIR / "charts"
FORMAT_PATH = REPORTE_CONFIG_DIR / "telegram-report-format.conf"
REPORTE_CONFIG_PATH = REPORTE_CONFIG_DIR / "reporte-config.json"

HAS_MATPLOTLIB = False
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except ImportError:
    pass


def format_bps(value: Any) -> str:
    try:
        v = float(value)
    except (ValueError, TypeError):
        return "0 bps"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f} Gbps"
    elif v >= 1_000_000:
        return f"{v / 1_000_000:.1f} Mbps"
    elif v >= 1_000:
        return f"{v / 1_000:.1f} Kbps"
    else:
        return f"{v:.0f} bps"


def format_mbps(value: Any) -> str:
    try:
        v = float(value)
    except (ValueError, TypeError):
        return "0 Mbps"
    if v >= 1000.0:
        return f"{v / 1000.0:.2f} Gbps"
    return f"{v:.1f} Mbps"


def format_age_ago(seconds_ago: float) -> str:
    minutes = int(seconds_ago / 60)
    if minutes < 1:
        return "< 1 min"
    elif minutes < 60:
        return f"{minutes} min"
    else:
        hours = minutes // 60
        mins = minutes % 60
        if mins > 0:
            return f"{hours}h {mins}min"
        return f"{hours}h"


def calculate_stats(values: List[float]) -> Dict[str, float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return {"avg": 0, "max": 0, "min": 0}
    return {"avg": sum(nums) / len(nums), "max": max(nums), "min": min(nums)}


def split_day_night(timestamps: List[float], values: List[float],
                    night_start: int = 21, night_end: int = 8) -> Dict[str, Dict]:
    day, night = [], []
    for ts, val in zip(timestamps, values):
        try:
            h = datetime.fromtimestamp(ts).hour
            v = float(val)
            (night if h >= night_start or h < night_end else day).append(v)
        except Exception:
            continue
    return {"day": calculate_stats(day), "night": calculate_stats(night)}


def analyze_traffic(stats: Dict) -> str:
    silence_min = stats.get("night_silence_min", 0)
    warn_min = stats.get("silence_warn_min", 30)
    if silence_min >= warn_min:
        return (f"⚠️ Posible corte en la noche (21:00-08:00): "
                f"tráfico ~0 durante {int(silence_min)} min")
    da = stats["day_night"]["day"]["avg"]
    na = stats["day_night"]["night"]["avg"]
    if na > 0:
        r = da / na
        if r > 1.5:
            return f"📈 Tráfico diurno {r:.1f}x superior al nocturno"
        elif r < 0.67:
            return f"📉 Tráfico nocturno {1/r:.1f}x superior al diurno (inusual)"
    return "📊 Tráfico estable entre períodos diurno y nocturno"


def split_text_chunks(text: str, limit: int = 4096, sep: str = "\n") -> List[str]:
    if len(text) <= limit:
        return [text]
    lines = text.split(sep)
    chunks, current = [], []
    current_len = 0
    for line in lines:
        line_len = len(line) + len(sep)
        if current_len + line_len > limit and current:
            chunks.append(sep.join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append(sep.join(current))
    return chunks


def iface_entries(raw_list: Any) -> List[Dict]:
    out = []
    for item in raw_list or []:
        if isinstance(item, str):
            out.append({"name": item, "analyze": False})
        elif isinstance(item, dict):
            out.append({"name": item.get("name", ""), "analyze": bool(item.get("analyze", False))})
    return [i for i in out if i["name"]]


def iface_names(entries: List[Dict]) -> List[str]:
    return [e["name"] for e in entries]


def analyzed_iface_names(entries: List[Dict]) -> List[str]:
    return [e["name"] for e in entries if e.get("analyze")]


class FormatLoader:
    DEFAULTS = {
        "branding": {
            "webhook_username": "Monitor VidaNet",
            "webhook_avatar": "",
            "footer_text": "Agente Monitor | Reporte diario",
            "timezone": "America/Caracas",
        },
        "emojis": {
            "olt_up": "\U0001f7e2", "olt_down": "\U0001f534",
            "olt_warn": "\U0001f7e1",
            "pon_up": "\U0001f7e2", "pon_down": "\U0001f534",
            "pon_warn": "\U0001f7e1", "pon_orange": "\U0001f7e0",
            "pon_unknown": "\u2753", "pon_users": "\U0001f465",
            "traffic_in": "\U0001f4e5", "traffic_out": "\U0001f4e4",
            "mk_up": "\U0001f7e2", "mk_down": "\U0001f534",
            "mk_warn": "\U0001f7e1",
            "mk_no_iface": "\u26a0\ufe0f", "mk_stale": "\u23f3",
            "sev_0": "\u26aa", "sev_1": "\U0001f535",
            "sev_2": "\U0001f7e2", "sev_3": "\U0001f7e1",
            "sev_4": "\U0001f7e0", "sev_5": "\U0001f534",
            "alert_ack": "\U0001f515",
            "summary_date": "\U0001f4c5", "summary_online": "\U0001f5a5\ufe0f",
            "summary_offline": "\U0001f534", "summary_alerts": "\u26a0\ufe0f",
            "section_olt": "\U0001f50c", "section_mk": "\U0001f4e1",
            "section_alerts": "\U0001f6a8", "report_icon": "\U0001f4e1",
            "chart_icon": "\U0001f4ca", "trend_up": "\U0001f4c8",
            "trend_down": "\U0001f4c9",
        },
        "colors": {
            "report_ok": 3066993, "report_alert": 15158332,
            "olt_color": 3447003, "mk_color": 3447003,
            "alert_color": 15158332, "chart_color": 5763719,
        },
        "text": {
            "report_title": "\U0001f4e1 Reporte VidaNet",
            "olt_section_title": "OLTs", "mk_section_title": "MikroTiks / Nodos",
            "alert_section_title": "Alertas Recientes",
            "generated_label": "Generado", "online_label": "En linea",
            "offline_label": "Fuera de linea", "alerts_label": "Alertas",
            "unit_text": "equipos", "no_iface_msg": "Sin datos de interfaz",
            "truncated_msg": "...", "date_format": "%d/%m/%Y %H:%M",
            "stale_label": "Sin datos recientes", "ago_suffix": "hace",
            "ago_min": "min",
        },
        "templates": {
            "summary": (
                "{summary_date} <b>{generated_label}:</b> {date} <i>{timezone}</i>\n"
                "{summary_online} <b>{online_label}:</b> {online_count}/{total}\n"
                "{summary_offline} <b>{offline_label}:</b> {offline_count}\n"
                "{offline_hosts}"
                "{summary_alerts} <b>{alerts_label}:</b> {problem_count}"
            ),
            "olt_section_header": (
                "{section_olt} <b>{olt_section_title}  —  {count} {unit_text}</b>"
            ),
            "olt_host": "<b>{olt_emoji} {name}</b> ({ip})",
            "pon_line": (
                "  <code>{pon}</code> {pon_emoji}  {pon_users} {online}/{offline}"
                "  {traffic_in} {traffic_in_val}  {traffic_out} {traffic_out_val}"
            ),
            "mk_section_header": "{section_mk} <b>{mk_section_title} ({count})</b>",
            "mk_host": "<b>{mk_emoji} {name}</b> ({ip})",
            "mk_iface": (
                "  <code>{iface}</code>  {traffic_in} {traffic_in_val}"
                "  {traffic_out} {traffic_out_val}"
            ),
            "mk_iface_stale": (
                "  <code>{iface}</code>  {mk_stale} {stale_label} ({ago_suffix} {ago_min})"
            ),
            "mk_no_iface": "  <i>{no_iface_msg}</i>",
            "alert_line": (
                "{alert_emoji}{alert_ack} <code>{time}</code> <b>{host_name}</b>\n  {alert_name}"
            ),
            "alert_section_header": (
                "{section_alerts} <b>{alert_section_title} ({count})</b>"
            ),
            "chart_stats": (
                "<b>{chart_icon} {host_name} - {iface_name}</b>\n"
                "\U0001f319 <b>Nocturno (00:00-06:00):</b> {night_avg} avg | {night_max} pico\n"
                "\u2600\ufe0f <b>Diurno (06:00-23:59):</b> {day_avg} avg | {day_max} pico\n"
                "\U0001f4ca <b>Total 24h:</b> {total_avg} avg | {total_max} max | {total_min} min\n"
                "{analysis_text}"
            ),
            "olt_separator": "",
            "mk_separator": "",
            "alert_separator": "",
        },
        "sections": {
            "order": "summary,olt,mk,charts,incidents",
            "show_alerts": "yes",
            "show_charts": "yes",
        },
        "limits": {
            "max_desc": "4000",
            "max_problems": "15",
            "max_alert_name": "50",
            "stale_threshold_sec": "300",
            "chart_hours": "24",
            "chart_dpi": "130",
        },
        "chart_targets": {
            "auto_detect": "yes",
            "mikrotik_max_interfaces": "5",
        },
    }

    def __init__(self, config_file: str):
        self.config = copy.deepcopy(self.DEFAULTS)
        path = Path(config_file)
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
                section = None
                current_key = None
                for raw_line in text.splitlines():
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#") or stripped.startswith(";"):
                        continue
                    if stripped.startswith("[") and stripped.endswith("]"):
                        section = stripped[1:-1].strip()
                        current_key = None
                        if section not in self.config:
                            self.config[section] = {}
                        continue
                    if section and "=" in stripped:
                        current_key, _, value = stripped.partition("=")
                        current_key = current_key.strip()
                        value = value.strip()
                        value = value.split("#")[0].strip()
                        value = value.split(";")[0].strip()
                        value = value.replace("\\n", "\n")
                        self.config[section][current_key] = value
                    elif section and current_key and stripped:
                        self.config[section][current_key] += "\n" + stripped
                logger.info(f"Formato cargado desde {config_file}")
            except Exception as e:
                logger.warning(f"Error leyendo {config_file}, usando defaults: {e}")
        else:
            logger.warning(f"No se encontró {config_file}, usando formato por defecto")

    def get(self, section: str, key: str, default: Any = None) -> Any:
        try:
            val = self.config[section][key]
            if isinstance(val, str):
                return val.split('#')[0].strip()
            return val
        except KeyError:
            return default

    def emoji(self, key: str) -> str:
        return self.get("emojis", key, "")

    def color(self, key: str) -> int:
        try:
            return int(self.get("colors", key, "0"))
        except (ValueError, TypeError):
            return 0

    def tmpl(self, key: str) -> str:
        return self.get("templates", key, "")

    def text_val(self, key: str) -> str:
        return self.get("text", key, key)

    def section_order(self) -> List[str]:
        raw = self.get("sections", "order", "summary,olt,mk,alerts")
        return [s.strip() for s in raw.split(",") if s.strip()]

    def show_alerts(self) -> bool:
        return self.get("sections", "show_alerts", "yes").lower() in ("yes", "true", "1")

    def show_charts(self) -> bool:
        return self.get("sections", "show_charts", "yes").lower() in ("yes", "true", "1")

    def chart_hours(self) -> int:
        val = self.get("limits", "chart_hours", "24")
        return int(str(val).split('#')[0].strip())

    def chart_dpi(self) -> int:
        val = self.get("limits", "chart_dpi", "130")
        return int(str(val).split('#')[0].strip())

    def stale_threshold(self) -> int:
        val = self.get("limits", "stale_threshold_sec", "300")
        return int(str(val).split('#')[0].strip())

    def auto_detect_charts(self) -> bool:
        return self.get("chart_targets", "auto_detect", "yes").lower() in ("yes", "true", "1")

    def mikrotik_max_ifaces(self) -> int:
        val = self.get("chart_targets", "mikrotik_max_interfaces", "5")
        return int(str(val).split('#')[0].strip())

    def render(self, template: str, **kwargs) -> str:
        try:
            return template.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Placeholder faltante: {e}")
            return template
        except Exception as e:
            logger.warning(f"Error en template: {e}")
            return template


class DailyReporter:
    def __init__(
        self,
        influx: InfluxClient,
        bot_token: str,
        chat_id: str,
        format_path: str = None,
        config_path: str = None,
    ):
        self.influx = influx
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.format_path = format_path or str(FORMAT_PATH)
        self.config_path = config_path or str(REPORTE_CONFIG_PATH)
        self.timeout = 30

    def _load_report_config(self) -> Dict:
        path = Path(self.config_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as e:
                logger.warning(f"Error cargando {self.config_path}: {e}")
        return {"enabled": True, "schedules": ["08:00", "12:30", "16:50", "20:45"], "mikrotiks": [], "olts": []}

    def _is_report_enabled(self) -> bool:
        return self._load_report_config().get("enabled", True)

    def _get_schedules(self) -> List[str]:
        return self._load_report_config().get("schedules", ["08:00", "12:30", "16:50", "20:45"])

    def _get_configured_mikrotiks(self) -> List[Dict]:
        return self._load_report_config().get("mikrotiks", [])

    def _get_configured_olts(self) -> List[Dict]:
        return self._load_report_config().get("olts", [])

    def _resolve_host_names(self, discovered: List[str], configured: List[Dict],
                            has_config: bool) -> List[str]:
        """Hosts to process: discovered (with data) plus configured hosts even
        if they have no recent SNMP data in InfluxDB."""
        if not has_config:
            return list(discovered)
        cfg_names = {h["name"] for h in configured}
        names = [n for n in discovered if n in cfg_names]
        for name in sorted(cfg_names - set(names)):
            names.append(name)
        return names

    def _mikrotik_interfaces_for(self, host_name: str) -> Optional[List[str]]:
        for h in self._get_configured_mikrotiks():
            if h.get("name", "").lower() == host_name.lower():
                return iface_names(iface_entries(h.get("interfaces")))
        return None

    def _mikrotik_analyze_interfaces_for(self, host_name: str) -> Optional[List[str]]:
        for h in self._get_configured_mikrotiks():
            if h.get("name", "").lower() == host_name.lower():
                return analyzed_iface_names(iface_entries(h.get("interfaces")))
        return None

    def _olt_config_for(self, olt_name: str) -> Optional[Dict]:
        for h in self._get_configured_olts():
            if h.get("name", "").lower() == olt_name.lower():
                return h
        return None

    def _olt_has_pon_monitoring(self, olt_name: str) -> bool:
        cfg = self._olt_config_for(olt_name)
        if cfg is None:
            return True
        return cfg.get("pon_monitoring", True)

    def _olt_ge_interfaces_for(self, olt_name: str) -> Optional[List[str]]:
        cfg = self._olt_config_for(olt_name)
        if cfg is None:
            return None
        return iface_names(iface_entries(cfg.get("ge_interfaces")))

    def _olt_ge_analyze_interfaces_for(self, olt_name: str) -> Optional[List[str]]:
        cfg = self._olt_config_for(olt_name)
        if cfg is None:
            return None
        return analyzed_iface_names(iface_entries(cfg.get("ge_interfaces")))

    # ------------------------------------------------------------------
    # Data collection from InfluxDB
    # ------------------------------------------------------------------

    def _query(self, flux: str) -> List[Dict]:
        return self.influx.query(flux)

    def _get_olt_names(self) -> List[str]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -24h)
                |> filter(fn: (r) => r["_measurement"] == "olt_system")
                |> last()
        ''')
        seen = set()
        names = []
        for r in results:
            name = r.get("olt_name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return sorted(names)

    def _get_olt_system(self, olt_name: str) -> Optional[Dict]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "olt_system")
                |> filter(fn: (r) => r["olt_name"] == "{olt_name}")
                |> last()
        ''')
        if not results:
            return None
        fields = {}
        tags = {}
        for r in results:
            field = r.get("field")
            value = r.get("value")
            if field:
                fields[field] = value
            for k, v in r.items():
                if k in ("time", "measurement", "field", "value", "result", "table"):
                    continue
                if k != "olt_name":
                    tags[k] = v
        tags["olt_name"] = olt_name
        return {"tags": tags, "fields": fields}

    def _get_olt_pons(self, olt_name: str) -> List[Dict]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
                |> filter(fn: (r) => r["device_name"] == "{olt_name}")
                |> filter(fn: (r) => r["interface_type"] == "PON")
                |> last()
        ''')
        if not results:
            return []
        pon_map: Dict[str, Dict] = {}
        for r in results:
            iface = r.get("interface_name", "")
            if not iface:
                continue
            if iface not in pon_map:
                pon_map[iface] = {"name": iface}
            field = r.get("field")
            value = r.get("value")
            if field:
                pon_map[iface][field] = value
            pon_map[iface]["device_ip"] = r.get("device_ip", "")

        pons = []
        for iface, data in sorted(pon_map.items(), key=lambda x: x[0]):
            status = data.get("ifOperStatus")
            online = data.get("pon_online", 0)
            offline = data.get("pon_offline", 0)

            status_str = "UP" if status == 1 else "DOWN" if status == 0 else "?"
            hcin = data.get("ifHCInOctets")
            hcout = data.get("ifHCOutOctets")

            in_rate = self._compute_rate(olt_name, iface, "ifHCInOctets")
            out_rate = self._compute_rate(olt_name, iface, "ifHCOutOctets")

            pons.append({
                "pon": iface,
                "status": status_str,
                "in_rate": in_rate,
                "out_rate": out_rate,
                "online": str(online),
                "offline": str(offline),
                "in_val": format_bps(in_rate),
                "out_val": format_bps(out_rate),
            })
        return pons

    def _compute_rate(self, device: str, iface: str, field: str) -> float:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -50m)
                |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
                |> filter(fn: (r) => r["device_name"] == "{device}")
                |> filter(fn: (r) => r["interface_name"] == "{iface}")
                |> filter(fn: (r) => r["_field"] == "{field}")
                |> sort(columns: ["_time"], desc: false)
                |> limit(n: 2)
        ''')
        if len(results) < 2:
            return 0.0
        try:
            v1 = float(results[0]["value"])
            v2 = float(results[1]["value"])
            t1 = results[0]["time"].timestamp()
            t2 = results[1]["time"].timestamp()
            if v2 >= v1 and t2 > t1:
                return (v2 - v1) * 8.0 / (t2 - t1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass
        return 0.0

    def _compute_rate_mikrotik(self, device: str, iface: str, field: str) -> float:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -10m)
                |> filter(fn: (r) => r["_measurement"] == "mikrotik_interface")
                |> filter(fn: (r) => r["device_name"] == "{device}")
                |> filter(fn: (r) => r["interface_name"] == "{iface}")
                |> filter(fn: (r) => r["_field"] == "{field}")
                |> sort(columns: ["_time"], desc: false)
                |> limit(n: 2)
        ''')
        if len(results) < 2:
            return 0.0
        try:
            v1 = float(results[0]["value"])
            v2 = float(results[1]["value"])
            t1 = results[0]["time"].timestamp()
            t2 = results[1]["time"].timestamp()
            if v2 >= v1 and t2 > t1:
                return (v2 - v1) * 8.0 / (t2 - t1)
        except (ValueError, TypeError, ZeroDivisionError):
            pass
        return 0.0

    def _get_onu_counts_per_pon(self, olt_name: str) -> Dict[str, Dict[str, int]]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "optical_power")
                |> filter(fn: (r) => r["olt_name"] == "{olt_name}")
                |> filter(fn: (r) => r["_field"] == "status")
                |> last()
        ''')
        pon_counts: Dict[str, Dict[str, int]] = {}
        for r in results:
            pon_port = r.get("pon_port", "")
            if not pon_port:
                continue
            if pon_port not in pon_counts:
                pon_counts[pon_port] = {"online": 0, "offline": 0}
            status = r.get("value")
            if status == 3:
                pon_counts[pon_port]["online"] += 1
            else:
                pon_counts[pon_port]["offline"] += 1
        return pon_counts

    def _get_mikrotik_names(self) -> List[str]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -24h)
                |> filter(fn: (r) => r["_measurement"] == "mikrotik_system")
                |> last()
        ''')
        seen = set()
        names = []
        for r in results:
            name = r.get("device_name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        return sorted(names)

    def _get_mikrotik_system(self, name: str) -> Optional[Dict]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "mikrotik_system")
                |> filter(fn: (r) => r["device_name"] == "{name}")
                |> last()
        ''')
        if not results:
            return None
        fields = {}
        tags = {}
        for r in results:
            field = r.get("field")
            value = r.get("value")
            if field:
                fields[field] = value
            for k, v in r.items():
                if k in ("time", "measurement", "field", "value", "result", "table"):
                    continue
                tags[k] = v
        return {"tags": tags, "fields": fields}

    def _get_mikrotik_interfaces(self, name: str, max_interfaces: int = 30, allowed: Optional[Set[str]] = None) -> List[Dict]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "mikrotik_interface")
                |> filter(fn: (r) => r["device_name"] == "{name}")
                |> last()
        ''')
        if not results:
            return []
        iface_map: Dict[str, Dict] = {}
        for r in results:
            iface = r.get("interface_name", "")
            if not iface:
                continue
            if iface not in iface_map:
                iface_map[iface] = {"name": iface, "ip": r.get("device_ip", "")}
            field = r.get("field")
            value = r.get("value")
            if field:
                iface_map[iface][field] = value
            iface_map[iface]["_time"] = r.get("time")

        interfaces = []
        now = time.time()
        stale_threshold = 300
        for iface, data in iface_map.items():
            skip_keywords = ["lo", "bridge", "lte", "wlan", "vlan", "sfp", "sfp+", "combo", "pppoe"]
            alias_match = re.search(r'<(.*?)>', iface)
            display_name = alias_match.group(1) if alias_match else iface
            if not display_name.strip():
                continue
            if allowed and display_name in allowed:
                skip = False
            else:
                skip = any(k in display_name.lower() for k in skip_keywords)
            if skip:
                continue

            hcin = data.get("ifHCInOctets")
            hcout = data.get("ifHCOutOctets")
            oper_status = data.get("ifOperStatus")

            last_time = data.get("_time")
            is_fresh = True
            age = 0
            if last_time:
                try:
                    age = now - last_time.timestamp()
                    is_fresh = age <= stale_threshold
                except Exception:
                    pass

            in_rate = self._compute_rate_mikrotik(name, iface, "ifHCInOctets")
            out_rate = self._compute_rate_mikrotik(name, iface, "ifHCOutOctets")

            interfaces.append({
                "iface_name": display_name,
                "traffic_in": format_bps(in_rate) if is_fresh else None,
                "traffic_out": format_bps(out_rate) if is_fresh else None,
                "in_rate": in_rate,
                "out_rate": out_rate,
                "is_fresh": is_fresh,
                "age": age,
                "stale": not is_fresh,
                "oper_status": oper_status,
            })

        interfaces.sort(key=lambda i: i.get("in_rate", 0) + i.get("out_rate", 0), reverse=True)
        interfaces = interfaces[:max_interfaces]
        interfaces.sort(key=lambda i: i.get("iface_name", ""))
        return interfaces

    def _get_mikrotik_ping_status(self, names: List[str]) -> Dict[str, Dict]:
        if not names:
            return {}
        pattern = "|".join(re.escape(n) for n in names)
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "ping_check")
                |> filter(fn: (r) => r["name"] =~ /^({pattern})$/)
                |> last()
        ''')
        out: Dict[str, Dict] = {}
        for r in results:
            name = r.get("name", "")
            if not name:
                continue
            info = out.setdefault(name, {"up": False, "latency": None})
            field = r.get("field")
            value = r.get("value")
            if field == "status":
                info["up"] = value == 1
            elif field == "latency_ms_avg" and info["latency"] is None:
                info["latency"] = value
        return out

    def _get_recent_problems(self) -> List[Dict]:
        problems = []

        ping_results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "ping_check")
                |> filter(fn: (r) => r["_field"] == "status")
                |> last()
        ''')
        for r in ping_results:
            status = r.get("value")
            name = r.get("name", "")
            ip = r.get("ip", "")
            if status == 0 and name:
                problems.append({
                    "host_name": name,
                    "alert_name": f"Host sin respuesta - {ip}",
                    "clock": r.get("time").timestamp() if r.get("time") else time.time(),
                    "severity": "4",
                    "acknowledged": "0",
                })

        onu_problems = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -30m)
                |> filter(fn: (r) => r["_measurement"] == "optical_power")
                |> filter(fn: (r) => r["_field"] == "status")
                |> last()
        ''')
        seen_olts: Dict[str, int] = {}
        for r in onu_problems:
            status = r.get("value")
            olt_name = r.get("olt_name", "")
            pon_port = r.get("pon_port", "")
            serial = r.get("onu_serial", "")
            if status is not None and status != 3 and olt_name:
                key = f"{olt_name}:{pon_port}"
                if key not in seen_olts:
                    seen_olts[key] = 0
                seen_olts[key] += 1

        for key, count in sorted(seen_olts.items()):
            parts = key.split(":")
            olt_name = parts[0]
            pon_port = parts[1] if len(parts) > 1 else ""
            problems.append({
                "host_name": olt_name,
                "alert_name": f"{count} ONU(s) offline en {pon_port}",
                "clock": time.time(),
                "severity": "3",
                "acknowledged": "0",
            })

        problems.sort(key=lambda p: p.get("clock", 0), reverse=True)
        return problems[:15]

    def _get_recent_incidents(self) -> List[Dict]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -24h)
                |> filter(fn: (r) => r["_measurement"] == "optical_power")
                |> filter(fn: (r) => r["_field"] == "status")
                |> filter(fn: (r) => r["_value"] != 3)
                |> sort(columns: ["_time"], desc: true)
        ''')
        seen: Dict[str, Dict] = {}
        for r in results:
            olt = r.get("olt_name", "")
            pon = r.get("pon_port", "")
            serial = r.get("onu_serial", "")
            status = r.get("value")
            ts = r.get("time")
            if not olt or not serial:
                continue
            key = f"{olt}:{pon}:{serial}"
            if key not in seen:
                status_map = {1: "Otro", 4: "LOS", 6: "DyingGap"}
                seen[key] = {
                    "olt": olt,
                    "pon": pon,
                    "serial": serial,
                    "type": status_map.get(int(status) if status else 0, f"status={status}"),
                    "clock": ts.timestamp() if hasattr(ts, "timestamp") else time.time(),
                    "time": ts,
                }
        incidents = sorted(seen.values(), key=lambda i: i["clock"], reverse=True)
        return incidents[:15]

    # ------------------------------------------------------------------
    # Chart generation
    # ------------------------------------------------------------------

    def _get_history_24h(self, hostname: str, iface: str, field: str, hours: int = 24) -> List[Tuple[float, float]]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{hours}h)
                |> filter(fn: (r) => r["_measurement"] == "interface_traffic")
                |> filter(fn: (r) => r["device_name"] == "{hostname}")
                |> filter(fn: (r) => r["interface_name"] == "{iface}")
                |> filter(fn: (r) => r["_field"] == "{field}")
                |> aggregateWindow(every: 5m, fn: last, createEmpty: false)
                |> sort(columns: ["_time"], desc: false)
        ''')
        data: List[Tuple[float, float]] = []
        for r in results:
            ts = r.get("time")
            val = r.get("value")
            if ts and val is not None:
                data.append((ts.timestamp(), float(val)))
        return data

    def _get_mikrotik_history_24h(self, hostname: str, iface: str, field: str,
                                  hours: int = 24, measurement: str = "mikrotik_interface") -> List[Tuple[float, float]]:
        results = self._query(f'''
            from(bucket: "{self.influx.bucket}")
                |> range(start: -{hours}h)
                |> filter(fn: (r) => r["_measurement"] == "{measurement}")
                |> filter(fn: (r) => r["device_name"] == "{hostname}")
                |> filter(fn: (r) => r["interface_name"] == "{iface}")
                |> filter(fn: (r) => r["_field"] == "{field}")
                |> aggregateWindow(every: 5m, fn: last, createEmpty: false)
                |> sort(columns: ["_time"], desc: false)
        ''')
        data: List[Tuple[float, float]] = []
        for r in results:
            ts = r.get("time")
            val = r.get("value")
            if ts and val is not None:
                data.append((ts.timestamp(), float(val)))
        return data

    def _detect_night_silence(self, points: List[Tuple[float, float]],
                              threshold_mbps: float = 1.0,
                              max_gap_minutes: float = 10.0) -> float:
        if len(points) < 2:
            return 0.0
        silent_start = None
        longest = 0.0
        prev_ts = None
        last_ts = points[-1][0]
        for ts, val in points:
            h = datetime.fromtimestamp(ts).hour
            is_night = h >= 21 or h < 8
            if not is_night:
                if silent_start is not None:
                    longest = max(longest, (ts - silent_start) / 60.0)
                    silent_start = None
                prev_ts = ts
                continue
            silent_now = val < threshold_mbps
            if prev_ts is not None and (ts - prev_ts) / 60.0 > max_gap_minutes:
                silent_now = True
            if silent_now and silent_start is None:
                silent_start = ts
            elif not silent_now and silent_start is not None:
                longest = max(longest, (ts - silent_start) / 60.0)
                silent_start = None
            prev_ts = ts
        if silent_start is not None:
            longest = max(longest, (last_ts - silent_start) / 60.0)
        return longest

    def _convert_to_mbps(self, data: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if len(data) < 2:
            return []
        result: List[Tuple[float, float]] = []
        for i in range(1, len(data)):
            t1, v1 = data[i - 1]
            t2, v2 = data[i]
            if v2 >= v1 and t2 > t1:
                bps = (v2 - v1) * 8.0 / (t2 - t1)
                result.append((t2, bps / 1_000_000.0))
        return result

    def _align_rx_tx(self, rx: List[Tuple[float, float]], tx: List[Tuple[float, float]]) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        rx_d = {t: v for t, v in rx}
        tx_d = {t: v for t, v in tx}
        common = sorted(rx_d.keys() & tx_d.keys())
        rx_out = [(t, rx_d[t]) for t in common]
        tx_out = [(t, tx_d[t]) for t in common]
        return rx_out, tx_out

    def _generate_traffic_chart(
        self, host_name: str, iface_name: str, rx_data: List[Tuple[float, float]],
        tx_data: List[Tuple[float, float]], fmt: FormatLoader, idx: int
    ) -> Tuple[Optional[str], Optional[Dict]]:
        if not HAS_MATPLOTLIB:
            return None, None

        dpi = fmt.chart_dpi()
        rx_vals = [d[1] for d in rx_data]
        tx_vals = [d[1] for d in tx_data]
        ts = [d[0] for d in rx_data]
        if not ts:
            return None, None

        combined = [(t, (r if r is not None else 0) + (x if x is not None else 0))
                    for (t, r), (_, x) in zip(rx_data, tx_data)]
        combined_vals = [c[1] for c in combined]

        rx_st = calculate_stats(rx_vals)
        tx_st = calculate_stats(tx_vals)
        combined_st = calculate_stats(combined_vals)
        dn = split_day_night(ts, combined_vals)
        night_silence = self._detect_night_silence(combined)

        fig, ax = plt.subplots(figsize=(14, 4), dpi=dpi)
        dates = [datetime.fromtimestamp(t) for t in ts]
        ax.plot(dates, rx_vals, color='#00FF88', linewidth=2, label='RX (Entrada)', alpha=0.8)
        ax.plot(dates, tx_vals, color='#4488FF', linewidth=2, label='TX (Salida)', alpha=0.8)
        ax.fill_between(dates, rx_vals, alpha=0.3, color='#00FF88')
        ax.fill_between(dates, tx_vals, alpha=0.3, color='#4488FF')
        ax.set_ylabel('Mbps', fontsize=10, color='white')
        ax.tick_params(axis='y', labelcolor='white', colors='white')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
        ax.tick_params(axis='x', labelcolor='white', colors='white', rotation=45)
        leg = ax.legend(loc='upper right', facecolor='#2C2F33', edgecolor='#2C2F33',
                        labelcolor='white', fontsize=9)
        plt.setp(leg.get_texts(), color='white')
        ax.set_title(f"{host_name} - {iface_name}", fontsize=12, fontweight='bold',
                     color='white', pad=10)
        ax.grid(True, alpha=0.2, color='white', linestyle='--')
        fig.patch.set_facecolor('#2C2F33')
        ax.set_facecolor('#2C2F33')
        plt.tight_layout()

        safe_host = host_name.replace(' ', '_')[:20]
        safe_iface = iface_name.replace(' ', '_').replace('/', '_')[:15]
        filename = f"chart_{idx}_{safe_host}_{safe_iface}.png"
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        filepath = CHART_DIR / filename
        plt.savefig(str(filepath), format='png', dpi=dpi,
                    facecolor=fig.get_facecolor(), edgecolor='none',
                    bbox_inches='tight')
        plt.close(fig)

        total_avg = combined_st["avg"]
        total_max = combined_st["max"]
        total_min = combined_st["min"]

        stats = {
            "rx": rx_st,
            "tx": tx_st,
            "day_night": dn,
            "total_avg": total_avg,
            "total_max": total_max,
            "total_min": total_min,
            "host_name": host_name,
            "iface_name": iface_name,
            "night_silence_min": night_silence,
            "silence_warn_min": 30,
        }
        return str(filepath), stats

    # ------------------------------------------------------------------
    # Build report text
    # ------------------------------------------------------------------

    def _build_report(
        self, olts_data: List[Dict], mk_data: List[Dict],
        problems: List[Dict], fmt: FormatLoader,
        chart_stats: Optional[List[Dict]] = None,
        incidents: Optional[List[Dict]] = None
    ) -> Dict[str, str]:
        date_fmt = fmt.get("text", "date_format", "%d/%m/%Y %H:%M")
        now_str = datetime.now().strftime(date_fmt)
        timezone = fmt.get("branding", "timezone", "America/Caracas")

        total_olts = len(olts_data)
        mk_online = sum(1 for m in mk_data if m.get("any_fresh", False))
        mk_ping_only = sum(1 for m in mk_data if not m.get("any_fresh", False) and m.get("ping_up", False))
        total_mk = len(mk_data)
        online_count = total_olts + mk_online + mk_ping_only
        total = total_olts + total_mk
        offline_count = total - online_count
        problem_count = len(problems)
        has_alerts = problem_count > 0 and fmt.show_alerts()

        offline_hosts_list = []
        for m in mk_data:
            if not m.get("any_fresh", False) and not m.get("ping_up", False):
                offline_hosts_list.append("- " + m.get("tags", {}).get("device_name", "?"))
        offline_hosts_text = "\n".join(offline_hosts_list)

        sections_text: Dict[str, str] = {}

        for section in fmt.section_order():
            if section == "summary":
                text = fmt.render(fmt.tmpl("summary"),
                    date=now_str, timezone=timezone,
                    online_count=online_count, total=total,
                    offline_count=offline_count, problem_count=problem_count,
                    offline_hosts=offline_hosts_text,
                    summary_date=fmt.emoji("summary_date"),
                    generated_label=fmt.text_val("generated_label"),
                    summary_online=fmt.emoji("summary_online"),
                    online_label=fmt.text_val("online_label"),
                    summary_offline=fmt.emoji("summary_offline"),
                    offline_label=fmt.text_val("offline_label"),
                    summary_alerts=fmt.emoji("summary_alerts"),
                    alerts_label=fmt.text_val("alerts_label"))
                sections_text["summary"] = text

            elif section == "olt" and olts_data:
                header = fmt.render(fmt.tmpl("olt_section_header"),
                    section_olt=fmt.emoji("section_olt"),
                    olt_section_title=fmt.text_val("olt_section_title"),
                    count=len(olts_data),
                    unit_text=fmt.text_val("unit_text"))
                full = header + "\n"
                for i, olt in enumerate(olts_data):
                    if i > 0:
                        sep = fmt.tmpl("olt_separator")
                        if sep:
                            full += fmt.render(sep) + "\n"
                    tags = olt.get("tags", {})
                    ip = tags.get("olt_ip", tags.get("device_ip", "N/A"))
                    name = tags.get("olt_name", "?")
                    pons = olt.get("pons", [])
                    pons_down = sum(1 for p in pons if p["status"] == "DOWN")
                    pons_up = sum(1 for p in pons if p["status"] == "UP")
                    if pons_down == 0:
                        olt_emoji = fmt.emoji("olt_up")
                    elif pons_up == 0:
                        olt_emoji = fmt.emoji("olt_down")
                    else:
                        olt_emoji = fmt.emoji("olt_warn")
                    full += fmt.render(fmt.tmpl("olt_host"),
                        olt_emoji=olt_emoji, name=name, ip=ip) + "\n"
                    if olt.get("pon_monitoring", True):
                        for p in pons:
                            try:
                                offline_count = int(p.get("offline", 0))
                            except (ValueError, TypeError):
                                offline_count = 0
                            if offline_count > 12:
                                pon_emoji = fmt.emoji("pon_down")
                            elif offline_count > 8:
                                pon_emoji = fmt.emoji("pon_orange")
                            elif offline_count > 5:
                                pon_emoji = fmt.emoji("pon_warn")
                            else:
                                pon_emoji = fmt.emoji("pon_up")
                            full += fmt.render(fmt.tmpl("pon_line"),
                                pon=p["pon"], pon_emoji=pon_emoji,
                                pon_users=fmt.emoji("pon_users"),
                                online=p["online"], offline=p["offline"],
                                traffic_in=fmt.emoji("traffic_in"),
                                traffic_in_val=p["in_val"],
                                traffic_out=fmt.emoji("traffic_out"),
                                traffic_out_val=p["out_val"]) + "\n"
                sections_text["olt"] = full.strip()

            elif section == "mk" and mk_data:
                header = fmt.render(fmt.tmpl("mk_section_header"),
                    section_mk=fmt.emoji("section_mk"),
                    mk_section_title=fmt.text_val("mk_section_title"),
                    count=len(mk_data))
                full = header + "\n"
                for i, mk_info in enumerate(mk_data):
                    if i > 0:
                        sep = fmt.tmpl("mk_separator")
                        if sep:
                            full += fmt.render(sep) + "\n"
                    tags = mk_info.get("tags", {})
                    ip = tags.get("device_ip", "N/A")
                    name = tags.get("device_name", "?")
                    ifaces = mk_info.get("ifaces", [])
                    is_fresh = mk_info.get("any_fresh", False)
                    ping_up = mk_info.get("ping_up", False)
                    fresh_count = sum(1 for i in ifaces if i.get("is_fresh"))
                    if fresh_count == len(ifaces) and fresh_count > 0:
                        mk_emoji = fmt.emoji("mk_up")
                    elif fresh_count > 0:
                        mk_emoji = fmt.emoji("mk_warn")
                    elif ping_up:
                        mk_emoji = fmt.emoji("mk_warn")
                    else:
                        mk_emoji = fmt.emoji("mk_down")
                    full += fmt.render(fmt.tmpl("mk_host"),
                        mk_emoji=mk_emoji, name=name, ip=ip) + "\n"
                    if ifaces:
                        for iface in ifaces:
                            if iface.get("stale", False):
                                full += fmt.render(fmt.tmpl("mk_iface_stale"),
                                    iface=iface["iface_name"],
                                    mk_stale=fmt.emoji("mk_stale"),
                                    stale_label=fmt.text_val("stale_label"),
                                    ago_suffix=fmt.text_val("ago_suffix"),
                                    ago_min=format_age_ago(iface.get("age", 0))) + "\n"
                            else:
                                full += fmt.render(fmt.tmpl("mk_iface"),
                                    iface=iface["iface_name"],
                                    traffic_in=fmt.emoji("traffic_in"),
                                    traffic_in_val=iface.get("traffic_in", "0"),
                                    traffic_out=fmt.emoji("traffic_out"),
                                    traffic_out_val=iface.get("traffic_out", "0")) + "\n"
                    else:
                        if ping_up:
                            latency = mk_info.get("ping_latency")
                            lat = f"{latency:.1f}" if isinstance(latency, (int, float)) else "?"
                            msg = f"{fmt.text_val('no_snmp_msg')} ({lat} ms)"
                        else:
                            msg = fmt.text_val("no_iface_msg")
                        full += fmt.render(fmt.tmpl("mk_no_iface"), no_iface_msg=msg) + "\n"
                sections_text["mk"] = full.strip()

            elif section == "alerts" and has_alerts:
                header = fmt.render(fmt.tmpl("alert_section_header"),
                    section_alerts=fmt.emoji("section_alerts"),
                    alert_section_title=fmt.text_val("alert_section_title"),
                    count=problem_count)
                txt = header + "\n"
                max_alert_name = int(fmt.get("limits", "max_alert_name", "50"))
                for i, p in enumerate(problems):
                    if i > 0:
                        sep = fmt.tmpl("alert_separator")
                        if sep:
                            txt += fmt.render(sep) + "\n"
                    host_name = p.get("host_name", "?")
                    alert_name = p.get("alert_name", "Desconocido")
                    if len(alert_name) > max_alert_name:
                        alert_name = alert_name[:max_alert_name] + "..."
                    time_str = datetime.fromtimestamp(
                        int(p.get("clock", time.time()))
                    ).strftime("%H:%M")
                    alert_emoji = fmt.emoji(f"sev_{p.get('severity', '0')}")
                    ack_icon = fmt.emoji("alert_ack") if p.get("acknowledged") == "1" else ""
                    txt += fmt.render(fmt.tmpl("alert_line"),
                        alert_emoji=alert_emoji, alert_ack=ack_icon,
                        time=time_str, host_name=host_name,
                        alert_name=alert_name) + "\n"
                sections_text["alerts"] = txt.strip()

            elif section == "incidents" and incidents:
                header = fmt.get("templates", "incident_section_header",
                    "{section_alerts} <b>Incidencias 24h (LOS / DyingGap)</b>  —  ult. {count}")
                txt = fmt.render(header,
                    section_alerts=fmt.emoji("section_alerts"),
                    count=len(incidents)) + "\n"
                for incident in incidents:
                    ts = incident.get("time", "")
                    t_str = ts.strftime("%H:%M") if hasattr(ts, "strftime") else "??:??"
                    line = fmt.get("templates", "incident_line",
                        "<code>{time}</code> <b>{olt}</b> {pon} — {type} <i>({serial})</i>")
                    txt += fmt.render(line,
                        time=t_str,
                        olt=incident.get("olt", "?"),
                        pon=incident.get("pon", "?"),
                        type=incident.get("type", "?"),
                        serial=incident.get("serial", "")) + "\n"
                sections_text["incidents"] = txt.strip()

            elif section == "charts" and fmt.show_charts() and chart_stats:
                txt = ""
                for stats in chart_stats:
                    if not stats:
                        continue
                    txt += fmt.render(fmt.tmpl("chart_stats"),
                        chart_icon=fmt.emoji("chart_icon"),
                        host_name=stats.get("host_name", "?"),
                        iface_name=stats.get("iface_name", "?"),
                        night_avg=format_mbps(stats["day_night"]["night"]["avg"]),
                        night_max=format_mbps(stats["day_night"]["night"]["max"]),
                        day_avg=format_mbps(stats["day_night"]["day"]["avg"]),
                        day_max=format_mbps(stats["day_night"]["day"]["max"]),
                        total_avg=format_mbps(stats["total_avg"]),
                        total_max=format_mbps(stats["total_max"]),
                        total_min=format_mbps(stats["total_min"]),
                        analysis_text=analyze_traffic(stats)) + "\n\n"
                sections_text["charts"] = txt.strip()

        return sections_text

    # ------------------------------------------------------------------
    # Telegram sending
    # ------------------------------------------------------------------

    def _send_message(self, text: str) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.error("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no definidos")
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, json=payload, timeout=self.timeout)
                data = resp.json()
                if data.get("ok"):
                    logger.info(f"Mensaje enviado ({len(text)} chars)")
                    return True
                desc = data.get("description", "Unknown")
                if "retry after" in desc.lower():
                    import re as _re
                    m = _re.search(r"retry after (\d+)", desc, _re.IGNORECASE)
                    wait = int(m.group(1)) + 1 if m else 10
                    logger.warning(f"Rate limited, esperando {wait}s (intento {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                logger.error(f"Telegram API error: {desc}")
                return False
            except Exception as e:
                logger.error(f"Error enviando a Telegram: {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                return False
        return False

    def _send_photo(self, photo_path: str, caption: str = "") -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
        caption = caption[:1024]
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with open(photo_path, "rb") as f:
                    files = {"photo": f}
                    payload = {
                        "chat_id": self.chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    }
                    resp = requests.post(url, data=payload, files=files, timeout=self.timeout)
                data = resp.json()
                if data.get("ok"):
                    logger.info(f"Foto enviada: {photo_path}")
                    return True
                desc = data.get("description", "Unknown")
                if "retry after" in desc.lower():
                    import re as _re
                    m = _re.search(r"retry after (\d+)", desc, _re.IGNORECASE)
                    wait = int(m.group(1)) + 2 if m else 10
                    logger.warning(f"Rate limited, esperando {wait}s (intento {attempt+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                logger.error(f"Telegram API error (photo): {desc}")
                return False
            except Exception as e:
                logger.error(f"Error enviando foto: {e}")
                if attempt < max_retries - 1:
                    time.sleep(5)
                    continue
                return False
        return False

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> bool:
        logger.info("=== Iniciando reporte diario ===")
        fmt = FormatLoader(self.format_path)
        stale_threshold = fmt.stale_threshold()
        hours = fmt.chart_hours()

        configured_mikrotiks = self._get_configured_mikrotiks()
        configured_olts = self._get_configured_olts()
        has_mk_config = len(configured_mikrotiks) > 0
        has_olt_config = len(configured_olts) > 0
        has_host_config = has_mk_config or has_olt_config

        # --- OLTs ---
        olt_names = self._get_olt_names()
        olt_names = self._resolve_host_names(olt_names, configured_olts, has_olt_config)
        logger.info(f"OLTs encontradas: {olt_names}")

        olts_data = []
        for name in olt_names:
            cfg = self._olt_config_for(name)
            sys_data = self._get_olt_system(name)
            if not sys_data:
                logger.warning(f"Sin datos de sistema para OLT: {name}")
                if cfg is None:
                    continue
                sys_data = {"tags": {"olt_name": name}, "fields": {}}
            pons = self._get_olt_pons(name)
            pon_monitoring = self._olt_has_pon_monitoring(name)
            pon_counts = self._get_onu_counts_per_pon(name)

            for p in pons:
                pon = p["pon"]
                if pon in pon_counts:
                    p["online"] = str(pon_counts[pon].get("online", 0))
                    p["offline"] = str(pon_counts[pon].get("offline", 0))

            olts_data.append({
                "tags": sys_data["tags"],
                "fields": sys_data["fields"],
                "pons": pons,
                "pon_monitoring": pon_monitoring,
            })
            logger.info(f"  {name}: {len(pons)} PONs (monitoring={pon_monitoring})")

        # --- MikroTiks ---
        mk_names = self._get_mikrotik_names()
        mk_names = self._resolve_host_names(mk_names, configured_mikrotiks, has_mk_config)
        logger.info(f"MikroTiks encontrados: {mk_names}")

        mk_data = []
        for name in mk_names:
            cfg_ifaces = self._mikrotik_interfaces_for(name)
            sys_data = self._get_mikrotik_system(name)
            if not sys_data:
                logger.warning(f"Sin datos de sistema para MikroTik: {name}")
                if cfg_ifaces is None:
                    continue
                sys_data = {"tags": {"device_name": name}, "fields": {}}
            ifaces = self._get_mikrotik_interfaces(name, allowed=set(cfg_ifaces) if cfg_ifaces else None)
            if cfg_ifaces is not None and len(cfg_ifaces) > 0:
                ifaces = [i for i in ifaces if i["iface_name"] in cfg_ifaces]
            any_fresh = any(i.get("is_fresh", False) for i in ifaces)
            mk_data.append({
                "tags": sys_data["tags"],
                "fields": sys_data["fields"],
                "ifaces": ifaces,
                "any_fresh": any_fresh,
            })
            fresh_count = sum(1 for i in ifaces if i.get("is_fresh"))
            logger.info(f"  {name}: {len(ifaces)} interfaces ({fresh_count} fresh)")

        ping_status = self._get_mikrotik_ping_status(
            [m.get("tags", {}).get("device_name", "") for m in mk_data]
        )
        for m in mk_data:
            info = ping_status.get(m.get("tags", {}).get("device_name", ""), {})
            m["ping_up"] = info.get("up", False)
            m["ping_latency"] = info.get("latency")

        # --- Charts ---
        chart_paths: List[str] = []
        chart_stats: List[Dict] = []

        if fmt.show_charts() and HAS_MATPLOTLIB:
            chart_targets: List[Tuple[str, str, str]] = []

            if has_host_config:
                for h_cfg in configured_mikrotiks:
                    hname = h_cfg["name"]
                    for iface in self._mikrotik_analyze_interfaces_for(hname) or []:
                        chart_targets.append((hname, iface, "mikrotik_interface"))
                for h_cfg in configured_olts:
                    hname = h_cfg["name"]
                    for iface in self._olt_ge_analyze_interfaces_for(hname) or []:
                        chart_targets.append((hname, iface, "interface_traffic"))
            else:
                max_mk = fmt.mikrotik_max_ifaces()
                for mk_info in mk_data:
                    name = mk_info.get("tags", {}).get("device_name", "")
                    ifaces = mk_info.get("ifaces", [])
                    top = sorted(
                        [i for i in ifaces if i.get("is_fresh")],
                        key=lambda i: i.get("in_rate", 0) + i.get("out_rate", 0),
                        reverse=True
                    )[:max_mk]
                    for i in top:
                        chart_targets.append((name, i["iface_name"], "mikrotik_interface"))

            for host, iface, measurement in chart_targets:
                rx_hist = self._get_mikrotik_history_24h(host, iface, "ifHCInOctets", hours, measurement)
                tx_hist = self._get_mikrotik_history_24h(host, iface, "ifHCOutOctets", hours, measurement)
                rx_mbps = self._convert_to_mbps(rx_hist)
                tx_mbps = self._convert_to_mbps(tx_hist)
                rx_mbps, tx_mbps = self._align_rx_tx(rx_mbps, tx_mbps)
                if rx_mbps and tx_mbps:
                    path, stats = self._generate_traffic_chart(
                        host, iface, rx_mbps, tx_mbps, fmt, len(chart_paths)
                    )
                    if path:
                        chart_paths.append(path)
                        chart_stats.append(stats)

            logger.info(f"Gráficos generados: {len(chart_paths)}")

        # --- Problems ---
        problems = self._get_recent_problems()
        logger.info(f"Problemas recientes: {len(problems)}")

        # --- Incidents (LOS / DyingGap) ---
        incidents = self._get_recent_incidents()
        logger.info(f"Incidencias (LOS/DyingGap): {len(incidents)}")

        # --- Build report ---
        logger.info("Construyendo reporte...")
        sections = self._build_report(olts_data, mk_data, problems, fmt, chart_stats, incidents)

        # --- Send ---
        logger.info("Enviando reporte a Telegram...")
        all_ok = True

        for section_key in fmt.section_order():
            text = sections.get(section_key, "")
            if not text:
                continue
            chunks = split_text_chunks(text)
            for chunk in chunks:
                ok = self._send_message(chunk)
                if not ok:
                    all_ok = False
                    break
                time.sleep(0.5)
            if not all_ok:
                break

        if chart_paths and all_ok:
            time.sleep(3.0)
            for i, (path, stats) in enumerate(zip(chart_paths, chart_stats)):
                time.sleep(2.0)
                caption = fmt.render(fmt.tmpl("chart_stats"),
                    chart_icon=fmt.emoji("chart_icon"),
                    host_name=stats.get("host_name", "?"),
                    iface_name=stats.get("iface_name", "?"),
                    night_avg=format_mbps(stats["day_night"]["night"]["avg"]),
                    night_max=format_mbps(stats["day_night"]["night"]["max"]),
                    day_avg=format_mbps(stats["day_night"]["day"]["avg"]),
                    day_max=format_mbps(stats["day_night"]["day"]["max"]),
                    total_avg=format_mbps(stats["total_avg"]),
                    total_max=format_mbps(stats["total_max"]),
                    total_min=format_mbps(stats["total_min"]),
                    analysis_text=analyze_traffic(stats))
                ok = self._send_photo(path, caption)
                if not ok:
                    all_ok = False
                    break

        if all_ok:
            logger.info("Reporte diario completado exitosamente")
        else:
            logger.error("Fallo el envío del reporte diario")

        # Cleanup chart files
        for path in chart_paths:
            try:
                os.unlink(path)
            except Exception:
                pass

        return all_ok

    def run_standalone(self) -> int:
        try:
            success = self.run()
            return 0 if success else 1
        except Exception as e:
            logger.error(f"Error en reporte diario: {e}", exc_info=True)
            return 1
