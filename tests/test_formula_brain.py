from app.services.formula_brain import build_formula_brain, formula_registry


def _rows(direction: str, n: int = 90):
    rows = []
    price = 100.0
    for i in range(n):
        step = 0.18 if direction == "UP" else -0.18
        open_ = price
        close = max(1.0, price + step)
        high = max(open_, close) + 0.06
        low = min(open_, close) - 0.06
        base_volume = 1000.0 + i * 8.0
        quote_volume = base_volume * ((open_ + close) / 2.0)
        ts = i * 300_000
        rows.append([
            ts, open_, high, low, close, base_volume,
            ts + 299_999, quote_volume, 10, 0, 0, 0
        ])
        price = close
    return rows


def test_formula_registry_is_research_only_and_broad():
    registry = formula_registry()
    assert registry["research_only"] is True
    assert len(registry["formulas"]) >= 12
    assert registry["policy"]["can_create_entry"] is False
    assert registry["policy"]["can_raise_leverage"] is False


def test_formula_brain_detects_persistent_uptrend_without_becoming_entry_authority():
    result = build_formula_brain(
        {"direction": "LONG", "metrics": {"atr_pct": 0.5}},
        {"klines": _rows("UP")},
    )

    assert result["available"] is True
    assert result["direction"] == "LONG"
    assert result["regime"] in {"TREND_PERSISTENT", "MIXED"}
    assert result["agrees_with_scanner"] is True
    assert result["policy"]["can_create_entry"] is False
    assert result["policy"]["can_raise_leverage"] is False
    assert result["score_is_probability"] is False


def test_formula_brain_detects_persistent_downtrend():
    result = build_formula_brain(
        {"direction": "SHORT", "metrics": {"atr_pct": 0.5}},
        {"klines": _rows("DOWN")},
    )

    assert result["available"] is True
    assert result["direction"] == "SHORT"
    assert result["agrees_with_scanner"] is True
    assert result["long_weight"] < result["short_weight"]


def test_formula_brain_reports_conflict_but_cannot_veto_yet():
    result = build_formula_brain(
        {"direction": "SHORT", "metrics": {"atr_pct": 0.5}},
        {"klines": _rows("UP")},
    )

    assert result["direction"] == "LONG"
    assert result["conflicts_with_scanner"] is True
    assert result["policy"]["can_veto_entry"] is False
