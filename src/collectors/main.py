# src/collectors/main.py
import os
import sys
import time
import signal
import logging
import logging.handlers
import threading
import concurrent.futures
from pathlib import Path
from typing import List

from ..config_ia import ConfigIA, load_config_ia
from ..config_manager import get_config_manager, init_config_manager, ConfigManager
from ..storage.influx_client import InfluxClient
from .olt_collector import OltCollector, OltConfig
from .mikrotik_collector import MikroTikCollector, MikroTikConfig
from .ping_collector import PingCollector
from .influx_writer import CollectorInfluxWriter
from .zabbix_sender import ZabbixBridge

logger = logging.getLogger("snmp_collector")


def setup_logging(config):
    log_dir = Path(config.logging.file).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
    )
    
    # Handler en el logger raiz: todos los modulos (influx_client, influx_writer,
    # olt_collector, mikrotik_collector, pysnmp, influxdb_client) loguean al archivo.
    root = logging.getLogger()
    root.setLevel(level)
    
    if not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        for h in root.handlers
    ):
        handler = logging.handlers.RotatingFileHandler(
            config.logging.file,
            maxBytes=config.logging.max_size_mb * 1024 * 1024,
            backupCount=config.logging.backup_count
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    
    if not any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        for h in root.handlers
    ):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root.addHandler(console)
    
    logger.setLevel(level)


def create_collectors_from_config(
    config_manager: ConfigManager,
    olt_timeout: int
) -> tuple[List[OltCollector], List[MikroTikCollector]]:
    """Create collectors from ConfigManager nodes."""
    olt_collectors = []
    mikrotik_collectors = []
    
    for node in config_manager.get_nodes():
        logger.info(f"Processing node: {node.name}")
        
        for olt in node.olts:
            olt_config = OltConfig(
                ip=olt.ip,
                community=olt.community,
                hostname=olt.hostname,
                modelo=olt.modelo,
                nodo=node.name,
                capturar_mac=olt.capturar_mac
            )
            collector = OltCollector(
                config=olt_config,
                timeout=olt_timeout
            )
            olt_collectors.append(collector)
            logger.info(f"  Added OLT: {olt.hostname} ({olt.ip}) - {olt.modelo}")
        
        for mt in node.mikrotiks:
            mikrotik_config = MikroTikConfig(
                ip=mt.ip,
                community=mt.community,
                hostname=mt.hostname,
                modelo=mt.modelo,
                username=mt.username,
                use_api=mt.use_api
            )
            collector = MikroTikCollector(config=mikrotik_config)
            mikrotik_collectors.append(collector)
            logger.info(f"  Added MikroTik: {mt.hostname} ({mt.ip}) - {mt.role}")
    
    return olt_collectors, mikrotik_collectors


def mikrotik_ping_worker(
    running,
    config_manager,
    writer,
    zabbix_bridge,
    mikrotik_interval,
    ping_collector,
    ping_interval
):
    """Background thread: collects MikroTik metrics and ping independently of OLT cycle."""
    import concurrent.futures
    
    mikrotik_collectors = []
    
    def rebuild_mikrotik_collectors():
        nonlocal mikrotik_collectors
        new_olt, new_mt = create_collectors_from_config(
            config_manager,
            olt_timeout=30
        )
        mikrotik_collectors = new_mt
        logger.info(f"MikroTik worker: {len(mikrotik_collectors)} collectors")
    
    rebuild_mikrotik_collectors()
    
    last_mikrotik = time.time()
    last_ping = time.time()
    last_config_reload = time.time()
    
    while running.is_set():
        now = time.time()
        
        # Periodic config reload (every 5 min)
        if now - last_config_reload > 300:
            try:
                config_manager.reload_config()
                rebuild_mikrotik_collectors()
            except Exception as e:
                logger.error(f"MikroTik worker config reload failed: {e}")
            last_config_reload = time.time()
            now = time.time()
        
        # MikroTik collection
        if now - last_mikrotik >= mikrotik_interval:
            def collect_mt(c):
                try:
                    return c.collect()
                except Exception as e:
                    logger.error(f"MikroTik failed {c.config.hostname}: {e}")
                    return None
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = {executor.submit(collect_mt, c): c for c in mikrotik_collectors}
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result and result.metrics:
                        writer.write_collector_result(result)
                        logger.info(f"{result.device_name}: {len(result.metrics)} metrics "
                                  f"in {result.duration_seconds:.1f}s")
                    if result and result.errors:
                        logger.warning(f"{result.device_name}: {result.errors}")
            last_mikrotik = time.time()
        
        # Ping collection
        if ping_collector and (time.time() - last_ping >= ping_interval):
            try:
                result = ping_collector.collect()
                if result.metrics:
                    written = writer.write_collector_result(result)
                    up_count = sum(1 for m in result.metrics if m["fields"]["status"] == 1)
                    down_count = sum(1 for m in result.metrics if m["fields"]["status"] == 0)
                    logger.info(f"Ping: {up_count} up, {down_count} down, "
                              f"{written} points in {result.duration_seconds:.1f}s")
                if result.errors:
                    logger.warning(f"Ping errors: {result.errors}")
                if zabbix_bridge:
                    try:
                        zabbix_bridge.send_ping_result(result)
                    except Exception as e:
                        logger.warning(f"Zabbix ping send failed: {e}")
            except Exception as e:
                logger.error(f"Ping collection failed: {e}")
            last_ping = time.time()
        
        # Sleep 5s between checks
        time.sleep(5)


def main():
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    nodes_path = os.environ.get("NODES_PATH", "config/nodes.yaml")
    
    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        return 1
    
    config = load_config_ia(config_path)
    setup_logging(config)
    
    logger.info("Starting SNMP Collector")
    
    config_manager = init_config_manager(config_path, nodes_path)
    
    summary = config_manager.get_summary()
    logger.info(f"Configuration loaded: {summary['nodes_count']} nodes, "
                f"{summary['total_mikrotiks']} MikroTik, {summary['total_olts']} OLTs")
    
    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
        token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
        org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
        bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket)
    )
    
    if not influx.connect():
        logger.error("Failed to connect to InfluxDB")
        return 1
    
    writer = CollectorInfluxWriter(influx)
    
    zabbix_bridge = None
    if hasattr(config, 'zabbix') and config.zabbix.enabled:
        zabbix_bridge = ZabbixBridge(
            server=config.zabbix.server,
            port=config.zabbix.port
        )
        logger.info(f"Zabbix bridge enabled: {config.zabbix.server}:{config.zabbix.port}")
    
    olt_collectors, _ = create_collectors_from_config(
        config_manager,
        olt_timeout=config.collectors.olt_timeout
    )
    
    ping_collector = None
    ping_targets_path = os.environ.get("PING_TARGETS_PATH", "config/ping_targets.yaml")
    if os.path.exists(ping_targets_path):
        ping_collector = PingCollector(config_path=ping_targets_path)
        logger.info(f"Ping collector loaded with {len(ping_collector.targets)} targets")
    
    logger.info(f"Created {len(olt_collectors)} OLT collectors, "
                f"ping {'enabled' if ping_collector else 'disabled'}")
    
    # Signal handling
    running = threading.Event()
    running.set()
    
    def signal_handler(signum, frame):
        logger.info("Shutdown signal received")
        running.clear()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start background thread for MikroTik + Ping (independent of OLT cycle)
    mikrotik_interval = 60
    ping_interval = 60
    
    bg_thread = threading.Thread(
        target=mikrotik_ping_worker,
        args=(running, config_manager, writer, zabbix_bridge,
              mikrotik_interval, ping_collector, ping_interval),
        daemon=True,
        name="mikrotik-ping"
    )
    bg_thread.start()
    logger.info(f"Background thread started: MikroTik every {mikrotik_interval}s, "
                f"ping every {ping_interval}s")
    
    # Main loop: OLT collection en paralelo, continua, con pausa olt_rest_seconds
    # entre pasadas. Antes el ciclo era secuencial (~22 min) y dejaba a las OLTs
    # del inicio del ciclo (SISAL/PRADO/YAGUA) fuera de la ventana de consulta
    # del modal; en paralelo una pasada completa tarda ~ max(ciclo_por_OLT).
    last_config_reload = time.time()
    config_reload_interval = 300

    olt_rest = max(0, int(config.collectors.olt_rest_seconds))
    logger.info(
        f"OLT scan: {len(olt_collectors)} OLTs en paralelo, "
        f"pausa de {olt_rest}s entre pasadas"
    )

    while running.is_set():
        try:
            cycle_start = time.time()

            # Periodic config reload (solo entre pasadas, sin escaneos en vuelo)
            if time.time() - last_config_reload > config_reload_interval:
                try:
                    config_manager.reload_config()
                    olt_collectors, _ = create_collectors_from_config(
                        config_manager,
                        olt_timeout=config.collectors.olt_timeout
                    )
                    logger.info(f"Config reloaded: {len(olt_collectors)} OLT collectors")
                except Exception as e:
                    logger.error(f"Failed to reload config: {e}")
                last_config_reload = time.time()
            
            # Collect from all OLTs in parallel
            def collect_one(c):
                try:
                    return c.collect()
                except Exception as e:
                    logger.error(f"OLT collection failed {c.config.hostname}: {e}")
                    return None

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, len(olt_collectors))
            ) as executor:
                futures = {
                    executor.submit(collect_one, c): c
                    for c in olt_collectors
                }
                for future in concurrent.futures.as_completed(futures):
                    collector = futures[future]
                    result = future.result()
                    if result is None:
                        continue
                    if result.metrics:
                        written = writer.write_collector_result(result)
                        logger.info(
                            f"{result.device_name}: {len(result.metrics)} metrics "
                            f"({written} escritos) in {result.duration_seconds:.1f}s"
                        )
                    if result.errors:
                        logger.warning(f"{result.device_name}: {result.errors}")
                    if zabbix_bridge:
                        try:
                            zabbix_bridge.send_olt_result(result)
                        except Exception as e:
                            logger.warning(
                                f"Zabbix OLT send failed for {result.device_name}: {e}"
                            )
            
            cycle_duration = time.time() - cycle_start
            logger.info(f"OLT cycle completed in {cycle_duration:.1f}s")
            
            # Pausa olt_rest_seconds entre pasadas (en chunks para apagar suave)
            sleep_time = olt_rest
            elapsed = 0
            while elapsed < sleep_time and running.is_set():
                time.sleep(min(10, sleep_time - elapsed))
                elapsed += 10
        
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error in OLT loop: {e}", exc_info=True)
            time.sleep(30)
    
    # Cleanup
    influx.close()
    logger.info("SNMP Collector stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
