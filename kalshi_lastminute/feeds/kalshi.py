"""Cliente de SOLO LECTURA de Kalshi: mercados, libro de órdenes y resultados.

No existe ningún método que envíe órdenes: la ejecución de este proyecto es
simulada (paper trading) y debe seguir siéndolo.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)


def iso_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


@dataclass
class Book:
    """Libro de un mercado binario. Kalshi solo publica bids de YES y de NO.

    Comprar YES como taker = cruzar contra el mejor bid de NO, así que
    ask_yes = 1 - mejor_bid_no, y la cantidad disponible es la de ese bid.
    """
    yes_bids: list[tuple[float, float]]  # (precio, cantidad) ascendente
    no_bids: list[tuple[float, float]]

    @staticmethod
    def _best(levels: list[tuple[float, float]]) -> tuple[float, float] | None:
        return levels[-1] if levels else None

    def best_bid(self, side: str) -> tuple[float, float] | None:
        return self._best(self.yes_bids if side == "yes" else self.no_bids)

    def best_ask(self, side: str) -> tuple[float, float] | None:
        """(precio, cantidad) del mejor ask del lado pedido, o None si no hay."""
        opp = self._best(self.no_bids if side == "yes" else self.yes_bids)
        if opp is None:
            return None
        return round(1.0 - opp[0], 4), opp[1]

    def asks(self, side: str) -> list[tuple[float, float]]:
        """Todos los niveles de ask del lado pedido, del más barato al más caro."""
        opp = self.no_bids if side == "yes" else self.yes_bids
        return [(round(1.0 - p, 4), q) for p, q in reversed(opp)]


def parse_orderbook(payload: dict) -> Book:
    """Acepta el formato actual (orderbook_fp, dólares en string) y el antiguo (centavos)."""
    def levels(raw, scale: float) -> list[tuple[float, float]]:
        out = [(float(p) * scale, float(q)) for p, q in (raw or [])]
        out = [(round(p, 4), q) for p, q in out if q > 0]
        out.sort()
        return out

    if "orderbook_fp" in payload:
        ob = payload["orderbook_fp"] or {}
        return Book(levels(ob.get("yes_dollars"), 1.0), levels(ob.get("no_dollars"), 1.0))
    ob = payload.get("orderbook") or {}
    return Book(levels(ob.get("yes"), 0.01), levels(ob.get("no"), 0.01))


def load_private_key():
    """Llave RSA del .env, o None si no está configurada (modo REST público)."""
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not path or not os.environ.get("KALSHI_API_KEY_ID"):
        return None
    from cryptography.hazmat.primitives import serialization
    key_path = Path(path)
    if not key_path.is_absolute():
        key_path = Path(__file__).resolve().parents[2] / key_path  # relativa a la raíz del proyecto
    with open(key_path, "rb") as fh:
        return serialization.load_pem_private_key(fh.read(), password=None)


def signed_headers(private_key, method: str, path: str, ts_ms: int | None = None) -> dict:
    """Firma de `timestamp + METHOD + path` (sin query): RSA-PSS/SHA256 o Ed25519."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding
    ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
    message = f"{ts}{method.upper()}{path.split('?')[0]}".encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        sig = private_key.sign(message)
    else:
        sig = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return {
        "KALSHI-ACCESS-KEY": os.environ["KALSHI_API_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }


class KalshiClient:
    def __init__(self, session: aiohttp.ClientSession, rest_url: str,
                 timeout_s: float = 5.0) -> None:
        self.session = session
        self.rest_url = rest_url.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self.private_key = load_private_key()
        self.requests = 0
        self.errors = 0

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.rest_url}{path}"
        headers = {}
        if self.private_key is not None:
            headers = signed_headers(self.private_key, "GET", urlparse(url).path)
        self.requests += 1
        try:
            async with self.session.get(url, params=params, headers=headers,
                                        timeout=self.timeout) as resp:
                resp.raise_for_status()
                return await resp.json()
        except Exception:
            self.errors += 1
            raise

    async def open_markets(self, series: str) -> list[dict]:
        data = await self._get("/markets", {"series_ticker": series, "status": "open",
                                            "limit": 10})
        return data.get("markets", [])

    async def market(self, ticker: str) -> dict:
        data = await self._get(f"/markets/{ticker}")
        return data.get("market", data)

    async def orderbook(self, ticker: str) -> Book:
        return parse_orderbook(await self._get(f"/markets/{ticker}/orderbook"))


def pick_active(markets: list[dict], now_ms: int) -> dict | None:
    """El mercado cuya ventana [open, close) contiene a now; si hay varios, el que cierra antes."""
    live = [m for m in markets
            if iso_to_ms(m["open_time"]) <= now_ms < iso_to_ms(m["close_time"])]
    return min(live, key=lambda m: m["close_time"]) if live else None


class BookState:
    """Libro de un mercado mantenido con los mensajes del canal `orderbook_delta`."""

    def __init__(self) -> None:
        self.sides: dict[str, dict[float, float]] = {"yes": {}, "no": {}}

    def apply_snapshot(self, msg: dict) -> None:
        # La clave de un lado no viene cuando ese lado está vacío.
        for side in ("yes", "no"):
            self.sides[side] = {round(float(p), 4): float(q)
                                for p, q in msg.get(f"{side}_dollars_fp") or [] if float(q) > 0}

    def apply_delta(self, msg: dict) -> None:
        levels = self.sides[msg["side"]]
        price = round(float(msg["price_dollars"]), 4)
        qty = round(levels.get(price, 0.0) + float(msg["delta_fp"]), 2)
        if qty > 0:
            levels[price] = qty
        else:
            levels.pop(price, None)

    def book(self) -> Book:
        return Book(sorted(self.sides["yes"].items()), sorted(self.sides["no"].items()))


class SeqGap(Exception):
    """Se perdió un mensaje del WebSocket: hay que reconectar y pedir snapshot nuevo."""


def parse_index_value(msg: dict) -> tuple[str, int, float] | None:
    """(index_id, ts_ms del índice, valor) de un mensaje `cfbenchmarks_value`."""
    body = msg.get("msg") or {}
    try:
        data = json.loads(body["data"])
        return body["index_id"], int(data["time"]), float(data["value"])
    except (KeyError, TypeError, ValueError):
        return None


async def run_kalshi_ws(session: aiohttp.ClientSession, ws_url: str, private_key,
                        get_tickers, on_book, on_index, status: dict,
                        index_ids: list[str]) -> None:
    """WebSocket autenticado de SOLO LECTURA: libros en tiempo real e índice oficial.

    get_tickers() -> conjunto de tickers activos.
    on_book(ticker, tipo, cuerpo, BookState) por cada snapshot o delta.
    on_index(mensaje crudo) por cada valor del índice de CF Benchmarks.
    status["live"] es el conjunto de tickers cuyo libro llega en tiempo real;
    para los demás el grabador sigue con polling REST.
    """
    backoff = 1.0
    while True:
        status["live"] = set()
        try:
            headers = signed_headers(private_key, "GET", urlparse(ws_url).path)
            async with session.ws_connect(ws_url, headers=headers, heartbeat=20,
                                          max_msg_size=0) as ws:
                log.info("Kalshi WS conectado")
                await ws.send_json({"id": 1, "cmd": "subscribe", "params": {
                    "channels": ["cfbenchmarks_value"], "index_ids": index_ids}})
                books: dict[str, BookState] = {}
                seqs: dict[int, int] = {}
                subscribed: set[str] = set()
                cmd_id = 1
                while True:
                    wanted = get_tickers()
                    for ticker in wanted - subscribed:
                        cmd_id += 1
                        await ws.send_json({"id": cmd_id, "cmd": "subscribe", "params": {
                            "channels": ["orderbook_delta"], "market_ticker": ticker}})
                        subscribed.add(ticker)
                    status["live"] &= wanted  # los mercados cerrados dejan de estar en vivo
                    for t in [t for t in books if t not in wanted]:
                        del books[t]
                    try:
                        raw = await ws.receive(timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.ERROR):
                        break
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        continue
                    m = json.loads(raw.data)
                    kind, body = m.get("type"), m.get("msg") or {}
                    # seq cuenta TODOS los mensajes de una suscripción, también los "ok".
                    sid, seq = m.get("sid"), m.get("seq")
                    if sid is not None and seq is not None:
                        if sid in seqs and seq != seqs[sid] + 1:
                            raise SeqGap(f"sid={sid} esperado {seqs[sid] + 1}, llegó {seq}")
                        seqs[sid] = seq
                    if kind == "error":
                        log.warning("Kalshi WS error: %s", body)
                    elif kind in ("orderbook_snapshot", "orderbook_delta"):
                        t = body["market_ticker"]
                        if t not in wanted:
                            continue
                        state = books.setdefault(t, BookState())
                        if kind == "orderbook_snapshot":
                            state.apply_snapshot(body)
                        else:
                            state.apply_delta(body)
                        status["live"].add(t)
                        backoff = 1.0
                        on_book(t, kind, body, state)
                    elif kind == "cfbenchmarks_value":
                        on_index(m)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Kalshi WS: %s: %s", type(exc).__name__, exc)
        status["live"] = set()
        status["reconnects"] = status.get("reconnects", 0) + 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)
