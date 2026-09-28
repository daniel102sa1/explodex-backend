from app.services.pattern_engine import analyze_pattern_context, detect_patterns
from app.services.terra_controller import build_terra_context
from app.services.terra_risk_engine import evaluate_risk


def _bars(direction: str = "up"):
    rows = []
    price = 100.0
    for i in range(80):
        drift = 0.12 if direction == "up" else -0.12
        close = price + drift
        high = max(price, close) + 0.25
        low = min(price, close) - 0.20
        rows.append([i * 60000, price, high, low, close, 1000 + i * 5, i * 60000 + 59999])
        price = close
    return rows


def test_pattern_engine_accepts_binance_rows():
    result = detect_patterns(_bars("up"))
    assert result["version"] == "terra_pattern_engine_v2"
    assert "long_score" in result
    assert "atr_pct" in result


def test_multi_timeframe_pattern_context():
    bars = _bars("up")
    snapshot = {
        "klines_1m": bars,
        "klines": bars,
        "klines_15m": bars,
        "klines_1h": bars,
        "klines_4h": bars,
        "klines_1d": bars,
        "klines_1w": bars,
        "klines_1M": bars,
    }
    result = analyze_pattern_context(snapshot)
    assert result["directional_timeframes"] >= 1
    assert result["bias"] in {"LONG", "SHORT", "NEUTRAL"}


def test_risk_engine_caps_extreme_request():
    risk = evaluate_risk(
        confidence_score=95,
        stop_distance_pct=1.0,
        volatility_score=85,
        liquidity_score=95,
        timeframe_alignment=95,
        pattern_score=90,
        btc_context_score=80,
        requested_leverage=20,
        requested_capital_allocation_pct=100,
        requested_risk_pct=10,
    )
    assert risk["allow_entry"] is True
    assert risk["leverage"] <= 15
    assert risk["capital_allocation_pct"] <= 40
    assert risk["risk_pct"] <= 1.0


def test_controller_can_veto_bad_geometry():
    decision = {
        "action": "ENTER",
        "allow_entry": True,
        "direction": "LONG",
        "entry_low": 100,
        "entry_high": 101,
        "stop_loss": 80,
        "leverage": 20,
        "risk_pct": 5,
        "capital_allocation_pct": 80,
        "evidence_strength": "HIGH",
    }
    context = build_terra_context(
        pattern={"score": 70, "agreement_pct": 80, "directional_timeframes": 4},
        market={"current_price": 100.5, "setup_score": 90, "btc_context_score": 70},
        decision=decision,
        snapshot={},
    )
    assert context["allow_entry"] is False
