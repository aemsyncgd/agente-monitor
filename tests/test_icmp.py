# tests/test_icmp.py
#
# Reproduce el fallo de portabilidad detectado en el LXC 172.16.125.9:
#   - /proc/sys/net/ipv4/ping_group_range = 65534 65534  (solo gid 65534)
#   - /usr/bin/ping sin cap_net_raw ni setuid
#   => `ping` falla con "Operation not permitted" aunque el host esté vivo.
#   - fping sí tiene cap_net_raw (filecap) y funciona.
#
# El chequeo ICMP debe degradar a fping cuando `ping` no puede hacer ICMP.

from unittest.mock import MagicMock, patch

from src.icmp import icmp_ping
from src.verifier import Verifier
from src.config import Config, VerificationConfig


def _ping_denegado_fping_ok(cmd, *args, **kwargs):
    """ping: EPERM (rc=2). fping: host responde (rc=0)."""
    return MagicMock(returncode=2 if cmd[0] == "ping" else 0)


def test_icmp_ping_degrada_a_fping_cuando_ping_no_tiene_icmp():
    with patch("src.icmp.subprocess.run", side_effect=_ping_denegado_fping_ok) as mock_run:
        res = icmp_ping("10.0.0.1", count=1, timeout=2)

    assert res["ok"] is True
    assert res["tool"] == "fping"
    assert mock_run.call_count == 2


def test_icmp_ping_host_abajo_falla_en_ambas_herramientas():
    with patch("src.icmp.subprocess.run", return_value=MagicMock(returncode=1)) as mock_run:
        res = icmp_ping("10.0.0.1", count=1, timeout=2)

    assert res["ok"] is False
    assert res["tool"] is None
    assert mock_run.call_count == 2


def test_icmp_ping_si_ping_responde_no_invoca_fping():
    with patch("src.icmp.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
        res = icmp_ping("10.0.0.1", count=1, timeout=2)

    assert res["ok"] is True
    assert res["tool"] == "ping"
    assert mock_run.call_count == 1


def test_icmp_ping_reporta_herramienta_y_rc_en_fallo():
    with patch("src.icmp.subprocess.run", side_effect=_ping_denegado_fping_ok):
        # fping también falla (lo simulo devolviendo rc!=0 para las dos)
        with patch("src.icmp.subprocess.run", return_value=MagicMock(returncode=1)) as mock_run:
            res = icmp_ping("10.0.0.1", count=1, timeout=2)

    assert res["ok"] is False
    assert res["returncode"] == 1
    assert mock_run.call_count >= 1
    assert res["attempts"]


def test_verifier_verify_ping_usa_fping_cuando_ping_no_puede_icmp():
    """src/verifier.py usaba `ping` a secas: en el CT todo verificación salía False."""
    config = Config()
    config.verification = VerificationConfig(
        ping_enabled=True, ping_timeout_seconds=1, snmp_enabled=False, ssh_enabled=False
    )
    verifier = Verifier(config, MagicMock())

    with patch("subprocess.run", side_effect=_ping_denegado_fping_ok):
        result = verifier._verify_ping("10.0.0.1")

    assert result.success is True
    assert result.details.get("tool") == "fping"
