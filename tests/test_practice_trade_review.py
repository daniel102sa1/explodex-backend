from app.services.practice_ai import _fallback


def test_trade_review_fallback_never_invents_future_signal() -> None:
    result = _fallback({
        "review_trade": {
            "side":"LONG", "symbol":"SOLUSDT", "net_pnl":-1.25,
            "close_reason":"SL", "entry_price":100, "exit_price":98,
        },
    }, "AI budget exhausted",
    )
    assert result["direction"] == "WAIT"
    assert result["available"] is False
    assert result["tp1"] == 0
    assert result["projection_to"] == 0
    assert "-1.25" in result["summary"]
    assert "SL" in result["summary"]
