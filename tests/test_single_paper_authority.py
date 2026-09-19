import inspect

from app import paper_trading_routes
from app.validation_scheduler import ValidationScheduler


def test_validation_scheduler_cannot_run_legacy_paper_executor():
    source = inspect.getsource(ValidationScheduler._loop)

    assert "run_validation_cycle" in source
    assert "run_paper_cycle_v2" not in source
    assert "paper_positions" not in source


def test_legacy_run_endpoint_delegates_to_canonical_fast_cycle():
    source = inspect.getsource(paper_trading_routes.run_cycle)

    assert "run_fast_paper_cycle" in source
    assert "run_paper_cycle_v2" not in source
