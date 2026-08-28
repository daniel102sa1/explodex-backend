from fastapi.middleware.cors import CORSMiddleware

from app.main import app as inner_app
from app.binance_user_routes import router as binance_user_router
from app.market_impact_routes import router as market_impact_router
from app.validation_routes import router as validation_router


inner_app.include_router(binance_user_router)
inner_app.include_router(market_impact_router)
inner_app.include_router(validation_router)

# Keep CORS as the outermost ASGI layer too. This guarantees that browser
# clients still receive Access-Control-Allow-Origin even when a private Binance
# endpoint raises before FastAPI builds a normal JSON response.
app = CORSMiddleware(
    app=inner_app,
    allow_origins=[
        "https://explodex-web.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
