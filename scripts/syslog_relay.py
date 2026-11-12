#!/usr/bin/env python3
"""
Relay syslog rootless para MikroTik/OLTs.

Problema: el contenedor "mikrotik-syslog" recibe el tráfico a través del NAT de
podman rootless (rootlessport/slirp), así que `fromhost-ip` siempre es la IP
interna del contenedor. Todos los equipos aparecían como 10.89.6.x.

Solución: este relay escucha en el puerto 5514 del host (donde ya apuntan los
MikroTik) y reenvía cada mensaje al contenedor por 127.0.0.1:5515, añadiendo
`__SRCIP__=<ip real>` al inicio del mensaje. Como el relay ve el paquete
original, la IP de origen se conserva; `scripts/log_monitor.py` extrae la marca
y la usa como `device_ip`.

No requiere root: escucha un puerto >1024 y solo usa la librería estándar.

Variables de entorno (opcionales):
    SYSLOG_RELAY_LISTEN_HOST      (0.0.0.0)
    SYSLOG_RELAY_PORT             (5514)
    SYSLOG_RELAY_FORWARD_HOST     (127.0.0.1)
    SYSLOG_RELAY_FORWARD_PORT     (5515)
    SYSLOG_RELAY_FORWARD_TIMEOUT  (2 s)
"""

import logging
import os
import re
import socket
import socketserver
import sys
import threading
import time

LISTEN_HOST = os.getenv("SYSLOG_RELAY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("SYSLOG_RELAY_PORT", "5514"))
FORWARD_HOST = os.getenv("SYSLOG_RELAY_FORWARD_HOST", "127.0.0.1")
FORWARD_PORT = int(os.getenv("SYSLOG_RELAY_FORWARD_PORT", "5515"))
FORWARD_TIMEOUT = float(os.getenv("SYSLOG_RELAY_FORWARD_TIMEOUT", "2"))

SRCIP_TOKEN = "__SRCIP__="

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# <PRI>Mmm dd HH:MM:SS HOST TAG: mensaje
RFC3164_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>"
    r"(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<rest>.*)$",
    re.S,
)
# El tag syslog es el primer token: NO puede contener espacios. Si el mensaje
# trae un ":" más adelante (p.ej. "netwatch,info event down [ type: ...]"),
# no debe confundirse con el separador del tag.
TAG_RE = re.compile(r"^(?P<tag>[^\s:]{1,64}):\s?(?P<msg>.*)$", re.S)
VALID_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")

logger = logging.getLogger("syslog_relay")


def _timestamp() -> str:
    now = time.localtime()
    return (f"{MONTHS[now.tm_mon - 1]} {now.tm_mday:2d} "
            f"{now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d}")


def _clean(value: str) -> str:
    """Un mensaje syslog no puede contener saltos de línea."""
    return value.replace("\r", " ").replace("\n", " ")


def build_forward_line(raw: str, peer_ip: str) -> str:
    """Convierte un paquete syslog en una línea para el contenedor.

    Conserva PRI, timestamp y hostname originales; añade la IP real al
    principio del mensaje para que log_monitor pueda atribuir el evento.
    """
    line = _clean(raw).strip()
    match = RFC3164_RE.match(line)
    if match:
        pri = match.group("pri")
        timestamp = match.group("ts")
        host = match.group("host")
        rest = match.group("rest")
    else:
        pri = "13"  # user.notice
        timestamp = _timestamp()
        host = peer_ip
        rest = line

    if not VALID_HOST_RE.match(host):
        host = peer_ip

    tag_match = TAG_RE.match(rest)
    if tag_match:
        tag = tag_match.group("tag").strip().rstrip(":") or "routeros"
        message = tag_match.group("msg")
    else:
        # RouterOS suele mandar el tópico como primera palabra SIN dos puntos
        # ("pppoe,ppp,debug,packet     sent PADI"). Hay que conservarlo como tag
        # para que el filtro de ruido de rsyslog ($syslogtag contains ",debug")
        # siga funcionando; si no, todo quedaría como "routeros:".
        parts = rest.split(None, 1)
        if parts:
            tag = parts[0].strip().rstrip(":") or "routeros"
            message = parts[1] if len(parts) > 1 else ""
        else:
            tag = "routeros"
            message = ""

    return f"<{pri}>{timestamp} {host} {tag}: {SRCIP_TOKEN}{peer_ip} {message}"


class Forwarder:
    """Conexión TCP persistente al contenedor; reconecta si se cae."""

    def __init__(self, host: str = FORWARD_HOST, port: int = FORWARD_PORT):
        self.host = host
        self.port = port
        self._sock = None
        self._lock = threading.Lock()

    def _connect(self):
        self._sock = socket.create_connection(
            (self.host, self.port), timeout=FORWARD_TIMEOUT
        )

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def send(self, line: str) -> bool:
        payload = line.encode("utf-8", "replace") + b"\n"
        with self._lock:
            for _ in range(2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall(payload)
                    return True
                except OSError as exc:
                    self._close()
                    last_error = exc
            logger.warning("no se pudo reenviar al contenedor: %s", last_error)
            return False


class TCPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        peer_ip = self.client_address[0]
        for raw in self.rfile:
            self.server.forwarder.send(
                build_forward_line(raw.decode("utf-8", "replace"), peer_ip)
            )


class RelayTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, forwarder):
        self.forwarder = forwarder
        super().__init__(address, TCPHandler)


def serve_udp(forwarder: Forwarder):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    logger.info("escuchando UDP en %s:%s", LISTEN_HOST, LISTEN_PORT)
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except OSError as exc:
            logger.warning("error recibiendo UDP: %s", exc)
            time.sleep(0.5)
            continue
        forwarder.send(
            build_forward_line(data.decode("utf-8", "replace"), addr[0])
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    forwarder = Forwarder()

    try:
        tcp_server = RelayTCPServer((LISTEN_HOST, LISTEN_PORT), forwarder)
    except OSError as exc:
        logger.error("no se pudo escuchar TCP en %s:%s: %s",
                     LISTEN_HOST, LISTEN_PORT, exc)
        return 1

    threading.Thread(target=tcp_server.serve_forever, daemon=True).start()
    logger.info("escuchando TCP en %s:%s", LISTEN_HOST, LISTEN_PORT)
    logger.info("reenviando a %s:%s (marca %s)",
                FORWARD_HOST, FORWARD_PORT, SRCIP_TOKEN)

    try:
        serve_udp(forwarder)
    except KeyboardInterrupt:
        logger.info("detenido por el usuario")
    finally:
        tcp_server.shutdown()
        tcp_server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
