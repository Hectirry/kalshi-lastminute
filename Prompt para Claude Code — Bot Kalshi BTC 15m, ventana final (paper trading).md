# Prompt para Claude Code — Bot Kalshi BTC 15m, ventana final (paper trading)

## Contexto

Voy a construir un repositorio NUEVO llamado `kalshi-lastminute`. Antes de escribir código, revisa estos repos míos en GitHub (cuenta Hectirry) y reutiliza lo que sirva, sin copiar el desorden:

- `Kalshi-crypto-agents`: feeds de Binance, Hyperliquid y Kalshi, autenticación RSA-PSS con Kalshi, modelo de fees, sizing Kelly y dashboard FastAPI.
- `btc5m-lab`: estructura de backtesting (dataset, estrategia como plugin de unas 30 líneas, reportes de KPIs y calibración).
- `polybot-btc5m`: lecciones aprendidas, motor de riesgo y log de decisiones.

Lección clave del intento anterior en Polymarket 5m: perdí contra bots que veían el precio final antes que yo en los últimos 10 a 20 segundos. Este bot intenta precisamente eso: estimar, en el último tramo de cada mercado de 15 minutos, el valor con el que se va a liquidar y comprar solo cuando el precio del contrato esté claramente mal valorado.

## Restricciones no negociables

1. **Solo paper trading.** Nada de órdenes reales. Los datos de mercado sí son reales (producción de Kalshi, solo lectura). La ejecución es simulada.
2. **Nada de LLM en el camino de decisión.** Las decisiones son puro código, en milisegundos. Cero costo de API de modelos.
3. **Corre en mi máquina local** mientras tenga la sesión abierta. Debe tolerar que lo apague y lo prenda: al reiniciar, continúa sin duplicar operaciones ni corromper la base de datos.
4. **Python 3.11+, asyncio, SQLite.** Mínimas dependencias. Configuración en YAML y secretos en `.env`, que nunca se suben a Git.

## Paso 0 — Investigación obligatoria antes de codear

Crea `docs/RESEARCH.md` con fuentes oficiales para:

- Reglas exactas de liquidación de los mercados BTC de 15 minutos en Kalshi: qué índice usan (por ejemplo, si es CF Benchmarks y si promedian una ventana final de segundos), a qué hora exacta cierran y cómo se redondea el strike.
- La fórmula vigente de fees de Kalshi para estos mercados, tanto taker como maker.
- Límites de la API: rate limits, canales de WebSocket del libro de órdenes y latencia típica.
- Si una persona residente en Colombia puede abrir cuenta y operar en Kalshi. Si no puede, dilo claramente en el documento y en la consola al arrancar, pero sigue con el paper trading.

No asumas nada de esto de memoria. Si algo no se puede confirmar, márcalo como supuesto en `config.yaml`.

## Estrategia base: `settlement_nowcast_v1`

1. Reconstruye en tiempo real una aproximación del índice de liquidación usando los exchanges que lo componen (o los más líquidos disponibles gratis: Coinbase, Kraken, Bitstamp, Binance), vía WebSocket.
2. Si la liquidación es un promedio de los últimos N segundos, a medida que avanza esa ventana una parte del resultado ya está "fijada". Calcula la probabilidad de cerrar por encima del strike combinando lo ya fijado con la volatilidad realizada de los segundos que faltan.
3. Compara esa probabilidad con el mejor ask de YES y de NO en el libro de Kalshi.
4. Entra solo si: edge neto de fees > umbral configurable, liquidez en el nivel suficiente para el tamaño, y quedan entre X e Y segundos (configurable, por defecto entre 90 y 5 segundos antes del cierre).
5. Una sola posición por mercado. Se mantiene hasta la liquidación.

La estrategia debe ser un plugin (`strategies/<nombre>.py` + `.yaml`) para poder crear variantes sin tocar el motor.

## Simulación de ejecución realista

Esto es lo que más importa; un paper trading optimista no sirve de nada.

- Siempre compras como taker al ask visible, nunca al mid.
- Aplica una latencia simulada configurable (por defecto 300 ms): el fill se hace contra el libro que había esa cantidad de tiempo DESPUÉS de la decisión. Si el nivel ya no existe, la orden se pierde o se llena a peor precio.
- El tamaño máximo por operación está limitado por la profundidad real del nivel.
- Fees reales según `docs/RESEARCH.md`.
- Registra cada orden que no se habría llenado: ese número indica cuánto me están ganando otros bots.

## Persistencia y observabilidad

- SQLite con tablas `ticks_index`, `orderbook_snapshots`, `decisions` (incluye las decisiones de NO entrar y su razón), `paper_trades` y `settlements`.
- Guarda suficiente data cruda para hacer replay y backtest después con otros parámetros.
- Dashboard FastAPI sencillo en localhost: mercado activo, cuenta regresiva, probabilidad del modelo frente al precio de Kalshi, posiciones, P&L acumulado y tasa de fills perdidos.

## KPIs (en `reports/`, se generan con un comando)

Win rate, P&L neto de fees, expectancy por operación, profit factor, Sharpe por operación, max drawdown, calibración (probabilidad del modelo frente a frecuencia real por buckets), tasa de fills perdidos por latencia, operaciones por día y edge promedio frente a edge realizado.

## Calculadora de rentabilidad (`python -m tools.breakeven`)

Con los resultados reales del paper trading debe responder:

1. Expectancy neta por dólar arriesgado y operaciones elegibles por día.
2. Tamaño máximo realista por operación según la profundidad observada del libro (la capacidad de la estrategia).
3. Costos fijos mensuales configurables: VPS (por defecto 10 USD/mes), datos y otros.
4. **Capital mínimo** para que la ganancia mensual esperada cubra los costos fijos, y capital para metas configurables (por ejemplo, 100 y 500 USD/mes).
5. Intervalos de confianza por bootstrap sobre las operaciones, no solo el promedio. Si el intervalo incluye pérdida, la conclusión debe decir "no hay evidencia de edge todavía".
6. Número mínimo de operaciones necesario para que el resultado sea estadísticamente confiable, y cuántas faltan.

## Fases y entregables

1. **Fase 1:** investigación, esqueleto del repo, feeds, grabación de datos y tests básicos. Corre una hora grabando sin operar y muestra un resumen.
2. **Fase 2:** estrategia, simulador de ejecución, dashboard y KPIs.
3. **Fase 3:** backtest por replay sobre la data grabada y calculadora de rentabilidad.
4. README en español con: cómo arrancar, cómo crear variantes de la estrategia y cómo leer los reportes.

Al terminar cada fase, detente, muéstrame qué hiciste y qué supuestos tomaste, y espera mi aprobación antes de seguir. Sé honesto con los resultados: si los números dicen que no hay edge, dilo sin suavizarlo.
