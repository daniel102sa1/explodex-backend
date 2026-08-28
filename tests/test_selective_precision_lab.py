from app.services.selective_precision_lab import evaluate_filter_grid, summarize_targets


def _row(fp, locks, master, barrier, trade_class="TRADE_NOW"):
    return {
        "fingerprint_score": fp,
        "locks_passed": locks,
        "master_state": master,
        "barrier_hit": barrier,
        "trade_class": trade_class,
    }


def test_selective_grid_can_identify_high_precision_low_coverage_cohort():
    rows = []
    # 45 strong outcomes: 41 TP1, 4 STOP -> 91.1% precision.
    rows += [_row(82, 6, "YES", "TP1") for _ in range(41)]
    rows += [_row(82, 6, "YES", "STOP") for _ in range(4)]
    # Noisier broader cohort.
    rows += [_row(64, 4, "NO", "TP1", "WATCHLIST") for _ in range(20)]
    rows += [_row(64, 4, "NO", "STOP", "WATCHLIST") for _ in range(20)]

    grid = evaluate_filter_grid(rows)
    valid = [r for r in grid if r["enough_sample"] and r["precision_pct"] is not None]
    assert valid
    assert valid[0]["precision_pct"] >= 90.0
    targets = summarize_targets(grid)
    ninety = next(r for r in targets if r["target_precision_pct"] == 90.0)
    assert ninety["achieved_out_of_sample_proxy"] is True


def test_small_perfect_sample_is_not_treated_as_valid_precision():
    rows = [_row(90, 6, "YES", "TP1") for _ in range(10)]
    grid = evaluate_filter_grid(rows)
    assert not any(r["enough_sample"] for r in grid)
