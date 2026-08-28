from datetime import datetime, timedelta, timezone

from app.services.contextual_precision_map import build_contextual_precision_map


def _row(i: int, *, symbol: str, direction: str, barrier: str, trade_class: str = "TRADE_NOW", catalyst: str = "SUPPORTIVE", path_bias: str = "LONG", ret: float = 1.0):
    return {
        "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=i),
        "symbol": symbol,
        "direction": direction,
        "trade_class": trade_class,
        "catalyst_state": catalyst,
        "path_bias": path_bias,
        "barrier_hit": barrier,
        "directional_return_pct": ret if barrier == "TP1" else -abs(ret),
        "mfe_pct": 1.4 if barrier == "TP1" else 0.4,
        "mae_pct": -0.3 if barrier == "TP1" else -1.1,
    }


def test_precision_map_separates_symbol_and_direction_contexts():
    rows = []
    # First 70% and holdout both preserve a strong SOL LONG edge.
    for i in range(70):
        rows.append(_row(i, symbol="SOLUSDT", direction="LONG", barrier="TP1" if i % 5 else "STOP"))
    for i in range(70, 100):
        rows.append(_row(i, symbol="SOLUSDT", direction="LONG", barrier="TP1" if i % 4 else "STOP"))
    # Separate weaker BTC SHORT context.
    for i in range(100, 140):
        rows.append(_row(i, symbol="BTCUSDT", direction="SHORT", barrier="TP1" if i % 2 else "STOP", path_bias="SHORT"))

    report = build_contextual_precision_map(rows)
    symbol_direction = report["levels"]["SYMBOL_DIRECTION"]
    sol_long = next(c for c in symbol_direction if c["dimensions"] == {"symbol": "SOLUSDT", "direction": "LONG"})
    btc_short = next(c for c in symbol_direction if c["dimensions"] == {"symbol": "BTCUSDT", "direction": "SHORT"})

    assert sol_long["holdout"]["observed_tp1_first_pct"] != btc_short["holdout"]["observed_tp1_first_pct"]
    assert sol_long["observed_frequency_not_probability"] is True
    assert report["method"]["live_rules_changed"] is False


def test_overfit_context_is_not_marked_stable_on_bad_holdout():
    rows = []
    # 70 old observations look excellent.
    for i in range(70):
        rows.append(_row(i, symbol="ETHUSDT", direction="LONG", barrier="TP1" if i % 10 else "STOP"))
    # 30 newest observations collapse badly.
    for i in range(70, 100):
        rows.append(_row(i, symbol="ETHUSDT", direction="LONG", barrier="STOP" if i % 3 else "TP1"))

    report = build_contextual_precision_map(rows)
    cohort = next(
        c for c in report["levels"]["SYMBOL_DIRECTION"]
        if c["dimensions"] == {"symbol": "ETHUSDT", "direction": "LONG"}
    )

    assert cohort["enough_sample"] is True
    assert cohort["stable_out_of_sample"] is False
    assert cohort["precision_drop_pct"] > 20
