"""Carga de config.yaml y de secretos desde .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Lee KEY=VALUE de .env hacia os.environ sin pisar lo ya definido."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _merge(base: dict, extra: dict) -> None:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load_config(path: Path | str | None = None) -> dict:
    load_env()
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    # config.local.yaml (fuera de Git) pisa valores de config.yaml en esta máquina.
    local = path.with_name("config.local.yaml")
    if local.exists():
        _merge(cfg, yaml.safe_load(local.read_text(encoding="utf-8")) or {})
    # La llave también puede venir de ahí; las variables de entorno y .env mandan.
    for env_name, key in (("KALSHI_API_KEY_ID", "api_key_id"),
                          ("KALSHI_PRIVATE_KEY_PATH", "private_key_path")):
        if cfg["kalshi"].get(key):
            os.environ.setdefault(env_name, str(cfg["kalshi"][key]))
    env = cfg["kalshi"]["env"]
    if env not in cfg["kalshi"]["endpoints"]:
        raise ValueError(f"kalshi.env desconocido: {env}")
    return cfg


def kalshi_endpoints(cfg: dict) -> dict:
    return cfg["kalshi"]["endpoints"][cfg["kalshi"]["env"]]


def db_path(cfg: dict) -> Path:
    p = Path(cfg["storage"]["db_path"])
    return p if p.is_absolute() else ROOT / p
