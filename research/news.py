"""Nieuws ophalen via de Alpaca News API (Benzinga-bron, gratis bij je account).

Extra bronnen (Finnhub/NewsAPI/RSS) kun je hier later pluggen: lever gewoon
dicts met dezelfde velden aan.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from trailing_stop.config import Settings


def make_news_client(settings: Settings) -> NewsClient:
    return NewsClient(settings.api_key, settings.secret_key)


def fetch_news(client: NewsClient, symbol: str, *, days_back: int = 2,
               limit: int = 50) -> list[dict]:
    """Haal recente artikelen voor één symbool op, genormaliseerd voor de DB."""
    start = datetime.now(timezone.utc) - timedelta(days=days_back)
    req = NewsRequest(
        symbols=symbol,
        start=start,
        limit=limit,
        include_content=False,
        sort="desc",
    )
    resp = client.get_news(req)
    articles = resp.data.get("news", [])
    fetched_at = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    for a in articles:
        published = getattr(a, "created_at", None)
        rows.append({
            "id": str(a.id),
            "symbol": symbol,
            "headline": a.headline,
            "summary": getattr(a, "summary", "") or "",
            "source": getattr(a, "source", "") or "",
            "url": getattr(a, "url", "") or "",
            "published": published.isoformat() if hasattr(published, "isoformat") else str(published),
            "fetched_at": fetched_at,
        })
    return rows
