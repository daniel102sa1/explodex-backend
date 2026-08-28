from __future__ import annotations

import asyncio
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.binance import binance_client

ADVANCED_ENTRY_VERSION = "advanced_entry_lab_v1"


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _return_pct(rows: list[list[Any]], bars: int) -> float:
    usable = [row for row in rows if len(row) >= 5]
    if len(usable) <= bars:
        return 0.0
    start = _f(usable[-(bars + 1)][4])
    end = _f(usable[-1][4])
    if start <= 0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _trade_imbalance(trades: list[dict[str, Any]]) -> tuple[float, float, float]:
    buy = 0.0
    sell = 0.0
    for trade in trades:
        notional = _f(trade.get("p")) * _f(trade.get("q"))
        if notional <= 0:
            continue
        if bool(trade.get("m", False)):
            sell += notional
        else:
            buy += notional
    total = buy + sell
    imbalance = (buy - sell) / total if total > 0 else 0.0
    return imbalance, buy, sell


def _toxicity_proxy(trades: list[dict[str, Any]], buckets: int = 8) -> float:
    """VPIN-like research proxy using equal-count recent trade buckets.

    This is intentionally not called true VPIN because we do not reconstruct
    historical volume buckets here.
    """
    usable = [trade for trade in trades if _f(trade.get("p")) > 0 and _f(trade.get("q")) > 0]
    if len(usable) < 16:
        return 0.0
    bucket_count = max(2, min(buckets, len(usable) // 4))
    chunk = max(1, math.ceil(len(usable) / bucket_count))
    values: list[float] = []
    for index in range(0, len(usable), chunk):
        imbalance, _, _ = _trade_imbalance(usable[index:index + chunk])
        values.append(abs(imbalance))
    return sum(values) / len(values) if values else 0.0


def build_toxic_flow(
    *,
    order_book: dict[str, Any] | None,
    futures_trades: list[dict[str, Any]] | None,
    spot_trades: list[dict[str, Any]] | None,
    klines: list[list[Any]] | None,
) -> dict[str, Any]:
    order_book = order_book or {}
    futures_trades = futures_trades or []
    spot_trades = spot_trades or []
    klines = klines or []

    bids = []
    asks = []
    for row in list(order_book.get("bids") or [])[:12]:
        if len(row) >= 2:
            price, qty = _f(row[0]), _f(row[1])
            if price > 0 and qty > 0:
                bids.append((price, qty))
    for row in list(order_book.get("asks") or [])[:12]:
        if len(row) >= 2:
            price, qty = _f(row[0]), _f(row[1])
            if price > 0 and qty > 0:
                asks.append((price, qty))

    bid_depth = sum(price * qty / (1.0 + index * 0.20) for index, (price, qty) in enumerate(bids))
    ask_depth = sum(price * qty / (1.0 + index * 0.20) for index, (price, qty) in enumerate(asks))
    depth_total = bid_depth + ask_depth
    book_imbalance = (bid_depth - ask_depth) / depth_total if depth_total > 0 else 0.0

    micro_edge_bps = 0.0
    if bids and asks:
        bid_price, bid_qty = bids[0]
        ask_price, ask_qty = asks[0]
        denom = bid_qty + ask_qty
        midpoint = (bid_price + ask_price) / 2.0
        if denom > 0 and midpoint > 0:
            microprice = (ask_price * bid_qty + bid_price * ask_qty) / denom
            micro_edge_bps = (microprice / midpoint - 1.0) * 10_000.0

    fut_imbalance, fut_buy, fut_sell = _trade_imbalance(futures_trades)
    spot_imbalance, spot_buy, spot_sell = _trade_imbalance(spot_trades)
    toxicity = _toxicity_proxy(futures_trades)

    price_ret_3 = _return_pct(klines, 3)
    absorption = 0.0
    # Strong aggressive selling without equivalent downside can imply bid absorption.
    if fut_imbalance <= -0.25 and price_ret_3 >= -0.10:
        absorption = min(1.0, abs(fut_imbalance) + max(0.0, price_ret_3) / 0.50)
    # Strong aggressive buying without upside can imply offer absorption.
    elif fut_imbalance >= 0.25 and price_ret_3 <= 0.10:
        absorption = -min(1.0, abs(fut_imbalance) + max(0.0, -price_ret_3) / 0.50)

    micro_component = _clamp(micro_edge_bps / 6.0, -1.0, 1.0)
    directional = 100.0 * (
        0.30 * book_imbalance
        + 0.28 * fut_imbalance
        + 0.14 * spot_imbalance
        + 0.10 * micro_component
        + 0.18 * absorption
    )
    directional = _clamp(directional, -100.0, 100.0)

    available_components = sum([
        bool(bids and asks),
        bool(futures_trades),
        bool(spot_trades),
        len(klines) >= 4,
    ])
    data_quality = available_components / 4.0

    if directional >= 35:
        state = "BUY_PRESSURE"
    elif directional <= -35:
        state = "SELL_PRESSURE"
    else:
        state = "MIXED"

    return {
        "version": "toxic_flow_proxy_v1",
        "available": data_quality >= 0.25,
        "state": state,
        "directional_score": round(directional, 2),
        "score_is_probability": False,
        "book_imbalance": round(book_imbalance, 4),
        "futures_trade_imbalance": round(fut_imbalance, 4),
        "spot_trade_imbalance": round(spot_imbalance, 4),
        "microprice_edge_bps": round(micro_edge_bps, 3),
        "absorption_score": round(absorption, 4),
        "toxicity_proxy": round(toxicity, 4),
        "futures_buy_notional": round(fut_buy, 2),
        "futures_sell_notional": round(fut_sell, 2),
        "spot_buy_notional": round(spot_buy, 2),
        "spot_sell_notional": round(spot_sell, 2),
        "price_return_3bar_pct": round(price_ret_3, 4),
        "data_quality": round(data_quality, 2),
        "note": "Proxy de microestructura; no es VPIN calibrado ni probabilidad futura.",
    }


def build_lead_lag(
    *,
    symbol: str,
    symbol_klines: list[list[Any]] | None,
    btc_klines: list[list[Any]] | None,
    eth_klines: list[list[Any]] | None,
) -> dict[str, Any]:
    symbol = symbol.upper()
    symbol_klines = symbol_klines or []
    btc_klines = btc_klines or []
    eth_klines = eth_klines or []

    alt_1 = _return_pct(symbol_klines, 1)
    alt_3 = _return_pct(symbol_klines, 3)
    alt_6 = _return_pct(symbol_klines, 6)
    btc_1, btc_3, btc_6 = (_return_pct(btc_klines, n) for n in (1, 3, 6))
    eth_1, eth_3, eth_6 = (_return_pct(eth_klines, n) for n in (1, 3, 6))

    majors_1 = 0.58 * btc_1 + 0.42 * eth_1
    majors_3 = 0.58 * btc_3 + 0.42 * eth_3
    majors_6 = 0.58 * btc_6 + 0.42 * eth_6

    # How much the majors have moved without the alt fully catching up yet.
    lag_gap_3 = majors_3 - alt_3
    momentum = 0.50 * majors_1 + 0.35 * majors_3 + 0.15 * majors_6
    consensus = 1.0 if btc_3 * eth_3 > 0 else 0.45
    lag_bonus = _clamp(abs(lag_gap_3) / 0.60, 0.0, 1.0)
    raw = math.copysign(1.0, momentum) * min(1.0, abs(momentum) / 0.55) if momentum != 0 else 0.0
    directional = 100.0 * raw * (0.65 + 0.20 * consensus + 0.15 * lag_bonus)

    # If the alt already moved substantially more than the leaders, reduce lead-lag value.
    if abs(alt_3) > abs(majors_3) * 1.8 + 0.20:
        directional *= 0.45
    directional = _clamp(directional, -100.0, 100.0)

    data_quality = sum([len(symbol_klines) >= 7, len(btc_klines) >= 7, len(eth_klines) >= 7]) / 3.0
    if symbol in {"BTCUSDT", "ETHUSDT"}:
        directional *= 0.35

    if directional >= 30:
        state = "LEAD_LONG"
    elif directional <= -30:
        state = "LEAD_SHORT"
    else:
        state = "NEUTRAL"

    return {
        "version": "lead_lag_v1",
        "available": data_quality >= 0.67,
        "state": state,
        "directional_score": round(directional, 2),
        "score_is_probability": False,
        "alt_return_1bar_pct": round(alt_1, 4),
        "alt_return_3bar_pct": round(alt_3, 4),
        "alt_return_6bar_pct": round(alt_6, 4),
        "btc_return_3bar_pct": round(btc_3, 4),
        "eth_return_3bar_pct": round(eth_3, 4),
        "majors_return_3bar_pct": round(majors_3, 4),
        "lag_gap_3bar_pct": round(lag_gap_3, 4),
        "data_quality": round(data_quality, 2),
    }


async def failure_fingerprint(
    db: AsyncSession,
    *,
    side: str,
    setup_type: str,
    score: float,
    strategy_mode: str = "MICRO_SCALP",
) -> dict[str, Any]:
    side = side.upper()
    score_floor = int(max(0.0, score) // 10 * 10)
    score_ceiling = score_floor + 10
    row = dict((await db.execute(text("""
        SELECT
            COUNT(*) AS trades,
            COUNT(*) FILTER (WHERE exit_reason IN ('STOP','AMBIGUOUS_STOP')) AS stops,
            COUNT(*) FILTER (WHERE exit_reason='TP1') AS tp1,
            COUNT(*) FILTER (WHERE net_pnl > 0) AS winners,
            COALESCE(SUM(net_pnl),0) AS net_pnl,
            COALESCE(SUM(net_pnl) FILTER (WHERE net_pnl > 0),0) AS positive_net,
            COALESCE(SUM(net_pnl) FILTER (WHERE net_pnl < 0),0) AS negative_net,
            COALESCE(SUM(COALESCE(fees,0)+COALESCE(slippage,0)+COALESCE(funding_estimate,0)),0) AS costs
        FROM paper_positions
        WHERE status='CLOSED'
          AND side=:side
          AND COALESCE(metadata->>'strategy_mode','TREND_PREMOVE')=:strategy_mode
          AND COALESCE(metadata->>'micro_setup_type', metadata->>'setup_type', grade, 'GENERAL')=:setup_type
          AND COALESCE(fingerprint_score,0) >= :score_floor
          AND COALESCE(fingerprint_score,0) < :score_ceiling
          AND closed_at >= NOW() - INTERVAL '90 days'
    """), {
        "side": side,
        "strategy_mode": strategy_mode,
        "setup_type": setup_type,
        "score_floor": score_floor,
        "score_ceiling": score_ceiling,
    })).mappings().one())

    trades = int(row.get("trades") or 0)
    stops = int(row.get("stops") or 0)
    tp1 = int(row.get("tp1") or 0)
    winners = int(row.get("winners") or 0)
    net = _f(row.get("net_pnl"))
    positive = _f(row.get("positive_net"))
    negative = abs(_f(row.get("negative_net")))
    costs = _f(row.get("costs"))
    expectancy = net / trades if trades else 0.0
    stop_rate = stops / trades if trades else 0.0
    win_rate = winners / trades if trades else 0.0
    profit_factor = positive / negative if negative > 0 else (999.0 if positive > 0 else 0.0)

    available = trades >= 8
    risk = 50.0
    if available:
        risk += (stop_rate - 0.50) * 80.0
        if expectancy < 0:
            risk += min(25.0, abs(expectancy) * 20.0)
        elif expectancy > 0:
            risk -= min(18.0, expectancy * 14.0)
        if profit_factor < 0.80:
            risk += 12.0
        elif profit_factor >= 1.25:
            risk -= 10.0
    risk = _clamp(risk, 0.0, 100.0)

    veto = bool(
        trades >= 15
        and (
            (stop_rate >= 0.65 and expectancy < 0)
            or (trades >= 20 and profit_factor < 0.75 and net < 0)
        )
    )

    return {
        "version": "failure_fingerprint_v1",
        "available": available,
        "strategy_mode": strategy_mode,
        "setup_type": setup_type,
        "side": side,
        "score_bucket": f"{score_floor}-{score_ceiling}",
        "trades": trades,
        "stops": stops,
        "tp1": tp1,
        "win_rate_pct": round(win_rate * 100.0, 2) if trades else None,
        "stop_rate_pct": round(stop_rate * 100.0, 2) if trades else None,
        "expectancy_net_usdt": round(expectancy, 6),
        "profit_factor": round(profit_factor, 4),
        "costs_usdt": round(costs, 6),
        "failure_risk": round(risk, 2),
        "veto": veto,
        "note": "Memoria PAPER por setup/dirección/score; no es probabilidad futura.",
    }


def combine_advanced_signals(
    *,
    side: str,
    toxic_flow: dict[str, Any],
    lead_lag: dict[str, Any],
    failure: dict[str, Any],
) -> dict[str, Any]:
    side = side.upper()
    sign = 1.0 if side == "LONG" else -1.0
    toxic_support = _f(toxic_flow.get("directional_score")) * sign
    lead_support = _f(lead_lag.get("directional_score")) * sign
    failure_risk = _f(failure.get("failure_risk"), 50.0)

    veto_reasons: list[str] = []
    if bool(failure.get("veto")):
        veto_reasons.append("failure_fingerprint")
    if toxic_flow.get("available") and _f(toxic_flow.get("data_quality")) >= 0.50 and toxic_support <= -48:
        veto_reasons.append("toxic_flow_conflict")
    if lead_lag.get("available") and _f(lead_lag.get("data_quality")) >= 0.67 and lead_support <= -55:
        veto_reasons.append("lead_lag_conflict")

    support = 0.55 * toxic_support + 0.30 * lead_support + 0.15 * (50.0 - failure_risk)
    support = _clamp(support, -100.0, 100.0)

    if veto_reasons:
        state = "VETO"
        risk_multiplier = 0.0
    elif support >= 35:
        state = "STRONG_SUPPORT"
        risk_multiplier = 1.0
    elif support >= 10:
        state = "SUPPORT"
        risk_multiplier = 0.80
    elif support <= -25:
        state = "CONFLICT"
        risk_multiplier = 0.35
    else:
        state = "MIXED"
        risk_multiplier = 0.55

    if failure.get("available") and failure_risk >= 65 and risk_multiplier > 0:
        risk_multiplier = min(risk_multiplier, 0.45)

    return {
        "version": ADVANCED_ENTRY_VERSION,
        "paper_only": True,
        "state": state,
        "support_score": round(support, 2),
        "score_is_probability": False,
        "risk_multiplier": round(risk_multiplier, 3),
        "veto": bool(veto_reasons),
        "veto_reasons": veto_reasons,
        "toxic_support_for_side": round(toxic_support, 2),
        "lead_lag_support_for_side": round(lead_support, 2),
        "failure_risk": round(failure_risk, 2),
        "creates_entry": False,
        "changes_real_money_rules": False,
    }


async def evaluate_advanced_entry(
    db: AsyncSession,
    *,
    symbol: str,
    side: str,
    setup_type: str,
    score: float,
) -> dict[str, Any]:
    symbol = symbol.upper()
    results = await asyncio.gather(
        binance_client.order_book(symbol, 20),
        binance_client.agg_trades(symbol, 250),
        binance_client.spot_agg_trades(symbol, 250),
        binance_client.klines(symbol, "5m", 36),
        binance_client.klines("BTCUSDT", "5m", 36),
        binance_client.klines("ETHUSDT", "5m", 36),
        return_exceptions=True,
    )

    def ok(index: int, fallback: Any) -> Any:
        value = results[index]
        return fallback if isinstance(value, Exception) else value

    symbol_klines = ok(3, [])
    toxic = build_toxic_flow(
        order_book=ok(0, {}),
        futures_trades=ok(1, []),
        spot_trades=ok(2, []),
        klines=symbol_klines,
    )
    lead = build_lead_lag(
        symbol=symbol,
        symbol_klines=symbol_klines,
        btc_klines=ok(4, []),
        eth_klines=ok(5, []),
    )
    failure = await failure_fingerprint(
        db,
        side=side,
        setup_type=setup_type,
        score=score,
        strategy_mode="MICRO_SCALP",
    )
    combined = combine_advanced_signals(side=side, toxic_flow=toxic, lead_lag=lead, failure=failure)
    return {
        **combined,
        "symbol": symbol,
        "side": side.upper(),
        "setup_type": setup_type,
        "toxic_flow": toxic,
        "lead_lag": lead,
        "failure_fingerprint": failure,
        "data_source": binance_client.active_source,
    }
