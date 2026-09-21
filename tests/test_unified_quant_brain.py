from app.services.paper_quant_risk_guard import build_quant_metrics, evaluate_quant_guard
from app.services.quant_brain import build_quant_brain
from app.services.unified_heart_contract import _hard_safety_clear


def _kline(i, o, h, l, c, v=1000.0):
    ts = 1_700_000_000_000 + i * 300_000
    return [ts, str(o), str(h), str(l), str(c), str(v), ts + 299_999, str(v * c), 0, 0, 0, 0]


def _trend(count=260, start=100.0, step=0.08, wobble=0.03):
    rows = []
    price = start
    for i in range(count):
        drift = step + (wobble if i % 4 in {0, 1} else -wobble)
        nxt = max(1.0, price + drift)
        rows.append(_kline(i, price, max(price, nxt) + 0.10, min(price, nxt) - 0.10, nxt, 1200 + (i % 7) * 25))
        price = nxt
    return rows


def _trend_15m(count=140, start=100.0, step=0.18):
    return _trend(count=count, start=start, step=step, wobble=0.05)


def test_quant_brain_unifies_math_flow_btc_and_strategy_selection():
    asset = _trend(start=50.0, step=0.10)
    btc = _trend(start=60000.0, step=18.0, wobble=8.0)
    result = build_quant_brain(
        symbol="TESTUSDT",
        direction="LONG",
        klines_5m=asset,
        klines_15m=_trend_15m(start=50.0, step=0.22),
        btc_5m=btc,
        metrics={
            "futures_delta_ratio": 0.18,
            "spot_delta_ratio": 0.15,
            "order_book_imbalance": 0.14,
            "oi_change_pct": 1.2,
            "taker_avg_3": 1.25,
            "funding_rate": 0.0001,
        },
        calibration={
            "status": "USABLE",
            "sample": 50,
            "accuracy_pct": 61.0,
            "bounded_conviction_adjustment": 2.5,
        },
    )

    assert result["available"] is True
    assert result["score_is_probability"] is False
    assert result["directional_edge"] > 0
    assert 0.2 <= result["risk_multiplier"] <= 1.0
    assert result["statistics"]["rsi14"] is not None
    assert result["statistics"]["macd_histogram"] is not None
    assert result["statistics"]["vwap20"] > 0
    assert result["btc_relationship"]["beta_5m"] != 0
    assert result["regime"]["hurst_exponent"] is not None
    assert result["regime"]["entropy_normalized"] is not None
    assert result["cointegration"]["method"].startswith("OLS residual")
    assert result["monte_carlo_market"]["available"] is True
    assert result["strategy_selector"]["preferred"]
    assert result["calibration"]["is_next_trade_probability"] is False


def test_quant_brain_detects_directional_conflict_without_flipping_side():
    asset = _trend(start=50.0, step=0.11)
    btc = _trend(start=60000.0, step=20.0, wobble=5.0)
    result = build_quant_brain(
        symbol="TESTUSDT",
        direction="SHORT",
        klines_5m=asset,
        klines_15m=_trend_15m(start=50.0, step=0.25),
        btc_5m=btc,
        metrics={
            "futures_delta_ratio": 0.25,
            "spot_delta_ratio": 0.22,
            "order_book_imbalance": 0.18,
            "oi_change_pct": 1.0,
            "taker_avg_3": 1.35,
            "funding_rate": 0.0001,
        },
        calibration={"status": "CALIBRATING", "sample": 10},
    )

    assert result["direction"] == "SHORT"
    assert result["directional_edge"] < 0
    assert result["rules"]["may_change_primary_direction"] is False
    assert result["rules"]["may_upgrade_wait_to_entry"] is False


def test_unified_contract_treats_quant_block_as_hard_safety_blocker():
    heart = {
        "quant_brain": {"block_new_entry": True, "strong_conflict": True},
        "thesis": {},
        "entry_latch": {},
    }
    prediction = {
        "prediction_stack_v5": {"risk_veto": {}},
        "sequence": {"risk_guard_pass": True},
        "decision_guard": {"risk_guard_pass": True},
    }

    clear, blockers = _hard_safety_clear(heart, prediction)

    assert clear is False
    assert "quant_brain_block" in blockers


def test_paper_quant_metrics_include_expectancy_kelly_and_monte_carlo():
    rows = []
    for i in range(60):
        pnl = 14.0 if i % 3 != 0 else -8.0
        rows.append({
            "net_pnl": pnl,
            "risk_usdt": 10.0,
            "side": "LONG",
            "grade": "HEART",
            "strategy_mode": "TACTICAL",
        })

    metrics = build_quant_metrics(rows, recent_window=20, trials=4)

    assert metrics["expectancy_kelly"]["sample"] == 60
    assert metrics["expectancy_kelly"]["status"] == "USABLE_REFERENCE"
    assert metrics["expectancy_kelly"]["expectancy_r"] > 0
    assert metrics["expectancy_kelly"]["kelly_is_direct_position_size"] is False
    assert metrics["paper_monte_carlo"]["available"] is True
    assert metrics["paper_monte_carlo"]["paths"] == 2000
    assert metrics["paper_monte_carlo"]["is_forecast_probability"] is False

    guard = evaluate_quant_guard(metrics=metrics, net_24h=5.0)
    assert guard["state"] in {"ALLOW", "REDUCE", "REDUCE_HARD"}
    assert guard["can_change_direction"] is False


def test_negative_paper_edge_reduces_quant_risk_after_mature_sample():
    rows = [{
        "net_pnl": -10.0 if i % 4 != 0 else 3.0,
        "risk_usdt": 10.0,
        "side": "LONG",
        "grade": "HEART",
        "strategy_mode": "TACTICAL",
    } for i in range(60)]

    metrics = build_quant_metrics(rows, recent_window=20, trials=3)
    guard = evaluate_quant_guard(metrics=metrics, net_24h=-5.0)

    assert metrics["expectancy_kelly"]["full_kelly_fraction"] <= 0
    assert guard["risk_multiplier"] <= 0.25
    assert guard["state"] in {"REDUCE_HARD", "HALT_NEW_ENTRIES"}


def test_non_extreme_quant_conflict_is_soft_not_duplicate_hard_block():
    heart = {
        "quant_brain": {"block_new_entry": False, "strong_conflict": True},
        "thesis": {},
        "entry_latch": {},
    }
    prediction = {
        "prediction_stack_v5": {"risk_veto": {}},
        "sequence": {"risk_guard_pass": True},
        "decision_guard": {"risk_guard_pass": True},
    }

    clear, blockers = _hard_safety_clear(heart, prediction)

    assert clear is True
    assert "quant_brain_conflict" not in blockers
