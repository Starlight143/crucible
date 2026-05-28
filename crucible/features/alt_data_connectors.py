"""
features/alt_data_connectors.py
=================================
Alternative data source connectors for news sentiment, Reddit social signals,
and economic calendar events.

Three sub-connectors:
* NewsAPI — fetches headlines, computes sentiment via word lists (key optional).
* Reddit  — public JSON endpoint (/r/sub.json?limit=25), no auth required.
* Economic calendar — FRED API observations for high-impact macro series, with
  a static fallback of known US release dates.

Environment variables
---------------------
ALT_DATA_SYMBOL           Ticker/topic to search for (default: 'SPY').
NEWS_API_KEY              NewsAPI.org key (optional — skipped if absent).
ALT_DATA_NEWSAPI_ENABLED  '1' to enable NewsAPI connector (default: '1').
ALT_DATA_REDDIT_ENABLED   '1' to enable Reddit connector (default: '1').
ALT_DATA_FRED_ENABLED     '1' to enable economic calendar (default: '1').
FRED_API_KEY              FRED API key (optional for higher rate limits).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from crucible.feature_registry import BaseFeature, FeatureConfig, FeatureResult, register

# ---------------------------------------------------------------------------
# Sentiment word lists
# ---------------------------------------------------------------------------

_POSITIVE_WORDS = frozenset([
    'bullish', 'rally', 'surge', 'beat', 'record', 'growth', 'profit',
    'gain', 'rise', 'up', 'positive', 'strong', 'buy', 'upgrade',
    'outperform', 'recovery', 'boom', 'momentum',
])
_NEGATIVE_WORDS = frozenset([
    'bearish', 'crash', 'drop', 'miss', 'loss', 'decline', 'fall',
    'down', 'negative', 'weak', 'sell', 'downgrade', 'underperform',
    'recession', 'slump', 'correction', 'risk', 'concern',
])


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _env(name: str, default: str = '') -> str:
    """Return stripped environment variable or default."""
    return os.environ.get(name, default).strip()


def _fetch_url(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 10,
) -> Tuple[int, str]:
    """Perform a GET request and return (http_status, body_text).

    Returns (0, '') on any network / timeout error.
    The response body is capped at 64 KiB to prevent memory exhaustion.
    """
    req = urllib.request.Request(
        url,
        headers=headers or {'User-Agent': 'Crucible/1.0'},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(65536).decode('utf-8', errors='replace')
    except urllib.error.HTTPError as exc:
        return exc.code, ''
    except Exception:
        return 0, ''


def _sentiment_score(text: str) -> Tuple[float, int, int, int]:
    """Compute a naive bag-of-words sentiment score.

    Returns
    -------
    (score, positive_count, negative_count, total_sentiment_words)
    score is in [-1, +1]; 0.0 when no sentiment words are found.
    """
    words = re.findall(r'[a-z]+', text.lower())
    pos = sum(1 for w in words if w in _POSITIVE_WORDS)
    neg = sum(1 for w in words if w in _NEGATIVE_WORDS)
    total = pos + neg
    score = (pos - neg) / total if total > 0 else 0.0
    return score, pos, neg, total


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class NewsSentimentResult:
    """Aggregated sentiment from NewsAPI headlines."""

    symbol: str
    score: float = 0.0
    articles_analyzed: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    error: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'score': round(self.score, 4),
            'articles_analyzed': self.articles_analyzed,
            'positive': self.positive,
            'negative': self.negative,
            'neutral': self.neutral,
            'error': self.error,
        }


@dataclass
class RedditSignalsResult:
    """Aggregated Reddit mention signals."""

    symbol: str
    mentions_24h: int = 0
    avg_upvotes: float = 0.0
    top_post_title: str = ''
    error: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'mentions_24h': self.mentions_24h,
            'avg_upvotes': round(self.avg_upvotes, 1),
            'top_post_title': self.top_post_title[:120],
            'error': self.error,
        }


@dataclass
class EconEvent:
    """A single economic calendar event."""

    date: str
    event: str
    impact: str

    def to_dict(self) -> Dict[str, str]:
        return {'date': self.date, 'event': self.event, 'impact': self.impact}


# ---------------------------------------------------------------------------
# Static economic calendar fallback (when FRED key is absent)
# ---------------------------------------------------------------------------

_STATIC_ECON_EVENTS: List[EconEvent] = [
    EconEvent('2024-01-12', 'CPI Release', 'HIGH'),
    EconEvent('2024-01-26', 'GDP Advance', 'HIGH'),
    EconEvent('2024-02-02', 'Non-Farm Payrolls', 'HIGH'),
    EconEvent('2024-02-13', 'CPI Release', 'HIGH'),
    EconEvent('2024-03-20', 'FOMC Rate Decision', 'HIGH'),
    EconEvent('2024-04-10', 'CPI Release', 'HIGH'),
    EconEvent('2024-05-01', 'FOMC Rate Decision', 'HIGH'),
    EconEvent('2024-06-12', 'FOMC Rate Decision', 'HIGH'),
]


# ---------------------------------------------------------------------------
# Sub-connectors
# ---------------------------------------------------------------------------

def fetch_news_sentiment(symbol: str, api_key: str) -> NewsSentimentResult:
    """Fetch up to 20 NewsAPI headlines for *symbol* and score sentiment.

    If *api_key* is empty, returns immediately with an informational error
    field; the feature continues without news data.
    """
    result = NewsSentimentResult(symbol=symbol)
    if not api_key:
        result.error = 'NEWS_API_KEY not set'
        return result

    url = (
        'https://newsapi.org/v2/everything'
        f'?q={urllib.parse.quote(symbol)}'
        '&language=en&pageSize=20&sortBy=publishedAt'
        f'&apiKey={api_key}'
    )
    status, body = _fetch_url(url)
    if status != 200:
        result.error = f'HTTP {status}'
        return result

    try:
        data = json.loads(body)
        articles = data.get('articles', [])
        scores: List[Tuple[float, int, int]] = []
        for art in articles:
            text = (art.get('title') or '') + ' ' + (art.get('description') or '')
            s, p, n, _t = _sentiment_score(text)
            scores.append((s, p, n))
            if p > n:
                result.positive += 1
            elif n > p:
                result.negative += 1
            else:
                result.neutral += 1
        result.articles_analyzed = len(scores)
        result.score = sum(s for s, _, _ in scores) / len(scores) if scores else 0.0
    except (json.JSONDecodeError, KeyError):
        result.error = 'Parse error'

    return result


def fetch_reddit_signals(symbol: str) -> RedditSignalsResult:
    """Search finance-focused subreddits for *symbol* mentions in the last day.

    Uses the unauthenticated Reddit JSON search endpoint; no API key required.
    Searches wallstreetbets, investing, algotrading, and stocks.
    """
    result = RedditSignalsResult(symbol=symbol)
    subreddits = ['wallstreetbets', 'investing', 'algotrading', 'stocks']
    total_mentions = 0
    total_upvotes = 0
    top_title = ''
    top_score = -1

    for sub in subreddits:
        url = (
            f'https://www.reddit.com/r/{sub}/search.json'
            f'?q={urllib.parse.quote(symbol)}&restrict_sr=1&limit=10&sort=new&t=day'
        )
        status, body = _fetch_url(
            url,
            headers={'User-Agent': 'Crucible-Research/1.0'},
        )
        if status != 200:
            continue
        try:
            data = json.loads(body)
            posts = data.get('data', {}).get('children', [])
            for post in posts:
                pd = post.get('data', {})
                title: str = pd.get('title', '')
                ups = int(pd.get('ups', 0))
                if symbol.upper() in title.upper():
                    total_mentions += 1
                    total_upvotes += ups
                    if ups > top_score:
                        top_score = ups
                        top_title = title
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    result.mentions_24h = total_mentions
    result.avg_upvotes = total_upvotes / total_mentions if total_mentions > 0 else 0.0
    result.top_post_title = top_title
    return result


def fetch_economic_calendar() -> List[EconEvent]:
    """Return a list of macro economic events.

    When FRED_API_KEY is set, supplements the static list with the 2 most
    recent observations for CPI, GDP, and UNRATE series from the FRED API.
    Falls back to the static list on any error or missing key.
    """
    fred_key = _env('FRED_API_KEY')
    if not fred_key:
        return list(_STATIC_ECON_EVENTS)

    series_ids = ['CPIAUCSL', 'GDP', 'UNRATE', 'FEDFUNDS', 'VIXCLS']
    events: List[EconEvent] = list(_STATIC_ECON_EVENTS)

    for sid in series_ids[:3]:
        url = (
            'https://api.stlouisfed.org/fred/series/observations'
            f'?series_id={sid}&limit=5&sort_order=desc'
            f'&api_key={fred_key}&file_type=json'
        )
        status, body = _fetch_url(url)
        if status == 200:
            try:
                data = json.loads(body)
                for obs in data.get('observations', [])[:2]:
                    events.append(
                        EconEvent(
                            date=obs.get('date', ''),
                            event=f'FRED: {sid}',
                            impact='MEDIUM',
                        )
                    )
            except (json.JSONDecodeError, KeyError):
                pass

    return events


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def prepare_alt_data(run_dir: str) -> Dict[str, Any]:
    """Run all enabled alt-data connectors, write ``alt_data_report.json``.

    Parameters
    ----------
    run_dir:
        Path to the current pipeline run directory.

    Returns
    -------
    Dict with keys: symbol, news_sentiment, reddit_signals,
    economic_events, overall_sentiment, overall_sentiment_score.
    """
    symbol = _env('ALT_DATA_SYMBOL', 'SPY')
    news_enabled = _env('ALT_DATA_NEWSAPI_ENABLED', '1').lower() not in ('0', 'false', 'no', 'off')
    reddit_enabled = _env('ALT_DATA_REDDIT_ENABLED', '1').lower() not in ('0', 'false', 'no', 'off')
    fred_enabled = _env('ALT_DATA_FRED_ENABLED', '1').lower() not in ('0', 'false', 'no', 'off')
    news_api_key = _env('NEWS_API_KEY')

    news_result = (
        fetch_news_sentiment(symbol, news_api_key)
        if news_enabled
        else NewsSentimentResult(symbol=symbol, error='disabled')
    )
    reddit_result = (
        fetch_reddit_signals(symbol)
        if reddit_enabled
        else RedditSignalsResult(symbol=symbol, error='disabled')
    )
    econ_events = fetch_economic_calendar() if fred_enabled else []

    # Compute a blended overall sentiment score
    total_sentiment = 0.0
    count = 0
    if news_result.articles_analyzed > 0:
        total_sentiment += news_result.score
        count += 1
    if reddit_result.mentions_24h > 0:
        # Normalise avg_upvotes to [-1, +1] range with a 1000-upvote ceiling
        reddit_score = max(0.0, min(1.0, reddit_result.avg_upvotes / 1000.0))
        total_sentiment += reddit_score
        count += 1

    overall_raw = total_sentiment / count if count > 0 else 0.0
    if overall_raw > 0.1:
        overall = 'bullish'
    elif overall_raw < -0.1:
        overall = 'bearish'
    else:
        overall = 'neutral'

    report: Dict[str, Any] = {
        'symbol': symbol,
        'news_sentiment': news_result.to_dict(),
        'reddit_signals': reddit_result.to_dict(),
        'economic_events': [e.to_dict() for e in econ_events[:10]],
        'overall_sentiment': overall,
        'overall_sentiment_score': round(overall_raw, 4),
    }

    out_path = os.path.join(run_dir, 'alt_data_report.json')
    try:
        from .._atomic_io import atomic_write_text
    except ImportError:  # flat-launcher mode
        from _atomic_io import atomic_write_text  # type: ignore[no-redef]
    try:
        atomic_write_text(
            out_path,
            json.dumps(report, indent=2, ensure_ascii=False),
        )
    except OSError:
        pass

    return report


# ---------------------------------------------------------------------------
# Feature registration
# ---------------------------------------------------------------------------

@register('alt_data_connectors')
class AltDataConnectorsFeature(BaseFeature):
    """Alternative data connectors: news, Reddit, economic calendar."""

    name = 'alt_data_connectors'
    label = 'Alternative Data Connectors'
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        t0 = time.monotonic()
        try:
            report = prepare_alt_data(run_dir)
            ns = report.get('news_sentiment', {})
            rs = report.get('reddit_signals', {})
            sentiment = report.get('overall_sentiment', 'unknown')
            summary = (
                f"Alt data: news {ns.get('articles_analyzed', 0)} articles "
                f"(sentiment={ns.get('score', 0):.2f}), "
                f"Reddit {rs.get('mentions_24h', 0)} mentions, "
                f"overall={sentiment}"
            )
            return FeatureResult(
                feature=self.name,
                success=True,
                summary=summary,
                details={'overall_sentiment': sentiment},
                duration_seconds=time.monotonic() - t0,
            )
        except Exception as exc:
            return FeatureResult(
                feature=self.name,
                success=False,
                summary=str(exc),
                error=str(exc),
                duration_seconds=time.monotonic() - t0,
            )
