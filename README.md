# ExplodeX Backend

Backend inicial para el scanner de oportunidades LONG/SHORT en Binance USDT-M Futures.

## Objetivo de esta versión

- Consumir únicamente datos públicos de Binance Futures.
- Escanear contratos USDT-M líquidos.
- Detectar candidatos tempranos antes de una expansión de volatilidad.
- Calcular `LONG score`, `SHORT score`, `setup_score` y `risk_score`.
- Clasificar la señal como `NO_TRADE`, `WATCH`, `PREPARING` o `READY`.
- Estimar entrada, invalidación, TP1/TP2/TP3 y horizonte aproximado.
- Guardar scanner runs, snapshots y señales en PostgreSQL.
- Empezar en modo análisis/paper. Esta versión NO abre operaciones reales.

## Stack

- Python 3.12
- FastAPI
- SQLAlchemy async
- PostgreSQL
- Binance Futures public REST API
- Railway

## Variables de entorno

Copia `.env.example` y configura `DATABASE_URL` con la URL interna de PostgreSQL de Railway.

```bash
cp .env.example .env
```

## Desarrollo local

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate  # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger: `http://localhost:8000/docs`

## Endpoints principales

- `GET /health`
- `GET /api/v1/market/price/{symbol}`
- `POST /api/v1/scanner/run`
- `GET /api/v1/scanner/latest`
- `GET /api/v1/signals/active`

## Seguridad

No agregues API keys privadas al repositorio. Esta etapa usa datos públicos y no necesita permisos de trading ni retiros.
