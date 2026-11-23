"""Tests herméticos de la Fase 3: replay y calculadora de rentabilidad."""
import copy
import json

import pytest

from kalshi_lastminute.config import load_config
from kalshi_lastminute.db import Database
from kalshi_lastminute.replay import book_at, book_for_fill, pick_strategy, run_replay
from tools.breakeven import analyze, bootstrap_ci, percentile

CLOSE = 1_800_000_000_000


def seed_market(db, ticker="KXBTC15M-T", result="yes", index=101.0, origin="ws",
                late_book=None):
    """Mercado con 400 s de índice oficial y libro: YES se vende a 0.60."""
    db.add("markets", (ticker, "BTC", "E", CLOSE - 900_000, CLOSE, 100.0, "greater_or_equal",
                       "", "{}", CLOSE - 900_000))
    db.add("settlements", (ticker, CLOSE, result, index, 100.0, CLOSE, None, 0, "{}"))
    for s in range(400, 0, -1):
        t = CLOSE - s * 1000
        db.add("ticks_index", (t + 150, "BTC", "oficial", None, None, index + (s % 2) * 0.02, t))
    cheap = (json.dumps([[0.55, 500.0]]), json.dumps([[0.40, 500.0]]))
    for s in range(200, 0, -1):
        ts = CLOSE - s * 1000
        y, n = late_book if (late_book and s <= 89) else cheap
        db.add("orderbook_snapshots", (ts, ticker, s, origin, y, n, 0.55, 0.60, 500.0, 0.40,
                                       0.45, 500.0))
    db.flush()


def test_replay_enters_fills_and_settles_like_live(tmp_path):
    db = Database(tmp_path / "r.db")
    seed_market(db)
    res = run_replay(db.conn, load_config(), pick_strategy("settlement_nowcast_v1", []),
                     latency_ms=300)
    assert (res["usable"], len(res["trades"])) == (1, 1)
    t = res["trades"][0]
    assert (t["status"], t["side"], t["fill_price"]) == ("filled", "yes", 0.60)
    assert t["secs_left"] == pytest.approx(90.0)          # primera evaluación de la ventana
    assert t["pnl"] == pytest.approx(t["filled_contracts"] * 0.40 - t["fee"])
    assert t["depth"] == 500.0
    db.close()


def test_replay_misses_when_book_moves_within_latency(tmp_path):
    db = Database(tmp_path / "r.db")
    gone = (json.dumps([[0.90, 500.0]]), json.dumps([[0.05, 500.0]]))   # YES pasa a 0.95
    seed_market(db, late_book=gone)
    res = run_replay(db.conn, load_config(), pick_strategy("settlement_nowcast_v1", []),
                     latency_ms=1500)
    t = res["trades"][0]
    assert (t["status"], t["miss_reason"], t["pnl"]) == ("missed", "precio_se_fue", 0.0)
    db.close()


def test_replay_param_override_changes_behaviour(tmp_path):
    db = Database(tmp_path / "r.db")
    seed_market(db)
    strat = pick_strategy("settlement_nowcast_v1", ["max_secs_left=20", "min_edge=0.9"])
    assert strat.params["max_secs_left"] == 20
    res = run_replay(db.conn, load_config(), strat)
    assert res["trades"] == [] and res["skips"]["edge_bajo"] > 0
    with pytest.raises(SystemExit):
        pick_strategy("settlement_nowcast_v1", ["no_existe=1"])
    db.close()


def test_replay_books_have_no_lookahead():
    row = lambda ts, origin: {"ts_ms": ts, "origin": origin,  # noqa: E731
                              "yes_bids": "[[0.5, 1.0]]", "no_bids": f"[[{ts % 7 / 10}, 1.0]]"}
    data = {"book_ts": [1000, 2000, 3000], "books": [row(1000, "rest"), row(2000, "rest"),
                                                     row(3000, "rest")]}
    assert book_at(data, 999) is None
    assert book_at(data, 2500).no_bids == [(2000 % 7 / 10, 1.0)]       # el último <= t
    # REST: la orden que llega en 2100 se llena contra el primer libro leído después
    assert book_for_fill(data, 2100, 10_000).no_bids == [(3000 % 7 / 10, 1.0)]
    assert book_for_fill(data, 3500, 10_000) is None                    # no hay libro posterior
    data["books"][1]["origin"] = "ws"
    assert book_for_fill(data, 2100, 10_000).no_bids == [(2000 % 7 / 10, 1.0)]
    assert book_for_fill(data, 2100, 2050) is None                      # llegó tras el cierre


# ── calculadora ─────────────────────────────────────────────────────────
def bcfg():
    return copy.deepcopy(load_config()["breakeven"])


def tr(pnl, ms, stake=25.0, depth=400.0):
    return {"status": "filled", "filled_contracts": stake / 0.5, "fill_price": 0.5, "fee": 0.0,
            "pnl": pnl, "decided_ms": ms, "depth": depth, "decision_price": 0.5}


def test_percentile_and_bootstrap_are_deterministic():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([1, 2, 3, 4], 0.25) == pytest.approx(1.75)
    pnls, risked = [1.0, -1.0, 2.0, 0.5] * 10, [10.0] * 40
    assert bootstrap_ci(pnls, risked, 500, 0.95) == bootstrap_ci(pnls, risked, 500, 0.95)
    lo, hi = bootstrap_ci([1.0] * 40, risked, 500, 0.95)
    assert lo == hi == pytest.approx(0.1)


def test_breakeven_says_no_evidence_when_interval_includes_loss():
    day = 86_400_000
    trades = [tr(25.0 if i % 2 else -25.0, i * day // 20) for i in range(40)]
    a = analyze(trades, bcfg())
    assert a["ci"][0] < 0 < a["ci"][1]
    assert a["conclusion"].startswith("No hay evidencia de edge todavía")


def test_breakeven_capital_math_with_a_clear_edge():
    day = 86_400_000
    # 200 operaciones en 10 días: gana 2.5 (10 %) nueve de cada diez, pierde 2.5 una
    trades = [tr(-2.5 if i % 10 == 0 else 2.5, i * day // 20) for i in range(200)]
    cfg = bcfg()
    cfg.update(fixed_costs_usd={"vps": 10.0}, monthly_targets_usd=[100.0],
               drawdown_reserve_mult=2.0, min_trades=100)
    a = analyze(trades, cfg, n_assets=3)
    assert a["expectancy_per_usd"] == pytest.approx(0.08)
    assert a["trades_per_day"] == pytest.approx(200 / (199 / 20), rel=1e-6)
    assert a["conclusion"].startswith("Hay evidencia de edge")
    plan = a["plans_point"]["cubrir costos fijos"]
    assert plan["stake"] == pytest.approx(10.0 / (30 * a["trades_per_day"] * 0.08))
    assert a["worst_streak_stakes"] == pytest.approx(0.1)        # una pérdida de 10 %
    assert plan["capital"] == pytest.approx(plan["stake"] * (3 + 2.0 * 0.1))
    assert a["capacity_usd"] == pytest.approx(200.0)             # 400 contratos × 0.5
    assert a["plans_point"]["100 USD/mes netos"]["feasible"] is True
    assert a["n_missing"] == 0


def test_breakeven_flags_targets_beyond_book_capacity_and_missing_trades():
    day = 86_400_000
    trades = [tr(-2.5 if i % 10 == 0 else 2.5, i * day, depth=2.0) for i in range(30)]
    cfg = bcfg()
    cfg.update(monthly_targets_usd=[5000.0], min_trades=100)
    a = analyze(trades, cfg)
    assert a["plans_point"]["5000 USD/mes netos"]["feasible"] is False
    assert a["n_missing"] == 70
    assert "no es confiable" in a["conclusion"]


def test_breakeven_with_no_trades():
    assert "menos de 2 operaciones" in analyze([], bcfg())["conclusion"]
