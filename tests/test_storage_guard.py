import json

from app.services.storage_guard import SCANNER_JSON_PROXY, compact_market_snapshot_bundle


def test_compacts_market_snapshot_bundle_but_preserves_core_fields():
    payload = {
        "score": {
            "direction": "SHORT", "state": "WATCH", "setup_score": 74.2,
            "risk_score": 12, "current_price": 826.51,
            "entry_low": 820, "entry_high": 828, "stop_loss": 835,
            "tp1": 810, "tp2": 800, "tp3": 790,
            "metrics": {"oi_change_pct": 1.2, "relative_volume": 2.1, "huge_unused": "x" * 50000},
            "components": {"oi": 15},
            "huge_nested": {"blob": "y" * 50000},
        },
        "local_score_before_coinglass": {"direction": "SHORT", "setup_score": 70, "metrics": {}},
        "ticker": {"symbol": "ZECUSDT", "priceChangePercent": -4.7, "quoteVolume": 123456, "extra": "z" * 10000},
        "btc_context": {"trend": "BEARISH"},
        "market_data_source": "BINANCE",
        "coinglass": {"available": True, "huge": "q" * 50000},
        "prediction": {"type": "dump", "direction": "SHORT", "phase": "PREACTIVACION", "preactivation_score": 74, "sequence": {"chase_risk": False}, "huge": "a" * 50000},
    }
    compact = compact_market_snapshot_bundle(payload)
    assert compact["score"]["direction"] == "SHORT"
    assert compact["score"]["metrics"]["oi_change_pct"] == 1.2
    assert "huge_nested" not in compact["score"]
    assert "huge_unused" not in compact["score"]["metrics"]
    assert compact["ticker"]["symbol"] == "ZECUSDT"
    assert len(json.dumps(compact)) < 10000


def test_proxy_leaves_normal_json_untouched():
    ordinary = {"explodex_heart": {"direction": "LONG"}, "reason": "keep me"}
    encoded = SCANNER_JSON_PROXY.dumps(ordinary)
    assert json.loads(encoded) == ordinary
