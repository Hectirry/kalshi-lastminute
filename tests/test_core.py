"""Tests herméticos (sin red): índice, libro, fees, parsers y base de datos."""
import pytest

from kalshi_lastminute.db import Database
from kalshi_lastminute.feeds.kalshi import parse_orderbook, pick_active
from kalshi_lastminute.feeds.spot import (parse_binance, parse_bitstamp, parse_coinbase,
                                          parse_gemini, parse_kraken)
from kalshi_lastminute.fees import fee
from kalshi_lastminute.index import Quote, composite, per_second_series, window_average


# ── índice ──────────────────────────────────────────────────────────────
def test_composite_ignores_stale_and_zero_weight():
    quotes = {"a": Quote(10_000, 99, 101), "b": Quote(10_000, 199, 201),
              "old": Quote(1_000, 1, 1), "ref": Quote(10_000, 999, 1001)}
    weights = {"a": 1.0, "b": 1.0, "old": 1.0, "ref": 0.0}
    assert composite(quotes, weights, 10_500, stale_ms=3000) == (150.0, 2)


def test_composite_none_when_everything_stale():
    assert composite({"a": Quote(0, 1, 1)}, {"a": 1.0}, 10_000, 3000) is None


def test_per_second_series_forward_fills_without_lookahead():
    pts = [(1000, 10.0), (2500, 20.0)]
    # segundos que terminan en 1000, 2000, 3000: el de 2000 no puede ver el tick de 2500
    assert per_second_series(pts, 0, 3000) == [10.0, 10.0, 20.0]


def test_per_second_series_drops_stale_values():
    assert per_second_series([(1000, 10.0)], 0, 8000, max_age_ms=5000)[-1] is None


def test_window_average_uses_exactly_last_60_seconds():
    close = 1_000_000
    pts = [(close - 61_000, 999.0)] + [(close - 60_000 + i * 1000, float(i)) for i in range(1, 61)]
    avg, n = window_average(pts, close, 60)
    assert n == 60 and avg == pytest.approx(30.5)


# ── libro de Kalshi ─────────────────────────────────────────────────────
def test_orderbook_ask_is_one_minus_opposite_bid():
    book = parse_orderbook({"orderbook_fp": {
        "yes_dollars": [["0.4200", "2.00"], ["0.9000", "10.00"]],
        "no_dollars": [["0.0030", "3238.00"], ["0.0700", "30.00"]]}})
    assert book.best_bid("yes") == (0.90, 10.0)
    assert book.best_ask("yes") == (0.93, 30.0)      # 1 - 0.07, cantidad del bid NO
    assert book.best_ask("no") == (0.10, 10.0)
    assert book.asks("yes") == [(0.93, 30.0), (0.997, 3238.0)]


def test_orderbook_empty_side_and_legacy_cents():
    assert parse_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": None}}
                           ).best_ask("yes") is None
    assert parse_orderbook({"orderbook": {"yes": [[42, 5]], "no": []}}).best_bid("yes") == (0.42, 5.0)


def test_pick_active_selects_window_containing_now():
    mk = lambda t, o, c: {"ticker": t, "open_time": o, "close_time": c}  # noqa: E731
    markets = [mk("next", "2026-11-22T23:00:00Z", "2026-11-22T23:15:00Z"),
               mk("now", "2026-11-22T22:45:00Z", "2026-11-22T23:00:00Z")]
    from kalshi_lastminute.feeds.kalshi import iso_to_ms
    assert pick_active(markets, iso_to_ms("2026-11-22T22:50:00Z"))["ticker"] == "now"
    assert pick_active(markets, iso_to_ms("2026-11-22T23:00:00Z"))["ticker"] == "next"
    assert pick_active(markets, iso_to_ms("2026-11-22T23:20:00Z")) is None


def test_bookstate_snapshot_and_deltas():
    from kalshi_lastminute.feeds.kalshi import BookState
    st = BookState()
    st.apply_snapshot({"market_ticker": "T", "yes_dollars_fp": [["0.0800", "300.00"]]})  # sin lado NO
    assert st.book().best_ask("yes") is None
    st.apply_delta({"side": "no", "price_dollars": "0.9000", "delta_fp": "54.00"})
    st.apply_delta({"side": "no", "price_dollars": "0.9000", "delta_fp": "-14.00"})
    assert st.book().best_ask("yes") == (0.10, 40.0)
    st.apply_delta({"side": "no", "price_dollars": "0.9000", "delta_fp": "-40.00"})
    assert st.book().no_bids == []


def test_signed_headers_rsa_and_ed25519(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    from kalshi_lastminute.feeds.kalshi import signed_headers
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id")
    for key in (rsa.generate_private_key(65537, 2048), ed25519.Ed25519PrivateKey.generate()):
        h = signed_headers(key, "get", "/trade-api/ws/v2?x=1", ts_ms=123)
        assert h["KALSHI-ACCESS-KEY"] == "test-id" and h["KALSHI-ACCESS-TIMESTAMP"] == "123"
        assert len(h["KALSHI-ACCESS-SIGNATURE"]) > 40


def test_dns_override_only_matches_configured_domain():
    from kalshi_lastminute.net import matches
    assert matches("external-api.kalshi.com", ["kalshi.com"])
    assert matches("kalshi.com", ["kalshi.com"])
    assert not matches("notkalshi.com", ["kalshi.com"])
    assert not matches("ws-feed.exchange.coinbase.com", ["kalshi.com"])


# ── fees ────────────────────────────────────────────────────────────────
def test_fee_matches_kalshi_base_taker_example():
    # 0.07 × 0.5 × 0.5 = 0.0175 por contrato; 100 contratos = 1.75 exacto
    assert fee(100, 0.50) == 1.75
    assert fee(1, 0.50) == 0.02                      # sube al centavo
    assert fee(1, 0.50, round_to=0.0001) == 0.0175
    assert fee(100, 0.90) == 0.63
    assert fee(0, 0.5) == 0.0


# ── parsers spot ────────────────────────────────────────────────────────
def test_spot_parsers_return_symbol():
    assert parse_coinbase({"type": "ticker", "product_id": "ETH-USD", "best_bid": "100.1",
                           "best_ask": "100.3", "time": "2026-11-22T22:54:58.102222561Z"}
                          )[:3] == ("ETH-USD", 100.1, 100.3)
    assert parse_coinbase({"type": "subscriptions"}) is None
    assert parse_kraken({"channel": "ticker", "data": [{"symbol": "SOL/USD", "bid": 1.0,
                                                        "ask": 2.0}]}) == ("SOL/USD", 1.0, 2.0, None)
    assert parse_kraken({"channel": "heartbeat"}) is None
    assert parse_bitstamp({"event": "data", "channel": "order_book_solusd",
                           "data": {"bids": [["5", "1"]], "asks": [["6", "1"]],
                                    "microtimestamp": "1700000000123456"}}
                          ) == ("solusd", 5.0, 6.0, 1700000000123)
    assert parse_binance({"s": "ETHUSDT", "b": "7.5", "a": "7.6"}) == ("ethusdt", 7.5, 7.6, None)
    assert parse_gemini({"u": 1, "E": 1700000000123456789, "s": "btcusd", "b": "9", "B": "1",
                         "a": "10", "A": "1"}) == ("btcusd", 9.0, 10.0, 1700000000123)
    assert parse_gemini({"id": "1", "status": 200}) is None


def test_symbols_by_exchange_and_subscriptions():
    from kalshi_lastminute.feeds.spot import EXCHANGES, symbols_by_exchange
    assets = {"BTC": {"symbols": {"coinbase": "BTC-USD", "binance": "btcusdt"}},
              "ETH": {"symbols": {"coinbase": "ETH-USD", "binance": "ethusdt"}}}
    by_ex = symbols_by_exchange(assets)
    assert by_ex["coinbase"] == {"BTC-USD": "BTC", "ETH-USD": "ETH"}
    assert EXCHANGES["binance"]["url"](list(by_ex["binance"])).endswith(
        "/ws/btcusdt@bookTicker/ethusdt@bookTicker")
    assert len(EXCHANGES["bitstamp"]["subscribe"](["btcusd", "ethusd"])) == 2


def test_config_assets_cover_every_weighted_exchange():
    from kalshi_lastminute.config import load_config
    cfg = load_config()
    for asset, spec in cfg["assets"].items():
        assert set(spec["symbols"]) == set(cfg["index"]["weights"]), asset


# ── base de datos ───────────────────────────────────────────────────────
def test_db_reopen_does_not_duplicate(tmp_path):
    path = tmp_path / "t.db"
    row = ("TICK", 1, "yes", 100.0, 99.0, 2, 100.1, 60, "{}")
    for _ in range(2):  # simula apagar y volver a prender
        db = Database(path)
        db.add("settlements", row)
        db.add("ticks_index", (1, "BTC", "coinbase", 1.0, 2.0, 1.5, None))
        db.close()
    db = Database(path)
    assert db.query("SELECT COUNT(*) n FROM settlements")[0]["n"] == 1
    assert db.query("SELECT COUNT(*) n FROM ticks_index")[0]["n"] == 2
    db.close()


def test_old_btc_only_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE ticks_index (id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, "
              "source TEXT NOT NULL, bid REAL, ask REAL, mid REAL NOT NULL, exch_ts_ms INTEGER)")
    c.execute("INSERT INTO ticks_index (ts_ms, source, mid) VALUES (1, 'composite', 5.0)")
    c.commit()
    c.close()
    db = Database(path)
    db.add("ticks_index", (2, "ETH", "composite", None, None, 6.0, None))
    db.flush()
    assert [r["asset"] for r in db.query("SELECT asset FROM ticks_index ORDER BY ts_ms")] == ["BTC", "ETH"]
    db.close()


def test_one_position_per_market_is_enforced(tmp_path):
    import sqlite3
    db = Database(tmp_path / "t.db")
    ins = ("INSERT INTO paper_trades (ticker, strategy, side, decided_ms, status, "
           "requested_contracts, decision_price) VALUES ('T','s','yes',1,'filled',1,0.5)")
    db.conn.execute(ins)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(ins)
    db.close()


def test_config_local_overrides_are_merged_deeply():
    from kalshi_lastminute.config import _merge
    base = {"kalshi": {"env": "prod", "x": 1}, "a": 1}
    _merge(base, {"kalshi": {"env": "demo"}, "b": 2})
    assert base == {"kalshi": {"env": "demo", "x": 1}, "a": 1, "b": 2}


def test_parse_index_value_from_kalshi_ws():
    from kalshi_lastminute.feeds.kalshi import parse_index_value
    data = '{"type":"value","time":1795404587000,"id":"BRTI","value":"61350.45"}'
    msg = {"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI", "data": data}}
    assert parse_index_value(msg) == ("BRTI", 1795404587000, 61350.45)
    assert parse_index_value({"msg": {"index_id": "BRTI", "data": "no-json"}}) is None
