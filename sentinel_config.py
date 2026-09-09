"""
Sentinel configuration — the one obvious place to edit.

Sentinel is Sundew's sibling: a live monitor for UNSCHEDULED material events
(EDGAR filings, trading halts, ...). If it was on a calendar, it's not news.
It surfaces and triages for a human; it does not predict prices or trade.

Pure data, no logic.
"""

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

# SEC requires a descriptive User-Agent with a contact address.
USER_AGENT = "Sundew Sentinel gupta.avdan@gmail.com"

POLL_INTERVAL_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 30
ANTHROPIC_MODEL = "claude-sonnet-4-6"

# CIK -> ticker mapping (SEC official). Cached in SQLite, refreshed weekly.
CIK_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
CIK_TICKER_REFRESH_DAYS = 7

# ---------------------------------------------------------------------------
# Sources. enabled=False sources are scaffolded but not polled yet.
# ---------------------------------------------------------------------------

EDGAR_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type={form}&company=&dateb=&owner=include&count=100&output=atom"
)
NASDAQ_HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
NYSE_HALTS_URL = "https://www.nyse.com/api/trade-halts/current/download"

# Which EDGAR form feeds to poll. Each is one Atom request per pass.
EDGAR_FORMS = ["8-K", "SC 13D", "425"]
# Form 4 is deliberately off for now: it needs XML doc parsing to be useful
# and is very high volume. Flip on when implemented end-to-end.
# EDGAR_FORMS += ["4"]

SOURCES_ENABLED = {
    "edgar": True,
    "nasdaq_halts": True,
    "nyse_halts": True,
    # not yet built:
    "ofac_sdn": False,
    "bis_entity_list": False,
    "federal_register_pi": False,
    "ustr": False,
    "fda": False,
    "courtlistener": False,
}

# Per-source heartbeat: how long a source may go without ANY new item during
# US business hours (weekdays ~06:00-22:00 ET) before we flag it dead, and
# how long a fetch may keep erroring before we flag it broken. Seconds.
HEARTBEAT_QUIET_MAX = {
    "edgar": 4 * 3600,        # filings flow all business day
    "nasdaq_halts": 5 * 86400,  # new halts are genuinely rare
    "nyse_halts": 5 * 86400,
}
HEARTBEAT_ERROR_MAX = 30 * 60   # any source erroring for 30 min -> alert

# ---------------------------------------------------------------------------
# 8-K item numbers -> (impact 0-1, note). The brief's view:
# 1.01 / 4.02 / 5.02 / 8.01 are high value; 2.02 earnings is scheduled.
# Items not listed default to IMPACT_DEFAULT_8K.
# ---------------------------------------------------------------------------
ITEM_IMPACT_8K = {
    "1.01": (0.75, "material definitive agreement"),
    "1.02": (0.70, "termination of material agreement"),
    "1.03": (0.95, "bankruptcy / receivership"),
    "2.01": (0.65, "completed acquisition/disposition"),
    "2.02": (0.15, "earnings (usually scheduled)"),
    "2.03": (0.45, "new direct financial obligation"),
    "2.04": (0.75, "triggering event on obligation"),
    "2.05": (0.60, "exit / disposal costs"),
    "2.06": (0.60, "material impairment"),
    "3.01": (0.80, "delisting / listing deficiency"),
    "4.01": (0.70, "auditor change"),
    "4.02": (0.90, "non-reliance on prior financials (restatement)"),
    "5.01": (0.70, "change in control"),
    "5.02": (0.70, "officer/director departure or appointment"),
    "5.03": (0.30, "charter/bylaw amendment"),
    "7.01": (0.30, "Reg FD disclosure"),
    "8.01": (0.55, "other material events"),
    "9.01": (0.05, "exhibits (boilerplate)"),
}
IMPACT_DEFAULT_8K = 0.35
IMPACT_SC13D = 0.65      # activist / >5% stake
IMPACT_425 = 0.55        # merger communications

# ---------------------------------------------------------------------------
# Halt reason codes -> (impact, note). T12 is the near-perfect breaking
# signal; LUDP means something already moved.
# ---------------------------------------------------------------------------
HALT_REASON_IMPACT = {
    "T12": (0.90, "news pending"),
    "T1":  (0.85, "news pending"),
    "T2":  (0.50, "news released"),
    "H10": (0.90, "SEC trading suspension"),
    "H11": (0.85, "regulatory concern"),
    "H4":  (0.60, "non-compliance"),
    "H9":  (0.60, "not current in filings"),
    "LUDP": (0.55, "volatility pause — something already happened"),
    "LUDS": (0.45, "volatility pause (straddle)"),
    "MWC1": (0.95, "market-wide circuit breaker L1"),
    "MWC2": (0.98, "market-wide circuit breaker L2"),
    "MWC3": (1.00, "market-wide circuit breaker L3"),
}
HALT_REASON_DEFAULT = 0.40
# NYSE gives prose, not codes. Map prose -> canonical code.
NYSE_REASON_TO_CODE = {
    "news pending": "T12",
    "news released": "T2",
    "regulatory concern": "H11",
    "sec trading suspension": "H10",
    "volatility trading pause": "LUDP",
    "market wide circuit breaker halt": "MWC1",
}

# ---------------------------------------------------------------------------
# Clustering thresholds
# ---------------------------------------------------------------------------
BLOCK_WINDOW_HOURS = 48         # only compare items within trailing window
SIM_MERGE = 0.72                # combined similarity >= this -> auto-merge
SIM_NEW = 0.35                  # < this -> definitely a new cluster
# between SIM_NEW and SIM_MERGE -> LLM adjudicates (same event or not)
SIMHASH_BITS = 64

# Weights for the combined similarity score
W_LEXICAL = 0.55
W_ENTITY = 0.45

# Fragmentation repair: every pass, clusters sharing an entity + event_type
# whose centroids score >= this get merged.
REPAIR_MERGE = 0.80

# ---------------------------------------------------------------------------
# Novelty
# ---------------------------------------------------------------------------
NOVELTY_NEW_CLUSTER = 0.55      # base for a brand-new cluster
NOVELTY_REPEAT_7D = -0.25       # same entity+event_type seen in last 7 days
NOVELTY_REPEAT_30D = -0.10      # ... in last 30 days
NOVELTY_SUPPRESSED = -0.45      # calendar-suppressed event types
NOVELTY_LAG_PENALTY_H = 6       # subtract linearly up to this many hours late

# Event types whose occurrence is usually scheduled -> suppressed by default.
# (The suppression calendar table in SQLite can add entity-specific dates.)
SUPPRESSED_EVENT_TYPES = {
    "8-K:2.02",                 # earnings
    "8-K:5.07",                 # shareholder vote results
    "8-K:9.01",                 # exhibits only
}

# ---------------------------------------------------------------------------
# Alert tiers: impact+novelty -> candidate; LLM triage -> email
# ---------------------------------------------------------------------------
TIER_EMAIL = 0.70               # impact >= this AND novelty >= NOV_MIN -> email CANDIDATE
TIER_DIGEST = 0.55              # >= this -> daily digest
NOVELTY_MIN_ALERT = 0.55        # below this novelty, never email (stale halts,
                                # late 425s were alerting at 0.40)

# ---------------------------------------------------------------------------
# LLM triage — the actionability gate.
# Every email candidate gets a second look by the LLM with the ACTUAL filing
# text (fetched from EDGAR for 8-K/SC13D/425). Only actionable events email;
# the rest are downgraded to the digest. This is what separates "CEO fired
# tonight" from "routine director election" — both are Item 5.02.
# ---------------------------------------------------------------------------
TRIAGE_ENABLED = True
TRIAGE_MAX_DOC_CHARS = 6000     # filing text fed to the model
TRIAGE_MIN_CONFIDENCE = 0.5     # below this, treat as not actionable

# Warrants/rights/units halt alongside their common stock and were emailing
# separately. Discount them below the email tier.
DERIVATIVE_ISSUE_DISCOUNT = 0.4  # multiply impact_hint for Wt/Rt/Unit issues
ENTITY_COOLDOWN_HOURS = 4       # after emailing about an entity, further emails
                                # for it are downgraded to digest for this long
UPDATE_MIN_IMPACT_DELTA = 0.15  # cluster update emails only if impact rose this much
DIGEST_HOUR_LOCAL = 17          # send the daily digest at ~5pm local time

# ---------------------------------------------------------------------------
# Market-cap notification gate.
# Only companies at/above this cap produce an OUTBOUND notification (email or
# digest). Ingestion, clustering, novelty/impact scoring are unaffected — this
# governs notification only. Resolved via Finnhub and cached in SQLite
# (refreshed weekly, like the CIK map). FAIL-OPEN: a company whose cap can't
# be resolved (no ticker, no data, transient error, or no API key) is allowed
# through, so a lookup miss never silently drops a real event.
# ---------------------------------------------------------------------------
MARKET_CAP_ENABLED = True                 # kill switch for the whole gate
MARKET_CAP_MIN_USD = 1_000_000_000        # floor: notify only for >= $1B caps
MARKET_CAP_REFRESH_DAYS = 7               # re-resolve a ticker's cap this often
# Finnhub free tier (60 req/min): stock/profile2 returns marketCapitalization
# in MILLIONS of the reporting currency (USD for US listings — all our
# sources are US EDGAR / Nasdaq / NYSE). Needs FINNHUB_API_KEY in .env.
FINNHUB_PROFILE_URL = (
    "https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={token}"
)
