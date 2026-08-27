from app.main import app
from app.binance_user_routes import router as binance_user_router


app.include_router(binance_user_router)
