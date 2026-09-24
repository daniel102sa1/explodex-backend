from app.services.impulse_pullback_confirmation import build_impulse_pullback_confirmation


def k(i, o, h, l, c, v=100.0):
    return [i * 300_000, o, h, l, c, v]


def _base_rows(n=40):
    rows = []
    for i in range(n):
        center = 100.0 + (i % 4 - 1.5) * 0.03
        rows.append(k(i, center, center + 0.22, center - 0.22, center + (0.05 if i % 2 == 0 else -0.04), 100))
    return rows


def test_bullish_impulse_waits_for_pullback_and_reaction_before_confirmation():
    rows = _base_rows(40)
    rows += [
        k(40, 99.95, 100.25, 99.85, 100.05, 100),
        k(41, 100.05, 102.30, 100.00, 102.10, 260),  # displacement + BOS
        k(42, 101.00, 102.20, 100.85, 101.70, 160),  # leaves bullish FVG vs bar 40 high
        k(43, 101.65, 101.80, 101.05, 101.20, 120),
        k(44, 101.15, 101.30, 100.55, 100.68, 130),  # pulls back into FVG
        k(45, 100.62, 101.35, 100.58, 101.22, 180),  # bullish reaction
        k(46, 101.20, 101.55, 101.05, 101.45, 140),
        k(47, 101.45, 101.70, 101.30, 101.60, 120),
        k(48, 101.60, 101.75, 101.40, 101.55, 110),
        k(49, 101.55, 101.65, 101.45, 101.58, 100),  # forming candle ignored
    ]
    result = build_impulse_pullback_confirmation(rows)
    assert result["available"] is True
    assert result["direction"] == "LONG"
    assert result["phase"] == "CONFIRMED"
    assert result["pending_zone"]["type"] == "BULLISH_FVG"
    assert result["pending_zone"]["touched"] is True
    assert result["reaction"]["confirmed"] is True
    assert result["paper_candidate"] is True
    assert result["policy"]["do_not_chase"] is True


def test_strong_impulse_without_retest_is_no_chase_wait_state():
    rows = _base_rows(40)
    rows += [
        k(40, 99.95, 100.25, 99.85, 100.05, 100),
        k(41, 100.05, 102.30, 100.00, 102.10, 260),
        k(42, 101.00, 102.40, 100.85, 102.20, 180),
        k(43, 102.15, 102.55, 102.00, 102.45, 150),
        k(44, 102.45, 102.75, 102.30, 102.65, 140),
        k(45, 102.65, 102.95, 102.55, 102.80, 130),
        k(46, 102.80, 103.10, 102.70, 103.00, 130),
        k(47, 103.00, 103.20, 102.90, 103.10, 120),
        k(48, 103.10, 103.25, 103.00, 103.15, 110),
        k(49, 103.15, 103.30, 103.05, 103.20, 100),
    ]
    result = build_impulse_pullback_confirmation(rows)
    assert result["direction"] == "LONG"
    assert result["phase"] in {"WAIT_PULLBACK", "WAIT_PULLBACK_NO_CHASE"}
    assert result["reaction"]["confirmed"] is False
