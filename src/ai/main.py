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
from ..client_lookup import ClientLookup
from ..notifier_ai import TelegramNotifierAI, TelegramConfig
from ..reporte.reporter import DailyReporter
from .autoencoder import OpticalPowerAutoencoder
from .trainer import ModelTrainer, TrainingConfig
from .detector import AnomalyDetector
from .instructions import InstructionManager, InstructionType

logger = logging.getLogger("ai_engine")

# Track down hosts to avoid repeated alerts
_down_hosts: Dict[str, float] = {}


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
                    notifier.send_ping_alert(
                        host_name=host_name,
                        host_ip=host_ip,
                        host_type=host_type,
                        status="down",
                        downtime_seconds=downtime,
                    )
                    _down_hosts[host_name] = now + 600  # Re-alert in 10 min
            else:
                up_count += 1
                if host_name in _down_hosts:
                    notifier.send_ping_alert(
                        host_name=host_name,
                        host_ip=host_ip,
                        host_type=host_type,
                        status="up",
                        downtime_seconds=0,
                        avg_latency=float(point.get("latency_ms_avg", 0)),
                    )
                    del _down_hosts[host_name]

        logger.info(f"Ping check: {up_count} up, {down_count} down")

    except Exception as e:
        logger.error(f"Ping status query failed: {e}")


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

    # === Load client lookup ===
    client_lookup = ClientLookup()
    csv_path = os.environ.get(
        "CSV_PATH",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            config.csv.path,
        ),
    )
    if config.csv.enabled and os.path.exists(csv_path):
        count = client_lookup.load_from_csv(csv_path)
        logger.info(f"Client lookup loaded: {count} clients from {csv_path}")
    else:
        logger.warning(f"CSV not found or disabled: {csv_path}")

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
        f"Retrain: {config.ai.retrain_hours}h | Clients: {client_lookup.count}"
    )

    last_train_time = 0
    last_csv_reload = time.time()
    last_daily_summary_hour = -1
    report_times_sent: Dict[str, str] = {}

    # === Main loop ===
    while running:
        try:
            current_time = time.time()
            enabled_instructions = instructions.get_enabled()
            enabled_ids = {i.id for i in enabled_instructions}

            # --- Periodic CSV reload ---
            if (
                config.csv.enabled
                and client_lookup.needs_reload(config.csv.reload_hours)
            ):
                if os.path.exists(csv_path):
                    count = client_lookup.load_from_csv(csv_path)
                    logger.info(f"Reloaded {count} clients from CSV")
                    last_csv_reload = current_time
                    instructions.record_run("client_lookup", f"loaded {count}")

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
                            # Client lookup
                            client = client_lookup.lookup_by_serial(anomaly.onu_serial)
                            client_name = client.nombre if client else ""
                            client_address = client.direccion if client else ""
                            client_node = client.nodo if client else ""

                            # Zone grouping
                            zone = ""
                            if "zone_grouping" in enabled_ids and client_address:
                                zone = _extract_zone(client_address)

                            # Affected clients on same PON
                            affected_count = 0
                            affected_names = []
                            if "zone_grouping" in enabled_ids and olt_name and pon_port:
                                pon_clients = client_lookup.clients_on_pon(olt_name, pon_port)
                                affected_count = len(pon_clients)
                                affected_names = [c.nombre for c in pon_clients if c.nombre][:10]

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
                                        client = client_lookup.lookup_by_serial(onu_serial)
                                        zone = ""
                                        if client and client.direccion:
                                            zone = _extract_zone(client.direccion)

                                        notifier.send_ai_prediction(
                                            olt_name=olt_name,
                                            pon_port=pon_port,
                                            onu_index=onu_index,
                                            onu_serial=onu_serial,
                                            confidence=confidence,
                                            hours_to_failure=hours_to_failure,
                                            current_power=recent_avg,
                                            client_name=client.nombre if client else "",
                                            client_address=client.direccion if client else "",
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
                    "client_count": client_lookup.count,
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
