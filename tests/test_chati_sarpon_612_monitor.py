from app.services.chati_sarpon_612_monitor import build_manual_monitor
from app.services.paper_loss_autopsy import metrics_from_rows


def _healthy_long_metrics():
    return {
        "taker_latest": 1.34,
        "taker_avg_3": 1.26,
        "taker_15m_ratio": 1.29,
        "taker_15m_ratio_source": "AGGREGATED_3X5M_BUYSELL_VOLUME",
        "futures_delta_ratio": 0.16,
        "spot_delta_ratio": 0.10,
        "oi_change_pct": 0.65,
        "relative_volume": 1.55,
        "volume_acceleration": 1.32,
        "funding_rate": 0.0001,
        "top_account_long_short_ratio": 1.08,
        "top_position_long_short_ratio": 1.18,
        "global_long_short_ratio": 1.15,
        "trend_15m": "BULLISH",
        "trend_1h": "BULLISH",
        "btc_trend": "BULLISH",
        "absorption_conflict": False,
    }


def test_chati_sarpon_612_green_requires_flow_structure_oi_and_btc():
    monitor = build_manual_monitor(
        side="LONG",
        metrics=_healthy_long_metrics(),
        current_price=101.0,
        entry_low=100.5,
        entry_high=101.2,
        entry_price=101.0,
        stop=99.5,
        tp1=103.0,
        btc_overlay={"direction": "BULLISH", "stress": "NORMAL"},
        is_open_position=False,
    )

    assert monitor["phase"] == "GREEN_CONFIRMATION"
    assert monitor["entry_gate"] == "ALLOW_CONFIRMATION"
    assert monitor["score_is_probability"] is False
    assert monitor["chati"]["five_minute_aligned"] is True
    assert monitor["chati"]["fifteen_minute_aligned"] is True
    assert monitor["chati"]["ratio_15m_source"] == "AGGREGATED_3X5M_BUYSELL_VOLUME"


def test_positive_flow_absorbed_under_structure_is_red_not_long_confirmation():
    metrics = _healthy_long_metrics()
    metrics.update({
        "absorption_conflict": True,
        "long_absorption_conflict": True,
        "futures_delta_ratio": 0.25,
        "taker_latest": 1.60,
        "taker_15m_ratio": 1.48,
    })
    monitor = build_manual_monitor(
        side="LONG",
        metrics=metrics,
        current_price=100.8,
        entry_low=100.5,
        entry_high=101.0,
        entry_price=100.8,
        stop=99.5,
        tp1=103.0,
        btc_overlay={"direction": "BULLISH", "stress": "NORMAL"},
    )

    assert monitor["phase"] == "RED_DAMAGED"
    assert monitor["entry_gate"] == "BLOCK"
    assert monitor["absorption_against_thesis"] is True
    assert "absorption_against_thesis" in monitor["contradictions"]


def test_good_5m_does_not_override_bad_15m_and_falling_oi():
    metrics = _healthy_long_metrics()
    metrics.update({
        "taker_latest": 1.42,
        "taker_15m_ratio": 0.72,
        "taker_avg_3": 0.75,
        "futures_delta_ratio": -0.12,
        "oi_change_pct": -0.90,
        "trend_15m": "BEARISH",
    })
    monitor = build_manual_monitor(
        side="LONG",
        metrics=metrics,
        current_price=100.0,
        entry_price=101.0,
        stop=99.0,
        tp1=104.0,
        btc_overlay={"direction": "NEUTRAL", "stress": "ELEVATED"},
        is_open_position=True,
    )

    assert monitor["phase"] == "RED_DAMAGED"
    assert "chati_15m_opposed" in monitor["contradictions"]
    assert "open_interest_deteriorating" in monitor["contradictions"]
    assert monitor["rules"]["does_not_widen_live_stop"] is True


def test_btc_extreme_opposite_direction_blocks_long():
    monitor = build_manual_monitor(
        side="LONG",
        metrics=_healthy_long_metrics(),
        current_price=101.0,
        entry_low=100.8,
        entry_high=101.2,
        stop=99.2,
        tp1=104.0,
        btc_overlay={"direction": "BEARISH", "stress": "EXTREME"},
    )

    assert monitor["phase"] == "RED_DAMAGED"
    assert monitor["btc"]["hard_conflict"] is True
    assert "btc_high_stress_direction_conflict" in monitor["contradictions"]


def test_loss_autopsy_counts_new_hard_and_structural_stop_labels():
    rows = [
        {"exit_reason": "HARD_STOP", "net_pnl": -10, "fees": 0, "slippage": 0, "funding_estimate": 0},
        {"exit_reason": "AMBIGUOUS_HARD_STOP", "net_pnl": -8, "fees": 0, "slippage": 0, "funding_estimate": 0},
        {"exit_reason": "STRUCTURAL_CLOSE_INVALIDATION", "net_pnl": -4, "fees": 0, "slippage": 0, "funding_estimate": 0},
        {"exit_reason": "TP1", "net_pnl": 12, "fees": 0, "slippage": 0, "funding_estimate": 0},
    ]

    metrics = metrics_from_rows(rows)

    assert metrics["trades"] == 4
    assert metrics["stops"] == 3
    assert metrics["stop_rate"] == 0.75



def test_sarpon_classic_yellow_keeps_manual_monitor_in_preactivation():
    monitor = build_manual_monitor(
        side="LONG",
        metrics=_healthy_long_metrics(),
        current_price=101.0,
        entry_low=100.5,
        entry_high=101.2,
        entry_price=101.0,
        stop=99.5,
        tp1=103.0,
        btc_overlay={"direction": "BULLISH", "stress": "NORMAL"},
        sarpon_classic={
            "available": True,
            "stage": "YELLOW_FORMING",
            "score": 74.0,
            "murphy": {"aligned": True, "retest": True},
            "nison": {"confirmed": False},
        },
    )

    assert monitor["phase"] == "YELLOW_PREACTIVATION"
    assert monitor["entry_gate"] == "WAIT"
    assert "sarpon_murphy_nison_waiting_confirmation" in monitor["contradictions"]


def test_sarpon_classic_red_hard_blocks_even_when_flow_is_healthy():
    monitor = build_manual_monitor(
        side="LONG",
        metrics=_healthy_long_metrics(),
        current_price=101.0,
        entry_low=100.5,
        entry_high=101.2,
        entry_price=101.0,
        stop=99.5,
        tp1=103.0,
        btc_overlay={"direction": "BULLISH", "stress": "NORMAL"},
        sarpon_classic={
            "available": True,
            "stage": "RED_INVALIDATED",
            "score": 20.0,
        },
    )

    assert monitor["phase"] == "RED_DAMAGED"
    assert monitor["entry_gate"] == "BLOCK"
    assert "sarpon_murphy_nison_invalidated" in monitor["contradictions"]
