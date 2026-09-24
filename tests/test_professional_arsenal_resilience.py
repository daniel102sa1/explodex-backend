import app.services.professional_arsenal as arsenal


def _row(i: int, price: float) -> list[float]:
    return [i * 300_000, price, price + 0.4, price - 0.4, price + 0.05, 100 + i]


def test_shadow_pattern_failures_do_not_take_down_professional_arsenal(monkeypatch):
    def boom(_rows):
        raise RuntimeError("synthetic shadow failure")

    monkeypatch.setattr(arsenal, "detect_price_action_patterns", boom)
    monkeypatch.setattr(arsenal, "build_impulse_pullback_confirmation", boom)

    rows = [_row(i, 100.0 + i * 0.02) for i in range(80)]
    result = arsenal.build_professional_arsenal_context(
        {
            "metrics": {
                "relative_volume": 1.1,
                "volume_acceleration": 1.0,
                "atr_pct": 0.8,
                "compression_ratio": 0.9,
                "order_book_imbalance": 0.0,
                "funding_rate": 0.0001,
                "oi_change_pct": 0.1,
                "btc_trend": "NEUTRAL",
            }
        },
        {
            "klines": rows,
            "agg_trades": [],
            "spot_agg_trades": [],
            "premium": {},
        },
        {},
    )

    assert result["available"] is True
    assert set(result["degraded_layers"]) == {
        "price_action_pattern_vision",
        "impulse_pullback_confirmation",
    }
    assert result["price_action_pattern_vision"]["available"] is False
    assert result["impulse_pullback_confirmation"]["available"] is False
    assert result["price_action_pattern_vision"]["reason"] == "shadow_layer_runtime_error"
    assert result["impulse_pullback_confirmation"]["phase"] == "UNAVAILABLE"
