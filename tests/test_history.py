"""Tests herméticos del histórico: parseo de velas y análisis de calibración."""
import pytest

from tools.backfill import candle_row, connect, market_row
from tools.calibration import bucket_of, calibration, load, observations, taker_fee, taker_returns

FEES = {"taker_rate": 0.07, "multiplier": 1, "round_to": 0.01}


def test_candle_row_reads_current_and_historical_formats():
    live = {"end_period_ts": 100, "volume_fp": "12.50",
            "price": {"mean_dollars": "0.4174"},
            "yes_bid": {"open_dollars": "0.40", "close_dollars": "0.41"},
            "yes_ask": {"open_dollars": "0.43", "close_dollars": "0.42"}}
    assert candle_row("T", live) == ("T", 100, 0.41, 0.42, 0.40, 0.43, 0.4174, 12.5)
    old = {"end_period_ts": 100, "volume": "3", "price": {"mean": "0.03"},
           "yes_bid": {"open": "0.006", "close": "0.002"}, "yes_ask": {"open": "0.007", "close": "0.003"}}
    assert candle_row("T", old) == ("T", 100, 0.002, 0.003, 0.006, 0.007, 0.03, 3.0)
    assert candle_row("T", {"end_period_ts": 1, "price": {}})[2:] == (None,) * 6


def test_market_row():
    m = {"ticker": "KXBTC15M-X", "open_time": "2026-11-23T03:00:00Z",
         "close_time": "2026-11-23T03:15:00Z", "floor_strike": 100.5, "result": "yes",
         "expiration_value": "101.25", "volume_fp": "9.00"}
    row = market_row("BTC", m)
    assert row[:2] == ("KXBTC15M-X", "BTC") and row[3] - row[2] == 900_000
    assert row[4:] == (100.5, "yes", 101.25, 9.0)


def test_observations_filter_unusable_quotes_and_price_both_sides():
    rows = [
        {"asset": "BTC", "result": "yes", "yes_bid": 0.60, "yes_ask": 0.62},
        {"asset": "BTC", "result": "no", "yes_bid": 0.0, "yes_ask": 0.001},    # sin bid
        {"asset": "BTC", "result": "no", "yes_bid": 0.999, "yes_ask": 1.0},    # sin ask
        {"asset": "BTC", "result": "no", "yes_bid": 0.30, "yes_ask": 0.70},    # spread enorme
        {"asset": "BTC", "result": "no", "yes_bid": None, "yes_ask": 0.5},
    ]
    obs = observations(rows, FEES)
    assert len(obs) == 1
    o = obs[0]
    assert o["mid"] == pytest.approx(0.61)
    assert o["pnl_yes"] == pytest.approx(1 - 0.62 - 0.07 * 0.62 * 0.38)
    assert o["no_ask"] == pytest.approx(0.40)                                  # 1 - bid de YES
    assert o["pnl_no"] == pytest.approx(0 - 0.40 - 0.07 * 0.40 * 0.60)


def test_taker_fee_peaks_at_half():
    assert taker_fee(0.5, 0.07, 1) == pytest.approx(0.0175)
    assert taker_fee(0.95, 0.07, 1) == pytest.approx(0.003325)


def test_buckets():
    assert bucket_of(0.07) == "0.05–0.10"
    assert bucket_of(0.95) == "0.95–1.00" and bucket_of(1.0) == "0.95–1.00"


def test_calibration_and_taker_returns_on_a_fair_market():
    # 100 mercados a 0.60/0.62: 61 cierran YES -> el precio medio 0.61 está bien calibrado
    rows = [{"asset": "BTC", "result": "yes" if i < 61 else "no", "yes_bid": 0.60, "yes_ask": 0.62}
            for i in range(100)]
    obs = observations(rows, FEES)
    cal = calibration(obs)
    assert len(cal) == 1 and cal[0]["n"] == 100
    assert cal[0]["freq"] == pytest.approx(0.61) and abs(cal[0]["z"]) < 0.01
    ret = {t["bucket"]: t for t in taker_returns(obs)}
    # comprar YES a 0.62 con 61 % de acierto pierde el medio spread más el fee
    assert ret["0.60–0.70"]["pnl"] == pytest.approx(0.61 - 0.62 - 0.07 * 0.62 * 0.38)
    assert ret["0.40–0.50"]["pnl"] == pytest.approx(0.39 - 0.40 - 0.07 * 0.40 * 0.60)
    assert ret["0.60–0.70"]["hi"] > ret["0.60–0.70"]["pnl"] > ret["0.60–0.70"]["lo"]


def test_load_picks_the_candle_k_minutes_before_close(tmp_path):
    conn = connect(tmp_path / "h.db")
    close = 1_800_000_000
    conn.execute("INSERT INTO hist_markets VALUES ('T','BTC',?,?,100,'yes',101,1,1)",
                 ((close - 900) * 1000, close * 1000))
    for k, bid in ((0, 0.99), (1, 0.80), (2, 0.70)):
        conn.execute("INSERT INTO hist_candles VALUES ('T',?,?,?,NULL,NULL,NULL,NULL)",
                     (close - 60 * k, bid, bid + 0.01))
    assert load(conn, 1)[0]["yes_bid"] == 0.80
    assert load(conn, 2)[0]["yes_bid"] == 0.70
    assert load(conn, 5) == []
