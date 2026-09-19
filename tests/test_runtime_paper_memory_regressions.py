import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from app.services.paper_orders import _insert_position_orders
from app.services.verdict_memory import capture_enter_verdicts


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def mappings(self):
        return _Mappings(self._rows)


class _PaperOrderDB:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((statement, params or {}))
        return _Result()


class _VerdictDB:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.committed = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if "FROM signals s" in sql:
            return _Result([self.row])
        return _Result()

    async def commit(self):
        self.committed = True


def test_paper_order_json_literals_are_bound_as_metadata_not_sql_params():
    db = _PaperOrderDB()
    now = datetime.now(timezone.utc)
    row = {
        "signal_id": None,
        "id": 1,
        "symbol": "BTCUSDT",
        "side": "LONG",
        "quantity": Decimal("0.01"),
        "leverage": 2,
        "opened_at": now,
        "entry_price": Decimal("60000"),
        "stop_loss": Decimal("59000"),
        "take_profit": Decimal("62000"),
    }

    asyncio.run(_insert_position_orders(db, row))

    assert len(db.calls) == 3
    for statement, params in db.calls:
        bind_names = set(statement._bindparams.keys())
        assert "true" not in bind_names
        assert "metadata" in bind_names
        assert "metadata" in params


def test_verdict_memory_maps_signal_timestamp_and_price_to_insert_fields():
    observed = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
    source = {
        "signal_id": "62378994-cc75-4268-bde1-f5fd7875041d",
        "symbol_id": "0cdde2bc-a80a-400a-8f0e-bd2807cf83c2",
        "symbol": "SNDKUSDT",
        "created_at": observed,
        "direction": "LONG",
        "setup_score": 80,
        "risk_score": 30,
        "current_price": Decimal("50.5"),
        "entry_low": Decimal("50"),
        "entry_high": Decimal("51"),
        "stop_loss": Decimal("49"),
        "tp1": Decimal("53"),
        "tp2": Decimal("55"),
        "tp3": Decimal("57"),
        "reason": {},
    }
    db = _VerdictDB(source)

    result = asyncio.run(capture_enter_verdicts(db, limit=10))

    assert result == {"seen": 1, "inserted": 1}
    insert_calls = [(sql, params) for sql, params in db.calls if "INSERT INTO verdict_memory" in sql]
    assert len(insert_calls) == 1
    params = insert_calls[0][1]
    assert params["observed_at"] == observed
    assert params["entry_price"] == Decimal("50.5")
    assert db.committed is True
