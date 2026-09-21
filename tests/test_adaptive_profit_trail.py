from app.services.adaptive_profit_trail import build_adaptive_profit_trail


def test_adaptive_trail_does_not_loosen_level():
    rows = []
    for i in range(20):
        start = 1700000000000 + i * 900000
        base = 100.0 + i * 0.5
        rows.append([start, str(base), str(base + 0.6), str(base - 0.2), str(base + 0.4), "1", start + 899999, "1", 0, 0, 0, 0])
    result = build_adaptive_profit_trail(
        side="LONG",
        entry=100.0,
        initial_stop=97.0,
        current_stop=102.0,
        tp1=112.0,
        candles=rows,
        strategy_mode="SWING_PAPER",
    )
    assert result["new_stop"] >= 102.0
