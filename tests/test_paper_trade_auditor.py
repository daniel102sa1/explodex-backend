from datetime import datetime, timezone

from app.services.paper_trade_auditor import (
    _entry_structure_audit,
    _management_recommendation,
    _path_metrics,
    technical_snapshot,
)


def _kline(i, o, h, l, c, v=1000.0):
    ts = 1_700_000_000_000 + i * 300_000
    return [ts, str(o), str(h), str(l), str(c), str(v), ts + 299_999, str(v * c), 0, 0, 0, 0]


def _trend_klines(count=80):
    rows = []
    price = 100.0
    for i in range(count):
        nxt = price + 0.08
        rows.append(_kline(i, price, nxt + 0.20, price - 0.18, nxt, 1200 + i * 3))
        price = nxt
    return rows


def test_technical_snapshot_exposes_rsi_macd_ema_and_atr():
    snap = technical_snapshot(_trend_klines())

    assert snap["ema20"] is not None
    assert snap["ema50"] is not None
    assert snap["ema20"] > snap["ema50"]
    assert snap["rsi14"] is not None
    assert snap["macd"] is not None
    assert snap["macd_signal"] is not None
    assert snap["macd_histogram"] is not None
    assert snap["atr"] > 0


def test_path_metrics_is_conservative_when_stop_and_target_share_candle():
    candles = [
        {"high": 101.2, "low": 98.8, "open": 100.0, "close": 100.5, "time": 1, "volume": 1},
    ]
    result = _path_metrics(
        side="LONG",
        entry=100.0,
        stop=99.0,
        tp1=101.0,
        candles=candles,
    )

    assert result["stop_before_tp1"] is True
    assert result["tp1_before_stop"] is False
    assert result["one_r_before_stop"] is False


def test_path_metrics_tracks_runner_after_tp1():
    candles = [
        {"high": 100.8, "low": 99.6, "open": 100.0, "close": 100.6, "time": 1, "volume": 1},
        {"high": 101.6, "low": 100.3, "open": 100.6, "close": 101.4, "time": 2, "volume": 1},
        {"high": 102.2, "low": 101.0, "open": 101.4, "close": 102.0, "time": 3, "volume": 1},
        {"high": 103.3, "low": 101.9, "open": 102.0, "close": 103.0, "time": 4, "volume": 1},
    ]
    result = _path_metrics(
        side="LONG",
        entry=100.0,
        stop=99.0,
        tp1=101.5,
        candles=candles,
    )

    assert result["tp1_before_stop"] is True
    assert result["one_r_before_stop"] is True
    assert result["runner_2r_after_tp1"] is True
    assert result["runner_3r_after_tp1"] is True


def test_stop_audit_flags_stop_inside_structure_as_too_tight():
    rows = []
    base = 100.0
    for i in range(30):
        rows.append(_kline(i, base, 100.8, 99.2, 100.1, 1000))
    opened_at = datetime.fromtimestamp((1_700_000_000_000 + 31 * 300_000) / 1000, tz=timezone.utc)

    audit = _entry_structure_audit(
        side="LONG",
        entry=100.5,
        actual_stop=99.25,
        klines_15m=rows,
        opened_at=opened_at,
    )

    assert audit["status"] == "TOO_TIGHT"
    assert audit["can_widen_live_stop"] is False
    assert audit["ideal_hard_stop_for_future_similar_setups"] < audit["structural_level"]


def test_management_after_tp1_prefers_partial_runner_when_alignment_strong():
    result = _management_recommendation(
        side="LONG",
        entry=100.0,
        current_price=102.0,
        stop=99.0,
        tp1=101.5,
        path={"tp1_touched": True, "first_one_r_index": 1},
        alignment={"state": "STRONG"},
        current_structure_stop=100.7,
    )

    assert result["action"] == "TAKE_PARTIAL_AND_TRAIL_STRUCTURE"
    assert result["suggested_protective_stop"] == 100.7
    assert result["may_increase_risk"] is False
    assert result["may_widen_live_stop"] is False


def test_management_never_recommends_widening_when_trade_is_weak():
    result = _management_recommendation(
        side="LONG",
        entry=100.0,
        current_price=99.4,
        stop=99.0,
        tp1=102.0,
        path={"tp1_touched": False, "first_one_r_index": None},
        alignment={"state": "WEAK"},
        current_structure_stop=98.5,
    )

    assert result["action"] == "HOLD_OR_EXIT_ON_STRUCTURAL_INVALIDATION"
    assert result["suggested_protective_stop"] is None
    assert result["may_widen_live_stop"] is False
