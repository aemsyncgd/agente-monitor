# src/collectors/olt_web_mac.py
"""Respaldo web para la tabla MAC por ONU de OLTs VSOL.

Cuando SNMP no expone la FDB privada (OID_VSOL_MAC_TABLE) se consulta la
misma informacion desde el panel web de la OLT (pagina "MAC Table",
`/action/macinfo.html`), que tambien atribuye cada MAC al puerto
`PON<n>:ONU<m>` donde esta instalado el cliente.

Credenciales via entorno (se cargan desde .env.ia):
    OLT_WEB_USER=root
    OLT_WEB_PASSWORD=...
    OLT_WEB_ENABLED=true

El login del panel no usa cookies (la sesion se ata a la IP de origen), por
lo que se crea una `requests.Session` por consulta con `verify=False` (la
OLT usa certificado autofirmado).
"""
import os
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WEB_MAC_PATH = "/action/macinfo.html"
WEB_LOGIN_PATH = "/action/main.html"
WEB_LOGIN_WHO = "100"

# <tr><td>256</td><td>6C:68:A4:D4:62:B9</td><td>Dynamic</td><td>PON1:ONU8</td></tr>
# Hay <script> entre filas; se ignoran.
_MACROW_RE = re.compile(
    r"<tr>\s*"
    r"<td>\s*([^<]*?)\s*</td>\s*"
    r"<td>\s*([^<]*?)\s*</td>\s*"
    r"<td>\s*([^<]*?)\s*</td>\s*"
    r"<td>\s*([^<]*?)\s*</td>\s*"
    r"</tr>",
    re.IGNORECASE,
)
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")


def web_mac_enabled() -> bool:
    """True si el respaldo web esta habilitado y hay contrasena configurada."""
    flag = os.environ.get("OLT_WEB_ENABLED", "true").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return bool(web_mac_credentials()[1])


def web_mac_credentials() -> Tuple[str, str]:
    """(usuario, contrasena) desde el entorno."""
    user = os.environ.get("OLT_WEB_USER", "root").strip() or "root"
    password = os.environ.get("OLT_WEB_PASSWORD", "")
    return user, password


def parse_macinfo_html(html: str) -> List[Dict[str, Any]]:
    """Extrae filas {mac, port, vlan, type} de la pagina MAC Table."""
    rows: List[Dict[str, Any]] = []
    for vlan, mac, mtype, port in _MACROW_RE.findall(html or ""):
        mac = mac.strip().upper()
        if not _MAC_RE.match(mac):
            continue
        vlan_val: Optional[int]
        try:
            vlan_val = int(vlan)
        except (ValueError, TypeError):
            vlan_val = None
        rows.append({
            "mac": mac,
            "port": port.strip(),
            "vlan": vlan_val,
            "type": mtype.strip(),
        })
    return rows


def fetch_web_macs(
    olt_ip: str,
    user: Optional[str] = None,
    password: Optional[str] = None,
    timeout: int = 15,
    scheme: str = "https",
) -> List[Dict[str, Any]]:
    """Login al panel VSOL y devuelve las filas de la MAC Table.

    Lanza excepcion si el login/consulta falla; el llamador decide el fallback.
    """
    import requests

    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass

    if user is None or password is None:
        env_user, env_pass = web_mac_credentials()
        user = user if user is not None else env_user
        password = password if password is not None else env_pass

    base = f"{scheme}://{olt_ip}"
    payload = {"user": user, "pass": password, "who": WEB_LOGIN_WHO}

    session = requests.Session()
    session.verify = False
    session.post(base + WEB_LOGIN_PATH, data=payload, timeout=timeout, verify=False)
    res = session.get(base + WEB_MAC_PATH, timeout=timeout, verify=False)
    res.raise_for_status()
    return parse_macinfo_html(res.text)
