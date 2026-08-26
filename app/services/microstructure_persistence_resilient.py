from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_QUEUE_MAX = 2000
_INSTALLED = False

_STATS: dict[str, Any] = {
    "enqueued_total": 0,
    "inserted_total": 0,
    "retry_batches_total": 0,
    "requeued_total": 0,
    "dropped_total": 0,
    "flush_failures_total": 0,
    "last_error": None,
    "last_flush_ok": None,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ResilientPendingQueue(deque):
    """Bounded queue that makes overflow explicit instead of silently discarding data."""

    def append(self, item: Any) -> None:  # type: ignore[override]
        if len(self) >= _QUEUE_MAX:
            self.popleft()
            _STATS["dropped_total"] = int(_STATS["dropped_total"] or 0) + 1
        super().append(item)
        _STATS["enqueued_total"] = int(_STATS["enqueued_total"] or 0) + 1

    def appendleft_retry(self, item: Any) -> None:
        # Failed batches are older than observations queued while the DB call was in
        # progress, so preserve them first. If full, discard the newest queued item.
        if len(self) >= _QUEUE_MAX:
            self.pop()
            _STATS["dropped_total"] = int(_STATS["dropped_total"] or 0) + 1
        super().appendleft(item)


def persistence_queue_status() -> dict[str, Any]:
    from app.services import sequential_microstructure as module

    queue = getattr(module, "_PENDING", None)
    return {
        "pending": len(queue) if queue is not None else 0,
        "capacity": _QUEUE_MAX,
        **_STATS,
    }


async def flush_pending_snapshots_resilient(db: AsyncSession, limit: int = 500) -> dict[str, Any]:
    """Persist a batch transactionally and requeue it if PostgreSQL fails.

    The original implementation removed snapshots from memory before commit. If an
    insert/commit failed, that whole batch could disappear. This version rolls back
    and restores the complete batch at the front of the queue, preserving order.
    """
    from app.services import sequential_microstructure as module

    queue: ResilientPendingQueue = module._PENDING
    batch: list[tuple[str, dict[str, Any]]] = []
    batch_limit = max(1, min(limit, 1000))
    while queue and len(batch) < batch_limit:
        batch.append(queue.popleft())

    if not batch:
        return {
            "queued": 0,
            "inserted": 0,
            "requeued": 0,
            **persistence_queue_status(),
        }

    try:
        for symbol, state in batch:
            await db.execute(
                text(
                    """
                    INSERT INTO microstructure_snapshots (
                        symbol, observed_at, bid_price, ask_price, bid_size, ask_size, bid_depth,
                        ask_depth, mid_price, imbalance, current_price, futures_delta, source
                    ) VALUES (
                        :symbol, :observed_at, :bid_price, :ask_price, :bid_size, :ask_size, :bid_depth,
                        :ask_depth, :mid_price, :imbalance, :current_price, :futures_delta, 'LIVE_CONTEXT'
                    )
                    """
                ),
                {
                    "symbol": symbol,
                    "observed_at": datetime.fromtimestamp(_f(state.get("ts")), tz=timezone.utc),
                    "bid_price": state["bid_price"],
                    "ask_price": state["ask_price"],
                    "bid_size": state["bid_size"],
                    "ask_size": state["ask_size"],
                    "bid_depth": state["bid_depth"],
                    "ask_depth": state["ask_depth"],
                    "mid_price": state["mid"],
                    "imbalance": state["imbalance"],
                    "current_price": state["price"],
                    "futures_delta": state["futures_delta"],
                },
            )
        await db.commit()
    except Exception as exc:
        try:
            await db.rollback()
        finally:
            # reverse + appendleft restores the original batch ordering.
            for item in reversed(batch):
                queue.appendleft_retry(item)
            _STATS["retry_batches_total"] = int(_STATS["retry_batches_total"] or 0) + 1
            _STATS["requeued_total"] = int(_STATS["requeued_total"] or 0) + len(batch)
            _STATS["flush_failures_total"] = int(_STATS["flush_failures_total"] or 0) + 1
            _STATS["last_error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            _STATS["last_flush_ok"] = False
        return {
            "queued": len(batch),
            "inserted": 0,
            "requeued": len(batch),
            "error": _STATS["last_error"],
            **persistence_queue_status(),
        }

    _STATS["inserted_total"] = int(_STATS["inserted_total"] or 0) + len(batch)
    _STATS["last_error"] = None
    _STATS["last_flush_ok"] = True
    return {
        "queued": len(batch),
        "inserted": len(batch),
        "requeued": 0,
        **persistence_queue_status(),
    }


def install_microstructure_persistence_hardening() -> None:
    """Install resilient queue/flush before runtime imports the original callables."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services import sequential_microstructure as module

    previous = list(getattr(module, "_PENDING", []))
    queue = ResilientPendingQueue()
    # Existing items are migration, not new observations, so avoid inflating enqueued_total.
    for item in previous[-_QUEUE_MAX:]:
        deque.append(queue, item)
    if len(previous) > _QUEUE_MAX:
        _STATS["dropped_total"] = len(previous) - _QUEUE_MAX

    module._PENDING = queue
    module.flush_pending_snapshots = flush_pending_snapshots_resilient
    module.persistence_queue_status = persistence_queue_status
    _INSTALLED = True
