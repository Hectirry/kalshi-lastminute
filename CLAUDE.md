# kalshi-lastminute

Bot de **paper trading** para los mercados de 15 minutos de Kalshi sobre BTC, ETH y SOL (series `KXBTC15M`, `KXETH15M`, `KXSOL15M`). Estima en el último tramo de cada mercado el valor de liquidación y simula compras cuando el contrato está mal valorado.

- Especificación original: `Prompt para Claude Code — Bot Kalshi BTC 15m, ventana final (paper trading).md` (pedía solo BTC; el usuario añadió ETH y SOL el 2026-11-22).
- Hechos sobre Kalshi y sus fuentes: `docs/RESEARCH.md`.
- Uso y lectura de reportes: `README.md`.
- Las tres fases de la especificación están implementadas.

## Reglas que no se rompen

- **Solo paper trading.** No escribas código que envíe, modifique o cancele órdenes en Kalshi. `feeds/kalshi.py` es de solo lectura.
- **Ningún LLM en el camino de decisión.** Las decisiones son código determinista.
- **Secretos fuera de Git.** La llave privada de Kalshi está en `Claude_Bot.txt` (ignorado por Git): no la abras ni la muestres; solo la carga `load_private_key()`. El identificador de la llave y la ruta están en `config.local.yaml` (también ignorado). Las reglas de permisos del usuario impiden leer o escribir `.env*`: no las rodees.
- **Bloqueo de DNS hacia `kalshi.com`** (`docs/RESEARCH.md` §0): el usuario autorizó el 2026-11-22 que el bot resuelva solo `kalshi.com` por DNS-over-HTTPS (`net.py`, `network.dns_override`). No lo amplíes a otros dominios, ni toques el DNS de Windows, ni añadas proxy o VPN sin que lo pida.
- **Nada sobre Kalshi se asume de memoria.** Si un dato no está en `docs/RESEARCH.md`, investígalo en fuente oficial o márcalo como `SUPUESTO` en `config.yaml`.
- **Honestidad con los números.** Si no hay edge, se dice sin suavizar. Con menos de 100 operaciones cerradas no hay veredicto, y si el intervalo de confianza incluye pérdida la conclusión es "no hay evidencia de edge todavía".
- **No ajustes parámetros mirando el P&L de pocas operaciones.** Un cambio de estrategia va como variante nueva (`.yaml` aparte) y se compara por replay; no se edita la variante que ya tiene historial.

## Comandos

Windows; el intérprete es el del venv.

```powershell
.venv\Scripts\python -m pytest -q                                    # tests, sin red
.venv\Scripts\python -m kalshi_lastminute.run                        # bot completo + dashboard en :8000
.venv\Scripts\python -m kalshi_lastminute.recorder                   # solo grabar
.venv\Scripts\python -m kalshi_lastminute.report                     # KPIs del paper trading
.venv\Scripts\python -m kalshi_lastminute.replay --strategy <variante> [--param k=v] [--latency-ms N]
.venv\Scripts\python -m tools.breakeven [--replay]                   # capital necesario y evidencia de edge
.venv\Scripts\python -m kalshi_lastminute.summary                    # calidad de datos y error del índice
.venv\Scripts\python -m tools.backfill [--days N]                       # histórico público -> data/history.db
.venv\Scripts\python -m tools.calibration                               # calibración del mercado cerca del cierre
.venv\Scripts\python -m tools.backfill --spot                           # velas 1 min de Coinbase -> data/history.db
.venv\Scripts\python -m tools.model_test                                # ¿el modelo añade algo al precio de Kalshi?
.venv\Scripts\python -m tools.backfill --trades 3                       # trades públicos resumidos (lento: ~1 h)
.venv\Scripts\python -m tools.flow                                      # quién gana: tomadores o creadores de mercado
```

- Solo puede correr un `run` o `recorder` a la vez (candado en el puerto local 47615). Los reportes y el replay abren la base en solo lectura y se pueden lanzar con el bot encendido.
- Para verificar un cambio del motor, reinicia el bot y mira `data/bot.log` y <http://127.0.0.1:8000/api/state>.
- En la consola de Windows antepón `$env:PYTHONIOENCODING='utf-8'` si los acentos fallan.

## Estructura

```
config.yaml                   parámetros y supuestos marcados
config.local.yaml             ajustes de esta máquina (llave de API); pisa a config.yaml; fuera de Git
strategies/<variante>.yaml    parámetros de una variante; `module:` indica el .py
strategies/<módulo>.py        evaluate(ctx, params) -> Decision, función pura
kalshi_lastminute/
  config.py   db.py   net.py  carga de config, esquema SQLite + migraciones, DNS propio
  feeds/spot.py               WebSockets de Coinbase, Kraken, Bitstamp, Gemini, Binance
  feeds/kalshi.py             Kalshi: REST y WebSocket (libro por deltas, índice oficial)
  index.py                    índice compuesto y series por segundo (puro)
  model.py                    nowcast: P(final >= strike) (puro)
  execution.py   fees.py      fill simulado, P&L y fees (puro)
  strategy.py                 Context, Decision y cargador de variantes
  recorder.py                 grabador: feeds -> SQLite
  engine.py                   Engine(Recorder): estrategias, fills con latencia, liquidación
  dashboard.py   run.py       FastAPI en el mismo proceso; comando de arranque
  kpis.py   report.py         KPIs (puro) y reporte
  replay.py                   backtest sobre la data grabada
  summary.py                  resumen de datos grabados
tools/breakeven.py            calculadora de rentabilidad
tools/backfill.py             descarga mercados liquidados y velas de 1 min (API pública) a data/history.db
tools/calibration.py          calibración del precio de Kalshi y retorno de un taker por rango de precio
tools/flowdata.py             descarga spot de Coinbase y trades públicos (agregados, no crudos)
tools/model_test.py           modelo vs mercado: regresión de inclusión y simulación, con ajuste/prueba separados
tools/flow.py                 P&L por contrato de tomadores y creadores de mercado
tests/                        herméticos, sin red
data/   reports/              bases, logs y reportes (fuera de Git)
```

## Invariantes del dominio

- Liquidación: promedio simple de los 60 valores por segundo del índice de CF Benchmarks (`BRTI`, `ETHUSD_RTI`, `SOLUSD_RTI`) con tiempo en [cierre − 60 s, cierre), redondeado a 2 decimales (4 en SOL). YES si final **≥** strike. El strike es el valor final del mercado anterior y tarda unos segundos en aparecer al abrir.
- Kalshi solo publica bids. Comprar YES como taker cruza contra el mejor bid de NO: `ask_yes = 1 − mejor_bid_no`, con la cantidad de ese bid. Usa `Book.best_ask()` / `Book.asks()`; no recalcules a mano.
- Precios en dólares de 0 a 1 con hasta 4 decimales; tick de $0.001 fuera de [0.10, 0.90]. Cantidades fraccionarias. No conviertas a centavos enteros.
- `ticks_index`: `source='oficial'` es el índice real que publica Kalshi (su tiempo es `exch_ts_ms`); `composite` es nuestra reconstrucción desde exchanges (su tiempo es `ts_ms`). `binance` cotiza contra USDT y va con peso 0.
- Sin mirar al futuro: en cualquier instante solo se usa lo ya recibido (`ts_ms` ≤ ahora). Series por segundo con forward-fill, nunca interpolación.
- Compras simuladas: siempre taker al ask visible, nunca al mid; el fill se hace contra el libro de `latency_ms` después; el tamaño no supera la cantidad real del nivel; si el precio se fue, la orden se pierde y queda registrada.
- El libro por WebSocket recibe más de mil cambios por segundo en BTC. No guardes una fila por cambio: `orderbook_snapshots` guarda el libro completo cada 5 s y, en los últimos `fast_window_s`, los mejores niveles cada `book_sample_ms`.
- El `seq` del WebSocket de Kalshi cuenta todos los mensajes de una suscripción, incluidos los `ok`.
- Los errores y distancias entre activos se comparan en bps, no en USD.
- Histórico público de Kalshi: `/markets?status=settled` cubre ~2 meses; antes de `/historical/cutoff` hay que usar `/historical/markets`. `/markets/candlesticks` acepta varios tickers con tope de 10 000 velas por petición (24 mercados consecutivos). Sin firma, más de ~5 peticiones/s devuelve 429.
- Trades públicos (`/markets/trades`): `taker_outcome_side` es el lado que COMPRÓ el tomador, al precio de ese lado (verificado contra el libro grabado). Un mercado de BTC tiene ~20 000 trades: se guardan agregados (`hist_flow`), no crudos.
- Un libro por deltas que deja de recibir mensajes queda congelado y muestra precios que ya no existen. El 2026-11-22 eso generó 5 operaciones con ganancia falsa. Defensas que no se quitan: reconexión ante `seq` roto o error 25 del servidor, `engine.max_book_age_ms`, y el test `test_engine_ignores_a_frozen_book`. Ante un P&L sorprendentemente bueno, sospecha primero del libro.
- Operaciones anuladas por fallos del bot: `status='void'` con la causa en `miss_reason`; se conservan, pero KPIs, calculadora y dashboard las excluyen.
- `kalshi.ws_update_interval_ms: 100` hace que el servidor agrupe los deltas (5 veces menos tráfico, medido). Sin eso la conexión se satura.
- `data/history.db` es una base aparte de la del bot: no escribas histórico en `kalshi_lastminute.db` mientras el bot corre.
- Resultado de referencia (19 689 mercados, sep–nov 2026): el precio está bien calibrado de 1 a 10 min del cierre y comprar al ask pierde en todos los rangos de precio. Una variante que solo use el precio de Kalshi no tiene edge; solo vale la pena probar información que el precio aún no refleje.
- Segundo resultado de referencia (mismos mercados, prueba fuera de muestra): el modelo de volatilidad no añade información al precio de Kalshi de 1 a 10 min del cierre (coeficiente 0.05 ± 0.05). Cuando ve 12¢ de edge, el P&L real es −0.6¢. No vale la pena afinar sus parámetros.

## Convenciones

- Python 3.11+, asyncio, `sqlite3` estándar. Dependencias: aiohttp, pyyaml, cryptography, fastapi, uvicorn, pytest. Antes de añadir otra, comprueba que no se resuelve con lo que hay.
- Tiempos en milisegundos Unix (`*_ms`), UTC.
- Reinicios seguros: toda escritura es idempotente (claves únicas + `INSERT OR IGNORE` o upsert). `paper_trades` tiene `UNIQUE (ticker, strategy)`: un intento por mercado y variante. El estado se reconstruye desde SQLite al arrancar.
- Columnas nuevas en tablas existentes van en `MIGRATIONS` de `db.py`, no editando el `CREATE TABLE`: hay bases ya creadas.
- SQLite pertenece al hilo del bucle de eventos: los endpoints de FastAPI son `async def`.
- Lo que decide o calcula va en funciones puras (sin reloj, red ni base) para que el replay ejecute el mismo código que el bot en vivo.
- `decisions.reason` y `paper_trades.miss_reason` son etiquetas cortas y estables (`edge_bajo`, no `edge 0.012 < 0.03`): se agregan por texto.
- Documentación, mensajes de consola y comentarios en español.
- Todo cálculo nuevo lleva test.
