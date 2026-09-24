from app.services.scoring import _order_book_metrics


def test_order_book_metrics_compare_depth_at_fixed_bps_from_mid():
    book = {
        "bids": [
            ["99.99", "10"],
            ["99.90", "20"],
            ["99.70", "50"],
            ["99.00", "100"],
        ],
        "asks": [
            ["100.01", "5"],
            ["100.10", "10"],
            ["100.30", "40"],
            ["101.00", "100"],
        ],
    }
    result = _order_book_metrics(book)
    assert result["spread_bps"] > 0
    assert result["bid_depth_10bps_usd"] > 0
    assert result["ask_depth_10bps_usd"] > 0
    assert result["bid_depth_50bps_usd"] >= result["bid_depth_10bps_usd"]
    assert result["ask_depth_50bps_usd"] >= result["ask_depth_10bps_usd"]
    assert result["imbalance"] == result["imbalance_25bps"]
