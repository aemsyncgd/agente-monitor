"""Utilidades para interpretar la severidad de los syslog de RouterOS.

RouterOS envía casi todos sus mensajes con severidad syslog ``notice`` y
codifica el nivel real en el tópico del tag: ``script,error``,
``system,error,critical``, ``pppoe,ppp,error``, ``netwatch,info``, etc.

Por eso no basta con mirar el campo ``severity``: hay que derivar la
severidad efectiva desde el tag. Este módulo centraliza esa lógica para que
el monitor de logs, la API y el dashboard clasifiquen igual.
"""

from typing import Optional, Tuple

# Niveles canónicos, de más a menos grave.
_CANONICAL = {
    "emerg": "critical",
    "emergency": "critical",
    "alert": "critical",
    "crit": "critical",
    "critical": "critical",
    "err": "error",
    "error": "error",
    "warn": "warning",
    "warning": "warning",
    "notice": "notice",
    "info": "info",
    "informational": "info",
    "debug": "debug",
}

_TOPIC_PRIORITY = (
    "emerg", "emergency", "alert", "crit", "critical",
    "err", "error",
    "warn", "warning",
    "notice",
    "info", "informational",
    "debug",
)

# Rango para combinar tag y severity: nunca subestimar una alerta.
_RANK = {
    "critical": 5,
    "error": 4,
    "warning": 3,
    "notice": 2,
    "info": 1,
    "debug": 0,
    "": -1,
}

# Palabras del mensaje que ya se consideraban importantes (se mantienen).
IMPORTANT_MESSAGE_KEYWORDS = (
    "down", "offline", "fail", "failure", "reboot", "reset",
    "link down", "interface down", "pppoe", "dhcp", "radius",
    "auth fail", "login failed", "blocked", "attack", "ospf", "bgp",
    "neighbor down",
)

_WARNING_KEYWORDS = ("down", "fail", "link")


def _normalize(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _topic_levels(tag: Optional[str]) -> set:
    """Niveles presentes en un tag de RouterOS (``pppoe,ppp,error``)."""
    levels = set()
    for segment in _normalize(tag).split(","):
        segment = segment.strip().strip(":").strip()
        if segment in _CANONICAL:
            levels.add(_CANONICAL[segment])
    return levels


def effective_severity(tag: Optional[str], severity: Optional[str] = None) -> str:
    """Severidad real del evento, según el tópico del tag y el campo syslog.

    RouterOS pone ``notice`` en casi todos los mensajes, pero el tópico
    (``script,error``) sí distingue el nivel. Se toma el nivel más grave
    entre tag y ``severity`` para no subestimar ninguna alerta.

    Excepción: los tags de depuración (``pppoe,ppp,debug,packet``) llegan con
    ``severity=notice``; si el tag dice ``debug`` y el campo no aporta un
    nivel más grave, el evento es ruido de depuración.
    """
    levels = _topic_levels(tag)
    canonical_severity = _CANONICAL.get(_normalize(severity), _normalize(severity))

    if "debug" in levels and canonical_severity in ("", "notice", "info", "debug"):
        return "debug"

    # ``notice`` es el relleno que pone RouterOS: si el tag trae nivel, manda el tag.
    if canonical_severity == "notice" and levels:
        candidates = set(levels)
    else:
        candidates = levels | ({canonical_severity} if canonical_severity else set())

    if not candidates:
        return ""

    return max(candidates, key=lambda level: _RANK.get(level, -1))


def is_important(event: dict) -> Tuple[bool, Optional[str]]:
    """Devuelve (importante, motivo) para un evento ya enriquecido o crudo."""
    tag = event.get("tag")
    severity = effective_severity(tag, event.get("severity"))
    message = _normalize(event.get("message"))

    if severity in ("critical", "error", "alert", "emerg"):
        return True, severity

    if any(keyword in message for keyword in IMPORTANT_MESSAGE_KEYWORDS):
        return True, "pattern"

    if severity == "warning" and any(keyword in message for keyword in _WARNING_KEYWORDS):
        return True, severity

    return False, None
