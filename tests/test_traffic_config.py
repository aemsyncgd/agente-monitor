# tests/test_traffic_config.py
from src.config_ia import ConfigIA, CollectorsConfig, TrafficSamplerConfig, load_config_ia


def test_defaults():
    cfg = ConfigIA()
    assert cfg.collectors.traffic_sampler.enabled is True
    assert cfg.collectors.traffic_sampler.interval_seconds == 300
    assert cfg.collectors.traffic_sampler.walk_delay_seconds == 3
    assert cfg.live_refresh_seconds == 5


def test_load_config_ia_parses_traffic_sampler(tmp_path):
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text(
        "collectors:\n"
        "  traffic_sampler:\n"
        "    enabled: false\n"
        "    interval_seconds: 600\n"
        "    walk_delay_seconds: 5\n"
        "live_refresh_seconds: 8\n"
    )
    cfg = load_config_ia(str(cfg_yaml))
    ts = cfg.collectors.traffic_sampler
    assert cfg.live_refresh_seconds == 8
    assert ts.enabled is False
    assert ts.interval_seconds == 600
    assert ts.walk_delay_seconds == 5


def test_load_config_ia_defaults_when_missing(tmp_path):
    cfg_yaml = tmp_path / "config.yaml"
    cfg_yaml.write_text("influxdb:\n  url: http://x:8086\n  token: t\n  org: o\n  bucket: b\n")
    cfg = load_config_ia(str(cfg_yaml))
    assert cfg.collectors.traffic_sampler == TrafficSamplerConfig()
    assert cfg.live_refresh_seconds == 5