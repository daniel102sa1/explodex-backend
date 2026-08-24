from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any


class ScannerProgress:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.run_id: str | None = None
        self.status = "idle"
        self.phase = "idle"
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.universe_size = 0
        self.early_pool_size = 0
        self.deep_total = 0
        self.deep_completed = 0
        self.candidates_found = 0
        self.current_symbols: set[str] = set()
        self.recent_symbols: deque[str] = deque(maxlen=30)
        self.recent_results: deque[dict[str, Any]] = deque(maxlen=30)
        self.errors: deque[str] = deque(maxlen=10)

    def start(self, run_id: str) -> None:
        self.reset()
        self.run_id = run_id
        self.status = "running"
        self.phase = "loading_market"
        self.started_at = datetime.now(timezone.utc).isoformat()

    def set_universe(self, universe_size: int, early_pool_size: int, deep_total: int) -> None:
        self.universe_size = universe_size
        self.early_pool_size = early_pool_size
        self.deep_total = deep_total
        self.phase = "deep_analysis"

    def symbol_started(self, symbol: str) -> None:
        self.current_symbols.add(symbol)
        self.recent_symbols.appendleft(symbol)

    def symbol_finished(self, symbol: str, score: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.current_symbols.discard(symbol)
        self.deep_completed += 1
        if error:
            self.errors.appendleft(f"{symbol}: {error}"[:400])
            return
        if score:
            if score.get("state") != "NO_TRADE":
                self.candidates_found += 1
            metrics = score.get("metrics", {}) or {}
            self.recent_results.appendleft({
                "symbol": symbol,
                "direction": score.get("direction"),
                "state": score.get("state"),
                "setup_score": score.get("setup_score"),
                "risk_score": score.get("risk_score"),
                "price": score.get("current_price"),
                "confirmations": metrics.get("confirmations"),
                "reject_reasons": metrics.get("reject_reasons", []),
                "oi_change_pct": metrics.get("oi_change_pct"),
                "taker_ratio": metrics.get("taker_avg_3"),
                "relative_volume": metrics.get("relative_volume"),
                "futures_delta_ratio": metrics.get("futures_delta_ratio"),
                "spot_delta_ratio": metrics.get("spot_delta_ratio"),
                "order_book_imbalance": metrics.get("order_book_imbalance"),
                "trend_15m": metrics.get("trend_15m"),
                "trend_1h": metrics.get("trend_1h"),
            })

    def finish(self, status: str = "completed") -> None:
        self.status = status
        self.phase = "finished" if status == "completed" else "failed"
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.current_symbols.clear()

    def as_dict(self) -> dict[str, Any]:
        progress_pct = (self.deep_completed / self.deep_total * 100) if self.deep_total else 0.0
        return {
            "run_id": self.run_id,
            "status": self.status,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "universe_size": self.universe_size,
            "early_pool_size": self.early_pool_size,
            "deep_total": self.deep_total,
            "deep_completed": self.deep_completed,
            "progress_pct": round(progress_pct, 1),
            "candidates_found": self.candidates_found,
            "current_symbols": sorted(self.current_symbols),
            "recent_symbols": list(self.recent_symbols),
            "recent_results": list(self.recent_results),
            "errors": list(self.errors),
        }


scanner_progress = ScannerProgress()
