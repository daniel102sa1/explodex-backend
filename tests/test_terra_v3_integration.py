from app.services.pattern_engine import analyze_pattern_context, detect_patterns
from app.services.terra_controller import apply_adaptive_risk, build_terra_context
from app.services.terra_risk_engine import evaluate_risk


def _candles(start=100.0, step=0.2, n=40):
    out = []
    price = start
    for i in range(n):
        o = price
        c = price + step
        h = max(o, c) + 0.15
        l = min(o, c) - 0.10
        out.append([i * 60000, o, h, l, c, 1000])
        price = c
    return out


def test_pattern_engine_accepts_binance_rows_and_all_timeframes():
    rows = _candles()
    snapshot = {
        "klines_1m": rows,
        "klines": rows,
        "klines_15m": rows,
        "klines_1h": rows,
        "klines_4h": rows,
        "klines_1d": rows,
        "klines_1w": rows,
        "klines_1M": rows,
    }
    ctx = analyze_pattern_context(snapshot)
    assert set(ctx) == {"1m", "5m", "15m", "1h", "4h", "1d", "1w", "1M"}
    assert "SHORT_TERM_UPTREND" in ctx["1h"]["patterns"]


def test_extreme_evidence_can_raise_leverage_but_risk_stays_bounded():
    risk = evaluate_risk(
        confidence_score=100,
        stop_distance_pct=1.0,
        volatility_score=85,
        liquidity_score=90,
        timeframe_alignment=100,
        pattern_score=95,
        btc_context_score=90,
    )
    assert risk["tier"] == "EXTREME"
    assert 8 <= risk["leverage"] <= 12
    assert risk["capital_allocation_pct"] <= 30
    assert risk["risk_pct"] <= 1.0


def test_wide_stop_forces_lower_exposure():
    tight = evaluate_risk(
        confidence_score=95,
        stop_distance_pct=1.0,
        volatility_score=80,
        liquidity_score=90,
        timeframe_alignment=95,
        pattern_score=90,
        btc_context_score=90,
    )
    wide = evaluate_risk(
        confidence_score=95,
        stop_distance_pct=7.0,
        volatility_score=80,
        liquidity_score=90,
        timeframe_alignment=95,
        pattern_score=90,
        btc_context_score=90,
    )
    assert wide["leverage"] < tight["leverage"]
    assert wide["capital_allocation_pct"] < tight["capital_allocation_pct"]


def test_controller_blocks_weak_enter():
    ctx = build_terra_context(
        pattern={"1h": {"score": 0}},
        market={
            "volatility_score": 30,
            "liquidity_score": 30,
            "timeframe_alignment": 30,
            "btc_context_score": 30,
        },
        decision={
            "action": "ENTER",
            "allow_entry": True,
            "confidence_score": 20,
            "stop_distance_pct": 2.0,
            "risk_pct": 1.0,
        },
    )
    final = apply_adaptive_risk(ctx)
    assert final["allow_entry"] is False
    assert final["action"] == "WAIT"
