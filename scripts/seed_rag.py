#!/usr/bin/env python3
"""
scripts/seed_rag.py
Carga inicial de documentos al RAG (vector store).

Uso:
    python scripts/seed_rag.py [--docs DIR] [--force]

Indexa los documentos de docs/ (manuales, runbooks, topología) al RAG.
"""
import os
import sys
import json
import time
import argparse
import logging
from pathlib import Path

# Asegurar que el proyecto está en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("seed_rag")


def index_topology(rag, config):
    """Exporta la topología desde InfluxDB y la indexa."""
    from src.storage.influx_client import InfluxClient

    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
        token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
        org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
        bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
    )
    if not influx.connect():
        logger.warning("No se pudo conectar a InfluxDB — topología no indexada")
        return 0

    # OLT summary
    flux = f'''
    from(bucket: "{config.influxdb.bucket}")
        |> range(start: -1h)
        |> filter(fn: (r) => r["_measurement"] == "olt_system")
        |> last() |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")'''
    results = influx.query(flux)

    if not results:
        logger.warning("Sin datos OLT para indexar topología")
        return 0

    text_parts = ["# Topología de Red VidaNet — OLTs\n"]
    for r in results:
        text_parts.append(
            f"OLT: {r.get('olt_name', r.get('olt_hostname', 'N/A'))} | "
            f"Nodo: {r.get('nodo', 'N/A')} | Modelo: {r.get('modelo', 'N/A')} | "
            f"CPU: {r.get('cpu_usage', r.get('cpu', 'N/A'))} | "
            f"RAM: {r.get('ram_usage', r.get('mem_usage', 'N/A'))} | "
            f"ONUs: {r.get('onu_total', r.get('onus_total', 'N/A'))}\n"
        )

    topology_text = "\n".join(text_parts)
    count = rag.index_text(
        topology_text,
        metadata={"type": "topology", "kind": "olt"},
        doc_id="topology_olts",
    )
    logger.info(f"Topología OLT indexada: {count} chunks")
    return count


def index_failure_history(rag, config):
    """Indexa histórico de fallas (si existe logs/llm_events.json)."""
    events_file = "logs/llm_events.json"
    if not os.path.exists(events_file):
        logger.info("No hay histórico de fallas (logs/llm_events.json) para indexar")
        return 0

    try:
        with open(events_file, "r", encoding="utf-8") as f:
            events = json.load(f)
    except Exception as e:
        logger.warning(f"No se pudo leer {events_file}: {e}")
        return 0

    if isinstance(events, dict):
        events = events.get("events", [])

    if not isinstance(events, list) or not events:
        logger.info("Sin eventos en el histórico")
        return 0

    text_parts = ["# Histórico de Fallas con Resolución\n"]
    for ev in events[:200]:
        if isinstance(ev, dict):
            title = ev.get("title", "")
            detail = ev.get("detail", "")
            severity = ev.get("severity", "")
            text_parts.append(f"## {title} [{severity}]\n{detail}\n")

    history_text = "\n".join(text_parts)
    count = rag.index_text(
        history_text,
        metadata={"type": "failure_history"},
        doc_id="failure_history",
    )
    logger.info(f"Histórico de fallas indexado: {count} chunks")
    return count


def main():
    parser = argparse.ArgumentParser(description="Seed RAG con conocimiento de la red")
    parser.add_argument("--docs", default="docs/", help="Directorio de documentos")
    parser.add_argument("--force", action="store_true", help="Reindexar todo")
    parser.add_argument("--config", default="config/config_ia.yaml", help="Ruta config")
    args = parser.parse_args()

    from src.config_ia import load_config_ia
    from src.ai.rag import get_rag_store

    config = load_config_ia(args.config)
    rag = get_rag_store()

    if not rag.ready:
        logger.error("RAG no disponible (chromadb no instalado)")
        return 1

    logger.info(f"RAG listo. Documentos actuales: {rag.count()}")

    total = 0

    # 1. Documentos locales (manuales, runbooks)
    docs_dir = args.docs
    if os.path.exists(docs_dir):
        count = rag.index_directory(docs_dir)
        total += count
        logger.info(f"Documentos locales: {count} chunks")

    # 2. Topología desde InfluxDB
    if args.force or True:
        count = index_topology(rag, config)
        total += count

    # 3. Histórico de fallas
    count = index_failure_history(rag, config)
    total += count

    logger.info(f"Seed RAG completado: {total} chunks indexados, "
                f"total en store: {rag.count()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
