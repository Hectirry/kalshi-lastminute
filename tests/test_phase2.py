"""Tests herméticos de la Fase 2: modelo, ejecución simulada, estrategia, motor y KPIs."""
import asyncio
import copy
import math

import pytest

from kalshi_lastminute.config import load_config
from kalshi_lastminute.execution import settle_pnl, simulate_fill
from kalshi_lastminute.feeds.kalshi import Book
from kalshi_lastminute.kpis import compute, verdict
from kalshi_lastminute.model import nowcast, realized_sigma
from kalshi_lastminute.strategy import Context, LoadedStrategy, load_strategies

FEES = {"taker_rate": 0.07, "multiplier": 1, "round_to": 0.01}
CLOSE = 1_800_000_000_000


def flat(value: float, first_s: int, last_s: int) -> list[tuple[int, float]]:
    """Un punto por segundo desde cierre-first_s hasta cierre-last_s, valor constante."""
    return [(CLOSE - s * 1000, value) for s in range(first_s, last_s - 1, -1)]


# ── modelo ──────────────────────────────────────────────────────────────
def test_nowcast_variance_matches_closed_form():
    nc = nowcast(flat(100.0, 120, 30), CLOSE, 100.0, sigma=0.01)
    assert (nc.fixed, nc.remaining) == (31, 29)          # cierre-60 ... cierre-30
    assert nc.std == pytest.approx(0.01 * math.sqrt(29 * 30 * 59 / 6) / 60)
    assert nc.prob_yes == pytest.approx(0.5)


def test_nowcast_before_window_adds_lead_time_variance():
    nc = nowcast(flat(100.0, 120, 91), CLOSE, 100.0, sigma=0.01)
    assert (nc.fixed, nc.remaining) == (0, 60)
    d = 30  # el primer valor de la ventana está a 31 s del último observado
    assert nc.std == pytest.approx(0.01 * math.sqrt(3600 * d + 60 * 61 * 121 / 6) / 60)


def test_nowcast_is_certain_when_window_is_complete_and_tie_is_yes():
    nc = nowcast(flat(100.0, 120, 1), CLOSE, 100.0, sigma=0.01)
    assert (nc.fixed, nc.remaining, nc.std) == (60, 0, 0.0)
    assert nc.prob_yes == 1.0                             # empate: final >= strike es YES
    assert nowcast(flat(100.0, 120, 1), CLOSE, 100.01, sigma=0.01).prob_yes == 0.0


def test_nowcast_fixed_part_pulls_the_mean():
    pts = flat(100.0, 120, 31) + [(CLOSE - 30_000, 106.0)]
    nc = nowcast(pts, CLOSE, 0.0, sigma=0.01)
    assert nc.mean == pytest.approx((30 * 100.0 + 106.0 + 29 * 106.0) / 60)


def test_nowcast_ignores_points_at_or_after_close():
    pts = flat(100.0, 120, 1) + [(CLOSE, 500.0), (CLOSE + 1000, 500.0)]
    assert nowcast(pts, CLOSE, 0.0, sigma=0.01).mean == pytest.approx(100.0)


def test_realized_sigma_needs_enough_data_and_skips_gaps():
    assert realized_sigma([1.0] * 10) is None
    series = [100.0 + (i % 2) * 0.02 for i in range(60)]
    assert realized_sigma(series) == pytest.approx(0.02)
    series[10] = None
    assert realized_sigma(series) == pytest.approx(0.02)


# ── ejecución simulada ──────────────────────────────────────────────────
def test_fill_is_capped_by_real_depth_and_limit_price():
    asks = [(0.60, 10.0), (0.61, 50.0)]
    f = simulate_fill(asks, 0.60, 40, FEES)
    assert (f.status, f.contracts, f.price) == ("partial", 10.0, 0.60)
    f = simulate_fill(asks, 0.61, 40, FEES)                # con deslizamiento permitido
    assert f.status == "filled" and f.price == pytest.approx((10 * 0.60 + 30 * 0.61) / 40)


def test_fill_is_missed_when_price_moved_away_or_book_is_empty():
    f = simulate_fill([(0.65, 100.0)], 0.60, 40, FEES)
    assert (f.status, f.contracts, f.fee, f.reason) == ("missed", 0.0, 0.0, "precio_se_fue")
    assert simulate_fill([], 0.60, 40, FEES).reason == "sin_oferta"


def test_fill_gets_price_improvement_and_pays_fee():
    f = simulate_fill([(0.55, 100.0)], 0.60, 40, FEES)
    assert (f.status, f.price) == ("filled", 0.55)
    assert f.fee == 0.70                                   # ceil(0.07*40*0.55*0.45 = 0.693)


def test_settle_pnl():
    assert settle_pnl("yes", "yes", 40, 0.60, 0.68) == pytest.approx(40 - 24 - 0.68)
    assert settle_pnl("no", "yes", 40, 0.60, 0.68) == pytest.approx(-24.68)


# ── estrategia ──────────────────────────────────────────────────────────
def strat() -> LoadedStrategy:
    return next(s for s in load_strategies() if s.name == "settlement_nowcast_v1")


def ctx(points, book, secs_left=30.0, strike=100.0, age=200) -> Context:
    return Context(asset="BTC", ticker="T", now_ms=CLOSE - int(secs_left * 1000), close_ms=CLOSE,
                   strike=strike, index_points=points, index_age_ms=age, book=book, fees=FEES)


def noisy(base: float, first_s: int, last_s: int) -> list[tuple[int, float]]:
    return [(CLOSE - s * 1000, base + (s % 2) * 0.02) for s in range(first_s, last_s - 1, -1)]


def test_strategy_enters_the_mispriced_side():
    s = strat()
    # Índice muy por encima del strike: YES casi seguro, pero se vende a 0.60.
    book = Book(yes_bids=[(0.55, 500.0)], no_bids=[(0.40, 500.0)])
    d = s.evaluate(ctx(noisy(101.0, 400, 30), book), s.params)
    assert (d.action, d.side, d.price) == ("enter", "yes", 0.60)
    assert d.contracts == int(s.params["stake_usd"] / 0.60)
    assert d.edge_net == pytest.approx(d.model_prob - 0.60 - 0.69 / 41, abs=1e-6)


def test_strategy_skip_reasons_are_stable_labels():
    s = strat()
    pts = noisy(101.0, 400, 30)
    fair = Book(yes_bids=[(0.985, 500.0)], no_bids=[(0.01, 500.0)])
    cases = {
        "sin_strike": ctx(pts, fair, strike=None),
        "indice_viejo": ctx(pts, fair, age=10_000),
        "sin_volatilidad": ctx(pts[-10:], fair),
        "fuera_de_ventana": ctx(pts, fair, secs_left=300),
        "sin_libro": ctx(pts, None),
        "precio_fuera_de_banda": ctx(pts, fair),            # YES a 0.99, NO a 0.015
        "liquidez_baja": ctx(pts, Book([(0.55, 0.4)], [(0.40, 0.4)])),
        # índice oscilando alrededor del strike (P≈0.5) y ambos lados a 0.51
        "edge_bajo": ctx(noisy(100.0, 400, 30), Book([(0.49, 500.0)], [(0.49, 500.0)]),
                         strike=100.005),
    }
    for reason, c in cases.items():
        d = s.evaluate(c, s.params)
        assert (d.action, d.reason) == ("skip", reason)


# ── motor ───────────────────────────────────────────────────────────────
def make_engine(tmp_path, latency_ms=10):
    from kalshi_lastminute.engine import Engine
    cfg = copy.deepcopy(load_config())
    cfg["storage"]["db_path"] = str(tmp_path / "e.db")
    cfg["engine"].update(latency_ms=latency_ms, dashboard=None)
    return Engine(cfg, strategies=[strat()])


def arm(engine, book, secs_left=30.0):
    """Deja al motor con un mercado BTC activo, índice fresco y libro en vivo."""
    from kalshi_lastminute.recorder import now_ms
    now = now_ms()
    close = (now // 1000) * 1000 + int(secs_left * 1000)
    iso = __import__("datetime").datetime.fromtimestamp(close / 1000, __import__("datetime").UTC)
    market = {"ticker": "KXBTC15M-TEST", "close_time": iso.isoformat().replace("+00:00", "Z"),
              "open_time": iso.isoformat().replace("+00:00", "Z"), "floor_strike": 100.0}
    engine.active["BTC"] = market
    engine.last_book["BTC"] = book
    engine.book_ts["BTC"] = now
    engine.ws_status["live"] = {"KXBTC15M-TEST"}
    last_t = (now // 1000) * 1000
    for i in range(400, -1, -1):
        engine.series["BTC"]["oficial"].append((last_t - i * 1000, 101.0 + (i % 2) * 0.02, now))
    return market, close


def test_engine_enters_once_fills_after_latency_and_settles(tmp_path):
    async def scenario():
        e = make_engine(tmp_path)
        market, close = arm(e, Book([(0.55, 500.0)], [(0.40, 500.0)]))
        e.evaluate_market("BTC", market)
        e.evaluate_market("BTC", market)                   # no debe duplicar
        await asyncio.sleep(0.1)
        e.db.flush()
        trades = e.db.query("SELECT * FROM paper_trades")
        assert len(trades) == 1
        t = trades[0]
        assert (t["status"], t["side"], t["fill_price"], t["asset"]) == ("filled", "yes", 0.60, "BTC")
        assert t["pnl"] is None
        assert e.db.query("SELECT COUNT(*) n FROM decisions WHERE action='enter'")[0]["n"] == 1
        e.db.add("settlements", ("KXBTC15M-TEST", close, "yes", 101.0, 100.0, close, None, 0, "{}"))
        e.db.flush()
        e.on_settled()
        e.on_settled()                                     # idempotente
        pnl = e.db.query("SELECT pnl FROM paper_trades")[0]["pnl"]
        assert pnl == pytest.approx(t["filled_contracts"] * 0.40 - t["fee"])
        e.db.close()
    asyncio.run(scenario())


def test_engine_records_missed_fill_when_level_disappears(tmp_path):
    async def scenario():
        e = make_engine(tmp_path, latency_ms=30)
        market, _ = arm(e, Book([(0.55, 500.0)], [(0.40, 500.0)]))
        e.evaluate_market("BTC", market)
        e.last_book["BTC"] = Book([(0.90, 500.0)], [(0.05, 500.0)])  # el ask de YES pasó a 0.95
        e.book_ts["BTC"] += 20
        await asyncio.sleep(0.15)
        t = e.db.query("SELECT * FROM paper_trades")[0]
        assert (t["status"], t["miss_reason"], t["filled_contracts"], t["pnl"]) == (
            "missed", "precio_se_fue", 0.0, 0.0)
        e.db.close()
    asyncio.run(scenario())


def test_engine_restart_marks_pending_as_missed_and_does_not_retry(tmp_path):
    async def scenario():
        e = make_engine(tmp_path, latency_ms=5000)
        market, _ = arm(e, Book([(0.55, 500.0)], [(0.40, 500.0)]))
        e.evaluate_market("BTC", market)                   # queda 'pending': se "apaga" antes del fill
        e.db.close()
        e2 = make_engine(tmp_path)
        t = e2.db.query("SELECT status, miss_reason FROM paper_trades")[0]
        assert (t["status"], t["miss_reason"]) == ("missed", "reinicio")
        market, _ = arm(e2, Book([(0.55, 500.0)], [(0.40, 500.0)]))
        e2.evaluate_market("BTC", market)
        assert e2.db.query("SELECT COUNT(*) n FROM paper_trades")[0]["n"] == 1
        e2.db.close()
    asyncio.run(scenario())


def test_engine_logs_skip_decisions_with_reason(tmp_path):
    async def scenario():
        e = make_engine(tmp_path)
        market, _ = arm(e, Book([(0.985, 500.0)], [(0.01, 500.0)]))
        e.evaluate_market("BTC", market)
        e.evaluate_market("BTC", market)                   # misma razón en <1 s: no se repite
        e.db.flush()
        rows = e.db.query("SELECT action, reason, asset FROM decisions")
        assert [tuple(r) for r in rows] == [("skip", "precio_fuera_de_banda", "BTC")]
        e.db.close()
    asyncio.run(scenario())


# ── KPIs ────────────────────────────────────────────────────────────────
def trade(pnl, status="filled", side="yes", prob=0.8, ms=0, price=0.5, n=10.0, fee=0.1, edge=0.05):
    return {"status": status, "side": side, "filled_contracts": n if status != "missed" else 0.0,
            "fill_price": price if status != "missed" else None, "fee": fee, "pnl": pnl,
            "model_prob": prob, "edge_net": edge, "decided_ms": ms, "asset": "BTC"}


def test_kpis_basic_numbers():
    win, loss = 10 * 0.5 - 0.1, -10 * 0.5 - 0.1            # +4.9 y -5.1
    trades = [trade(win, ms=0), trade(loss, ms=1000), trade(win, ms=2000),
              trade(0.0, status="missed", ms=3000), trade(None, ms=4000)]
    k = compute(trades)
    assert (k["attempts"], k["filled"], k["missed"], k["closed"], k["open"]) == (5, 4, 1, 3, 1)
    assert k["missed_rate"] == pytest.approx(0.2)
    assert k["win_rate"] == pytest.approx(2 / 3)
    assert k["pnl_net"] == pytest.approx(2 * win + loss)
    assert k["profit_factor"] == pytest.approx(2 * win / -loss)
    assert k["max_drawdown"] == pytest.approx(-loss)
    assert k["edge_realized"] == pytest.approx((2 * win + loss) / 30)
    assert k["edge_expected"] == pytest.approx(0.05)
    assert k["calibration"] == [{"bucket": "0.8-0.9", "n": 3, "model": pytest.approx(0.8),
                                 "actual": pytest.approx(2 / 3)}]


def test_kpis_calibration_uses_probability_of_the_side_bought():
    k = compute([trade(4.9, side="no", prob=0.1)])          # compró NO con P(YES)=0.1 → 0.9
    assert k["calibration"][0]["bucket"] == "0.9-1.0"


def test_verdict_refuses_to_conclude_with_few_trades():
    assert "Sin veredicto" in verdict(compute([trade(4.9)] * 5))
    assert "No hay edge" in verdict(compute([trade(-5.1, ms=i) for i in range(120)]))
    assert "Sin operaciones" in verdict(compute([]))


def test_engine_ignores_a_frozen_book(tmp_path):
    """Un libro que dejó de actualizarse no puede generar entradas (ni fills)."""
    async def scenario():
        e = make_engine(tmp_path)
        market, _ = arm(e, Book([(0.55, 500.0)], [(0.40, 500.0)]))
        e.book_ts["BTC"] -= 10_000                         # el libro lleva 10 s congelado
        e.evaluate_market("BTC", market)
        e.db.flush()
        assert e.db.query("SELECT COUNT(*) n FROM paper_trades")[0]["n"] == 0
        assert [r["reason"] for r in e.db.query("SELECT reason FROM decisions")] == ["sin_libro"]
        e.db.close()
    asyncio.run(scenario())


def test_kpis_ignore_voided_trades():
    k = compute([trade(4.9, ms=0), trade(50.0, status="void", ms=1), trade(-5.1, ms=2)])
    assert (k["attempts"], k["closed"]) == (2, 2)
    assert k["pnl_net"] == pytest.approx(-0.2)
