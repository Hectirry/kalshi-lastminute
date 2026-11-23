"""Feeds WebSocket públicos de spot (mejor bid/ask), sin autenticación.

Una conexión por exchange con todos los símbolos configurados. Cada exchange
es una entrada de EXCHANGES con: `url(symbols)`, `subscribe(symbols)` (lista
de mensajes) y un parser puro `parse(msg) -> (símbolo, bid, ask, exch_ts_ms | None) | None`.
El símbolo devuelto es el mismo texto que se configura en `assets.*.symbols`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime

import aiohttp

log = logging.getLogger(__name__)

Parsed = tuple[str, float, float, int | None] | None


def _iso_ms(s: str) -> int | None:
    try:
        # Coinbase manda nanosegundos; fromisoformat acepta hasta microsegundos.
        head, _, frac = s.rstrip("Z").partition(".")
        return int(datetime.fromisoformat(f"{head}.{frac[:6] or '0'}+00:00").timestamp() * 1000)
    except ValueError:
        return None


def parse_coinbase(msg: dict) -> Parsed:
    if msg.get("type") != "ticker" or not msg.get("best_bid"):
        return None
    return (msg["product_id"], float(msg["best_bid"]), float(msg["best_ask"]),
            _iso_ms(msg.get("time", "")))


def parse_kraken(msg: dict) -> Parsed:
    if msg.get("channel") != "ticker" or not msg.get("data"):
        return None
    d = msg["data"][0]
    return d["symbol"], float(d["bid"]), float(d["ask"]), None


def parse_bitstamp(msg: dict) -> Parsed:
    if msg.get("event") != "data":
        return None
    d = msg["data"]
    if not d.get("bids") or not d.get("asks"):
        return None
    ts = int(d["microtimestamp"]) // 1000 if d.get("microtimestamp") else None
    symbol = msg.get("channel", "").removeprefix("order_book_")
    return symbol, float(d["bids"][0][0]), float(d["asks"][0][0]), ts


def _parse_book_ticker(msg: dict) -> Parsed:
    # Formato bookTicker de Gemini y Binance: {u, s, b, B, a, A} (+ E en ns en Gemini)
    if "b" not in msg or "a" not in msg or "s" not in msg:
        return None
    ts = int(msg["E"]) // 1_000_000 if msg.get("E") else None
    return msg["s"].lower(), float(msg["b"]), float(msg["a"]), ts


parse_gemini = _parse_book_ticker
parse_binance = _parse_book_ticker


EXCHANGES: dict[str, dict] = {
    "coinbase": {
        "url": lambda syms: "wss://ws-feed.exchange.coinbase.com",
        "subscribe": lambda syms: [{"type": "subscribe", "product_ids": syms,
                                    "channels": ["ticker"]}],
        "parse": parse_coinbase,
    },
    "kraken": {
        "url": lambda syms: "wss://ws.kraken.com/v2",
        "subscribe": lambda syms: [{"method": "subscribe",
                                    "params": {"channel": "ticker", "symbol": syms,
                                               "event_trigger": "bbo"}}],
        "parse": parse_kraken,
    },
    "bitstamp": {
        "url": lambda syms: "wss://ws.bitstamp.net",
        "subscribe": lambda syms: [{"event": "bts:subscribe",
                                    "data": {"channel": f"order_book_{s}"}} for s in syms],
        "parse": parse_bitstamp,
    },
    "gemini": {
        "url": lambda syms: "wss://ws.gemini.com",
        "subscribe": lambda syms: [{"id": "1", "method": "subscribe",
                                    "params": [f"{s}@bookTicker" for s in syms]}],
        "parse": parse_gemini,
    },
    "binance": {
        "url": lambda syms: "wss://stream.binance.com:9443/ws/"
                            + "/".join(f"{s}@bookTicker" for s in syms),
        "subscribe": lambda syms: [],
        "parse": parse_binance,
    },
}

# on_quote(asset, source, bid, ask, recv_ts_ms, exch_ts_ms)
OnQuote = Callable[[str, str, float, float, int, int | None], None]


def symbols_by_exchange(assets: dict) -> dict[str, dict[str, str]]:
    """{exchange: {símbolo: activo}} a partir de la sección `assets` de config.yaml."""
    out: dict[str, dict[str, str]] = {}
    for asset, spec in assets.items():
        for exchange, symbol in spec["symbols"].items():
            out.setdefault(exchange, {})[symbol] = asset
    return out


async def run_spot_feed(name: str, symbols: dict[str, str], session: aiohttp.ClientSession,
                        on_quote: OnQuote, stats: dict) -> None:
    """Mantiene la conexión con reconexión y backoff exponencial (máx 30 s)."""
    spec = EXCHANGES[name]
    syms = list(symbols)
    backoff = 1.0
    while True:
        try:
            async with session.ws_connect(spec["url"](syms), heartbeat=20, max_msg_size=0) as ws:
                for m in spec["subscribe"](syms):
                    await ws.send_json(m)
                log.info("%s conectado (%s)", name, ", ".join(syms))
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        continue
                    msg = json.loads(raw.data)
                    if not isinstance(msg, dict):
                        continue
                    parsed = spec["parse"](msg)
                    if parsed is None:
                        continue
                    symbol, bid, ask, exch_ts = parsed
                    asset = symbols.get(symbol)
                    if asset is None or bid <= 0 or ask < bid:
                        continue
                    backoff = 1.0
                    on_quote(asset, name, bid, ask, int(time.time() * 1000), exch_ts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # red caída, DNS, cierre remoto: se reintenta
            log.warning("%s: %s: %s", name, type(exc).__name__, exc)
        stats[name] = stats.get(name, 0) + 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
