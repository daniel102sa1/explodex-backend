from __future__ import annotations

from typing import Any

VERSION = "execution_math_v1"

TAKER_FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
FUNDING_ESTIMATE_8H = 0.0001


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _favorable_distance(side: str, entry: float, target: float) -> float:
    return (target - entry) if str(side).upper() == "LONG" else (entry - target)


def estimate_round_trip_cost_distance(entry: float, expected_hold_hours: float) -> float:
    """Approximate all-in round-trip trading cost as price distance per unit.

    Includes taker fee + slippage on both sides and expected funding. This is
    deliberately conservative for PAPER gating; actual funding can differ.
    """
    if entry <= 0:
        return 0.0
    hold = max(0.0, float(expected_hold_hours))
    trading = 2.0 * (TAKER_FEE_RATE + SLIPPAGE_RATE)
    funding = FUNDING_ESTIMATE_8H * (hold / 8.0)
    return entry * (trading + funding)


def evaluate_trade_math(
    *,
    side: str,
    entry: float,
    stop: float,
    target: float,
    expected_hold_hours: float,
) -> dict[str, Any]:
    entry = _f(entry)
    stop = _f(stop)
    target = _f(target)
    side = str(side or "").upper()
    risk_distance = abs(entry - stop)
    reward_distance = _favorable_distance(side, entry, target)
    cost_distance = estimate_round_trip_cost_distance(entry, expected_hold_hours)

    valid_geometry = (
        entry > 0
        and stop > 0
        and target > 0
        and risk_distance > 0
        and reward_distance > 0
        and ((side == "LONG" and stop < entry < target) or (side == "SHORT" and target < entry < stop))
    )
    if not valid_geometry:
        return {
            "version": VERSION,
            "valid": False,
            "gross_rr": 0.0,
            "net_rr": 0.0,
            "breakeven_win_rate_pct": 100.0,
            "cost_distance_pct": 0.0,
        }

    gross_rr = reward_distance / risk_distance
    net_reward = reward_distance - cost_distance
    net_loss = risk_distance + cost_distance
    net_rr = net_reward / net_loss if net_reward > 0 and net_loss > 0 else 0.0
    breakeven = 1.0 / (1.0 + net_rr) if net_rr > 0 else 1.0
    return {
        "version": VERSION,
        "valid": True,
        "gross_rr": round(gross_rr, 4),
        "net_rr": round(net_rr, 4),
        "breakeven_win_rate_pct": round(breakeven * 100.0, 2),
        "risk_distance_pct": round(risk_distance / entry * 100.0, 4),
        "reward_distance_pct": round(reward_distance / entry * 100.0, 4),
        "cost_distance_pct": round(cost_distance / entry * 100.0, 4),
        "net_reward_distance_pct": round(max(0.0, net_reward) / entry * 100.0, 4),
        "expected_hold_hours": round(max(0.0, float(expected_hold_hours)), 2),
    }


def choose_target_for_min_net_rr(
    *,
    side: str,
    entry: float,
    stop: float,
    targets: list[tuple[str, float]],
    expected_hold_hours: float,
    min_net_rr: float,
) -> dict[str, Any]:
    """Choose the nearest valid target that satisfies minimum *net* R/R.

    A plan with a nearby TP1 can still be traded if TP2/TP3 makes the economics
    viable. If no target clears the threshold, PAPER must reject the trade.
    """
    evaluated: list[dict[str, Any]] = []
    for name, price in targets:
        price = _f(price)
        if price <= 0:
            continue
        math = evaluate_trade_math(
            side=side,
            entry=entry,
            stop=stop,
            target=price,
            expected_hold_hours=expected_hold_hours,
        )
        item = {"name": name, "price": price, **math}
        evaluated.append(item)
        if math.get("valid") and _f(math.get("net_rr")) >= float(min_net_rr):
            return {
                "version": VERSION,
                "accepted": True,
                "chosen_target": item,
                "min_net_rr": float(min_net_rr),
                "candidates": evaluated,
            }
    return {
        "version": VERSION,
        "accepted": False,
        "chosen_target": None,
        "min_net_rr": float(min_net_rr),
        "candidates": evaluated,
        "reason": "no_target_meets_min_net_rr",
    }
