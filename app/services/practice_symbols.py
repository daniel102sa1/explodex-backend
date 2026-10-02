from __future__ import annotations

from typing import Any

from app.services.binance import binance_client


def normalize_perpetual_symbols(info: dict[str, Any]) -> list[str]:
    """Extract active USDT perpetuals from Binance or OKX public instruments."""
    symbols: set[str] = set()
    okx = info.get("source") == "OKX_FALLBACK"
    for item in info.get("symbols") or []:
        if not isinstance(item, dict):
            continue
        if okx:
            inst_id = str(item.get("instId") or "").upper()
            if (inst_id.endswith("-USDT-SWAP") and
                    str(item.get("state") or "live").lower() == "live" and
                    str(item.get("settleCcy") or "USDT").upper() == "USDT"):
                base = inst_id.removesuffix("-USDT-SWAP").replace("-", "")
                if base.isalnum():
                    symbols.add(base + "USDT")
        else:
            symbol = str(item.get("symbol") or "").upper()
            if (symbol.endswith("USDT") and symbol[:-4].isalnum() and
                    str(item.get("quoteAsset") or "USDT").upper() == "USDT" and
                    str(item.get("status") or "").upper() == "TRADING" and
                    str(item.get("contractType") or "").upper() == "PERPETUAL"):
                symbols.add(symbol)
    return sorted(symbols)


async def practice_symbol_catalog() -> dict[str, Any]:
    """Cached public instruments; never reads/writes PostgreSQL."""
    async def load() -> dict[str, Any]:
        info = await binance_client.exchange_info()
        items = normalize_perpetual_symbols(info)
        return {
            "source": "OKX_FALLBACK" if info.get("source") == "OKX_FALLBACK" else "BINANCE_FUTURES",
            "symbols": items,
            "count": len(items),
            "paper_only": True,
        }

    return await binance_client._cached_call(
        key="practice:active_perpetual_symbols:v1", ttl_seconds=900.0,
        stale_seconds=3_600.0, loader=load,
    )
