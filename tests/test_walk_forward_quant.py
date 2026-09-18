from app.services.walk_forward import _regime_coverage, _summary


def test_walk_forward_summary_exposes_quant_metrics():
    rows = [
        {"outcome": "TP1_FIRST", "rr1": 2.0},
        {"outcome": "STOP_FIRST", "rr1": 2.0},
        {"outcome": "TP1_FIRST", "rr1": 2.5},
        {"outcome": "STOP_FIRST", "rr1": 2.0},
    ] * 10
    report = _summary(rows, trials=12)

    assert report["sample"] == 40
    assert report["trade_sharpe"] is not None
    assert report["trade_sortino"] is not None
    assert report["cvar_95_r"] == -1.0
    assert report["deflated_sharpe_proxy"] <= report["trade_sharpe"]
    assert report["deflated_sharpe_is_exact"] is False


def test_regime_coverage_flags_single_regime_as_limited():
    rows = [{"market_regime": "BULLISH"} for _ in range(100)]
    coverage = _regime_coverage(rows)

    assert coverage["status"] == "LIMITED"
    assert coverage["dominant_regime_share_pct"] == 100.0


def test_regime_coverage_accepts_broader_samples():
    rows = (
        [{"market_regime": "BULLISH"} for _ in range(40)]
        + [{"market_regime": "BEARISH"} for _ in range(35)]
        + [{"market_regime": "MIXED"} for _ in range(25)]
    )
    coverage = _regime_coverage(rows)

    assert coverage["status"] == "BROAD"
    assert len(coverage["meaningful_regimes"]) == 3
