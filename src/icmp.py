# src/icmp.py
"""
Chequeo ICMP con fallback a fping.

`ping` (iputils) necesita cap_net_raw+p o un ping_group_range amplio. En
contenedores LXC unprivileged el default frecuente es `65534 65534` y ping
falla con "Operation not permitted" aunque el host esté vivo, generando falsos
"host caído". fping lleva filecap cap_net_raw y sí funciona, así que se usa
como respaldo: si ninguna herramienta responde, el host realmente está abajo.
"""

import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _attempt(name: str, cmd: List[str], budget: float) -> Tuple[Optional[bool], Optional[int], str]:
    """Ejecuta una herramienta ICMP. Devuelve (ok, returncode, nota_para_el_log)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=budget)
    except FileNotFoundError:
        return None, None, f"{name}: no instalado"
    except subprocess.TimeoutExpired:
        return None, None, f"{name}: timeout"
    except Exception as exc:  # noqa: BLE001 - cualquier error debe degradar a la siguiente herramienta
        return None, None, f"{name}: {type(exc).__name__}: {exc}"

    if proc.returncode == 0:
        return True, proc.returncode, f"{name}: rc=0"

    detail = ""
    stderr = getattr(proc, "stderr", None)
    if isinstance(stderr, (bytes, bytearray)):
        lines = stderr.decode(errors="replace").strip().splitlines()
        if lines:
            detail = f" ({lines[-1]})"
    return False, proc.returncode, f"{name}: rc={proc.returncode}{detail}"


def icmp_ping(ip: str, count: int = 2, timeout: int = 2) -> Dict[str, Any]:
    """
    Prueba ICMP a `ip` con `ping` y, si falla, reintenta con `fping`.

    Returns:
        {"ok": bool, "tool": "ping"|"fping"|None, "returncode": int, "attempts": [str]}
    """
    attempts: List[str] = []
    budget = timeout * count + 3

    ok, rc, note = _attempt("ping", ["ping", "-c", str(count), "-W", str(timeout), ip], budget)
    attempts.append(note)
    if ok is True:
        return {"ok": True, "tool": "ping", "returncode": rc, "attempts": attempts}

    last_rc = rc
    ok, rc, note = _attempt(
        "fping",
        ["fping", "-c", str(count), "-t", str(timeout * 1000), "-r", "1", ip],
        budget,
    )
    attempts.append(note)
    last_rc = rc if rc is not None else last_rc

    if ok is True:
        logger.debug("icmp_ping %s: `ping` no sirvió (%s); respondió vía fping", ip, attempts[0])
        return {"ok": True, "tool": "fping", "returncode": 0, "attempts": attempts}

    return {"ok": False, "tool": None, "returncode": last_rc if last_rc is not None else 2,
            "attempts": attempts}
