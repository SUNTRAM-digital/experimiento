# Weatherbot Polymarket

Bot de trading automatizado para mercados de temperatura en Polymarket. Compara el forecast oficial de weather.gov contra el precio del mercado y ejecuta trades cuando encuentra edge estadístico.

---

## Requisitos

- Python 3.10+
- Cuenta en Polymarket con USDC
- Conexión a internet

---

## Instalación

```bash
cd "C:\Users\carlo\OneDrive\Escritorio\proyectos claude\weatherbot"
pip install -r requirements.txt
```

---

## Configuración

El archivo `.env` ya está configurado con las credenciales. Si necesitas reconfigurarlo desde cero, copia `.env.example` a `.env` y completa los valores:

```bash
copy .env.example .env
```

Variables críticas en `.env`:

```
POLY_PRIVATE_KEY=0x...        # Private key exportada de Polymarket (Settings → Export Private Key)
POLY_SIGNATURE_TYPE=1         # 1 = cuenta creada con email/Magic; 0 = MetaMask EOA
POLY_WALLET_ADDRESS=0x...     # Dirección de tu wallet en Polymarket
```

---

## Correr el bot

```bash
cd "C:\Users\carlo\OneDrive\Escritorio\proyectos claude\weatherbot"
python main.py
```

Luego abrir en el navegador: **http://localhost:8000**

Desde la interfaz:
1. Ajustar parámetros en el panel izquierdo si se desea
2. Click **"Guardar Parametros"**
3. Click **"Iniciar Bot"**

Para detenerlo: click **"Detener Bot"** en la interfaz, o `Ctrl+C` en la terminal.

---

## Estructura de archivos

```
weatherbot/
├── main.py              ← Punto de entrada
├── bot.py               ← Loop principal, ejecuta trades
├── markets.py           ← Busca mercados de temperatura en Polymarket
├── weather.py           ← Obtiene forecasts de weather.gov (gratis)
├── strategy.py          ← Calcula probabilidad, EV y Kelly Criterion
├── api.py               ← Servidor FastAPI + WebSocket para la UI
├── config.py            ← Configuración, parámetros, ciudades/estaciones ICAO
├── .env                 ← Credenciales (no compartir)
├── .env.example         ← Plantilla de configuración
├── requirements.txt     ← Dependencias Python
├── AGENTE_MEMORIA.md    ← Memoria técnica detallada para agentes/devs
└── static/
    └── index.html       ← Dashboard web
```

---

## Parámetros configurables (desde la UI o el .env)

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| Max por trade | $5 USDC | Máximo a invertir en una sola posición |
| Min por trade | $1 USDC | Mínimo por posición |
| Fracción Kelly | 0.25 | Qué fracción del Kelly completo usar (0.25 = conservador) |
| EV mínimo | 10% | No entra si el edge esperado es menor a esto |
| Pérdida máxima diaria | 20% | El bot se detiene si pierde este % del balance inicial del día |
| Horas máx. a resolución | 48h | Solo opera en mercados que cierran dentro de este tiempo |
| Liquidez mínima | $50 | Ignora mercados con poca liquidez |
| Intervalo de scan | 30 min | Cada cuánto busca nuevas oportunidades |

---

## Notas técnicas importantes

**Tipo de wallet:** La cuenta de Polymarket fue creada con email (Magic wallet). Esto requiere:
- `signature_type=1` en el cliente CLOB
- `funder=wallet_address` al inicializar el cliente
- `fee_rate_bps=1000` en cada orden (fee del 10% para mercados de clima)
- Tamaño mínimo de orden: **5 shares**

**Datos meteorológicos:** Se usa weather.gov (NOAA) — completamente gratis, sin API key. Cada ciudad resuelve en una estación ICAO específica (no la app del clima del celular):

| Ciudad | Estación ICAO |
|--------|--------------|
| New York | KLGA (LaGuardia) |
| Chicago | KORD (O'Hare) |
| Dallas | KDAL (Love Field, NO DFW) |
| Austin | KAUS |
| Atlanta | KATL |
| Miami | KMIA |
| Seattle | KSEA |
| Los Angeles | KLAX |
| Houston | KHOU |
| San Francisco | KSFO |
| Denver | KDEN |
| Boston | KBOS |

---

## Cómo funciona el análisis

1. **Forecast** → weather.gov devuelve temperatura máxima esperada para la estación ICAO
2. **Probabilidad** → distribución normal (`scipy.stats.norm`) centrada en el forecast
3. **EV** → compara nuestra probabilidad vs. precio del mercado
4. **Sizing** → Kelly Criterion fraccionado sobre el balance disponible
5. **Trade** → solo si EV supera el umbral mínimo configurado

---

## Solución de problemas

**El bot se detiene inmediatamente**
→ Revisar logs en la pestaña "Logs" de la interfaz. Probablemente error de credenciales en `.env`.

**"invalid signature"**
→ Verificar que `POLY_SIGNATURE_TYPE=1` y que `POLY_WALLET_ADDRESS` está correcto en `.env`.

**"invalid fee rate"**
→ Error interno del código, reportar al desarrollador.

**No encuentra mercados**
→ Puede que no haya mercados de temperatura US activos en ese momento. Volver a intentar más tarde o reducir `MAX_HOURS_TO_RESOLUTION` a 72h desde la UI.

**Puerto 8000 ocupado**
→ Ejecutar `netstat -ano | findstr :8000` para ver qué proceso lo usa, o cambiar el puerto en `main.py`.
