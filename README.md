# kalshi-lastminute

Paper trading de la ventana final de los mercados de 15 minutos de Kalshi sobre BTC, ETH y SOL.

**No envía órdenes reales.** Lee datos reales de producción de Kalshi (solo lectura) y simula la ejecución. No usa ningún modelo de lenguaje para decidir.

La idea: cada mercado se liquida con el promedio de los últimos 60 segundos de un índice de CF Benchmarks. A medida que avanza ese minuto, parte del resultado ya está fijada. El bot calcula la probabilidad de cerrar sobre el strike y compra el lado que el libro esté vendiendo demasiado barato.

## Cómo arrancar

Requiere Python 3.11 o superior.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m kalshi_lastminute.run
```

Con eso el bot graba datos, evalúa las estrategias, simula las operaciones y sirve el dashboard en <http://127.0.0.1:8000>. Se detiene con `Ctrl+C`.

Se puede apagar y volver a prender cuando quieras. Al reiniciar continúa sobre la misma base de datos, no duplica operaciones (una por mercado y variante), liquida las que quedaron abiertas y marca como perdidas las que estaban a medio llenar.

Opciones de `run`: `--minutes N` (duración), `--no-dashboard`, `--env demo` (entorno de pruebas de Kalshi), `--db ruta`.

Para grabar datos sin operar: `python -m kalshi_lastminute.recorder`.

### Llave de API de Kalshi (recomendada)

Sin llave, el libro de órdenes se lee por polling REST cada 0.5 s y no hay índice oficial: sirve para probar, pero los resultados salen optimistas. Con llave, llegan por WebSocket el libro en tiempo real y el índice oficial de CF Benchmarks.

1. Crea una llave de API en la web de Kalshi (mejor de solo lectura) y guarda el archivo de llave privada en esta carpeta.
2. Crea `config.local.yaml`:
   ```yaml
   kalshi:
     api_key_id: el-identificador-de-tu-llave
     private_key_path: nombre-del-archivo-de-llave
   ```

Los dos archivos quedan fuera de Git; añade el nombre del archivo de llave a `.gitignore`. También valen las variables `KALSHI_API_KEY_ID` y `KALSHI_PRIVATE_KEY_PATH` en un `.env`.

### Si tu red bloquea kalshi.com

`config.yaml` trae activado `network.dns_override`: el bot resuelve `kalshi.com`, y solo ese dominio, por DNS-over-HTTPS contra 1.1.1.1. No modifica el DNS de Windows. Se desactiva con `enabled: false`.

## Configuración

| Archivo | Para qué |
|---|---|
| `config.yaml` | Activos, latencia simulada, fees, costos fijos, metas. Las claves marcadas `SUPUESTO` no están confirmadas con fuente oficial. |
| `config.local.yaml` | Ajustes de esta máquina (llave de API). Pisa a `config.yaml`. |
| `strategies/*.yaml` | Parámetros de cada variante de estrategia. |

Lo más importante de `config.yaml`:

- `engine.latency_ms` (300): tiempo simulado entre decidir y llegar al libro.
- `engine.max_slippage` (0): cuánto peor que el precio visto se acepta llenar. Con 0, si el nivel ya no está, la orden se pierde.
- `fees`: fórmula de Kalshi, con el redondeo más caro mientras no se confirme el tarifario.
- `assets`: para añadir o quitar activos.

## Cómo crear variantes de la estrategia

Una variante es un archivo `.yaml` en `strategies/`. No hay que tocar el motor.

**Cambiar parámetros** (lo habitual): copia una variante existente y edítala.

```powershell
Copy-Item strategies\settlement_nowcast_v1.yaml strategies\mi_variante.yaml
```

Deja `module: settlement_nowcast_v1` y cambia lo que quieras en `params`. Al reiniciar el bot, la variante corre en paralelo con las demás y lleva su propio registro de decisiones, operaciones y KPIs bajo el nombre del archivo. Con `enabled: false` se apaga.

Vienen dos variantes:

| Variante | Ventana de entrada | Qué prueba |
|---|---|---|
| `settlement_nowcast_v1` | 90 a 5 s antes del cierre | La estrategia base de la especificación |
| `settlement_nowcast_late` | 30 a 3 s antes del cierre | Solo entra cuando al menos medio promedio ya está fijado |

**Cambiar la lógica**: crea `strategies/mi_logica.py` con una función pura

```python
def evaluate(ctx, params) -> Decision: ...
```

y un `.yaml` con `module: mi_logica`. `ctx` trae el índice por segundo, el libro, el strike, los segundos que faltan y los fees (`kalshi_lastminute/strategy.py`). Devuelve `Decision("enter", ...)` o `Decision("skip", "razon_corta")`. No debe leer reloj, red ni base de datos: así el replay puede ejecutarla igual que el bot en vivo.

**Probar antes de dejarla corriendo**: el replay la ejecuta sobre toda la data ya grabada, en segundos.

```powershell
.venv\Scripts\python -m kalshi_lastminute.replay --strategy mi_variante
.venv\Scripts\python -m kalshi_lastminute.replay --strategy settlement_nowcast_v1 --param min_edge=0.05 --latency-ms 500
```

`--param` cambia un parámetro solo para esa corrida, sin editar el archivo.

## Reportes

Todos se escriben en `reports/` y se imprimen en consola.

| Comando | Archivo | Qué responde |
|---|---|---|
| `python -m kalshi_lastminute.report` | `kpis.md` | Cómo le va al paper trading en vivo |
| `python -m kalshi_lastminute.replay --strategy X` | `replay_X.md` | Cómo le habría ido a X sobre la data grabada |
| `python -m tools.breakeven` | `breakeven.md` | Cuánto capital hace falta y si hay evidencia de edge |
| `python -m tools.breakeven --replay` | `breakeven_replay.md` | Lo mismo, con las operaciones del replay |
| `python -m kalshi_lastminute.summary` | (consola) | Calidad de los datos grabados y error del índice |

### Cómo leer `kpis.md` y `replay_*.md`

Lo primero es la línea en negrita. Con menos de 100 operaciones cerradas dice "Sin veredicto" y todo lo demás es provisional.

- **Tasa de fills perdidos**: órdenes que no se habrían llenado porque el precio se movió durante la latencia. Mide cuánto te ganan otros bots. Si es alta y las que sí se llenan pierden, el bot solo consigue llenar cuando está equivocado.
- **Expectancy por dólar arriesgado**: ganancia neta media por cada dólar puesto. Es el número que decide todo.
- **Edge esperado vs realizado**: lo que el modelo creía ganar por contrato frente a lo que ganó. Si el esperado es 0.05 y el realizado ronda 0, el modelo se engaña.
- **Calibración**: para las operaciones donde el modelo daba, por ejemplo, 80–90 % al lado comprado, qué fracción ganó de verdad. Si gana bastante menos de lo que dice el modelo, el modelo está sobreconfiado.
- **Razones para no entrar**: `precio_fuera_de_banda` (el lado ya vale más de 0.95 o menos de 0.05), `edge_bajo`, `liquidez_baja`, `indice_viejo`, `sin_volatilidad` (menos de 2 minutos de historia), `sin_libro`, `sin_strike`.
- **Por qué se perdieron órdenes**: `precio_se_fue` (el nivel desapareció), `sin_oferta`, `mercado_cerrado`, `sin_libro`, `reinicio`.

### Cómo leer `breakeven.md`

La conclusión en negrita sale del intervalo de confianza por bootstrap. Si el intervalo incluye pérdida, dice "No hay evidencia de edge todavía", aunque el promedio sea positivo.

- **Capacidad por operación**: dólares disponibles en el mejor precio al decidir (percentil 25). Operar más grande mueve el precio.
- **Capital necesario**: para cubrir costos fijos y para cada meta mensual, con la estimación central y con el límite inferior del intervalo. La columna "¿Cabe en el libro?" avisa si el tamaño requerido supera la capacidad.
- **Cuántas operaciones hacen falta**: y cuántos días más de bot encendido representan.

Costos y metas se cambian en `config.yaml` (`breakeven`) o por línea de comandos: `--vps 10 --datos 0 --otros 5 --meta 100 --meta 500`.

## Qué se graba (`data/kalshi_lastminute.db`)

| Tabla | Contenido |
|---|---|
| `ticks_index` | Mejor bid/ask de cada exchange, nuestro índice (`composite`) y el índice oficial (`oficial`), por activo |
| `orderbook_snapshots` | Libro de cada mercado: completo cada 5 s y, en los últimos 2 minutos, los 10 mejores niveles cada 100 ms cuando cambian |
| `markets` | Mercados vistos, con strike y reglas |
| `settlements` | Resultado oficial de cada mercado |
| `decisions` | Cada entrada y cada decisión de no entrar, con su razón |
| `paper_trades` | Operaciones simuladas: llenas, parciales y perdidas |
| `brti_raw` | Mensajes crudos del índice oficial |

Crece del orden de cientos de MB por día.

## Límites conocidos

- La simulación no modela el impacto propio: asume que tomar liquidez no cambia el libro para los demás.
- En la ventana final el libro se muestrea cada 100 ms, no en cada cambio.
- El modelo de probabilidad es un paseo aleatorio gaussiano con la volatilidad de los últimos 5 minutos. No ajusta por colas gordas.
- Un solo intento de entrada por mercado y variante: si la orden se pierde, no reintenta.
- No está confirmado que los términos de Kalshi permitan operar a un residente en Colombia (`docs/RESEARCH.md` §4).

## Más

- `docs/RESEARCH.md`: reglas de liquidación, fees, API y fuentes.
- `CLAUDE.md`: reglas del proyecto para trabajar con Claude Code.
