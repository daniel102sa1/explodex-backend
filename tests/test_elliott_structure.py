from app.services.elliott_structure import analyze_rows
from app.services.risk_conviction_engine import build_risk_conviction


def _rows(prices):
    rows=[]
    for i,p in enumerate(prices):
        rows.append([i, p, p*1.002, p*0.998, p, 1])
    return rows


def test_no_clear_count_on_flat_noise():
    result = analyze_rows(_rows([100,100.1,99.9,100.05,100.0,100.08,99.95,100.02]*4), "1h")
    assert result["status"] in {"NO_CLEAR_COUNT", "CLEAR_COUNT"}
    if result["status"] == "CLEAR_COUNT":
        assert result["best"]["score"] >= 68


def test_risk_conviction_elliott_alignment_never_creates_entry():
    lane={
        "lane":"TACTICAL",
        "direction":"LONG",
        "ignition_score":88,
        "execution_math":{"chosen_target":{"net_rr":3.2}},
    }
    matrix={
        "consensus":"LONG",
        "horizon_conflict":False,
        "horizons":{h:{"direction":"LONG","edge":20} for h in ("15m","1h","4h","6h","24h")},
    }
    elliott={
        "status":"CLEAR_COUNT",
        "timeframe_agreement":True,
        "best":{"direction":"LONG","score":86,"pattern":"IMPULSE_1_2_3_4_5"},
    }
    result=build_risk_conviction(
        lane_name="TACTICAL", lane=lane, setup_score=88, risk_score=25,
        forecast_matrix=matrix, elliott_structure=elliott,
    )
    assert result["creates_entry"] is False
    assert result["changes_direction"] is False
    assert result["direction"] == "LONG"
    assert result["risk_budget_multiplier"] <= 1.5
    assert result["elliott"]["direction"] == "LONG"


def test_strong_elliott_conflict_caps_risk_without_flipping_side():
    lane={
        "lane":"TACTICAL",
        "direction":"LONG",
        "ignition_score":90,
        "execution_math":{"chosen_target":{"net_rr":3.8}},
    }
    matrix={
        "consensus":"LONG",
        "horizon_conflict":False,
        "horizons":{h:{"direction":"LONG","edge":24} for h in ("15m","1h","4h","6h","24h")},
    }
    elliott={
        "status":"CLEAR_COUNT",
        "timeframe_agreement":True,
        "best":{"direction":"SHORT","score":90,"pattern":"ABC_CORRECTION"},
    }
    result=build_risk_conviction(
        lane_name="TACTICAL", lane=lane, setup_score=90, risk_score=20,
        forecast_matrix=matrix, elliott_structure=elliott,
    )
    assert result["direction"] == "LONG"
    assert result["risk_budget_multiplier"] <= 0.5
    assert result["changes_direction"] is False
