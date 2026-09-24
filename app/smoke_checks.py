from __future__ import annotations

"""Zero-network production smoke checks for ExplodeX.

This module is intentionally lightweight and deterministic. Railway runs it
before a deployment is promoted so pure-Python regressions in the trading
analysis stack fail the deployment instead of reaching users.
"""

from app.services import paper_portfolio
from app.services.impulse_pullback_confirmation import build_impulse_pullback_confirmation
from app.services.historical_market_brain import POLICY as HISTORICAL_POLICY, _distance as historical_distance, _feature_vector as historical_feature_vector, _similarity as historical_similarity
from app.services.paper_unified_heart_executor import _sarpon_leverage_policy
from app.services.prediction_engine import build_pre_move_prediction as build_raw_pre_move_prediction
from app.services.prediction_guarded import build_pre_move_prediction as build_guarded_pre_move_prediction
from app.services.price_action_pattern_vision import _candles, _pivots, detect_price_action_patterns
from app.services.pump_state_machine import classify_pump_state
from app.services.scoring import _order_book_metrics


def _k(i: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> list[float]:
    return [i * 300_000, o, h, l, c, v]


def _impulse_wait_rows() -> list[list[float]]:
    rows: list[list[float]] = []
    for i in range(40):
        center = 100.0 + (i % 4 - 1.5) * 0.03
        rows.append(_k(i, center, center + 0.22, center - 0.22, center + (0.05 if i % 2 == 0 else -0.04), 100))
    rows += [
        _k(40, 99.95, 100.25, 99.85, 100.05, 100),
        _k(41, 100.05, 102.30, 100.00, 102.10, 260),
        _k(42, 101.00, 102.40, 100.85, 102.20, 180),
        _k(43, 102.15, 102.55, 102.00, 102.45, 150),
        _k(44, 102.45, 102.75, 102.30, 102.65, 140),
        _k(45, 102.65, 102.95, 102.55, 102.80, 130),
        _k(46, 102.80, 103.10, 102.70, 103.00, 130),
        _k(47, 103.00, 103.20, 102.90, 103.10, 120),
        _k(48, 103.10, 103.25, 103.00, 103.15, 110),
        _k(49, 103.15, 103.30, 103.05, 103.20, 100),
    ]
    return rows


def _pattern_rows() -> list[list[float]]:
    rows: list[list[float]] = []
    lows = [98.4, 98.7, 99.0, 99.3, 99.55, 99.75]
    for i in range(72):
        block = min(len(lows) - 1, i // 12)
        floor = lows[block]
        phase = i % 12
        c = floor + (phase / 11.0) * (101.0 - floor)
        o = c - 0.04 if phase % 2 == 0 else c + 0.03
        h = min(101.03, max(o, c) + 0.12)
        l = min(o, c) - 0.12
        rows.append(_k(i, o, h, l, c, 100 + i))
    return rows


def run() -> None:
    # 1) Book-depth code path.
    book = {
        "bids": [["99.99", "10"], ["99.90", "20"], ["99.70", "50"], ["99.00", "100"]],
        "asks": [["100.01", "5"], ["100.10", "10"], ["100.30", "40"], ["101.00", "100"]],
    }
    depth = _order_book_metrics(book)
    assert depth["spread_bps"] > 0
    assert depth["bid_depth_50bps_usd"] >= depth["bid_depth_10bps_usd"]

    # 2) Pattern vision — exercises flat trend-line calculations with ATR.
    vision = detect_price_action_patterns(_pattern_rows())
    assert vision["available"] is True
    assert vision["policy"]["can_create_entry"] is False

    candle_bars = [
        {"open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1, "volume": 100, "time": 1},
        {"open": 10.1, "high": 10.25, "low": 9.95, "close": 10.15, "volume": 100, "time": 2},
        {"open": 10.15, "high": 10.2, "low": 9.75, "close": 9.8, "volume": 100, "time": 3},
        {"open": 9.75, "high": 10.3, "low": 9.7, "close": 10.22, "volume": 130, "time": 4},
    ]
    assert any(item["name"] == "BULLISH_ENGULFING" for item in _candles(candle_bars, 0.3))

    pivot_bars = [
        {"time": 0, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 100},
        {"time": 1, "open": 100.0, "high": 100.8, "low": 99.6, "close": 100.2, "volume": 100},
        {"time": 2, "open": 100.2, "high": 101.25, "low": 99.7, "close": 100.1, "volume": 100},
        {"time": 3, "open": 100.1, "high": 100.79, "low": 99.65, "close": 100.0, "volume": 100},
        {"time": 4, "open": 100.0, "high": 100.6, "low": 99.55, "close": 99.9, "volume": 100},
    ]
    assert any(p["type"] == "H" and p["index"] == 2 for p in _pivots(pivot_bars, window=2, atr=1.0))

    # 3) Impulse/no-chase state.
    rows = _impulse_wait_rows()
    timing = build_impulse_pullback_confirmation(rows)
    assert timing["available"] is True
    assert timing["phase"] in {"WAIT_PULLBACK", "WAIT_PULLBACK_NO_CHASE"}
    assert timing["policy"]["do_not_chase"] is True

    # 4) Full pre-move prediction path. This catches undefined variable/name
    # regressions in the same branch that previously caused production errors.
    scored = {
        "symbol": "SMOKEUSDT",
        "direction": "LONG",
        "setup_score": 70.0,
        "risk_score": 35.0,
        "current_price": rows[-1][4],
        "metrics": {
            "atr_pct": 0.8,
            "compressed": False,
            "compression_ratio": 0.9,
            "volume_acceleration": 1.1,
            "relative_volume": 1.2,
            "futures_delta_ratio": 0.02,
            "spot_delta_ratio": 0.02,
            "order_book_imbalance": 0.03,
            "change_5m_pct": 0.15,
            "change_15m_pct": 0.3,
            "change_1h_pct": 0.6,
            "taker_avg_3": 1.01,
            "oi_change_pct": 0.1,
            "funding_rate": 0.0001,
            "btc_trend": "NEUTRAL",
        },
    }
    snapshot = {
        "symbol": "SMOKEUSDT",
        "klines": rows,
        "klines_15m": rows,
        "klines_1h": rows,
        "klines_4h": rows,
        "order_book": book,
        "trades": [],
        "spot_trades": [],
        "premium": {},
    }
    raw_prediction = build_raw_pre_move_prediction(scored, snapshot, {})
    assert isinstance(raw_prediction, dict)
    assert raw_prediction.get("type") is not None

    guarded_prediction = build_guarded_pre_move_prediction(scored, snapshot, {})
    assert isinstance(guarded_prediction, dict)
    assert guarded_prediction.get("type") is not None
    assert "technical_arsenal" in guarded_prediction

    # 5) Pump-state classifier is descriptive only.
    pump = classify_pump_state(
        score={"metrics": {
            "change_5m_pct": 1.1,
            "change_15m_pct": 2.3,
            "change_1h_pct": 4.0,
            "atr_pct": 0.9,
            "relative_volume": 3.0,
            "volume_acceleration": 1.6,
            "oi_change_pct": 0.45,
            "funding_rate": 0.0001,
            "order_book_imbalance": 0.12,
            "order_book_spread_bps": 2.0,
            "compression_ratio": 0.85,
        }},
        prediction={
            "professional_arsenal": {
                "flow": {
                    "futures_cvd_proxy": {"normalized_delta": 0.12},
                    "spot_cvd_proxy": {"normalized_delta": 0.15},
                },
                "derivatives": {"liquidation_squeeze": "NONE", "crowding": "NONE"},
                "absorption_exhaustion": {"exhaustion": "NONE"},
                "patterns": {"patterns": []},
            }
        },
        fundamental={},
    )
    assert pump["can_create_entry"] is False

    # 6) Historical replay invariants: point-in-time features must ignore future candles.
    hist_rows = []
    hist_price = 100.0
    for i in range(520):
        o = hist_price
        c = hist_price + (0.03 if (i // 40) % 2 == 0 else -0.015) + ((i % 7) - 3) * 0.005
        h = max(o, c) + 0.18
        l = min(o, c) - 0.18
        hist_rows.append([i * 300_000, o, h, l, c, 100000 + (i % 13) * 2500])
        hist_price = c
    btc_rows = [list(row) for row in hist_rows]
    btc_times = [int(row[0]) for row in btc_rows]
    hist_idx = 220
    hist_before = historical_feature_vector(hist_rows, hist_idx, btc_rows=btc_rows, btc_times=btc_times)
    for i in range(hist_idx + 1, len(hist_rows)):
        hist_rows[i][4] *= 4
        hist_rows[i][5] *= 20
    hist_after = historical_feature_vector(hist_rows, hist_idx, btc_rows=btc_rows, btc_times=btc_times)
    assert hist_before == hist_after
    assert historical_similarity(historical_distance(hist_before, hist_before)) == 100.0
    assert HISTORICAL_POLICY["can_create_entry"] is False
    assert HISTORICAL_POLICY["can_raise_leverage"] is False

    # 7) Central risk/leverage invariants.
    sized = paper_portfolio.size_position(1000.0, 100.0, 99.0, 200)
    assert sized["risk_usdt"] <= sized["risk_budget_usdt"]
    assert sized["margin"] <= 300.0 + 1e-9

    leverage = _sarpon_leverage_policy(
        lane_name="TACTICAL",
        lane={
            "max_leverage": 3,
            "shadow_calibration_status": "USABLE",
            "shadow_calibration_sample": 250,
        },
        heart={
            "chati_sarpon_612_monitor": {
                "phase": "GREEN_CONFIRMATION",
                "contradictions": [],
                "sarpon": {"classic": {"stage": "GREEN_CONFIRMATION"}},
                "btc": {"hard_conflict": False},
            }
        },
        conviction={"tier": "HIGH", "horizon_conflict": False},
        defensive=False,
        btc_overlay={"stress": "NORMAL"},
        quant_multiplier=1.0,
        council_multiplier=1.0,
        shadow_risk_multiplier=1.0,
        btc_side_multiplier=1.0,
        fundamental_multiplier=1.0,
        pump_state={"state": "NORMAL"},
    )
    assert 1 <= leverage["selected_leverage"] <= paper_portfolio.MAX_PAPER_LEVERAGE

    print("EXPLODEX_SMOKE_OK")


if __name__ == "__main__":
    run()
