from pathlib import Path


def test_signal_bridge_uses_not_valid_canonical_fk():
    source = Path("app/services/paper_signal_bridge.py").read_text(encoding="utf-8")
    assert "REFERENCES signals(id)" in source
    assert "ON DELETE SET NULL NOT VALID" in source
    assert "UPDATE paper_positions pp" not in source


def test_summary_repairs_schema_before_reading_portfolio():
    source = Path("app/paper_trading_routes.py").read_text(encoding="utf-8")
    summary_start = source.index('@router.get("/summary")')
    summary_block = source[summary_start:source.index('@router.get("/edge-lab")')]
    assert "await _ensure_paper_dependencies(db)" in summary_block
    assert "result = await paper_summary(db)" in summary_block
    assert summary_block.index("await _ensure_paper_dependencies(db)") < summary_block.index("result = await paper_summary(db)")


def test_dependencies_install_canonical_fk_after_base_schema():
    source = Path("app/paper_trading_routes.py").read_text(encoding="utf-8")
    dep_start = source.index("async def _ensure_paper_dependencies")
    dep_block = source[dep_start:source.index("async def _safe_component")]
    assert dep_block.index("await ensure_paper_schema(db)") < dep_block.index("await ensure_signal_fk(db)")
