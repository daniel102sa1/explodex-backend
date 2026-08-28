from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from app.services.binance import binance_client
from app.services.coinglass import coinglass_client
from app.services.coinglass_confirmation import apply_coinglass_confirmation
from app.services.market_context import market_context
from app.services.market_impact_engine import apply_market_impact_gate, build_market_impact
from app.services.news_context import news_context_for_symbol
from app.services.prediction_guarded import build_pre_move_prediction
from app.services.scoring import build_btc_context, score_snapshot


router = APIRouter(prefix="/api/v1/market-impact", tags=["market-impact"])


def _safe_symbol(symbol: str) -> str:
    value = symbol.upper().strip()
    if not value.endswith("USDT"):
        value = f"{value}USDT"
    if not value.replace("USDT", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid symbol")
    return value


@router.get("/{symbol}")
async def market_impact_for_symbol(symbol: str):
    safe_symbol = _safe_symbol(symbol)
    try:
        snapshot, btc_klines, symbol_news, global_news, broad = await asyncio.gather(
            binance_client.deep_snapshot(safe_symbol),
            binance_client.klines("BTCUSDT", interval="5m", limit=120),
            news_context_for_symbol(safe_symbol),
            news_context_for_symbol("BTCUSDT"),
            market_context(),
        )
        btc_context = build_btc_context(btc_klines)
        local_scored = score_snapshot(snapshot, btc_context=btc_context)
        cg = await coinglass_client.enrich_symbol(safe_symbol)
        scored = apply_coinglass_confirmation(local_scored, cg)
        prediction = build_pre_move_prediction(scored, snapshot, cg)
        impact = build_market_impact(scored, prediction, cg, symbol_news, global_news, broad)
        gated_prediction = apply_market_impact_gate(prediction, impact)
        return {
            "symbol": safe_symbol,
            "impact": impact,
            "armed_trigger": gated_prediction.get("armed_trigger"),
            "premove_fingerprint": gated_prediction.get("premove_fingerprint"),
            "path_forecast": gated_prediction.get("path_forecast"),
            "note": "Market Impact puede advertir o degradar una entrada técnica; noticias por sí solas no crean TRADE NOW.",
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Market impact unavailable: {exc}") from exc
