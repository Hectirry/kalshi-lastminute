"""Interfaz de estrategias (plugins) y cargador.

Una estrategia es un par de archivos en `strategies/`:
  <variante>.yaml   parámetros; `module:` dice qué .py usar (por defecto, el mismo nombre)
  <módulo>.py       define `evaluate(ctx, params) -> Decision`, función pura

El motor llama a `evaluate` varias veces por segundo durante cada mercado. La
misma función se usa en vivo y en el replay.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .feeds.kalshi import Book

STRATEGIES_DIR = Path(__file__).resolve().parent.parent / "strategies"


@dataclass(frozen=True)
class Context:
    asset: str
    ticker: str
    now_ms: int
    close_ms: int
    strike: float | None
    index_points: list[tuple[int, float]]  # (tiempo del índice ms, valor), 1/s, ascendente
    index_age_ms: int | None               # antigüedad del último valor del índice
    book: Book | None
    fees: dict
    window_s: int = 60

    @property
    def secs_left(self) -> float:
        return (self.close_ms - self.now_ms) / 1000.0


@dataclass(frozen=True)
class Decision:
    action: str                    # 'enter' | 'skip'
    reason: str                    # etiqueta corta y estable
    side: str | None = None        # 'yes' | 'no'
    price: float | None = None     # ask visto al decidir
    contracts: float = 0.0
    model_prob: float | None = None  # P(YES) del modelo
    edge_net: float | None = None    # edge neto de fees por contrato, del lado elegido
    info: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedStrategy:
    name: str        # nombre de la variante (= nombre del .yaml)
    params: dict
    evaluate: callable


def load_strategies(directory: Path | None = None) -> list[LoadedStrategy]:
    """Carga todas las variantes con `enabled: true`."""
    directory = directory or STRATEGIES_DIR
    out = []
    for path in sorted(directory.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not spec.get("enabled", True):
            continue
        module = importlib.import_module(f"strategies.{spec.get('module', path.stem)}")
        out.append(LoadedStrategy(path.stem, spec.get("params", {}), module.evaluate))
    return out
