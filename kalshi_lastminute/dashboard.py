"""Dashboard local (FastAPI) servido desde el mismo proceso que el motor."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .feeds.kalshi import iso_to_ms
from .recorder import now_ms

log = logging.getLogger("dashboard")


def state(engine) -> dict:
    now = now_ms()
    assets = []
    for asset in engine.assets:
        m, book = engine.active[asset], engine.last_book[asset]
        row = {"asset": asset, "ticker": None, "strategies": []}
        for source in ("oficial", "composite"):
            s = engine.series[asset][source]
            row[source] = s[-1][1] if s and now - s[-1][2] < 5000 else None
        if m:
            ya = book.best_ask("yes") if book else None
            na = book.best_ask("no") if book else None
            row.update(ticker=m["ticker"], strike=m.get("floor_strike"),
                       secs_left=(iso_to_ms(m["close_time"]) - now) / 1000.0,
                       yes_ask=ya[0] if ya else None, no_ask=na[0] if na else None,
                       live=m["ticker"] in engine.ws_status.get("live", ()))
            for strat in engine.strategies:
                ev = engine.last_eval.get((asset, strat.name))
                if not ev or ev["ticker"] != m["ticker"]:
                    continue
                pos = engine.db.query(
                    "SELECT side, status, filled_contracts, fill_price, decision_price "
                    "FROM paper_trades WHERE ticker=? AND strategy=?", (m["ticker"], strat.name))
                row["strategies"].append({
                    "name": strat.name, "model_prob": ev["model_prob"], "reason": ev["reason"],
                    "edge_net": ev["edge_net"], "side": ev["side"], "source": ev["source"],
                    "position": dict(pos[0]) if pos else None})
        assets.append(row)

    totals = [dict(r) for r in engine.db.query(
        "SELECT strategy, COUNT(*) intentos, "
        "SUM(status IN ('filled','partial')) llenas, SUM(status='missed') perdidas, "
        "SUM(status='pending') pendientes, "
        "SUM(CASE WHEN status IN ('filled','partial') AND pnl IS NULL THEN 1 ELSE 0 END) abiertas, "
        "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) ganadas, "
        "SUM(CASE WHEN status IN ('filled','partial') AND pnl IS NOT NULL THEN 1 ELSE 0 END) cerradas, "
        "COALESCE(SUM(pnl), 0) pnl FROM paper_trades WHERE status != 'void' GROUP BY strategy")]
    trades = [dict(r) for r in engine.db.query(
        "SELECT decided_ms, strategy, asset, ticker, side, status, miss_reason, requested_contracts, "
        "filled_contracts, decision_price, fill_price, fee, model_prob, edge_net, pnl "
        "FROM paper_trades ORDER BY id DESC LIMIT 30")]
    return {"now_ms": now, "assets": assets, "totals": totals, "trades": trades,
            "counts": engine.counts}


PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>kalshi-lastminute (paper)</title>
<style>
 body{font:14px system-ui,sans-serif;margin:20px;background:#111;color:#ddd}
 h1{font-size:18px} h2{font-size:15px;margin-top:22px}
 table{border-collapse:collapse;width:100%} th,td{padding:5px 9px;text-align:right;border-bottom:1px solid #333}
 th{color:#999;font-weight:500} td:first-child,th:first-child{text-align:left}
 .pos{color:#4caf50}.neg{color:#ef5350}.dim{color:#888}.tag{background:#333;border-radius:4px;padding:1px 6px}
</style></head><body>
<h1>kalshi-lastminute <span class="tag">PAPER: sin órdenes reales</span></h1>
<h2>Mercados activos</h2><table id="m"></table>
<h2>Acumulado por estrategia</h2><table id="t"></table>
<h2>Últimas operaciones simuladas</h2><table id="o"></table>
<p class="dim" id="c"></p>
<script>
const f=(v,n=2)=>v==null?'–':Number(v).toFixed(n);
const cls=v=>v>0?'pos':v<0?'neg':'';
async function tick(){
 const s=await (await fetch('/api/state')).json();
 let h='<tr><th>Activo</th><th>Mercado</th><th>Cierra en</th><th>Strike</th><th>Índice oficial</th><th>Nuestro índice</th><th>P(YES) modelo</th><th>Ask YES</th><th>Ask NO</th><th>Edge neto</th><th>Estado</th><th>Posición</th></tr>';
 for(const a of s.assets){
  const st=a.strategies[0]||{}; const p=st.position;
  const nd=a.asset=='SOL'?4:2;
  h+=`<tr><td>${a.asset}</td><td>${a.ticker||'sin mercado'}${a.live?'':' <span class="dim">(REST)</span>'}</td>
  <td>${a.secs_left==null?'–':Math.floor(a.secs_left/60)+':'+String(Math.floor(a.secs_left%60)).padStart(2,'0')}</td>
  <td>${f(a.strike,nd)}</td><td>${f(a.oficial,nd)}</td><td>${f(a.composite,nd)}</td>
  <td>${f(st.model_prob,3)}</td><td>${f(a.yes_ask,3)}</td><td>${f(a.no_ask,3)}</td>
  <td class="${cls(st.edge_net)}">${f(st.edge_net,3)}${st.side?' '+st.side.toUpperCase():''}</td>
  <td>${st.reason||'–'}</td>
  <td>${p?`${p.side.toUpperCase()} ${f(p.filled_contracts,0)} @ ${f(p.fill_price??p.decision_price,3)} (${p.status})`:'–'}</td></tr>`;
 }
 document.getElementById('m').innerHTML=h;
 h='<tr><th>Estrategia</th><th>Intentos</th><th>Llenas</th><th>Perdidas por latencia</th><th>Tasa de pérdidas</th><th>Abiertas</th><th>Ganadas / cerradas</th><th>P&L neto USD</th></tr>';
 for(const t of s.totals){h+=`<tr><td>${t.strategy}</td><td>${t.intentos}</td><td>${t.llenas}</td><td>${t.perdidas}</td>
  <td>${t.intentos?f(100*t.perdidas/t.intentos,1)+' %':'–'}</td><td>${t.abiertas}</td><td>${t.ganadas} / ${t.cerradas}</td>
  <td class="${cls(t.pnl)}">${f(t.pnl)}</td></tr>`;}
 if(!s.totals.length)h+='<tr><td colspan="8" class="dim">Aún no hay operaciones.</td></tr>';
 document.getElementById('t').innerHTML=h;
 h='<tr><th>Hora</th><th>Mercado</th><th>Lado</th><th>Estado</th><th>Contratos</th><th>Precio visto</th><th>Precio lleno</th><th>Fee</th><th>P(YES)</th><th>Edge esperado</th><th>P&L</th></tr>';
 for(const o of s.trades){h+=`<tr><td>${new Date(o.decided_ms).toLocaleTimeString()}</td><td>${o.ticker}</td><td>${o.side.toUpperCase()}</td>
  <td>${o.status}${o.miss_reason?' ('+o.miss_reason+')':''}</td><td>${f(o.filled_contracts,0)} / ${f(o.requested_contracts,0)}</td>
  <td>${f(o.decision_price,3)}</td><td>${f(o.fill_price,3)}</td><td>${f(o.fee)}</td><td>${f(o.model_prob,3)}</td><td>${f(o.edge_net,3)}</td>
  <td class="${cls(o.pnl)}">${o.pnl==null?'abierta':f(o.pnl)}</td></tr>`;}
 document.getElementById('o').innerHTML=h;
 document.getElementById('c').textContent='Filas grabadas: '+Object.entries(s.counts).map(([k,v])=>k+'='+v).join('  ');
}
tick();setInterval(tick,1000);
</script></body></html>"""


def create_app(engine) -> FastAPI:
    app = FastAPI(title="kalshi-lastminute", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return PAGE

    @app.get("/api/state")
    async def api_state() -> dict:  # async: corre en el hilo del motor, dueño de SQLite
        return state(engine)

    return app


async def serve(engine) -> None:
    import uvicorn
    d = engine.ecfg["dashboard"]
    server = uvicorn.Server(uvicorn.Config(create_app(engine), host=d["host"], port=d["port"],
                                           log_level="warning"))
    log.info("dashboard en http://%s:%s", d["host"], d["port"])
    try:
        await server.serve()
    finally:
        engine.stop.set()  # Ctrl+C lo captura uvicorn: al salir, se detiene el motor
