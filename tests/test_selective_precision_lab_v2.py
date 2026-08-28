from datetime import datetime, timedelta, timezone

from app.services.selective_precision_lab import (
    chronological_split,
    evaluate_filter_grid,
    reliability_table,
    validate_train_candidate,
)


def _row(index: int, *, win: bool, fp: float = 80.0, locks: int = 6, master: str = "YES", catalyst: str = "NEUTRAL"):
    return {
        "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
        "fingerprint_score": fp,
        "locks_passed": locks,
        "master_state": master,
        "catalyst_state": catalyst,
        "trade_class": "TRADE_NOW",
        "barrier_hit": "TP1" if win else "STOP",
        "directional_return_pct": 0.8 if win else -0.5,
        "mfe_pct": 1.0 if win else 0.3,
        "mae_pct": -0.2 if win else -0.8,
    }


def test_chronological_split_keeps_newest_rows_for_holdout():
    rows = [_row(i, win=True) for i in range(10)]
    train, holdout = chronological_split(rows, 0.70)
    assert len(train) == 7
    assert len(holdout) == 3
    assert train[-1]["observed_at"] < holdout[0]["observed_at"]


def test_overfit_candidate_fails_when_holdout_collapses():
    train_rows = [_row(i, win=(i % 5 != 0)) for i in range(60)]
    holdout_rows = [_row(100 + i, win=(i % 4 == 0)) for i in range(30)]
    grid = evaluate_filter_grid(train_rows)
    candidate = next(row for row in grid if row["min_fingerprint"] == 80 and row["min_locks"] == 6 and row["master_yes"] is True and row["catalyst_guard"] is False)
    validated = validate_train_candidate(candidate, holdout_rows)
    assert validated["train"]["precision_pct"] > validated["holdout"]["precision_pct"]
    assert validated["stable_out_of_sample"] is False


def test_stable_candidate_survives_chronological_holdout():
    train_rows = [_row(i, win=(i % 4 != 0)) for i in range(80)]
    holdout_rows = [_row(100 + i, win=(i % 4 != 0)) for i in range(40)]
    grid = evaluate_filter_grid(train_rows)
    candidate = next(row for row in grid if row["min_fingerprint"] == 80 and row["min_locks"] == 6 and row["master_yes"] is True and row["catalyst_guard"] is False)
    validated = validate_train_candidate(candidate, holdout_rows)
    assert validated["holdout"]["decided"] >= 20
    assert validated["holdout"]["avg_directional_return_pct"] > 0
    assert validated["stable_out_of_sample"] is True


def test_reliability_table_does_not_treat_score_as_probability():
    rows = [_row(i, win=(i % 2 == 0), fp=88.0) for i in range(20)]
    table = reliability_table(rows)
    bucket = next(row for row in table if row["fingerprint_bin"] == "85-89")
    assert bucket["decided"] == 20
    assert bucket["observed_tp1_first_pct"] == 50.0
    assert bucket["enough_sample"] is True
