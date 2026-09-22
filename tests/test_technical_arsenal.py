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
