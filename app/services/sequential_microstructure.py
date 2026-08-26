from __future__ import annotations

from collections import defaultdict, deque
from time import time
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _book_state(
    book: dict[str, Any],
    current_price: float,
    futures_delta: float | None,
) -> dict[str, float | None] | None:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None

    try:
        bid_price = _f(bids[0][0])
        ask_price = _f(asks[0][0])
        bid_size = _f(bids[0][1])
        ask_size = _f(asks[0][1])
    except (IndexError, TypeError):
        return None

    if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
        return None

    bid_depth = sum(_f(row[0]) * _f(row[1]) for row in bids[:10] if len(row) >= 2)
    ask_depth = sum(_f(row[0]) * _f(row[1]) for row in asks[:10] if len(row) >= 2)
    depth = bid_depth + ask_depth
    mid = (bid_price + ask_price) / 2.0
    imbalance = (bid_depth - ask_depth) / depth if depth > 0 else 0.0

    return {
        "ts": time(),
        "bid_price": bid_price,
        "ask_price": ask_price,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "mid": mid,
        "imbalance": imbalance,
        "price": current_price if current_price > 0 else mid,
        "futures_delta": futures_delta,
    }


def _normalized_ofi(previous: dict[str, Any], current: dict[str, Any]) -> float | None:
    """Top-of-book order-flow imbalance normalized to roughly [-1, 1]."""
    pb0, pb1 = _f(previous.get("bid_price")), _f(current.get("bid_price"))
    pa0, pa1 = _f(previous.get("ask_price")), _f(current.get("ask_price"))
    qb0, qb1 = _f(previous.get("bid_size")), _f(current.get("bid_size"))
    qa0, qa1 = _f(previous.get("ask_size")), _f(current.get("ask_size"))
    if min(pb0, pb1, pa0, pa1) <= 0:
        return None

    event = 0.0
    if pb1 >= pb0:
        event += qb1
    if pb1 <= pb0:
        event -= qb0
    if pa1 <= pa0:
        event -= qa1
    if pa1 >= pa0:
        event += qa0

    scale = max(1e-12, (qb0 + qb1 + qa0 + qa1) / 4.0)
    return _clamp(event / (2.0 * scale), -1.0, 1.0)


def _replenishment(rows: list[dict[str, Any]]) -> tuple[float | None, str]:
    if len(rows) < 3:
        return None, "WARMING_UP"
    first, depleted, restored = rows[-3], rows[-2], rows[-1]

    f_bid, d_bid, r_bid = _f(first.get("bid_size")), _f(depleted.get("bid_size")), _f(restored.get("bid_size"))
    f_ask, d_ask, r_ask = _f(first.get("ask_size")), _f(depleted.get("ask_size")), _f(restored.get("ask_size"))

    bid_depleted = f_bid > 0 and d_bid <= f_bid * 0.68
    ask_depleted = f_ask > 0 and d_ask <= f_ask * 0.68
    bid_restored = bid_depleted and r_bid >= max(d_bid * 1.35, f_bid * 0.72) and _f(restored.get("bid_price")) >= _f(depleted.get("bid_price")) * 0.9995
    ask_restored = ask_depleted and r_ask >= max(d_ask * 1.35, f_ask * 0.72) and _f(restored.get("ask_price")) <= _f(depleted.get("ask_price")) * 1.0005

    if bid_restored and ask_restored:
        return 0.0, "BOTH"
    if bid_restored:
        return 1.0, "BIDS"
    if ask_restored:
        return -1.0, "ASKS"
    return 0.0, "NONE"


def _sequential_absorption(rows: list[dict[str, Any]]) -> tuple[float | None, str]:
    if len(rows) < 3:
        return None, "WARMING_UP"
    recent = rows[-5:]
    deltas = [_f(row.get("futures_delta")) for row in recent if row.get("futures_delta") is not None]
    if len(deltas) < 2:
        return None, "NO_FLOW_DATA"

    start_price = _f(recent[0].get("price"))
    end_price = _f(recent[-1].get("price"))
    if start_price <= 0 or end_price <= 0:
        return None, "NO_PRICE_DATA"

    avg_delta = sum(deltas) / len(deltas)
    move_pct = (end_price - start_price) / start_price * 100.0

    # Persistent aggressive buying without upward progress is bearish absorption.
    if avg_delta >= 0.08 and move_pct <= 0.05:
        return -1.0, "BUYS_ABSORBED"
    # Persistent aggressive selling without downward progress is bullish absorption.
    if avg_delta <= -0.08 and move_pct >= -0.05:
        return 1.0, "SELLS_ABSORBED"
    return 0.0, "NONE"


def _metrics(history: deque[dict[str, Any]]) -> dict[str, Any]:
    rows = list(history)
    if not rows:
        return {
            "ready": False,
            "snapshot_count": 0,
            "window_seconds": 0.0,
            "ofi": None,
            "replenishment": None,
            "replenishment_side": "WARMING_UP",
            "liquidity_speed": None,
            "imbalance_speed_per_sec": None,
            "sequential_absorption": None,
            "sequential_absorption_label": "WARMING_UP",
        }

    window = max(0.0, _f(rows[-1].get("ts")) - _f(rows[0].get("ts")))
    ofi = _normalized_ofi(rows[-2], rows[-1]) if len(rows) >= 2 else None
    replenishment, replenishment_side = _replenishment(rows)
    absorption, absorption_label = _sequential_absorption(rows)

    liquidity_speed = None
    imbalance_speed = None
    if len(rows) >= 2:
        previous, current = rows[-2], rows[-1]
        dt = max(0.001, _f(current.get("ts")) - _f(previous.get("ts")))
        previous_mid = _f(previous.get("mid"))
        current_mid = _f(current.get("mid"))
        if previous_mid > 0 and current_mid > 0:
            liquidity_speed = abs((current_mid - previous_mid) / previous_mid * 10000.0) / dt
        imbalance_speed = (_f(current.get("imbalance")) - _f(previous.get("imbalance"))) / dt

    return {
        "ready": len(rows) >= 3,
        "snapshot_count": len(rows),
        "window_seconds": round(window, 1),
        "ofi": round(ofi, 4) if ofi is not None else None,
        "replenishment": replenishment,
        "replenishment_side": replenishment_side,
        "liquidity_speed": round(liquidity_speed, 5) if liquidity_speed is not None else None,
        "imbalance_speed_per_sec": round(imbalance_speed, 6) if imbalance_speed is not None else None,
        "sequential_absorption": absorption,
        "sequential_absorption_label": absorption_label,
    }


_HISTORY: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=18))


def observe_sequential_microstructure(
    symbol: str,
    book: dict[str, Any],
    current_price: float,
    futures_delta: float | None,
) -> dict[str, Any]:
    """Build sequential metrics only from real order-book snapshots observed by this process.

    The history is intentionally process-local in v1: no values are fabricated after a
    deploy/restart, and metrics remain in warm-up until enough fresh snapshots arrive.
    """
    key = (symbol or "UNKNOWN").upper()
    state = _book_state(book, current_price, futures_delta)
    history = _HISTORY[key]
    if state is None:
        result = _metrics(history)
        result["data_note"] = "Order-book snapshot unavailable; sequential metrics keep the last real in-process window and do not fabricate data."
        return result

    if history and _f(state.get("ts")) - _f(history[-1].get("ts")) < 2.0:
        history[-1] = state
    else:
        history.append(state)

    result = _metrics(history)
    result["data_note"] = (
        "Sequential L2 metrics use real snapshots observed by the running backend. "
        "They need at least 3 snapshots and reset to warm-up after a deploy/restart."
    )
    return result
