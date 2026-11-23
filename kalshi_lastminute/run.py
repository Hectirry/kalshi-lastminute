"""Arranca el bot de paper trading: graba, decide, simula fills y sirve el dashboard.

Uso:
    python -m kalshi_lastminute.run               # hasta Ctrl+C
    python -m kalshi_lastminute.run --minutes 120
    python -m kalshi_lastminute.run --no-dashboard
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket

from .config import load_config
from .engine import Engine
from .recorder import LOCK_PORT, banner


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=None, help="duración; sin valor, hasta Ctrl+C")
    ap.add_argument("--config", default=None)
    ap.add_argument("--env", choices=["prod", "demo"], default=None, help="pisa kalshi.env")
    ap.add_argument("--db", default=None, help="pisa storage.db_path")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.env:
        cfg["kalshi"]["env"] = args.env
    if args.db:
        cfg["storage"]["db_path"] = args.db
    if args.no_dashboard:
        cfg["engine"]["dashboard"] = None
    banner(cfg)
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        raise SystemExit("Ya hay un bot o grabador corriendo en esta máquina; no se inicia otro.")
    engine = Engine(cfg)
    print("Estrategias: " + (", ".join(s.name for s in engine.strategies) or "ninguna"))
    try:
        asyncio.run(engine.run(args.minutes))
    except KeyboardInterrupt:
        pass
    print("Bot detenido. Reporte: python -m kalshi_lastminute.report")


if __name__ == "__main__":
    main()
