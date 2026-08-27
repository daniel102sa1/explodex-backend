from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import settings


BRIDGE_MAX_AGE_SECONDS = 20
BRIDGE_SIGNATURE_WINDOW_SECONDS = 90


@dataclass
class BinanceBridgeCache:
    received_at_ms: int | None = None
    source_timestamp_ms: int | None = None
    positions: list[dict[str, Any]] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    source: str = "LOCAL_BRIDGE"
    last_error: str | None = None

    def clear(self) -> None:
        self.received_at_ms = None
        self.source_timestamp_ms = None
        self.positions = []
        self.open_orders = []
        self.last_error = None

    def store(self, *, source_timestamp_ms: int, positions: list[dict[str, Any]], open_orders: list[dict[str, Any]]) -> None:
        self.received_at_ms = int(time.time() * 1000)
        self.source_timestamp_ms = int(source_timestamp_ms)
        self.positions = [dict(row) for row in positions if isinstance(row, dict)]
        self.open_orders = [dict(row) for row in open_orders if isinstance(row, dict)]
        self.last_error = None

    @property
    def age_seconds(self) -> float | None:
        if self.received_at_ms is None:
            return None
        return max(0.0, (int(time.time() * 1000) - self.received_at_ms) / 1000.0)

    @property
    def fresh(self) -> bool:
        age = self.age_seconds
        return age is not None and age <= BRIDGE_MAX_AGE_SECONDS

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": self.received_at_ms is not None,
            "fresh": self.fresh,
            "age_seconds": round(self.age_seconds, 2) if self.age_seconds is not None else None,
            "received_at_ms": self.received_at_ms,
            "source_timestamp_ms": self.source_timestamp_ms,
            "position_count": len(self.positions),
            "open_order_count": len(self.open_orders),
            "source": self.source,
            "last_error": self.last_error,
        }


binance_bridge_cache = BinanceBridgeCache()


def canonical_bridge_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_bridge_signature(timestamp_ms: int, payload: dict[str, Any], secret: str) -> str:
    message = str(int(timestamp_ms)).encode("utf-8") + b"." + canonical_bridge_body(payload)
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_bridge_request(*, api_key: str, timestamp_ms: int, signature: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    configured_key = settings.binance_user_api_key.strip()
    configured_secret = settings.binance_user_api_secret.strip()
    if not configured_key or not configured_secret:
        return False, "Binance user credentials are not configured on the backend."
    if not hmac.compare_digest(str(api_key or ""), configured_key):
        return False, "Bridge API key does not match the configured Binance user key."
    now_ms = int(time.time() * 1000)
    if abs(now_ms - int(timestamp_ms)) > BRIDGE_SIGNATURE_WINDOW_SECONDS * 1000:
        return False, "Bridge timestamp is outside the allowed signing window."
    expected = build_bridge_signature(int(timestamp_ms), payload, configured_secret)
    if not hmac.compare_digest(str(signature or ""), expected):
        return False, "Bridge signature is invalid."
    return True, None
