# mikrotik-syslog

Este servicio se define **únicamente** en `/home/server/agente-monitor/compose.yml`
(red `agente-monitor-net`).

- `docker-compose.yml.disabled`: definición antigua de este subdirectorio. NO usar;
  crea un segundo contenedor con el mismo nombre y red distinta.
- `config/rsyslog.conf`: escucha en 5514 (no privilegiado) UDP y TCP.
- `logs/all.log`: destino de los logs.

El directorio `logs/` pertenece al uid/gid del usuario `syslog` dentro del
contenedor (101:4). Si se recrea desde cero, ejecutar:

    podman unshare chown -R 101:4 mikrotik-syslog/logs
