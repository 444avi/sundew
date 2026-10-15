"""
Sundew configuration — the one obvious place to edit.

Everything tunable lives here: runtime knobs, the cheap keyword pre-filter,
and the gazetteers (macro / geopolitical / commodity terms + company->ticker
map) that both the pre-filter and the LLM classifier lean on.

Pure data, no logic. Edit freely.
"""

# ---------------------------------------------------------------------------
# Runtime knobs
# ---------------------------------------------------------------------------

# CNN's free Truth Social archive (refreshes ~5 min; no official API).
ARCHIVE_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

# How often the forever-loop polls, in seconds. The archive only refreshes
# ~every 5 min, so polling faster than that just re-fetches the same bytes.
POLL_INTERVAL_SECONDS = 300

# Network timeout for the archive fetch and API calls.
HTTP_TIMEOUT_SECONDS = 30

# Anthropic model used for classification. Cheap + strict-JSON friendly.
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# On the very first run the dedup table is empty. Without a cap we'd try to
# classify the entire 34k-post backlog. Instead, on a cold start we mark all
# but the newest N posts as "already seen" so we only act on genuinely new
# posts going forward. Set to 0 to process the whole backlog (don't, on a Pi).
COLD_START_BACKFILL = 5

# ---------------------------------------------------------------------------
# Kalshi augmentation (the "sundew.ks" second copy) — see kalshi.py
# ---------------------------------------------------------------------------
# Recipient lists (all in .env; see .env.example for the full description):
#   EMAIL_TO       -> shared "everyone" list. Sentinel emails it; plain Sundew
#                     falls back to it when EMAIL_TO_SUNDEW is unset.
#   EMAIL_TO_SUNDEW  -> plain-Sundew subset (no Kalshi section).
#   EMAIL_TO_KS    -> Sundew.ks subset (augmented copy).
# The plain-Sundew and Sundew.ks emails are two independent messages, so keep
# EMAIL_TO_SUNDEW and EMAIL_TO_KS disjoint — nobody on both.
#
# When a post passes Sundew's relevance gate, the plain email goes out first and
# UNCHANGED. If KS_ENABLED and EMAIL_TO_KS is set, the augmented copy (same body
# + a "Relevant Kalshi markets" section) is then sent to EMAIL_TO_KS. Kalshi
# reads are public — no API key. Turn the whole feature off here (or by leaving
# EMAIL_TO_KS empty) for byte-identical old behavior.
KS_ENABLED = True

# Kalshi public read API (no auth needed for reads).
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# How many Kalshi markets to attach to an email, at most.
KS_MAX_MARKETS = 3

# Markets are fetched on demand — only for posts that clear the relevance gate,
# and only the curated series that match the post's category/entities (see the
# seed map in kalshi.py). Fetched series are cached in-process for this many
# seconds so a burst of same-category posts doesn't refetch; short, so emailed
# prices stay fresh. Relevant posts are rare, so this is a few API calls a day.
KS_SERIES_TTL_SECONDS = 900

# Safety cap on markets pulled per series (series are small, ~30-60 open each).
KS_MARKETS_PER_SERIES = 200

# Politeness delay (seconds) after each per-series network fetch, to stay under
# Kalshi's public rate limit. Skipped on cache hits.
KS_FETCH_PACE_SECONDS = 0.3

# ---------------------------------------------------------------------------
# Cheap keyword pre-filter (NO LLM)
# ---------------------------------------------------------------------------
# A post must contain at least one of these (case-insensitive, word-ish match)
# to survive the pre-filter and earn an LLM call. This deliberately drops the
# large majority of posts (personal grievance, campaign rhetoric, "MAGA!",
# media attacks) before we spend a single token.
#
# Built from the gazetteers below plus extra trigger words that signal
# market-relevant intent even when no specific entity is named.

PREFILTER_EXTRA_TERMS = [
    # tariffs / trade
    "tariff", "tariffs", "trade deal", "trade war", "import", "imports",
    "export", "exports", "duties", "duty", "sanction", "sanctions",
    "trade deficit", "trade surplus", "section 232", "section 301",
    # monetary / macro
    "interest rate", "interest rates", "rate cut", "rate hike", "the fed",
    "federal reserve", "powell", "inflation", "deflation", "recession",
    "gdp", "jobs report", "unemployment", "stock market", "stocks",
    "bond", "bonds", "treasury", "treasuries", "dollar", "currency",
    "devalue", "devaluation", "crypto", "bitcoin", "ethereum",
    # commodities
    "oil", "crude", "gasoline", "gas prices", "barrel", "opec",
    "natural gas", "gold", "silver", "copper", "wheat", "soybean",
    "soybeans", "corn",
    # corporate / deal signals
    "company", "companies", "factory", "factories", "plant", "merger",
    "acquisition", "antitrust", "ipo", "shareholders", "ceo", "earnings",
    "subsidy", "subsidies", "bailout", "chips act", "semiconductor",
    "semiconductors",
    # geopolitics with market teeth
    "war", "ceasefire", "peace deal", "nuclear", "missile", "strike",
    "blockade", "embargo", "nato", "defense", "military",
]

# ---------------------------------------------------------------------------
# Gazetteers — macro / geopolitical / commodity terms
# ---------------------------------------------------------------------------
# These give the classifier a shared vocabulary and seed the pre-filter.
# Map term -> short note the LLM can use as context.

MACRO_TERMS = {
    "federal reserve": "US central bank / monetary policy",
    "fed": "US central bank / monetary policy",
    "powell": "Fed Chair Jerome Powell",
    "interest rates": "monetary policy lever",
    "inflation": "price level / CPI",
    "recession": "macro contraction",
    "gdp": "output growth",
    "unemployment": "labor market",
    "treasury": "US govt debt / yields",
    "dollar": "USD / FX",
    "tax": "fiscal policy",
    "tariff": "trade policy / import tax",
    "budget": "fiscal policy",
    "debt ceiling": "fiscal / Treasury risk",
}

GEO_TERMS = {
    "china": "CNY, trade, semis, broad risk",
    "taiwan": "semiconductors, US-China risk",
    "russia": "energy, sanctions, defense",
    "ukraine": "defense, grain, energy",
    "iran": "oil supply, Middle East risk",
    "israel": "Middle East risk",
    "saudi arabia": "oil / OPEC",
    "mexico": "USMCA trade, MXN, autos",
    "canada": "USMCA trade, energy, CAD",
    "europe": "EU trade, EUR",
    "european union": "EU trade, EUR",
    "north korea": "geopolitical tail risk",
    "venezuela": "oil supply",
    "india": "trade, EM",
    "japan": "trade, JPY, autos",
    "germany": "EU industrial / autos",
    "nato": "defense spending",
}

COMMODITY_TERMS = {
    "oil": "WTI/Brent crude — USO, XLE",
    "crude": "crude oil",
    "gasoline": "refined fuel — retail gas prices",
    "opec": "oil supply cartel",
    "natural gas": "Henry Hub — UNG",
    "gold": "GLD, safe haven",
    "silver": "SLV",
    "copper": "industrial demand proxy",
    "wheat": "grain — agriculture",
    "soybeans": "ag export, China trade proxy",
    "corn": "grain — agriculture",
    "bitcoin": "BTC — crypto risk",
    "ethereum": "ETH — crypto risk",
}

# ---------------------------------------------------------------------------
# Company -> ticker map
# ---------------------------------------------------------------------------
# Trump names companies by brand. Map the common ways he refers to them to a
# ticker so the classifier can attach an instrument. Keys are lowercase.

COMPANY_TICKERS = {
    "apple": "AAPL",
    "amazon": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "microsoft": "MSFT",
    "tesla": "TSLA",
    "nvidia": "NVDA",
    "boeing": "BA",
    "ford": "F",
    "general motors": "GM",
    "gm": "GM",
    "intel": "INTC",
    "tsmc": "TSM",
    "taiwan semiconductor": "TSM",
    "walmart": "WMT",
    "coca-cola": "KO",
    "coca cola": "KO",
    "coke": "KO",
    "mcdonald's": "MCD",
    "mcdonalds": "MCD",
    "goldman sachs": "GS",
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "exxon": "XOM",
    "exxonmobil": "XOM",
    "chevron": "CVX",
    "lockheed": "LMT",
    "lockheed martin": "LMT",
    "raytheon": "RTX",
    "us steel": "X",
    "u.s. steel": "X",
    "harley-davidson": "HOG",
    "harley davidson": "HOG",
    "truth social": "DJT",
    "trump media": "DJT",
    "nippon steel": "5401.T",
    "tiktok": "(bytedance, private)",
    "openai": "(private)",
}

# ---------------------------------------------------------------------------
# Categories the classifier may assign.
# ---------------------------------------------------------------------------
CATEGORIES = ["tariff", "monetary", "company", "geopolitical", "commodity", "other"]
