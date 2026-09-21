from app.services.evidence_council import build_evidence_council


def _score():
    return {
        "direction": "LONG",
        "metrics": {
            "futures_delta_ratio": 0.18,
            "spot_delta_ratio": 0.14,
            "order_book_imbalance": 0.12,
            "oi_change_pct": 0.8,
        },
    }


def _prediction():
    return {
        "direction": "LONG",
        "sarpon_classic": {
            "available": True,
            "side": "LONG",
            "stage": "GREEN_CONFIRMATION",
            "score": 82,
            "murphy": {"structure_aligned": True},
            "compression_priority": {"stage": "ARMED_EARLY"},
        },
        "murphy_patterns": {
            "available": True,
            "aggregate_bias": "LONG",
            "top_pattern": {"name": "ASCENDING_TRIANGLE", "state": "FORMING", "confidence_score": 78},
        },
        "formula_brain": {
            "available": True,
            "direction": "LONG",
            "consensus_score": 74,
            "regime": "TREND_PERSISTENT",
            "risk_flags": [],
        },
    }


def test_council_counts_independent_domains_once_and_keeps_shadow_separate():
    heart = {
        "direction": "LONG",
        "macro_cycle": {"available": True, "bias": "LONG", "confidence_score": 76, "state": "ACCUMULATION_LATE"},
        "quant_brain": {"available": True, "directional_edge": 34, "evidence_strength": 70, "stance": "SUPPORT"},
        "trajectory_forecast": {"direction": "LONG", "trajectory_score": 79, "direction_edge": 28, "horizon": "8-24h", "aligned_htf_frames": 3},
        "action_decision": {"should_enter": True, "action": "ENTRAR_LONG"},
        "ignition": {"score": 84, "stage": "IGNITING"},
    }
    result = build_evidence_council(score=_score(), prediction=_prediction(), heart=heart)

    assert result["independent_support_count"] >= 5
    assert result["independent_conflict_count"] == 0
    assert "murphy_patterns" in result["research_shadow_domains"]
    assert "formula_brain" in result["research_shadow_domains"]
    assert result["policy"]["can_create_entry"] is False
    assert result["policy"]["one_vote_per_independent_domain"] is True


def test_council_conflicts_reduce_risk_but_do_not_become_hard_veto():
    heart = {
        "direction": "LONG",
        "macro_cycle": {"available": True, "bias": "SHORT", "confidence_score": 82, "state": "DISTRIBUTION_LATE"},
        "quant_brain": {"available": True, "directional_edge": -45, "evidence_strength": 75, "stance": "CONFLICT"},
        "trajectory_forecast": {"direction": "SHORT", "trajectory_score": 76, "direction_edge": 24, "horizon": "8-24h", "aligned_htf_frames": 2},
        "action_decision": {"should_enter": False, "action": "ESPERAR"},
        "ignition": {"score": 50, "stage": "LOADING"},
    }
    score = _score()
    score["metrics"] = {"futures_delta_ratio": -0.12, "spot_delta_ratio": -0.10, "order_book_imbalance": -0.08, "oi_change_pct": 0.5}
    result = build_evidence_council(score=score, prediction=_prediction(), heart=heart)

    assert result["independent_conflict_count"] >= 3
    assert result["risk_multiplier_recommendation"] <= 0.55
    assert result["policy"]["can_override_hard_safety"] is False
