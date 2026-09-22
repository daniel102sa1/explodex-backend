import inspect

from app.services.edge_engine import label_due_observations


def test_edge_labeler_prioritizes_recent_due_rows():
    source = inspect.getsource(label_due_observations)
    assert "ORDER BY eo.due_at DESC" in source
