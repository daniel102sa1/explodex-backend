from __future__ import annotations

from typing import Any, Awaitable, Callable

_INSTALLED = False


def install_server_snapshot_extensions() -> None:
    """Add real 1m candles to deep snapshots without changing provider semantics."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.binance import binance_client

    original: Callable[[str], Awaitable[dict[str, Any]]] = binance_client.deep_snapshot

    async def deep_snapshot_with_1m(symbol: str) -> dict[str, Any]:
        snapshot = await original(symbol)
        try:
            rows = await binance_client.klines(symbol, "1m", 120)
            snapshot["klines_1m"] = rows
            snapshot["klines_1m_available"] = bool(rows)
        except Exception as exc:
            snapshot["klines_1m"] = []
            snapshot["klines_1m_available"] = False
            snapshot["klines_1m_error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        return snapshot

    binance_client.deep_snapshot = deep_snapshot_with_1m  # type: ignore[method-assign]
    _INSTALLED = True
