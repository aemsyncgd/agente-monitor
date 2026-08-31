# src/api/routes/tv.py
import json
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter()
CONFIG_FILE = Path("config/tv_config.json")

DEFAULT_CONFIG = {
    "wan1_interface": "1sfp-sfpplus3-WAN_3-UPSTREAM",
    "wan2_interface": "vlan251_UPSTREAM",
    "wan2_sfp_physical": "1sfp-sfpplus1-WAN_1-UPSTREAM",
    "selected_interfaces": [
        "1sfp-sfpplus1-WAN_1-UPSTREAM", "1sfp-sfpplus2",
        "1sfp-sfpplus3-WAN_3-UPSTREAM", "1sfp-sfpplus4-SWITCH-FIBRA-LAN",
        "vlan251_UPSTREAM", "Lo0-PUBLICACION", "LAN_LOCAL",
        "ether1", "ether2", "ether3", "ether4",
        "ether5", "ether6", "ether7", "ether8"
    ],
    "refresh_seconds": 10
}


class TVConfigModel(BaseModel):
    wan1_interface: str = DEFAULT_CONFIG["wan1_interface"]
    wan2_interface: str = DEFAULT_CONFIG["wan2_interface"]
    wan2_sfp_physical: str = DEFAULT_CONFIG["wan2_sfp_physical"]
    selected_interfaces: List[str] = []
    refresh_seconds: int = 10


def load_tv_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {**DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_tv_config(data: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@router.get("/api/tv/config")
async def get_tv_config():
    return load_tv_config()


@router.put("/api/tv/config")
async def update_tv_config(config: TVConfigModel):
    data = config.model_dump() if hasattr(config, 'model_dump') else config.dict()
    save_tv_config(data)
    return {"ok": True, "config": data}
