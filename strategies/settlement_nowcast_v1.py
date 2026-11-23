"""settlement_nowcast_v1: compra el lado mal valorado en la ventana final.

Estima P(YES) combinando la parte ya fijada del promedio de liquidación con la
volatilidad realizada de los segundos que faltan, y entra como taker si el
edge neto de fees supera el umbral.
"""
from kalshi_lastminute.fees import fee
from kalshi_lastminute.index import per_second_series
from kalshi_lastminute.model import nowcast, realized_sigma
from kalshi_lastminute.strategy import Context, Decision


def evaluate(ctx: Context, p: dict) -> Decision:
    if ctx.strike is None:
        return Decision("skip", "sin_strike")
    if not ctx.index_points or ctx.index_age_ms is None or ctx.index_age_ms > p["max_index_age_ms"]:
        return Decision("skip", "indice_viejo")

    t_last = ctx.index_points[-1][0]
    lookback = per_second_series(ctx.index_points, t_last - p["vol_lookback_s"] * 1000, t_last)
    sigma = realized_sigma(lookback, p["vol_min_points"])
    if sigma is None:
        return Decision("skip", "sin_volatilidad")
    last = ctx.index_points[-1][1]
    sigma = max(sigma, p["vol_floor_bps"] * 1e-4 * last) * p["vol_mult"]
    nc = nowcast(ctx.index_points, ctx.close_ms, ctx.strike, sigma, ctx.window_s,
                 extra_sigma=p["index_error_bps"] * 1e-4 * last)
    if nc is None:
        return Decision("skip", "indice_viejo")
    prob = nc.prob_yes
    info = {"mean": nc.mean, "std": nc.std, "fixed": nc.fixed, "sigma": nc.sigma}

    if not (p["min_secs_left"] <= ctx.secs_left <= p["max_secs_left"]):
        return Decision("skip", "fuera_de_ventana", model_prob=prob, info=info)
    if ctx.book is None:
        return Decision("skip", "sin_libro", model_prob=prob, info=info)

    best = None
    seen_price = False
    for side, p_win in (("yes", prob), ("no", 1.0 - prob)):
        ask = ctx.book.best_ask(side)
        if ask is None:
            continue
        price, depth = ask
        if not (p["min_price"] <= price <= p["max_price"]):
            continue
        seen_price = True
        contracts = min(round(p["stake_usd"] / price, 2), depth * p["max_depth_fraction"])
        contracts = float(int(contracts))  # contratos enteros
        if contracts < p["min_contracts"]:
            continue
        cost_fee = fee(contracts, price, ctx.fees["taker_rate"], ctx.fees["multiplier"],
                       ctx.fees["round_to"]) / contracts
        edge = p_win - price - cost_fee
        if best is None or edge > best[0]:
            best = (edge, side, price, contracts, depth)

    if best is None:
        reason = "liquidez_baja" if seen_price else "precio_fuera_de_banda"
        return Decision("skip", reason, model_prob=prob, info=info)
    edge, side, price, contracts, depth = best
    info["depth"] = depth  # contratos disponibles en el mejor ask al decidir
    if edge < p["min_edge"]:
        return Decision("skip", "edge_bajo", side=side, price=price, contracts=contracts,
                        model_prob=prob, edge_net=edge, info=info)
    return Decision("enter", "edge", side=side, price=price, contracts=contracts,
                    model_prob=prob, edge_net=edge, info=info)
