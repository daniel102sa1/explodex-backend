from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _exchange_name(row: dict[str, Any]) -> str:
    return str(row.get("exchange") or row.get("exchange_name") or row.get("name") or "UNKNOWN").upper()


def _oi_exchange_rows(coinglass: dict[str, Any]) -> list[dict[str, Any]]:
    oi = coinglass.get("open_interest") if isinstance(coinglass, dict) else None
    rows = oi.get("exchanges") if isinstance(oi, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _liq_exchange_rows(coinglass: dict[str, Any]) -> list[dict[str, Any]]:
    liq = coinglass.get("liquidations") if isinstance(coinglass, dict) else None
    rows = liq.get("exchanges") if isinstance(liq, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _taker_exchange_rows(coinglass: dict[str, Any]) -> list[dict[str, Any]]:
    taker = coinglass.get("taker") if isinstance(coinglass, dict) else None
    rows = taker.get("exchanges") if isinstance(taker, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def _taker_bias(row: dict[str, Any]) -> float | None:
    buy = _f(row.get("buy_volume_usd") or row.get("buy_vol_usd") or row.get("buy_volume"))
    sell = _f(row.get("sell_volume_usd") or row.get("sell_vol_usd") or row.get("sell_volume"))
    if buy + sell > 0:
        return (buy - sell) / (buy + sell)
    buy_ratio = row.get("buy_ratio") or row.get("buy_ratio_pct")
    sell_ratio = row.get("sell_ratio") or row.get("sell_ratio_pct")
    if buy_ratio is not None or sell_ratio is not None:
        b = _f(buy_ratio, 50.0)
        s = _f(sell_ratio, 50.0)
        total = b + s
        return (b - s) / total if total > 0 else None
    return None


def build_exchange_lead_lag(coinglass: dict[str, Any] | None, direction: str) -> dict[str, Any]:
    """Best-effort cross-exchange pressure comparison.

    This is a SHADOW contextual engine. It does not claim causal lead/lag unless
    exchange-level inputs exist. Missing detail stays N/D rather than being inferred.
    """
    cg = coinglass or {}
    side = 1.0 if str(direction).upper() == "LONG" else -1.0
    by_exchange: dict[str, dict[str, Any]] = {}

    for row in _oi_exchange_rows(cg):
        name = _exchange_name(row)
        item = by_exchange.setdefault(name, {"exchange": name})
        item["oi_usd"] = _f(row.get("open_interest_usd"))
        item["oi_change_5m_pct"] = _f(row.get("change_5m_pct"))
        item["oi_change_15m_pct"] = _f(row.get("change_15m_pct"))

    for row in _liq_exchange_rows(cg):
        name = _exchange_name(row)
        item = by_exchange.setdefault(name, {"exchange": name})
        item["long_liq_1h"] = _f(row.get("long_1h"))
        item["short_liq_1h"] = _f(row.get("short_1h"))
        item["total_liq_1h"] = _f(row.get("total_1h"))

    for row in _taker_exchange_rows(cg):
        name = _exchange_name(row)
        item = by_exchange.setdefault(name, {"exchange": name})
        item["taker_bias"] = _taker_bias(row)

    rows = list(by_exchange.values())
    if len(rows) < 2:
        return {
            "mode": "SHADOW_ONLY",
            "available": False,
            "status": "N/D",
            "leader": None,
            "leader_bias": "NEUTRAL",
            "agreement": None,
            "support_direction": False,
            "conflict_direction": False,
            "exchanges": rows,
            "data_note": "Se requieren al menos dos exchanges con detalle real comparable; no se infiere lead/lag faltante.",
        }

    scored: list[dict[str, Any]] = []
    for row in rows:
        evidence = 0.0
        inputs = 0
        oi5 = row.get("oi_change_5m_pct")
        oi15 = row.get("oi_change_15m_pct")
        taker = row.get("taker_bias")
        short_liq = _f(row.get("short_liq_1h"))
        long_liq = _f(row.get("long_liq_1h"))
        liq_total = short_liq + long_liq

        if oi5 is not None:
            evidence += _clamp(_f(oi5) * 8.0, -25.0, 25.0)
            inputs += 1
        if oi15 is not None:
            evidence += _clamp(_f(oi15) * 4.0, -20.0, 20.0)
            inputs += 1
        if taker is not None:
            evidence += _clamp(_f(taker) * 35.0, -30.0, 30.0)
            inputs += 1
        if liq_total > 0:
            liq_bias = (short_liq - long_liq) / liq_total
            evidence += liq_bias * 20.0
            row["liquidation_bias"] = liq_bias
            inputs += 1

        score = _clamp(50.0 + evidence)
        bias = "LONG" if score >= 58 else "SHORT" if score <= 42 else "NEUTRAL"
        scored.append({**row, "pressure_score": round(score, 1), "bias": bias, "inputs": inputs})

    scored.sort(key=lambda item: abs(_f(item.get("pressure_score"), 50.0) - 50.0), reverse=True)
    leader = scored[0]
    usable = [row for row in scored if int(row.get("inputs") or 0) >= 2]
    if len(usable) < 2:
        return {
            "mode": "SHADOW_ONLY",
            "available": False,
            "status": "PARTIAL",
            "leader": leader.get("exchange"),
            "leader_bias": leader.get("bias"),
            "agreement": None,
            "support_direction": False,
            "conflict_direction": False,
            "exchanges": scored,
            "data_note": "Hay múltiples exchanges, pero menos de dos tienen suficientes inputs comparables.",
        }

    directional = [row for row in usable if row.get("bias") in {"LONG", "SHORT"}]
    long_count = sum(1 for row in directional if row.get("bias") == "LONG")
    short_count = sum(1 for row in directional if row.get("bias") == "SHORT")
    agreement = max(long_count, short_count) / len(directional) if directional else 0.0
    aggregate_bias = "LONG" if long_count > short_count else "SHORT" if short_count > long_count else "NEUTRAL"
    support = aggregate_bias != "NEUTRAL" and (1.0 if aggregate_bias == "LONG" else -1.0) * side > 0
    conflict = aggregate_bias != "NEUTRAL" and not support and agreement >= 0.66

    dispersion = max(_f(row.get("pressure_score"), 50.0) for row in usable) - min(_f(row.get("pressure_score"), 50.0) for row in usable)
    status = "ALIGNED" if agreement >= 0.75 else "DIVERGENT" if dispersion >= 28 else "MIXED"

    return {
        "mode": "SHADOW_ONLY",
        "available": True,
        "status": status,
        "leader": leader.get("exchange"),
        "leader_bias": leader.get("bias"),
        "aggregate_bias": aggregate_bias,
        "agreement": round(agreement, 3),
        "dispersion": round(dispersion, 1),
        "support_direction": support,
        "conflict_direction": conflict,
        "exchanges": scored[:10],
        "data_note": "Pressure comparison is empirical cross-exchange context, not proof that one venue causally leads the next move.",
    }


def apply_exchange_lead_lag(coinglass: dict[str, Any] | None, prediction: dict[str, Any]) -> dict[str, Any]:
    if not prediction:
        return prediction
    result = dict(prediction)
    direction = str(result.get("direction") or "LONG")
    model = build_exchange_lead_lag(coinglass, direction)
    sequence = dict(result.get("sequence") or {})
    sequence["exchange_lead_lag_status"] = model.get("status")
    sequence["exchange_leader"] = model.get("leader")
    sequence["exchange_aggregate_bias"] = model.get("aggregate_bias")
    sequence["exchange_agreement"] = model.get("agreement")
    result["sequence"] = sequence
    result["exchange_lead_lag"] = model
    return result
