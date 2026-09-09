#!/usr/bin/env python3
"""
Kalshi augmentation for Sundew — turn a Sundew classification into a short list of
relevant, live Kalshi prediction markets, with NO extra LLM call.

This module is standalone and stdlib-only. It knows nothing about the Sundew DB
and can be exercised on its own (see `python3 kalshi.py --demo`). Sundew calls
two things:

    find_markets(c)        -> the top few live markets for a classification
    render_section(mkts)   -> an email block for those markets

Matching is fetch-on-demand and category-scoped. A Trump post only ever falls
into a handful of market-relevant buckets (macro/monetary, tariff/trade,
commodity, country/geopolitical, company), so there is no need to index all of
Kalshi. When (and only when) a post clears Sundew's relevance gate, we:

  1. Map its `category` + `entities` to a small set of curated Kalshi *series*
     via the seed map below (CATEGORY_SERIES / TERM_SERIES).
  2. List the open markets for just those series (each a single public API
     call), reusing a short-TTL in-process cache so a burst of same-category
     posts doesn't refetch.
  3. Rank the pooled markets and return the top KS_MAX_MARKETS.

Because relevant posts are rare, this does a few API calls a day (scaling with
actual relevant posts) instead of a fixed hourly sweep, and prices are fresh at
send time.

Design notes (verified live 2026-08 against the PUBLIC read API):

  * Base: https://api.elections.kalshi.com/trade-api/v2 — reads are PUBLIC,
    no key/auth needed.
  * There is no free-text search; you list markets by series and match locally.
    `GET /markets?series_ticker=<S>&status=open` returns that series' open
    markets (a page or two; ~30-60 each in practice).
  * Per-market fields we rely on (confirmed live): `ticker`, `event_ticker`,
    `title`, `yes_sub_title`, `close_time`, `status`, and PRICES as decimals in
    dollars (0..1): `last_price_dollars`, `yes_bid_dollars`, `yes_ask_dollars`,
    `no_bid_dollars`, `no_ask_dollars`. (The old cents fields `last_price` /
    `yes_bid` / `yes_ask` are gone.) `status` reads "active" for open markets.
  * A market's series ticker is the prefix of its `ticker` before the first "-"
    (e.g. KXFEDDECISION-28JAN-H0 -> KXFEDDECISION).

Everything here fails open: any network/parse error yields fewer (or no)
markets and never raises into Sundew's send path.
"""

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config

USER_AGENT = "sundew-kalshi/0.1 (+https://github.com/) market-awareness-monitor"
WEB_BASE = "https://kalshi.com"


# ---------------------------------------------------------------------------
# Curated seed map: Sundew vocabulary -> Kalshi series tickers.
#
# Tickers below were looked up live against the Kalshi series catalog; series
# rotate (weekly/daily markets open and close around events), so some may have
# no open markets at any given moment — that is harmless, that series just
# contributes nothing. Add freely; keys are lowercase and matched against
# entity names/tokens and gazetteer terms.
# ---------------------------------------------------------------------------

# Core series that define a whole category — attached to every post of that
# category (kept deliberately small; country/commodity specifics live in
# TERM_SERIES so a geopolitical post about Iran doesn't pull China markets).
CATEGORY_SERIES = {
    "monetary": ["KXFEDDECISION", "KXEFFR", "KXCPIYOY", "KXNBERRECESSQ", "KXUSTYLD"],
    "tariff": ["KXEFFTARIFF", "KXTARIFFREVENUE", "KXTRUMPTRADEGAP"],
    "commodity": [],
    "geopolitical": [],
    "company": [],
    "other": [],
}

# Specific term -> series. Terms come from the config gazetteers and from the
# entity names the classifier emits.
TERM_SERIES = {
    # --- monetary / macro ---
    "federal reserve": ["KXFEDDECISION", "KXEFFR"],
    "fed": ["KXFEDDECISION", "KXEFFR"],
    "the fed": ["KXFEDDECISION", "KXEFFR"],
    "powell": ["KXFEDDECISION", "KXEFFR"],
    "interest rate": ["KXFEDDECISION", "RATECUTS", "TERMINALRATE"],
    "interest rates": ["KXFEDDECISION", "RATECUTS", "TERMINALRATE"],
    "rate cut": ["KXFEDDECISION", "RATECUTS"],
    "rate hike": ["KXFEDDECISION"],
    "monetary": ["KXFEDDECISION", "KXEFFR"],
    "inflation": ["KXCPIYOY", "CPIYOY", "PCECORE"],
    "cpi": ["KXCPIYOY", "CPIYOY"],
    "deflation": ["KXCPIYOY"],
    "recession": ["KXNBERRECESSQ", "KXRECSSNBER"],
    "unemployment": ["KXU3", "KXUE"],
    "jobs report": ["KXPAYROLLS"],
    "gdp": ["KXCHGDPYOY", "CHINAUSGDP"],
    "treasury": ["KXUSTYLD"],
    "treasuries": ["KXUSTYLD"],
    "dollar": ["KXUSTYLD"],
    "stock market": ["KXINX", "KXINXY"],
    "stocks": ["KXINX", "KXINXY"],
    "s&p": ["KXINX", "KXINXY", "KXSPXFOMC"],
    "nasdaq": ["NASDAQ100", "KXNASDAQ100M"],
    # --- tariff / trade ---
    "tariff": ["KXEFFTARIFF", "KXTARIFFREVENUE", "KXAVGTARIFF", "KXTARIFFCOUNTRY"],
    "tariffs": ["KXEFFTARIFF", "KXTARIFFREVENUE", "KXAVGTARIFF", "KXTARIFFCOUNTRY"],
    "trade war": ["KXTRUMPTRADEGAP", "KXEFFTARIFF"],
    "trade deal": ["KXTRUMPTRADEGAP"],
    "trade deficit": ["TRDDEFCN", "KXTRUMPTRADEGAP"],
    # --- commodities ---
    "oil": ["KXWTIW", "KXBRENTMON", "KXBRENTD", "KXEIACRUDEW"],
    "crude": ["KXWTIW", "KXBRENTMON", "KXEIACRUDEW"],
    "opec": ["KXLEAVEOPEC", "KXEIACRUDEW"],
    "gasoline": ["KXAAAGASMAX"],
    "gas prices": ["KXAAAGASMAX"],
    "natural gas": ["KXAAAGASMAX"],
    "gold": ["KXGOLDD", "KXGOLDY", "KXINXVSGOLD"],
    "silver": ["KXSILVERW"],
    "copper": ["KXCOPPERD", "KXCOPPERW"],
    "wheat": ["KXWHEAT"],
    "soybeans": ["KXSOYBEANMON"],
    "soybean": ["KXSOYBEANMON"],
    "bitcoin": ["KXBTCVSGOLD", "KXTREASBUYBTC"],
    "crypto": ["KXBTCVSGOLD"],
    # --- geopolitical (country-specific) ---
    "china": ["KXTRUMPTRADEGAP", "KXCHGDPYOY", "KXCPICN", "TRDDEFCN"],
    "russia": ["KXCBDECISIONRUSSIA", "KXRUCRUDEX"],
    "iran": ["KXIRANCRUDE", "KXOFAC"],
    "ukraine": ["KXUKRAINE"],
    "sanction": ["KXOFAC"],
    "sanctions": ["KXOFAC"],
}


# ---------------------------------------------------------------------------
# Tokenizer + small helpers
# ---------------------------------------------------------------------------
def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _log(msg):
    print(f"[{_now_iso()}] kalshi: {msg}", flush=True)


_STOPWORDS = {
    "the", "will", "be", "a", "an", "of", "in", "on", "at", "by", "to", "is",
    "and", "or", "for", "vs", "above", "below", "over", "under", "price",
    "close", "value", "index", "yearly", "weekly", "daily", "monthly", "month",
    "week", "day", "year", "range", "high", "low", "next", "usd", "us", "u.s.",
    "than", "this", "their", "meeting", "date", "no", "yes", "how", "when",
    "what", "which",
}


def _tokens(text):
    """Lowercase alphanumeric tokens, minus stopwords and 1-char noise.
    Short-but-meaningful tokens (fed, oil, gdp, cpi, wti) survive."""
    if not text:
        return set()
    raw = re.findall(r"[a-z0-9&]+", text.lower())
    out = set()
    for t in raw:
        t = t.strip("&")
        if not t or t in _STOPWORDS:
            continue
        if len(t) < 2 and not t.isdigit():
            continue
        out.add(t)
    return out


def _series_of(market_ticker):
    return (market_ticker or "").split("-", 1)[0]


def _to_price(*vals):
    """First finite value in [0, 1] from the candidates, else None."""
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= f <= 1.0:
            return f
    return None


def _extract(m, series_ticker=None):
    """Keep only the fields we need from a raw market record."""
    ticker = m.get("ticker", "")
    yes = _to_price(m.get("last_price_dollars"))
    if yes is None or yes == 0.0:
        # fall back to the bid/ask midpoint when there is no last trade
        yb, ya = _to_price(m.get("yes_bid_dollars")), _to_price(m.get("yes_ask_dollars"))
        if yb is not None and ya is not None:
            yes = round((yb + ya) / 2, 4)
        elif yb is not None:
            yes = yb
        elif ya is not None:
            yes = ya
    return {
        "ticker": ticker,
        "event_ticker": m.get("event_ticker", ""),
        "series_ticker": series_ticker or _series_of(ticker),
        "title": m.get("title", "") or "",
        "yes_sub_title": m.get("yes_sub_title", "") or "",
        "yes_price": yes,  # decimal dollars in [0,1], or None
        "close_time": m.get("close_time", "") or "",
        "status": m.get("status", "") or "",
    }


# ---------------------------------------------------------------------------
# On-demand series fetch (the only place that touches the network), with a
# short-TTL in-process cache so a burst of same-category posts reuses results.
# ---------------------------------------------------------------------------
_SERIES_CACHE = {}  # series_ticker -> (epoch_fetched, [compact markets])


def _http_get_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _fetch_series(series_ticker, base, per_series_cap, timeout):
    """All open markets for one series (paginated), compact-extracted."""
    out = []
    cursor = None
    while True:
        url = (f"{base}/markets?series_ticker={series_ticker}"
               f"&status=open&limit=200")
        if cursor:
            url += f"&cursor={cursor}"
        data = _http_get_json(url, timeout=timeout)
        for m in data.get("markets", []):
            out.append(_extract(m, series_ticker))
        cursor = data.get("cursor")
        if not cursor or not data.get("markets") or len(out) >= per_series_cap:
            break
    return out


def _get_series(series_ticker, base, ttl, cap, timeout, pace):
    """Cached open markets for one series. Never raises: on a fetch error it
    returns the stale cache if any, else an empty list."""
    hit = _SERIES_CACHE.get(series_ticker)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]
    try:
        markets = _fetch_series(series_ticker, base, cap, timeout)
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        _log(f"series {series_ticker} fetch failed: {e}")
        return hit[1] if hit else []
    _SERIES_CACHE[series_ticker] = (time.time(), markets)
    if pace:
        time.sleep(pace)  # politeness spacing, only after a real network call
    return markets


# ---------------------------------------------------------------------------
# Matching (no LLM). Determine the relevant series from the classification,
# fetch just those, then rank the pooled markets by title relevance.
# ---------------------------------------------------------------------------
def _query_from_classification(c):
    """Return (query_terms, wanted_series) built from a classification dict."""
    terms = set()
    series = set()
    cat = (c.get("category") or "").lower()
    series.update(CATEGORY_SERIES.get(cat, []))
    if cat in TERM_SERIES:
        series.update(TERM_SERIES[cat])
    terms.update(_tokens(cat))

    for e in c.get("entities") or []:
        name = (e.get("name") or "").strip()
        ticker = (e.get("ticker") or "").strip()
        terms.update(_tokens(name))
        terms.update(_tokens(ticker))
        nm = name.lower()
        if nm in TERM_SERIES:
            series.update(TERM_SERIES[nm])
        for tok in _tokens(name):
            if tok in TERM_SERIES:
                series.update(TERM_SERIES[tok])
    return terms, series


def find_markets(c, base=None, ttl=None, max_markets=None):
    """Rank live Kalshi markets against a Sundew classification dict.

    Fetches (on demand, cached) only the curated series relevant to the post's
    category/entities — never all of Kalshi — and returns up to `max_markets`
    compact market dicts, best first. `[]` when the post maps to no relevant
    series (e.g. a nonsense/"other" classification) or when fetching yields
    nothing. Fails open; never raises.
    """
    if not c:
        return []
    base = base or config.KALSHI_BASE_URL
    ttl = config.KS_SERIES_TTL_SECONDS if ttl is None else ttl
    max_markets = config.KS_MAX_MARKETS if max_markets is None else max_markets
    timeout = config.HTTP_TIMEOUT_SECONDS

    terms, wanted_series = _query_from_classification(c)
    if not wanted_series:
        return []

    pool = {}
    for st in sorted(wanted_series):
        for m in _get_series(st, base, ttl, config.KS_MARKETS_PER_SERIES,
                             timeout, config.KS_FETCH_PACE_SECONDS):
            if m["ticker"]:
                pool[m["ticker"]] = m
    if not pool:
        return []

    scored = []
    for m in pool.values():
        title_tokens = _tokens(m.get("title", "") + " " + m.get("yes_sub_title", ""))
        overlap = len(terms & title_tokens)
        fuzzy = (overlap / len(terms)) if terms else 0.0
        score = 1.0 + fuzzy  # every fetched market is already category-relevant
        close = m.get("close_time") or "9999"
        yp = m.get("yes_price")
        neg_price = -(yp if yp is not None else 0.0)
        scored.append((score, close, neg_price, m))

    # best title match first, then nearest close, then higher (more likely) yes
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [m for _, _, _, m in scored[:max_markets]]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def build_market_url(series_ticker):
    """Public Kalshi link. Uses the series page (kalshi.com/markets/<series>),
    which lists the market and is the reliably-valid public form."""
    return f"{WEB_BASE}/markets/{(series_ticker or '').lower()}"


def _fmt_cents(price):
    if price is None:
        return "n/a"
    return f"{round(price * 100)}¢"


def _fmt_close(close_time):
    if not close_time:
        return "n/a"
    try:
        dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return close_time[:10]


def render_section(markets):
    """Email block for the matched markets. '' for an empty list."""
    if not markets:
        return ""
    lines = [
        "-" * 60,
        "Relevant Kalshi markets",
        "Live Kalshi market prices — not Sundew predictions.",
        "",
    ]
    for m in markets:
        title = m.get("title") or m.get("ticker")
        sub = m.get("yes_sub_title")
        head = f"  • {title}"
        if sub and sub.lower() not in title.lower():
            head += f" [{sub}]"
        yes = m.get("yes_price")
        no = None if yes is None else max(0.0, min(1.0, 1.0 - yes))
        lines.append(head)
        lines.append(f"      Yes {_fmt_cents(yes)} / No {_fmt_cents(no)}"
                     f"   closes {_fmt_close(m.get('close_time'))}")
        lines.append(f"      {build_market_url(m.get('series_ticker'))}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def _demo():
    """`python3 kalshi.py --demo`: fetch-on-demand matches for sample posts."""
    samples = [
        ("MACRO/monetary", {"category": "monetary",
                            "entities": [{"name": "Federal Reserve"},
                                         {"name": "Powell"}]}),
        ("COUNTRY/geopolitical", {"category": "geopolitical",
                                  "entities": [{"name": "Iran"}]}),
        ("COMMODITY", {"category": "commodity",
                       "entities": [{"name": "oil"}]}),
        ("COMPANY", {"category": "company",
                     "entities": [{"name": "Apple", "ticker": "AAPL"}]}),
        ("NONSENSE/other", {"category": "other",
                            "entities": [{"name": "Taylor Swift concert"}]}),
    ]
    for label, c in samples:
        ms = find_markets(c)
        print("=" * 70)
        print(f"{label} -> {len(ms)} market(s)")
        print(render_section(ms) or "  (no section)\n")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        _demo()
    else:
        print("usage: python3 kalshi.py --demo")
