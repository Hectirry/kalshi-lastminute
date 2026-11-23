# RESEARCH — Kalshi cripto 15 min (KXBTC15M, KXETH15M, KXSOL15M)

Investigado el 2026-11-22. Cada dato lleva su nivel de evidencia:

- **[API]** leído por mí en la respuesta de la API de Kalshi (entorno demo, que es el único accesible desde esta red; ver §0).
- **[DOC]** documento oficial descargado y leído (S3 de Kalshi, cfbenchmarks.com, cftc.gov, docs.kalshi.com).
- **[FRAGMENTO]** texto de una página oficial visto solo como resultado de búsqueda; la redacción puede no ser literal.
- **[SUPUESTO]** no confirmado; está marcado igual en `config.yaml`.

## ETH y SOL (añadidos el 2026-11-22)

Las series `KXETH15M` y `KXSOL15M` tienen las mismas reglas que BTC. [API, producción]

- `rules_primary`: "simple average of the sixty seconds of CF Benchmarks' ETHUSDRTI" (y `SOLUSDRTI`), "is at least", `strike_type: greater_or_equal`.
- Redondeo del valor final: 2 decimales en ETH, **4 decimales en SOL** (`custom_strike.round_digits`).
- Fees: `fee_type: quadratic`, `fee_multiplier: 1`, igual que BTC.
- Mismo tick (`tapered_deci_cent`) y mismos horarios.
- Liquidez mucho menor: en el mismo instante el mercado abierto llevaba ~2.4 M contratos en BTC, ~129 mil en ETH y ~63 mil en SOL.
- Los constituyentes de los índices de ETH y SOL no se verificaron; se asumen los mismos exchanges que BRTI. [SUPUESTO]

Todo lo que sigue se investigó para BTC.

## 0. Bloqueo de red (leer primero)

Desde esta máquina, el DNS del proveedor de internet responde `166.210.35.97` para `kalshi.com`, `api.elections.kalshi.com`, `external-api.kalshi.com`, `docs.kalshi.com` y también para `polymarket.com`. Es un sumidero: la conexión no llega a Kalshi. Con un DNS público (1.1.1.1) los mismos nombres resuelven a IPs reales de AWS.

- El bloqueo es de DNS local, no de Kalshi. No pude establecer quién lo ordena; que afecte por igual a Kalshi y a Polymarket sugiere una lista de bloqueo de apuestas del proveedor o del regulador colombiano. **[SUPUESTO]**
- El entorno demo (`*.demo.kalshi.co`) sí resuelve y responde.
- Decisión del dueño del proyecto (2026-11-22): el bot resuelve `kalshi.com`, y solo ese dominio, por DNS-over-HTTPS contra 1.1.1.1 (`kalshi_lastminute/net.py`, `network.dns_override` en `config.yaml`). El DNS de Windows no se modifica.

## 1. Reglas de liquidación

| Dato | Valor | Evidencia |
|---|---|---|
| Índice | CF Benchmarks Bitcoin Real-Time Index (BRTI) | [API] `rules_primary`; [DOC] [términos CRYPTO15M](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/CRYPTO15M.pdf): "The Source Agency is CF Benchmarks." |
| Valor final | Promedio simple de los 60 s de BRTI previos al cierre, redondeado a 2 decimales | [API] `rules_secondary`: "60 RTI prices are collected… rounded to the nearest 2 decimal places" |
| Strike | Promedio simple de los 60 s de BRTI previos a la **apertura** | [API] `rules_primary` |
| Strike = valor final del mercado anterior | Observado: `expiration_value` 60744.91 del mercado 17:45 = `floor_strike` del de 18:00 | [API] |
| Regla de empate | YES si final **≥** strike (`strike_type: greater_or_equal`, "is at least") | [API] |
| Horario | Abre y cierra cada 15 min exactos (:00, :15, :30, :45 UTC); `close_time` es el fin de la ventana de 60 s | [API] `open_time`/`close_time` |
| Resultado disponible | Segundos después del cierre (`settlement_timer_seconds: 1`; visto a los ~13 s por REST) | [API] |
| Si faltan datos del índice | "affected strikes resolve to No" | [DOC] términos |
| Revisiones posteriores | No se tienen en cuenta | [DOC] términos |
| Tick de precio | `tapered_deci_cent`: $0.001 por debajo de $0.10 y por encima de $0.90; $0.01 en el medio | [API] `price_ranges` |
| Contratos fraccionarios | Sí (cantidades con 2 decimales, mínimo 0.01) | [API]; [DOC] [fixed-point](https://docs.kalshi.com/getting_started/fixed_point_migration.md) |

Ejemplo literal de `rules_primary` [API]: "If the simple average of the sixty seconds of CF Benchmarks' BRTI before 6:00 PM EST on Nov 22, 2026 is at least the simple average of the sixty seconds of CF Benchmarks' BRTI before 5:45 PM EST…, then the market resolves to Yes."

**No confirmado:**
- Cuáles son exactamente los 60 valores (si incluye el del segundo del cierre). Usamos los valores vigentes al final de cada segundo de [cierre−60 s, cierre). [SUPUESTO]
- El strike aparece como `None` por REST durante los primeros segundos del mercado nuevo [API, observado]; el grabador lo actualiza cuando llega.

### BRTI: cómo se calcula

- Constituyentes vigentes: **Bitstamp, Coinbase, Kraken, Gemini, LMAX Digital, Bullish, Crypto.com**. itBit/Paxos suspendido desde 2026-07-27. [DOC] [Constituent Exchanges v13.7](https://docs.cfbenchmarks.com/CME%20CF%20Constituent%20Exchanges.pdf)
- Se calcula sobre los **libros de órdenes** de todos los constituyentes, no sobre últimas transacciones: se consolidan los libros, se limita el tamaño por nivel, se construye una curva de precio medio por volumen y se pondera con una distribución exponencial hasta una profundidad útil. [DOC] [metodología v17.0 §4.1.1](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)
- Se calcula cada 200 ms; Kalshi toma 60 valores (uno por segundo). [DOC] + [API]
- Nuestra aproximación (promedio simple de los mids de Coinbase, Kraken, Bitstamp y Gemini) **no es la fórmula real** y le faltan tres constituyentes sin feed gratuito integrado. El error se mide contra `expiration_value` en cada liquidación. [SUPUESTO]

### Kalshi publica el índice real

Kalshi tiene un canal WebSocket `cfbenchmarks_value` (índice `BRTI`, ~1 tick/s, y una variante de 5 Hz) que incluye `last_60s_windowed_average_15min`: el promedio que liquida este mercado, en vivo. Requiere autenticación y posiblemente un permiso aparte. [DOC] [cfbenchmarks-value](https://docs.kalshi.com/websockets/cfbenchmarks-value.md)

Esto importa para la tesis: cualquier participante con llave de API puede ver el valor exacto que se está fijando. Reconstruir el índice desde exchanges es, en el mejor caso, una copia peor de lo que los demás ya ven.

## 2. Fees

| Dato | Valor | Evidencia |
|---|---|---|
| Tipo de fee de la serie | `fee_type: quadratic`, `fee_multiplier: 1` (sin fee de maker) | [API] `/series/KXBTC15M` |
| Taker | `redondeo_arriba(M × 0.07 × C × P × (1−P))` | [FRAGMENTO] tarifario jul-2026; coeficiente corroborado en [DOC] [CFTC 2026-10-14](https://www.cftc.gov/filings/orgrules/rules10142633445.pdf): "Base Taker Fee per contract is $0.0175 (0.07 × 0.50 × 0.50)" |
| Maker | Mismo cálculo con 0.0175, pero multiplicador 0 por defecto; `quadratic` (no `quadratic_with_maker_fees`) implica 0 | [FRAGMENTO] + [API] |
| Redondeo | Al centavo hacia arriba (regla antigua) o a $0.0001 con acumulador por orden (regla nueva) | [FRAGMENTO] [fee_rounding](https://docs.kalshi.com/getting_started/fee_rounding) |
| Fee de liquidación | No se cobra | [FRAGMENTO] |

No pude abrir el tarifario oficial (`kalshi.com/docs/kalshi-fee-schedule.pdf`) por el bloqueo de §0. Mientras tanto el código usa la opción más cara: redondeo al centavo hacia arriba por orden. [SUPUESTO]

## 3. API

| Dato | Valor | Evidencia |
|---|---|---|
| REST producción | `https://external-api.kalshi.com/trade-api/v2` (recomendado); `api.elections.kalshi.com` sigue soportado | [DOC] [api_environments](https://docs.kalshi.com/getting_started/api_environments.md) |
| WS producción | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | [DOC] |
| Demo | `https://external-api.demo.kalshi.co/trade-api/v2` | [DOC] + [API] |
| REST de mercado sin autenticación | `/markets`, `/markets/{t}`, `/markets/{t}/orderbook`, `/series/{s}` | [DOC] + [API] |
| REST va con caché | `/markets`: `Cache-Control: public, max-age=5` en demo; la doc avisa que puede ir "several seconds" atrasado. `/orderbook` no mostró caché en demo. | [API] + [DOC] |
| WebSocket | **Siempre** exige handshake firmado (llave de API), aun para canales públicos | [DOC] [websocket-connection](https://docs.kalshi.com/websockets/websocket-connection.md) |
| Libro en tiempo real | Canal `orderbook_delta`: snapshot + deltas con `seq`; campos `yes_dollars_fp`, `no_dollars_fp`, `price_dollars`, `delta_fp` | [DOC] |
| Libro por REST | `orderbook_fp.yes_dollars` / `no_dollars`, strings, ascendente (mejor bid al final). Solo bids: ask YES = 1 − mejor bid NO | [DOC] + [API] |
| Firma | RSA-PSS SHA-256 sobre `timestamp_ms + METHOD + path` (sin query); para WS el path es `/trade-api/ws/v2`. También Ed25519. | [DOC] [api_keys](https://docs.kalshi.com/getting_started/api_keys.md) |
| Rate limit | Cubetas de tokens; Basic: 200 tokens/s de lectura, 10 por request ≈ 20 lecturas/s. 429 sin `Retry-After`. | [DOC] [rate_limits](https://docs.kalshi.com/getting_started/rate_limits.md) |
| Límite sin autenticar | "separate limits", sin cifra | [DOC] |
| Latencia / región | Sin cifra oficial. Solo se menciona AWS PrivateLink. | [DOC] |

Consecuencias para el diseño:

- Sin llave de API solo hay polling REST del libro. Sirve para grabar y para validar el sistema, pero no es lo que ven los bots con WebSocket, y la simulación de latencia de 300 ms queda limitada por el intervalo de polling.
- Para grabar el libro como lo ve un competidor hace falta una llave de API, es decir, una cuenta de Kalshi (ver §4). El cliente WebSocket (`run_kalshi_ws`) se probó contra producción el 2026-11-22 con una llave Ed25519. Observado [API]:
  - `cfbenchmarks_value` está en el socket estándar y no pidió permiso aparte. Los índices son `BRTI`, `ETHUSD_RTI` y `SOLUSD_RTI`, un valor por segundo.
  - Cada mensaje trae `last_60s_windowed_average_15min` con `window_start_ts_ms` = cierre − 60 s y `window_end_ts_exclusive`: la ventana de liquidación es [cierre − 60 s, cierre), fin exclusivo.
  - `seq` cuenta todos los mensajes de una suscripción, incluidos los `ok` al añadir mercados.
  - `orderbook_delta` de los tres mercados suma cientos de mensajes por segundo; más de mil por segundo solo en BTC. Por eso no se guarda cada cambio: se guarda el libro completo cada 5 s y, en los últimos 2 minutos, los 10 mejores niveles cada 100 ms.

## 4. ¿Puede operar un residente en Colombia?

**No confirmado en ningún sentido.**

- Colombia no figura entre las jurisdicciones restringidas del Member Agreement leído (copia de S3 de oct-2025; en Latinoamérica nombra Bolivia, Cuba, Haití, Nicaragua y Venezuela). [DOC] [member agreement](https://kalshi-public-docs.s3.amazonaws.com/kalshi-member-agreement.pdf)
- El mismo acuerdo prohíbe cualquier lugar donde operar esté prohibido por la ley local. [DOC]
- La versión vigente (10.1.26) y `where.kalshi.com` no se pudieron abrir; por fragmentos de búsqueda, Colombia no aparece como restringida. [FRAGMENTO]
- No se verificó si la verificación de identidad acepta documentos colombianos.
- El bloqueo de DNS de §0 es una señal en contra por el lado local, de origen no establecido.

Mientras esto no se aclare, este proyecto es solo paper trading y el grabador lo avisa al arrancar.
