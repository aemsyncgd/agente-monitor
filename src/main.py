# src/main.py
import os
import sys
import time
import signal
import logging
import logging.handlers
from pathlib import Path

from .config import load_config
from .zabbix_client import ZabbixClient
from .buffer import EventBuffer
from .monitor import Monitor
from .verifier import Verifier
from .grouper import ZoneGrouper
from .notifier import TelegramNotifier

logger = logging.getLogger("zabbix_agent")

# Mapeo de OLTs en Zabbix a Nodos del CSV
# El nombre del host en Zabbix contiene el nombre de la OLT
# Ej: "OLT-NARANJILLOS-1" → Nodo "NARANJILLOS-VIDANET"
NODE_MAP = {
    "NARANJILLOS": "NARANJILLOS-VIDANET",
    "PRADO": "PRADO-VIDANET",
    "SISAL": "SISAL-VIDANET",
    "YAGUA": "YAGUA-VIDANET",
}

def resolve_node_from_host(zabbix_host: str) -> str:
    """Extrae el nodo del CSV a partir del nombre del host en Zabbix."""
    host_upper = zabbix_host.upper()
    for keyword, node in NODE_MAP.items():
        if keyword in host_upper:
            return node
    return zabbix_host  # Fallback: usar el nombre del host como nodo

def setup_logging(config):
    log_dir = Path(config.logging.file).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    
    handler = logging.handlers.RotatingFileHandler(
        config.logging.file,
        maxBytes=config.logging.max_size_mb * 1024 * 1024,
        backupCount=config.logging.backup_count
    )
    
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
    )
    handler.setFormatter(formatter)
    
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
    
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

def main():
    config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")
    
    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        return 1
    
    config = load_config(config_path)
    setup_logging(config)
    
    logger.info("Starting Zabbix Realtime Agent")
    
    zabbix_url = os.environ.get("ZABBIX_API_URL", "")
    zabbix_user = os.environ.get("ZABBIX_API_USER", "")
    zabbix_pass = os.environ.get("ZABBIX_API_PASS", "")
    if not all([zabbix_url, zabbix_user, zabbix_pass]):
        logger.error("ZABBIX_API_URL, ZABBIX_API_USER, ZABBIX_API_PASS must be set")
        return 1

    zabbix_client = ZabbixClient(
        url=zabbix_url,
        username=zabbix_user,
        password=zabbix_pass
    )
    
    token = zabbix_client.login()
    if not token:
        logger.error("Failed to login to Zabbix")
        return 1
    
    buffer = EventBuffer(window_seconds=config.monitoring.threshold_window_minutes * 60 * 2)
    monitor = Monitor(config, zabbix_client, buffer)
    verifier = Verifier(config, zabbix_client)
    grouper = ZoneGrouper(config)
    notifier = TelegramNotifier(config)
    
    csv_path = config.csv.path
    if os.path.exists(csv_path):
        clients = grouper.load_csv(csv_path)
        
        if config.ml.model_path and os.path.exists(config.ml.model_path):
            grouper.load_model(config.ml.model_path)
        
        if not grouper.model_fitted:
            addresses = [c.direccion for c in clients]
            grouper.fit(addresses)
        
        logger.info(f"Loaded {len(clients)} clients, {len(set(c.nodo for c in clients if c.nodo))} nodes")
    else:
        clients = []
        logger.warning(f"CSV not found: {csv_path}")
    
    running = True
    
    def signal_handler(signum, frame):
        nonlocal running
        logger.info("Shutdown signal received")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info(f"Monitoring every {config.monitoring.interval_seconds}s")
    
    while running:
        try:
            events = monitor.run_once()
            
            if events:
                groups = buffer.group_by_pon()
                
                for (olt_name, pon), pon_events in groups.items():
                    if len(pon_events) >= config.monitoring.threshold_events:
                        sample_event = pon_events[0]
                        
                        # Resolver el nodo a partir del host de Zabbix
                        nodo = resolve_node_from_host(olt_name)
                        
                        verification = verifier.verify_fault(sample_event)
                        verification.nodo = nodo
                        
                        if verification.confirmed:
                            # Buscar clientes por nodo (del CSV) + PON
                            zone_clients = [c for c in clients 
                                          if c.nodo == nodo and c.puerto_pon == pon]
                            
                            # Fallback: solo por nodo
                            if not zone_clients:
                                zone_clients = [c for c in clients if c.nodo == nodo]
                            
                            # Fallback: solo por OLT de Zabbix
                            if not zone_clients:
                                zone_clients = [c for c in clients if olt_name.lower() in c.nodo.lower()]
                            
                            if zone_clients:
                                # Agrupar por zona (ML o por defecto)
                                zone_groups: dict[str, list] = {}
                                for c in zone_clients:
                                    zone = c.zona or grouper.predict_zone(c.direccion)
                                    if zone not in zone_groups:
                                        zone_groups[zone] = []
                                    zone_groups[zone].append(c)
                                
                                for zone, z_clients in zone_groups.items():
                                    notifier.send_alert(zone, z_clients, verification)
            
            time.sleep(config.monitoring.interval_seconds)
        
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            time.sleep(config.monitoring.interval_seconds)
    
    logger.info("Zabbix Realtime Agent stopped")
    return 0

if __name__ == "__main__":
    sys.exit(main())
