# src/collectors/onu_scan.py
import argparse
import logging
import os
import sys
from typing import Dict, List, Any, Optional, Tuple

from ..config_ia import OLTDevice, Node, load_config_ia, load_nodes_config
from ..storage.influx_client import InfluxClient
from .base import CollectorResult
from .influx_writer import CollectorInfluxWriter
from .olt_collector import OltCollector, OltConfig

logger = logging.getLogger("onu_scan")


def select_target_devices(
    nodes: List[Node], olt_ref: Optional[str]
) -> List[Tuple[OLTDevice, str]]:
    """Devuelve [(OLTDevice, node_name)] objetivo.

    Si olt_ref es None (--all), selecciona todas las OLTs del inventory.
    Si olt_ref es hostname o IP, selecciona solo esa OLT.
    """
    if olt_ref is None:
        return [
            (olt, node.name)
            for node in nodes
            for olt in node.olts
        ]
    matches = []
    for node in nodes:
        for olt in node.olts:
            if olt_ref in (olt.hostname, olt.ip):
                matches.append((olt, node.name))
    return matches


def print_summary(metrics: List[Dict[str, Any]]) -> None:
    """Imprime resumen legible a partir de los puntos onu_location."""
    rows = [m for m in metrics if m["measurement"] == "onu_location"]
    if not rows:
        print("  (sin MACs ubicadas en PON en este scan)")
        return
    print(f"  {'MAC':<20} {'UBICACION':<12} {'SERIAL':<15} {'MODELO':<8} ESTADO")
    for m in rows:
        t = m["tags"]
        f = m["fields"]
        print(f"  {t['mac']:<20} {t.get('location',''):<12} "
              f"{f.get('onu_serial','-'):<15} "
              f"{f.get('onu_model','-'):<8} {f.get('estado_onu','-')}")


def main(
    argv: Optional[List[str]] = None,
    collector_factory=None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Captura de MAC de clientes (FDB) desde OLTs VSOL"
    )
    parser.add_argument("--olt", help="Hostname o IP de una OLT especifica")
    parser.add_argument("--all", action="store_true",
                        help="Ejecutar sobre todas las OLTs del inventory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo imprime resumen, no escribe a InfluxDB")
    parser.add_argument("--nodes-path",
                        default=os.environ.get("NODES_PATH", "config/nodes.yaml"),
                        help="Ruta a nodes.yaml (default: $NODES_PATH)")
    parser.add_argument("--config-path",
                        default=os.environ.get("CONFIG_PATH", "config/config_ia.yaml"),
                        help="Ruta a config_ia.yaml (default: $CONFIG_PATH)")
    args = parser.parse_args(argv)

    if args.olt and args.all:
        print("Usar --olt X o --all, no ambos.")
        return 2

    nodes = load_nodes_config(args.nodes_path)
    targets = select_target_devices(nodes, args.olt if not args.all else None)
    if not targets:
        print(f"No se encontraron OLTs objetivo ({args.olt or '--all'}).")
        return 1

    writer = None
    if not args.dry_run:
        config = load_config_ia(args.config_path)
        influx = InfluxClient(
            url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
            token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
            org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
            bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
        )
        if not influx.connect():
            logger.error("No se pudo conectar a InfluxDB")
            return 1
        writer = CollectorInfluxWriter(influx)

    total_written = 0
    for olt, node_name in targets:
        print(f"=== {olt.hostname} ({olt.ip}) nodo {node_name} ===")
        if collector_factory is None:
            cfg = OltConfig(
                ip=olt.ip,
                community=olt.community,
                hostname=olt.hostname,
                modelo=olt.modelo,
                nodo=node_name,
                capturar_mac=olt.capturar_mac,
            )
            collector = OltCollector(config=cfg)
            result = collector.capture_macs()
        else:
            result = collector_factory(olt, node_name)
        for err in result.errors:
            logger.warning(f"{olt.hostname}: {err}")
        if not args.dry_run and result.metrics:
            written = writer.write_collector_result(result)
            total_written += written
            print(f"  -> {written} puntos escritos")
        else:
            print("  -> (dry-run) no se escribio")
        print_summary(result.metrics)

    if not args.dry_run and writer:
        print(f"Total puntos escritos: {total_written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())