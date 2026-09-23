from app.services.technical_arsenal import (
    build_technical_arsenal_context,
    technical_arsenal_registry,
)


def k(i, o, h, l, c, v=100.0):
    start = 1_700_000_000_000 + i * 300_000
    return [start, str(o), str(h), str(l), str(c), str(v), start + 299_999, str(v * c), 0, 0, 0, 0]


def trending_rows():
    rows = []
    p = 100.0
    for i in range(70):
        o = p
        c = p + 0.35
        h = c + 0.20
        l = o - 0.15
        rows.append(k(i, o, h, l, c, 100 + i))
        p = c
    return rows


def test_registry_is_shadow_only_and_does_not_duplicate_existing_domains():
    reg = technical_arsenal_registry()
    assert reg["policy"]["research_only"] is True
    assert reg["policy"]["can_create_entry"] is False
    assert reg["policy"]["can_veto_entry"] is False
    assert "macd" in reg["already_covered_elsewhere"]
    assert "elliott_wave_structure" in reg["already_covered_elsewhere"]
    assert "harmonic_patterns" in reg["mentioned_but_not_rule_defined_in_source"]


def test_technical_arsenal_builds_structure_and_indicator_context():
    result = build_technical_arsenal_context(
        {"metrics": {"atr_pct": 0.5}},
        {"klines": trending_rows()},
        {},
    )
    assert result["available"] is True
    assert result["research_only"] is True
    assert result["score_is_probability"] is False
    assert "market_structure" in result
    assert "fibonacci" in result
    assert "fair_value_gaps" in result
    assert "order_blocks" in result
    assert "stochastic" in result
    assert "parabolic_sar" in result
    assert "supertrend" in result
    assert "volume_profile" in result


def test_bullish_fvg_is_detected_as_open_when_not_filled():
    rows = trending_rows()[:40]
    base = 115.0
    rows.extend([
        k(40, base, base + 0.4, base - 0.2, base + 0.2),
        k(41, base + 0.3, base + 2.8, base + 0.25, base + 2.5),
        k(42, base + 2.6, base + 3.0, base + 1.0, base + 2.8),
    ])
    result = build_technical_arsenal_context(
        {"metrics": {"atr_pct": 0.5}},
        {"klines": rows},
        {},
    )
    gaps = result["fair_value_gaps"]["nearest_open_gaps"]
    assert any(g["type"] == "BULLISH_FVG" for g in gaps)


def test_volume_profile_is_explicitly_approximate():
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": trending_rows()},
        {},
    )
    profile = result["volume_profile"]
    assert profile["available"] is True
    assert profile["approximation"] is True
    assert "NOT_TICK_VOLUME_PROFILE" in profile["method"]



def test_heikin_ashi_and_renko_are_analysis_only_contexts():
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": trending_rows()},
        {},
    )
    assert result["heikin_ashi"]["available"] is True
    assert result["heikin_ashi"]["analysis_only_not_execution_price"] is True
    assert result["renko"]["available"] is True
    assert result["renko"]["approximation"] is True
    assert result["renko"]["analysis_only_not_execution_price"] is True


def test_harmonics_remain_unconfirmed_until_full_source_ratios_arrive():
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": trending_rows()},
        {},
    )
    harmonics = result["harmonics"]
    assert harmonics["can_confirm_harmonic_pattern"] is False
    assert harmonics["confirmation_status"] == "INCOMPLETE_SOURCE_RULESET"
    assert harmonics["source_rules_captured"]["bat_xb_range_stated"] == [0.382, 0.5]
    assert harmonics["source_rules_captured"]["ac_range_stated"] == [0.382, 0.886]


def test_gann_and_lunar_are_not_promoted_to_signal_logic():
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": trending_rows()},
        {},
    )
    assert result["gann_fan"]["available"] is False
    assert result["gann_fan"]["can_create_entry"] is False
    assert result["lunar_phases"]["implemented_as_signal"] is False


def test_divergence_and_dynamic_levels_are_exposed():
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": trending_rows()},
        {},
    )
    assert result["divergence"]["available"] is True
    assert "ema20" in result["dynamic_support_resistance"]
    assert "ema50" in result["dynamic_support_resistance"]


def test_shared_strat_212_bullish_continuation_is_detected():
    rows = trending_rows()[:40]
    rows.extend([
        k(40, 114.0, 115.0, 113.0, 114.5),
        k(41, 114.5, 116.0, 113.2, 115.6),
        k(42, 115.2, 115.7, 113.5, 114.9),
        k(43, 114.9, 116.2, 113.6, 116.0),
    ])
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": rows},
        {},
    )
    strat = result["strat_price_action"]
    names = {p["name"] for p in strat["patterns"]}
    assert "2-1-2_CONTINUATION_BULLISH" in names
    assert strat["aggregate_bias"] == "LONG"
    assert strat["can_create_entry"] is False


def test_ohlc_liquidity_sweep_rejection_is_exposed_without_double_counting():
    rows = [
        k(i, 100.0, 101.0, 99.0, 100.1 if i % 2 else 99.9, 1000 + i)
        for i in range(44)
    ]
    rows.append(k(44, 100.2, 101.8, 99.3, 100.7, 1600))
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": rows},
        {},
    )
    sweep = result["liquidity_sweep"]
    assert sweep["event"] == "HIGH_LIQUIDITY_SWEEP_REJECTED"
    assert sweep["bias"] == "SHORT"
    assert sweep["can_create_entry"] is False


def test_amd_low_manipulation_then_distribution_up_is_detected():
    rows = [
        k(i, 100.0, 101.0, 99.0, 100.0, 900 + i)
        for i in range(22)
    ]
    start = len(rows)
    for j in range(14):
        close = 100.08 if j % 2 else 99.92
        rows.append(k(start + j, 100.0, 100.5, 99.5, close, 1000 + j))
    i = len(rows)
    rows.extend([
        k(i, 100.0, 100.2, 98.7, 99.8, 1800),
        k(i + 1, 99.8, 100.3, 99.7, 100.1, 1400),
        k(i + 2, 100.1, 100.45, 100.0, 100.3, 1300),
        k(i + 3, 100.3, 100.45, 100.2, 100.4, 1200),
    ])
    result = build_technical_arsenal_context(
        {"metrics": {}},
        {"klines": rows},
        {},
    )
    amd = result["amd_market_story"]
    assert amd["phase"] == "DISTRIBUTION_UP_AFTER_LOW_MANIPULATION"
    assert amd["bias"] == "LONG"
    assert amd["heuristic_not_institutional_intent_claim"] is True


def test_registry_marks_existing_liquidity_orderflow_and_risk_as_covered_elsewhere():
    reg = technical_arsenal_registry()
    covered = set(reg["already_covered_elsewhere"])
    assert "liquidity_target_engine" in covered
    assert "order_flow_spot_futures_delta_orderbook" in covered
    assert "structural_stop_position_sizing" in covered
    assert "risk_reward_execution_math" in covered
