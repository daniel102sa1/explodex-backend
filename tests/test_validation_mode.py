from app.services.validation_mode import evaluate_horizon_candles


def candle(ts: int, o: float, h: float, l: float, c: float):
    return [ts, str(o), str(h), str(l), str(c)]


def test_long_tp1_before_stop():
    rows = [
        candle(1, 100, 101, 99.6, 100.5),
        candle(2, 100.5, 103.2, 100.1, 102.8),
    ]
    result = evaluate_horizon_candles(
        direction="LONG", entry=100, stop=98, tp1=103, candles=rows, atr_pct=2.0
    )
    assert result["barrier_hit"] == "TP1"
    assert result["mfe_pct"] == 3.2
    assert result["mae_pct"] == -0.4
    assert result["mfe_atr"] == 1.6


def test_long_stop_before_tp1():
    rows = [
        candle(1, 100, 100.4, 97.8, 98.2),
        candle(2, 98.2, 103.4, 98.0, 103.0),
    ]
    result = evaluate_horizon_candles(
        direction="LONG", entry=100, stop=98, tp1=103, candles=rows
    )
    assert result["barrier_hit"] == "STOP"


def test_short_tp1_before_stop_and_directional_metrics():
    rows = [
        candle(1, 100, 100.3, 98.8, 99.2),
        candle(2, 99.2, 99.5, 96.7, 97.0),
    ]
    result = evaluate_horizon_candles(
        direction="SHORT", entry=100, stop=102, tp1=97, candles=rows, atr_pct=2.0
    )
    assert result["barrier_hit"] == "TP1"
    assert result["mfe_pct"] == 3.3
    assert result["mae_pct"] == -0.3
    assert result["directional_return_pct"] == 3.0


def test_same_candle_tp_and_stop_is_ambiguous():
    rows = [candle(1, 100, 103.5, 97.5, 100.2)]
    result = evaluate_horizon_candles(
        direction="LONG", entry=100, stop=98, tp1=103, candles=rows
    )
    assert result["barrier_hit"] == "AMBIGUOUS"


def test_empty_candles_are_unavailable():
    result = evaluate_horizon_candles(
        direction="LONG", entry=100, stop=98, tp1=103, candles=[]
    )
    assert result == {"available": False}
