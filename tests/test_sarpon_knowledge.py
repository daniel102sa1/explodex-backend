from app.services.sarpon_knowledge import (
    build_sarpon_classic_context,
    sarpon_knowledge_registry,
)


def _row(i: int, open_: float, high: float, low: float, close: float):
    ts = i * 300_000
    return [ts, open_, high, low, close, 1000.0, ts + 299_999, close * 1000.0, 10, 0, 0, 0]


def test_registry_starts_with_user_confirmed_murphy_and_nison_only():
    registry = sarpon_knowledge_registry()
    authors = [item["author"] for item in registry["confirmed_sources"]]

    assert authors == ["John J. Murphy", "Steve Nison"]
    assert registry["pending_sources"] is True
    assert "Do not invent" in registry["pending_source_rule"]


def test_sarpon_classic_green_needs_structure_retest_and_candle_confirmation():
    rows = []
    price = 100.0
    for i in range(38):
        open_ = price
        close = price + 0.12
        rows.append(_row(i, open_, close + 0.08, open_ - 0.05, close))
        price = close

    rows.append(_row(38, 104.56, 104.62, 104.28, 104.34))
    rows.append(_row(39, 104.30, 104.72, 104.24, 104.66))

    prediction = {
        "direction": "LONG",
        "entry_low": 104.20,
        "entry_high": 104.80,
        "trigger_price": 104.55,
        "invalidation_price": 103.60,
        "stop_loss": 103.45,
        "tp1": 105.60,
        "tp2": 106.40,
        "tp3": 107.30,
        "sequence": {"chase_risk": False},
    }
    result = build_sarpon_classic_context(
        {"direction": "LONG"},
        {"symbol": "TESTUSDT", "klines": rows},
        prediction,
    )

    assert result["available"] is True
    assert result["stage"] == "GREEN_CONFIRMATION"
    assert result["murphy"]["aligned"] is True
    assert result["murphy"]["retest"] is True
    assert result["nison"]["confirmed"] is True
    assert "BULLISH_ENGULFING_HEURISTIC" in result["nison"]["patterns"]
    assert result["score_is_probability"] is False


def test_sarpon_classic_never_allows_chasing():
    rows = []
    price = 100.0
    for i in range(40):
        open_ = price
        close = price + 0.10
        rows.append(_row(i, open_, close + 0.05, open_ - 0.04, close))
        price = close

    prediction = {
        "direction": "LONG",
        "entry_low": 102.0,
        "entry_high": 102.3,
        "trigger_price": 102.2,
        "invalidation_price": 101.0,
        "stop_loss": 100.8,
        "tp1": 104.0,
        "tp2": 105.0,
        "tp3": 106.0,
        "sequence": {"chase_risk": True},
    }
    result = build_sarpon_classic_context(
        {"direction": "LONG"},
        {"symbol": "TESTUSDT", "klines": rows},
        prediction,
    )

    assert result["murphy"]["no_chase"] is False
    assert result["stage"] != "GREEN_CONFIRMATION"
    assert result["entry_gate"] == "WAIT"
