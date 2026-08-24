from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote_plus

import httpx

from app.config import settings


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()

POSITIVE_WORDS = {
    "approval", "approved", "adoption", "adopts", "adopted", "partnership",
    "partners", "launch", "launched", "upgrade", "growth", "surge", "record",
    "integration", "listing", "listed", "bullish", "rally", "breakout", "inflows",
    "investment", "invests", "expands", "expansion", "milestone", "success",
}

NEGATIVE_WORDS = {
    "hack", "hacked", "exploit", "lawsuit", "delist", "delisted", "ban", "banned",
    "investigation", "outflow", "outflows", "crash", "plunge", "fraud", "scam",
    "breach", "shutdown", "bankruptcy", "liquidation", "liquidations", "bearish",
    "attack", "stolen", "theft", "warning", "probe", "fine", "fined",
}

KNOWN_NAMES = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "BNB": "BNB",
    "XRP": "XRP",
    "ADA": "Cardano",
    "DOGE": "Dogecoin",
    "AVAX": "Avalanche",
    "LINK": "Chainlink",
    "DOT": "Polkadot",
    "SUI": "Sui",
    "APT": "Aptos",
    "ARB": "Arbitrum",
    "OP": "Optimism",
    "FET": "Fetch.ai",
    "FIL": "Filecoin",
    "ZEC": "Zcash",
    "LTC": "Litecoin",
    "ATOM": "Cosmos",
    "NEAR": "NEAR Protocol",
    "ICP": "Internet Computer",
    "TRX": "TRON",
    "TON": "Toncoin",
}


def _base_asset(symbol: str) -> str:
    symbol = symbol.upper().strip()
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def _query_for_symbol(symbol: str) -> str:
    base = _base_asset(symbol)
    name = KNOWN_NAMES.get(base)
    if name and name.upper() != base:
        return f'"{name}" crypto OR "{base}" cryptocurrency'
    return f'"{base}" cryptocurrency'


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z]+", text.lower())


def _headline_score(title: str) -> int:
    words = set(_tokenize(title))
    positive = len(words & POSITIVE_WORDS)
    negative = len(words & NEGATIVE_WORDS)
    return positive - negative


async def _fetch_google_news_rss(query: str, timeout_seconds: float = 8.0) -> list[dict[str, str]]:
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    headers = {"User-Agent": "ExplodeX/0.5 market research bot"}
    async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()

    root = ET.fromstring(response.text)
    output: list[dict[str, str]] = []
    for item in root.findall("./channel/item")[: settings.news_max_headlines]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        source = ""
        source_node = item.find("source")
        if source_node is not None and source_node.text:
            source = source_node.text.strip()
        if title:
            output.append(
                {
                    "title": title,
                    "link": link,
                    "published": published,
                    "source": source,
                }
            )
    return output


async def news_context_for_symbol(symbol: str) -> dict[str, Any]:
    if not settings.news_enabled:
        return {
            "enabled": False,
            "symbol": symbol.upper(),
            "sentiment": "UNAVAILABLE",
            "score_adjustment": 0.0,
            "headlines": [],
            "note": "News enrichment is disabled.",
        }

    cache_key = symbol.upper()
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached and now < cached[0]:
        return cached[1]

    async with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(cache_key)
        if cached and now < cached[0]:
            return cached[1]

        try:
            headlines = await _fetch_google_news_rss(_query_for_symbol(cache_key))
        except Exception as exc:
            value = {
                "enabled": True,
                "symbol": cache_key,
                "sentiment": "UNAVAILABLE",
                "raw_sentiment_score": 0,
                "score_adjustment": 0.0,
                "headline_count": 0,
                "headlines": [],
                "error": str(exc)[:300],
                "note": "News source unavailable; no score penalty or bonus applied.",
            }
            _CACHE[cache_key] = (time.monotonic() + 120, value)
            return value

        scored: list[dict[str, Any]] = []
        raw = 0
        for headline in headlines:
            score = _headline_score(headline["title"])
            raw += score
            scored.append({**headline, "headline_sentiment": score})

        if raw >= 3:
            sentiment = "POSITIVE"
        elif raw <= -3:
            sentiment = "NEGATIVE"
        else:
            sentiment = "NEUTRAL"

        # News is deliberately capped. It may confirm/block a setup, not create one by itself.
        adjustment = max(-5.0, min(5.0, raw * 1.25))

        value = {
            "enabled": True,
            "symbol": cache_key,
            "sentiment": sentiment,
            "raw_sentiment_score": raw,
            "score_adjustment": round(adjustment, 2),
            "headline_count": len(scored),
            "headlines": scored[:5],
            "note": "Headline sentiment is a secondary filter, not a trade signal and not a certainty estimate.",
        }
        _CACHE[cache_key] = (time.monotonic() + settings.news_cache_ttl_seconds, value)
        return value
