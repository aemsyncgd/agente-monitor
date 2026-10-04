# src/ai/main.py
"""
AI Monitoring Agent — main loop.

Integrates anomaly detection, client lookup, zone grouping, Telegram alerting,
and configurable instructions. Lightweight by design for low-resource servers.
"""
import os
import sys
import time
import signal
import logging
import logging.handlers
from pathlib import Path
from typing import List, Dict, Optional

from ..config_ia import load_config_ia, load_nodes_config, ConfigIA
from ..storage.influx_client import InfluxClient
from ..notifier_ai import TelegramNotifierAI, TelegramConfig
from ..reporte.reporter import DailyReporter
from .autoencoder import OpticalPowerAutoencoder
from .trainer import ModelTrainer, TrainingConfig
from .detector import AnomalyDetector
from .instructions import InstructionManager, InstructionType
from .llm_agent.tools import InfluxReader

logger = logging.getLogger("ai_engine")

# Track down hosts to avoid repeated alerts
_down_hosts: Dict[str, float] = {}


def _lookup_by_serial(reader: InfluxReader, serial: str) -> Optional[Dict]:
    """Cliente enriquecido por serial ONU (fuente InfluxDB)."""
    if not serial:
        return None
    rows = reader.enriched_clients(search=str(serial).strip(), limit=10)
    serial_u = str(serial).strip().upper()
    for r in rows:
        if (r.get("serial_onu") or "").upper() == serial_u:
            return r
    return rows[0] if rows else None


def _clients_on_pon(reader: InfluxReader, olt_name: str, pon_port: str) -> List[Dict]:
    """Clientes enriquecidos servidos por el puerto PON indicado."""
    rows = reader.enriched_clients(olt=str(olt_name or ""), limit=100)
    pon = str(pon_port or "").strip().lower()
    return [r for r in rows if pon in (r.get("pon_port") or "").lower()]


def _enriched_client_count() -> int:
    try:
        from ..api.influx_helper import get_clientes_stats
        return int(get_clientes_stats().get("enriquecidos", 0))
    except Exception:
        return 0


def _check_ping_status(influx, notifier, config):
    """Query InfluxDB for ping_check data and alert on down hosts."""
    try:
        flux = f'''
        from(bucket: "{config.influxdb.bucket}")
            |> range(start: -5m)
            |> filter(fn: (r) => r["_measurement"] == "ping_check")
            |> filter(fn: (r) => r["_field"] == "status")
            |> last()
        '''
        results = influx.query(flux)

        if not results:
            logger.debug("Ping check: no data in InfluxDB")
            return

        now = time.time()
        down_threshold = getattr(config, 'ping', None)
        if down_threshold and hasattr(down_threshold, 'alert_down_seconds'):
            down_threshold = down_threshold.alert_down_seconds
        else:
            down_threshold = 120

        up_count = 0
        down_count = 0
        for point in results:
            host_name = point.get("name", "")
            host_ip = point.get("ip", "")
            host_type = point.get("type", "")
            status = int(point.get("value", 1))

            if status == 0:
                down_count += 1
                if host_name not in _down_hosts:
                    _down_hosts[host_name] = now
                downtime = now - _down_hosts[host_name]
                if downtime >= down_threshold:
                    notifier.send_llm_alert(
                        title=f"host_down — {host_name}",
                        severity="critical",
                        dedup_key=f"ping:{host_name}",
                        state_fingerprint={"status": "down"},
                        extra={
                            "host_name": host_name,
                            "host_ip": host_ip,
                            "host_type": host_type,
                            "downtime_seconds": downtime,
                            "down_since": _down_hosts[host_name],
                        },
                    )
            else:
                up_count += 1
                if host_name in _down_hosts:
                    downtime = now - _down_hosts[host_name]
                    notifier.send_llm_alert(
                        title=f"host_up — {host_name}",
                        severity="info",
                        dedup_key=f"ping:{host_name}",
                        state_fingerprint={"status": "up"},
                        extra={
                            "host_name": host_name,
                            "host_ip": host_ip,
                            "host_type": host_type,
                            "downtime_seconds": downtime,
                        },
                    )
                    del _down_hosts[host_name]

        logger.info(f"Ping check: {up_count} up, {down_count} down")

    except Exception as e:
        logger.error(f"Ping status query failed: {e}")


def run_ping_check():
    """Run ping check standalone (used by manual trigger)."""
    from ..config_ia import load_config_ia
    from ..storage.influx_client import InfluxClient
    from .. import notifier_ai

    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")
    try:
        config = load_config_ia(config_path)
    except Exception as e:
        logger.error(f"Failed to load config for ping check: {e}")
        return

    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
        token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
        org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
        bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
    )
    if not influx.connect():
        logger.error("Ping check: no se pudo conectar a InfluxDB")
        return
    notifier = notifier_ai.TelegramNotifierAI(config.telegram)
    _check_ping_status(influx, notifier, config)


def setup_logging(config: ConfigIA):
    log_dir = Path(config.logging.file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        config.logging.file,
        maxBytes=config.logging.max_size_mb * 1024 * 1024,
        backupCount=config.logging.backup_count,
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
    config_path = os.environ.get("CONFIG_PATH", "config/config_ia.yaml")

    if not os.path.exists(config_path):
        print(f"Config not found: {config_path}")
        return 1

    config = load_config_ia(config_path)
    setup_logging(config)

    logger.info("=" * 50)
    logger.info("Starting AI Monitoring Agent")
    logger.info("=" * 50)

    # === Connect to InfluxDB ===
    influx = InfluxClient(
        url=os.environ.get("INFLUXDB_URL", config.influxdb.url),
        token=os.environ.get("INFLUXDB_TOKEN", config.influxdb.token),
        org=os.environ.get("INFLUXDB_ORG", config.influxdb.org),
        bucket=os.environ.get("INFLUXDB_BUCKET", config.influxdb.bucket),
    )

    if not influx.connect():
        logger.error("Failed to connect to InfluxDB")
        return 1

    # === Reader de clientes enriquecidos (InfluxDB, sin grid_servicios.csv) ===
    client_reader = InfluxReader(influx)
    logger.info("Client lookup listo sobre clientes_enriquecidos (InfluxDB)")

    # === Load instruction manager ===
    instr_path = os.environ.get(
        "INSTRUCTIONS_PATH",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            config.agent.instructions_path,
        ),
    )
    instructions = InstructionManager(instr_path)
    logger.info(f"Loaded {len(instructions.get_all())} instructions")

    # === Initialize trainer (lightweight) ===
    training_config = TrainingConfig(
        input_size=config.ai.training.input_size,
        hidden_size=config.ai.training.hidden_size,
        num_layers=config.ai.training.num_layers,
        dropout=config.ai.training.dropout,
        learning_rate=config.ai.training.learning_rate,
        batch_size=config.ai.training.batch_size,
        epochs=config.ai.training.epochs,
        window_size=config.ai.training.window_size,
        validation_split=config.ai.training.validation_split,
        early_stopping_patience=config.ai.training.early_stopping_patience,
    )

    model_path = os.environ.get("MODEL_PATH", config.ai.model_path)
    trainer = ModelTrainer(training_config, model_path)

    # === Load or train model ===
    model = None
    if os.path.exists(model_path):
        model = trainer.load_model()
        if model:
            logger.info("Model loaded successfully")

    if model is None and config.ai.enabled:
        logger.info("No model found, will train when enough data is available")

    # === Initialize detector ===
    detector = AnomalyDetector(model, trainer, influx)

    # === Initialize Telegram notifier ===
    tg_config = TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id),
        cooldown_minutes=config.telegram.cooldown_minutes,
        state_file=config.telegram.state_file,
    )
    notifier = TelegramNotifierAI(tg_config)
    logger.info("Telegram notifier initialized")

    reporter = DailyReporter(
        influx=influx,
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", config.telegram.bot_token),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID", config.telegram.chat_id),
    )
    logger.info("Daily reporter initialized")

    # === Signal handling ===
    running = True

    def signal_handler(signum, frame):
        nonlocal running
        logger.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info(
        f"AI Agent running | Interval: {config.agent.check_interval_seconds}s | "
        f"Retrain: {config.ai.retrain_hours}h | Clients: {_enriched_client_count()}"
    )

    last_train_time = 0
    if os.path.exists(model_path):
        try:
            last_train_time = os.path.getmtime(model_path)
        except OSError:
            last_train_time = 0
    last_daily_summary_hour = -1
    report_times_sent: Dict[str, str] = {}

    # === Main loop ===
    while running:
        try:
            current_time = time.time()
            enabled_instructions = instructions.get_enabled()
            enabled_ids = {i.id for i in enabled_instructions}

            # --- Retrain model ---
            if (
                "retrain_model" in enabled_ids
                and config.ai.enabled
                and (current_time - last_train_time) > config.ai.retrain_hours * 3600
            ):
                logger.info("Checking if retraining is needed...")

                flux = f'''
                from(bucket: "{config.influxdb.bucket}")
                    |> range(start: -24h)
                    |> filter(fn: (r) => r["_measurement"] == "optical_power")
                    |> filter(fn: (r) => r["_field"] == "rx_power")
                    |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
                '''

                results = influx.query(flux)

                if results and len(results) >= config.ai.min_samples:
                    power_values = [r["value"] for r in results if r.get("value") is not None]

                    if len(power_values) >= config.ai.min_samples:
                        logger.info(f"Training model with {len(power_values)} samples...")

                        success = trainer.train(power_values)
                        if success:
                            model = trainer.load_model()
                            detector.model = model
                            last_train_time = current_time
                            logger.info("Model trained and loaded successfully")
                            instructions.record_run("retrain_model", f"trained with {len(power_values)} samples")
                        else:
                            instructions.record_run("retrain_model", "training failed")

            # --- Anomaly detection ---
            if model and "detect_anomaly" in enabled_ids:
                flux = f'''
                from(bucket: "{config.influxdb.bucket}")
                    |> range(start: -1h)
                    |> filter(fn: (r) => r["_measurement"] == "optical_power")
                    |> filter(fn: (r) => r["_field"] == "rx_power")
                    |> last()
                '''

                latest_data = influx.query(flux)

                # Group by ONU
                onu_data: Dict[tuple, List] = {}
                for point in latest_data:
                    key = (
                        point.get("olt_name"),
                        point.get("pon_port"),
                        point.get("onu_index"),
                        point.get("onu_serial"),
                    )
                    if key not in onu_data:
                        onu_data[key] = []
                    onu_data[key].append(point.get("value", 0))

                # Detect anomalies
                anomalies = []
                for (olt_name, pon_port, onu_index, onu_serial), values in onu_data.items():
                    if values:
                        result = detector.detect(
                            olt_name=olt_name,
                            pon_port=pon_port,
                            onu_index=onu_index,
                            onu_serial=onu_serial,
                            power_history=values,
                            current_power=values[-1],
                        )
                        if result.is_anomaly:
                            anomalies.append(result)
                            logger.warning(f"Anomaly detected: {result.message}")

                if anomalies:
                    logger.info(f"Detected {len(anomalies)} anomalies")
                    instructions.record_run("detect_anomaly", f"{len(anomalies)} anomalies found")

                    # --- Enrich and send alerts ---
                    if "send_telegram" in enabled_ids:
                        for anomaly in anomalies:
                            # Client lookup (base enriquecida)
                            client = _lookup_by_serial(client_reader, anomaly.onu_serial)
                            client_name = (client or {}).get("nombre", "")
                            client_address = (client or {}).get("direccion", "")
                            client_node = (client or {}).get("nodo", "")

                            # Zone grouping
                            zone = ""
                            if "zone_grouping" in enabled_ids and client_address:
                                zone = _extract_zone(client_address)

                            # Affected clients on same PON
                            affected_count = 0
                            affected_names = []
                            if "zone_grouping" in enabled_ids and olt_name and pon_port:
                                pon_clients = _clients_on_pon(
                                    client_reader, olt_name, pon_port
                                )
                                affected_count = len(pon_clients)
                                affected_names = [
                                    c.get("nombre", "") for c in pon_clients
                                    if c.get("nombre")
                                ][:10]

                            # Send enriched alert
                            notifier.send_anomaly_alert(
                                olt_name=anomaly.olt_name,
                                pon_port=anomaly.pon_port,
                                onu_index=anomaly.onu_index,
                                onu_serial=anomaly.onu_serial,
                                anomaly_type=anomaly.anomaly_type.value,
                                current_power=anomaly.current_power,
                                avg_power=anomaly.avg_power,
                                trend=anomaly.trend,
                                predicted_time=anomaly.predicted_time_to_failure,
                                client_name=client_name,
                                client_address=client_address,
                                zone=zone,
                                node=client_node,
                                affected_count=affected_count,
                                affected_names=affected_names,
                            )

                # --- Predictions ---
                if "predict_failure" in enabled_ids:
                    for (olt_name, pon_port, onu_index, onu_serial), values in onu_data.items():
                        if not values or len(values) < 20:
                            continue

                        # Simple trend-based prediction
                        recent = values[-12:]  # last hour
                        older = values[-24:-12] if len(values) >= 24 else values[: len(values) // 2]

                        if recent and older:
                            recent_avg = sum(recent) / len(recent)
                            older_avg = sum(older) / len(older)
                            hours_data = len(recent) * 5 / 60
                            if hours_data > 0:
                                trend = (recent_avg - older_avg) / hours_data

                                if trend < -0.5 and recent_avg > -30:
                                    hours_to_failure = (recent_avg - (-30)) / abs(trend)
                                    confidence = min(1.0, abs(trend) / 2.0)

                                    if hours_to_failure < 48:
                                        client = _lookup_by_serial(client_reader, onu_serial)
                                        zone = ""
                                        if client and client.get("direccion"):
                                            zone = _extract_zone(client.get("direccion", ""))

                                        notifier.send_ai_prediction(
                                            olt_name=olt_name,
                                            pon_port=pon_port,
                                            onu_index=onu_index,
                                            onu_serial=onu_serial,
                                            confidence=confidence,
                                            hours_to_failure=hours_to_failure,
                                            current_power=recent_avg,
                                            client_name=(client or {}).get("nombre", ""),
                                            client_address=(client or {}).get("direccion", ""),
                                            zone=zone,
                                        )

            # --- Daily summary ---
            if (
                "daily_summary" in enabled_ids
                and time.strftime("%H") == "8"
                and int(time.strftime("%M")) == 0
                and last_daily_summary_hour != int(time.strftime("%H"))
            ):
                last_daily_summary_hour = int(time.strftime("%H"))

                stats = {
                    "olt_count": len(config.nodes) if hasattr(config, "nodes") else 0,
                    "onu_count": 0,
                    "client_count": _enriched_client_count(),
                    "anomaly_count": 0,
                    "prediction_count": 0,
                    "by_type": {},
                    "zones_affected": [],
                }

                notifier.send_daily_summary(stats)
                instructions.record_run("daily_summary", "sent")

            # --- Daily report (full OLT/MikroTik/chart report) ---
            if "send_report" in enabled_ids:
                today = time.strftime("%Y-%m-%d")
                current_t = time.strftime("%H:%M")
                # Try schedules from reporte-config.json first
                send_times = reporter._get_schedules()
                if not send_times:
                    inst = instructions.get_by_id("send_report")
                    send_times = inst.params.get("send_times", ["08:00", "12:30", "16:50", "20:50"]) if inst else []
                for t in send_times:
                    if current_t == t and report_times_sent.get(t) != today:
                        try:
                            reporter.run()
                            report_times_sent[t] = today
                            instructions.record_run("send_report", f"sent at {t}")
                        except Exception as e:
                            logger.error(f"Report failed at {t}: {e}", exc_info=True)
                            instructions.record_run("send_report", f"error: {e}")
                        break

            # --- Ping status check ---
            if "check_ping_status" in enabled_ids:
                try:
                    _check_ping_status(influx, notifier, config)
                    instructions.record_run("check_ping_status", "checked")
                except Exception as e:
                    logger.error(f"Ping status check failed: {e}")

            # Sleep
            time.sleep(config.agent.check_interval_seconds)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            time.sleep(60)

    # Cleanup
    influx.close()
    logger.info("AI Agent stopped")
    return 0


def _extract_zone(address: str) -> str:
    """Simple zone extraction from address (first 3 meaningful words)."""
    if not address:
        return ""
    # Remove common prefixes
    addr = address.strip()
    for prefix in ["Sector ", "Barrio ", "Colonia ", "Zona ", "Urb. "]:
        if addr.startswith(prefix):
            addr = addr[len(prefix):]
    words = addr.split()[:3]
    return " ".join(words) if words else ""


if __name__ == "__main__":
    sys.exit(main())
