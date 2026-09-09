#!/usr/bin/env python3
"""
Sentinel — Sundew's sibling: a live monitor for UNSCHEDULED material events.

Not a news agent: if it was on a calendar, it's not news — suppress it.
It surfaces and triages for a human to act on with discretion. It does not
predict prices and contains no trading logic.

Pipeline per pass:
  fetch sources -> dedup by doc_hash -> normalize -> cluster (SimHash +
  entity blocking; LLM only for the ambiguous band) -> novelty & impact
  (scored separately, both logged) -> tiered alerts (email / digest / log)
  -> per-source heartbeats.

CLI mirrors sundew.py: forever-loop by default, --once (idempotent, cron-safe),
--no-email, --no-llm, --source NAME. Stdlib only. Shares .env with Sundew.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import sentinel_cluster as clu
import sentinel_config as cfg
import sentinel_sources as src
import sesmail
import sundew  # reuse load_env + .env conventions
from notification_app import (AppStore, DeliveryWorker, sentinel_categories,
                              sentinel_significance)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "sentinel.db")
DEFAULT_APP_DB = os.path.join(HERE, "app.db")


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, path=DEFAULT_DB):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._schema()

    def _schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            doc_hash    TEXT PRIMARY KEY,
            source      TEXT, observed_at TEXT, event_time TEXT,
            title       TEXT, body TEXT, url TEXT,
            entities    TEXT, event_type TEXT,
            simhash     INTEGER, impact_hint REAL,
            cluster_id  INTEGER, raw TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
        CREATE INDEX IF NOT EXISTS idx_items_time ON items(observed_at);

        CREATE TABLE IF NOT EXISTS clusters (
            cluster_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT, entities TEXT, simhash INTEGER,
            title       TEXT, first_seen TEXT, last_update TEXT,
            n_items     INTEGER DEFAULT 1,
            novelty     REAL, impact REAL, tier TEXT,
            alerted_at  TEXT, alerted_impact REAL, update_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS calendar (
            entity TEXT, date TEXT, event_type TEXT, note TEXT,
            PRIMARY KEY (entity, date, event_type)
        );

        CREATE TABLE IF NOT EXISTS entity_alerts (
            entity TEXT PRIMARY KEY, last_alert_at TEXT
        );

        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT PRIMARY KEY,
            last_fetch_ok TEXT, last_new_item TEXT,
            error_since TEXT, flagged_dead INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS llm_log (
            at TEXT, item_hash TEXT, cluster_id INTEGER,
            verdict TEXT, raw TEXT
        );

        CREATE TABLE IF NOT EXISTS digest_queue (
            cluster_id INTEGER PRIMARY KEY, queued_at TEXT
        );

        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);

        -- market-cap cache: cap_usd NULL means 'looked up, no data' (cached
        -- so we don't re-query every pass); refreshed per MARKET_CAP_REFRESH_DAYS
        CREATE TABLE IF NOT EXISTS market_cap (
            symbol TEXT PRIMARY KEY, cap_usd REAL, resolved_at TEXT
        );
        """)
        # migrations for DBs created before LLM triage existed
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(clusters)")}
        if "direction" not in cols:
            self.conn.execute(
                "ALTER TABLE clusters ADD COLUMN direction TEXT")
        if "triage" not in cols:
            self.conn.execute("ALTER TABLE clusters ADD COLUMN triage TEXT")
        self.conn.commit()

    # -- kv ---------------------------------------------------------------
    def kv_get(self, k):
        r = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def kv_set(self, k, v):
        self.conn.execute(
            "INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
        self.conn.commit()

    # -- market-cap cache -------------------------------------------------
    def market_cap_get(self, symbol):
        return self.conn.execute(
            "SELECT cap_usd, resolved_at FROM market_cap WHERE symbol=?",
            (symbol,)).fetchone()

    def market_cap_put(self, symbol, cap_usd):
        # cap_usd may be None: caches a confirmed 'no data' so we don't re-query
        self.conn.execute(
            "INSERT OR REPLACE INTO market_cap(symbol, cap_usd, resolved_at) "
            "VALUES (?,?,?)", (symbol, cap_usd, now_iso()))

    # -- items / clusters -------------------------------------------------
    def have_item(self, doc_hash):
        return self.conn.execute(
            "SELECT 1 FROM items WHERE doc_hash=?", (doc_hash,)).fetchone() is not None

    def insert_item(self, it, cluster_id):
        self.conn.execute(
            "INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["doc_hash"], it["source"], it["observed_at"], it["event_time"],
             it["title"], it["body"], it["url"], json.dumps(it["entities"]),
             it["event_type"], it["simhash"], it["impact_hint"], cluster_id,
             json.dumps(it.get("raw", {}))))

    def recent_clusters(self, hours):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM clusters WHERE last_update >= ?", (cutoff,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["entities"] = json.loads(d["entities"])
            out.append(d)
        return out

    def new_cluster(self, it):
        cur = self.conn.execute(
            "INSERT INTO clusters(event_type, entities, simhash, title, "
            "first_seen, last_update) VALUES (?,?,?,?,?,?)",
            (it["event_type"], json.dumps(it["entities"]), it["simhash"],
             it["title"], it["observed_at"], it["observed_at"]))
        return cur.lastrowid

    def add_to_cluster(self, cluster_id, it):
        row = self.conn.execute(
            "SELECT entities, n_items FROM clusters WHERE cluster_id=?",
            (cluster_id,)).fetchone()
        ents = sorted(set(json.loads(row["entities"])) | set(it["entities"]))
        self.conn.execute(
            "UPDATE clusters SET entities=?, last_update=?, n_items=? "
            "WHERE cluster_id=?",
            (json.dumps(ents), it["observed_at"], row["n_items"] + 1, cluster_id))

    def cluster_items(self, cluster_id):
        rows = self.conn.execute(
            "SELECT * FROM items WHERE cluster_id=?", (cluster_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["entities"] = json.loads(d["entities"])
            out.append(d)
        return out

    def set_cluster_scores(self, cluster_id, novelty, impact, tier):
        self.conn.execute(
            "UPDATE clusters SET novelty=?, impact=?, tier=? WHERE cluster_id=?",
            (novelty, impact, tier, cluster_id))

    def repeats(self, entities, event_type, days, exclude_cluster_id):
        """Any OTHER cluster with same event_type sharing an entity in the
        last N days? (Repetition decay for novelty.)"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        for r in self.conn.execute(
                "SELECT cluster_id, entities FROM clusters WHERE event_type=? "
                "AND first_seen >= ? AND cluster_id != ?",
                (event_type, cutoff, exclude_cluster_id)):
            if set(json.loads(r["entities"])) & set(entities):
                return True
        return False

    def calendar_match(self, entities, date_iso):
        d = date_iso[:10]
        for r in self.conn.execute(
                "SELECT entity FROM calendar WHERE date=?", (d,)):
            if r["entity"] in entities:
                return True
        return False

    # -- cooldown ---------------------------------------------------------
    def entity_in_cooldown(self, entities):
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=cfg.ENTITY_COOLDOWN_HOURS)).isoformat()
        for e in entities:
            r = self.conn.execute(
                "SELECT last_alert_at FROM entity_alerts WHERE entity=?",
                (e,)).fetchone()
            if r and r["last_alert_at"] > cutoff:
                return True
        return False

    def touch_entity_alerts(self, entities):
        now = now_iso()
        self.conn.executemany(
            "INSERT OR REPLACE INTO entity_alerts VALUES (?,?)",
            [(e, now) for e in entities])

    def commit(self):
        self.conn.commit()


# ---------------------------------------------------------------------------
# CIK -> ticker map (cached in kv, refreshed weekly)
# ---------------------------------------------------------------------------
def cik_ticker_map(store):
    cached_at = store.kv_get("cik_map_at")
    if cached_at and (datetime.now(timezone.utc) - clu.parse_iso(cached_at)
                      ).days < cfg.CIK_TICKER_REFRESH_DAYS:
        return json.loads(store.kv_get("cik_map") or "{}")
    try:
        raw = src._get(cfg.CIK_TICKER_URL)
        data = json.loads(raw)
        mapping = {str(v["cik_str"]).zfill(10): v["ticker"]
                   for v in data.values()}
        store.kv_set("cik_map", json.dumps(mapping))
        store.kv_set("cik_map_at", now_iso())
        log(f"refreshed CIK->ticker map: {len(mapping)} entries")
        return mapping
    except Exception as e:  # noqa: BLE001
        log(f"CIK map refresh failed ({e}); using stale cache")
        return json.loads(store.kv_get("cik_map") or "{}")


# ---------------------------------------------------------------------------
# Market-cap gate — resolve (cached weekly), then suppress sub-threshold caps.
# Only a KNOWN cap below the floor suppresses; unknown caps fall through
# (fail-open), so a lookup miss never silently drops a real event.
# ---------------------------------------------------------------------------
def _cap_stale(resolved_at):
    try:
        return (datetime.now(timezone.utc) - clu.parse_iso(resolved_at)
                ).days >= cfg.MARKET_CAP_REFRESH_DAYS
    except Exception:  # noqa: BLE001
        return True


def resolve_ticker_cap(store, symbol, token):
    """USD market cap for one ticker, via the weekly cache. Returns a float, or
    None when unknown (no data, no token, or a transient error). A confirmed
    'no data' is cached as NULL; transient errors leave the cache untouched."""
    row = store.market_cap_get(symbol)
    if row and not _cap_stale(row["resolved_at"]):
        return row["cap_usd"]
    if not token:
        return row["cap_usd"] if row else None
    try:
        cap = src.fetch_market_cap(symbol, token)
    except Exception as e:  # noqa: BLE001 — transient; keep any prior value
        log(f"  market-cap lookup failed for {symbol}: {e}")
        return row["cap_usd"] if row else None
    store.market_cap_put(symbol, cap)
    return cap


def cluster_market_cap(store, entities, env):
    """Best-known market cap (max USD) across a cluster's tickers, or None if
    none resolve. Max, not min: a multi-company event (e.g. an M&A 425 naming
    acquirer and target) qualifies if ANY party is large."""
    token = env.get("FINNHUB_API_KEY", "")
    caps = [c for c in (
        resolve_ticker_cap(store, e.split(":", 1)[1], token)
        for e in entities if e.startswith("TICKER:")) if c is not None]
    return max(caps) if caps else None


def below_market_cap_floor(store, entities, env):
    """True only when the cluster's cap is KNOWN and below the floor."""
    cap = cluster_market_cap(store, entities, env)
    return cap is not None and cap < cfg.MARKET_CAP_MIN_USD


# ---------------------------------------------------------------------------
# LLM adjudication (ambiguous band only)
# ---------------------------------------------------------------------------
ADJUDICATE_SYSTEM = """You decide whether a new market-event report describes the SAME underlying \
event as an existing cluster of reports, or a DIFFERENT event.

Same event = same company/entity AND same underlying happening (a trading \
halt and the news that caused it are the SAME event; two different filings \
about unrelated matters are DIFFERENT).

Respond with strict JSON only: {"same_event": true|false, "why": "<= 15 words"}"""


def llm_same_event(item, cluster, cluster_members, api_key):
    user = (
        "EXISTING CLUSTER:\n"
        + "\n".join(f"- [{m['source']}] {m['title']}" for m in cluster_members[:5])
        + f"\n  event_type={cluster['event_type']}, entities={cluster['entities'][:6]}"
        + "\n\nNEW ITEM:\n"
        + f"- [{item['source']}] {item['title']}\n  {item['body'][:400]}\n"
        + f"  event_type={item['event_type']}, entities={item['entities'][:6]}"
        + "\n\nJSON now.")
    body = {
        "model": cfg.ANTHROPIC_MODEL, "max_tokens": 100, "temperature": 0,
        "system": ADJUDICATE_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01",
                 "User-Agent": cfg.USER_AGENT},
        method="POST")
    with urllib.request.urlopen(req, timeout=cfg.HTTP_TIMEOUT_SECONDS) as resp:
        payload = json.load(resp)
    text = "".join(b.get("text", "") for b in payload.get("content", [])
                   if b.get("type") == "text")
    obj = sundew._parse_classification(text)  # tolerant JSON extraction
    return bool(obj.get("same_event")), text


# ---------------------------------------------------------------------------
# Clustering pass
# ---------------------------------------------------------------------------
def assign_cluster(store, it, api_key, use_llm):
    """Returns (cluster_id, is_new). Blocking: recent clusters sharing >=1
    entity. Cheapest-first: thresholds decide; ambiguous band -> LLM."""
    candidates = []
    for c in store.recent_clusters(cfg.BLOCK_WINDOW_HOURS):
        if not set(c["entities"]) & set(it["entities"]):
            continue
        verdict, score = clu.decide(it, c)
        candidates.append((score, verdict, c))
    candidates.sort(key=lambda t: -t[0])

    for score, verdict, c in candidates:
        if verdict == "merge":
            store.add_to_cluster(c["cluster_id"], it)
            return c["cluster_id"], False
        if verdict == "ambiguous":
            if not use_llm:
                break  # no LLM available: treat as new; llm_log stays empty
            try:
                same, raw = llm_same_event(
                    it, c, store.cluster_items(c["cluster_id"]), api_key)
                store.conn.execute(
                    "INSERT INTO llm_log VALUES (?,?,?,?,?)",
                    (now_iso(), it["doc_hash"], c["cluster_id"],
                     "same" if same else "different", raw[:500]))
                if same:
                    store.add_to_cluster(c["cluster_id"], it)
                    return c["cluster_id"], False
            except Exception as e:  # noqa: BLE001
                log(f"  adjudication error (treating as new): {e}")
            break  # only ask about the best candidate
        break  # best candidate already below ambiguous band

    return store.new_cluster(it), True


def repair_fragmentation(store):
    """Delayed merge pass: clusters in-window sharing an entity AND
    event_type whose centroids are near-identical get merged."""
    cs = store.recent_clusters(cfg.BLOCK_WINDOW_HOURS)
    merged = 0
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            a, b = cs[i], cs[j]
            if a["event_type"] != b["event_type"]:
                continue
            if not set(a["entities"]) & set(b["entities"]):
                continue
            score, _, _ = clu.combined_sim(
                {"simhash": a["simhash"], "entities": a["entities"]}, b)
            if score >= cfg.REPAIR_MERGE:
                keep, drop = ((a, b) if a["cluster_id"] < b["cluster_id"]
                              else (b, a))
                store.conn.execute(
                    "UPDATE items SET cluster_id=? WHERE cluster_id=?",
                    (keep["cluster_id"], drop["cluster_id"]))
                ents = sorted(set(keep["entities"]) | set(drop["entities"]))
                store.conn.execute(
                    "UPDATE clusters SET entities=?, n_items="
                    "(SELECT COUNT(*) FROM items WHERE cluster_id=?) "
                    "WHERE cluster_id=?",
                    (json.dumps(ents), keep["cluster_id"], keep["cluster_id"]))
                store.conn.execute(
                    "DELETE FROM clusters WHERE cluster_id=?",
                    (drop["cluster_id"],))
                merged += 1
    if merged:
        log(f"fragmentation repair: merged {merged} cluster pair(s)")
    return merged


# ---------------------------------------------------------------------------
# LLM triage — the actionability + direction gate for email candidates
# ---------------------------------------------------------------------------
TRIAGE_SYSTEM = """You triage market-event alerts for a human investor. You get one event \
(clustered reports + for SEC filings the actual filing text) and decide if it \
deserves an immediate email.

actionable = an unscheduled, material, company- or market-moving development a \
human would plausibly want to look at TODAY: CEO/CFO forced exits, M&A, \
bankruptcy, restatements, delistings of real operating companies, activist \
stakes, major contract wins/losses, trading halts on companies of consequence.

NOT actionable: routine governance (director elections, comp plans, committee \
assignments), boilerplate credit-agreement amendments, ATM/shelf offerings of \
tiny issuers, SPAC/shell housekeeping, halts or filings of micro-cap shells \
with no real business, scheduled items (earnings, votes).

direction = your read of the likely effect on the company's stock: "bullish", \
"bearish", or "unclear". This is a directional characterization to orient a \
human, not advice and not a price target.

Strict JSON only:
{"actionable": true|false, "direction": "bullish"|"bearish"|"unclear",
 "confidence": 0..1, "headline": "<= 12 words, concrete, what happened",
 "why": "<= 25 words"}"""


def llm_triage(cluster, members, api_key):
    """Second-look triage of an email candidate. Fetches real filing text for
    EDGAR members. Returns dict or None on failure (fail-open: alert anyway).
    """
    doc_text = ""
    for m in members:
        if m["source"] == "edgar" and m.get("url"):
            doc_text = src.fetch_edgar_doc(
                m["url"], cfg.TRIAGE_MAX_DOC_CHARS)
            if doc_text:
                break
    user = (
        f"EVENT (type {cluster['event_type']}, "
        f"entities {cluster['entities'][:6]}):\n"
        + "\n".join(f"- [{m['source']}] {m['title']} :: {m['body'][:200]}"
                    for m in members[:4])
        + (f"\n\nFILING TEXT (truncated):\n{doc_text}" if doc_text
           else "\n\n(no filing text available — judge from the metadata)")
        + "\n\nJSON now.")
    body = {
        "model": cfg.ANTHROPIC_MODEL, "max_tokens": 200, "temperature": 0,
        "system": TRIAGE_SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01",
                 "User-Agent": cfg.USER_AGENT},
        method="POST")
    with urllib.request.urlopen(req, timeout=cfg.HTTP_TIMEOUT_SECONDS) as resp:
        payload = json.load(resp)
    text = "".join(b.get("text", "") for b in payload.get("content", [])
                   if b.get("type") == "text")
    obj = sundew._parse_classification(text)
    if "actionable" not in obj:
        return None
    obj.setdefault("direction", "unclear")
    obj.setdefault("confidence", 0.0)
    obj.setdefault("headline", "")
    obj.setdefault("why", "")
    return obj


_DIR_EMOJI = {"bullish": "🟢", "bearish": "🔴", "unclear": "⚪"}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
def _fmt_cluster_email(c, members, update=False, triage=None):
    tickers = sorted({e.split(":", 1)[1] for e in c["entities"]
                      if e.startswith("TICKER:")})
    t = triage or {}
    arrow = _DIR_EMOJI.get(t.get("direction"), "")
    head = ("UPDATE: " if update else "") + (
        f"[{c['event_type']}] " + (", ".join(tickers) if tickers else
                                   c["title"][:40]))
    headline = t.get("headline") or ""
    subject = " ".join(x for x in ("Sentinel", arrow, head,
                                   ("— " + headline) if headline else "") if x)
    lines = [
        f"Cluster #{c['cluster_id']} — {c['event_type']}",
        f"impact={c['impact']:.2f}  novelty={c['novelty']:.2f}  "
        f"members={len(members)}  first_seen={c['first_seen']}",
    ]
    if t:
        lines += [
            "",
            f"Assessment: {t.get('headline','')}",
            f"  direction:  {t.get('direction','unclear')} "
            f"(confidence {t.get('confidence',0):.2f})",
            f"  why: {t.get('why','')}",
        ]
    lines.append("")
    for m in sorted(members, key=lambda m: m["observed_at"]):
        lines += [f"[{m['source']}] {m['title']}",
                  f"  {m['event_time']}  {m['url']}",
                  f"  {m['body'][:300]}", ""]
    lines += ["--", "Sentinel surfaces unscheduled events for human review. "
              "The direction is a characterization of the news, not advice "
              "and not a price prediction."]
    return subject, "\n".join(lines)


def send_email(subject, body_text, env, to=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env["EMAIL_FROM"]
    msg["To"] = to or env["EMAIL_TO"]
    msg.set_content(body_text)
    sesmail.send(env, msg)


def _publish_delivery_event(app_store, c, members, action, triage, env,
                            no_email, market_cap_usd=None):
    """Adapt an already-eligible cluster to the shared delivery model."""
    subject, body_text = _fmt_cluster_email(
        c, members, update=(action == "update"), triage=triage)
    if no_email:
        log(f"  [no-email] would create canonical event: {subject}")
        return 0
    event_kind = "update" if action == "update" else "original"
    latest_version = app_store.latest_event_version("sentinel", c["cluster_id"])
    promoted_original = action == "alert" and latest_version > 0
    event_version = latest_version + 1 if action in {"alert", "update"} else 1
    newest = max(members, key=lambda member: member["observed_at"])
    result = app_store.publish_event(
        monitor="sentinel",
        source_event_id=str(c["cluster_id"]),
        event_version=event_version,
        event_kind=event_kind,
        categories=sentinel_categories(c["event_type"], c["entities"]),
        direction=(triage or {}).get("direction") or c.get("direction") or "unclear",
        significance=sentinel_significance(c["impact"]),
        canonical_tier="immediate" if action else "digest",
        occurred_at=newest.get("event_time") or c["first_seen"],
        subject_data={"subject": subject},
        body_data={
            "body": body_text,
            "cluster_id": c["cluster_id"],
            "event_type": c["event_type"],
        },
        market_cap_usd=market_cap_usd,
        supersede_previous=promoted_original,
    )
    if result["created"]:
        log(f"  canonical event {result['event_id']} created; "
            f"delivery decisions={result['decisions']}")
    return 0


def score_and_alert(store, cluster_id, is_new, env, no_email,
                    mark_alerted=True, use_llm=True, app_store=None):
    """mark_alerted=False is the dry-run case: log what WOULD be sent without
    recording it as alerted, so a later real pass still emails it. Cold start
    passes True deliberately — the backlog should be suppressed forever."""
    members = store.cluster_items(cluster_id)
    c = dict(store.conn.execute(
        "SELECT * FROM clusters WHERE cluster_id=?", (cluster_id,)).fetchone())
    c["entities"] = json.loads(c["entities"])

    newest = max(members, key=lambda m: m["observed_at"])
    suppressed = store.calendar_match(c["entities"], newest["event_time"])
    nov = clu.novelty_score(
        is_new, c["event_type"], suppressed,
        store.repeats(c["entities"], c["event_type"], 7, cluster_id),
        store.repeats(c["entities"], c["event_type"], 30, cluster_id),
        clu.lag_hours(newest))
    imp = clu.impact_score(members)

    # tier: novelty and impact are separate axes; both logged, both gate email
    if imp >= cfg.TIER_EMAIL and nov >= cfg.NOVELTY_MIN_ALERT:
        tier = "email"
    elif imp >= cfg.TIER_DIGEST:
        tier = "digest"
    else:
        tier = "log"

    if tier == "email" and store.entity_in_cooldown(c["entities"]) \
            and not c["alerted_at"]:
        tier = "digest"  # cooldown: downgrade fresh clusters on hot entities

    store.set_cluster_scores(cluster_id, nov, imp, tier)
    c.update(novelty=nov, impact=imp, tier=tier)

    already_alerted = bool(c["alerted_at"])
    materially_changed = (c["alerted_impact"] is not None
                          and imp - c["alerted_impact"] >= cfg.UPDATE_MIN_IMPACT_DELTA)

    # A pass that is both dry-run and alert-marking is the cold start: the whole
    # backlog is suppressed anyway, so skip the market-cap lookups and triage
    # LLM calls it would otherwise trigger.
    cold_start_pass = no_email and mark_alerted

    # Resolve once per canonical event, before any per-user decisions. The
    # result uses Sentinel's Finnhub-backed weekly cache and is persisted on
    # the event, so a large recipient list never multiplies API traffic.
    would_notify = (tier in ("email", "digest")
                    or (already_alerted and materially_changed))
    market_cap_usd = None
    if would_notify and not cold_start_pass:
        market_cap_usd = cluster_market_cap(store, c["entities"], env)
    if (cfg.MARKET_CAP_ENABLED and market_cap_usd is not None
            and market_cap_usd < cfg.MARKET_CAP_MIN_USD):
        log(f"  below ${cfg.MARKET_CAP_MIN_USD / 1e9:g}B market-cap floor — "
            f"no notification (cluster #{cluster_id})")
        store.set_cluster_scores(cluster_id, nov, imp, "log")
        store.conn.execute(
            "DELETE FROM digest_queue WHERE cluster_id=?", (cluster_id,))
        return "log(sub-cap)"

    action = None
    if tier == "email" and not already_alerted:
        action = "alert"
    elif already_alerted and materially_changed:
        action = "update"
    elif tier == "digest" and not already_alerted:
        if app_store is None:
            store.conn.execute(
                "INSERT OR IGNORE INTO digest_queue VALUES (?,?)",
                (cluster_id, now_iso()))

    triage = None
    if action and cfg.TRIAGE_ENABLED and use_llm and not cold_start_pass:
        try:
            triage = llm_triage(c, members, env.get("ANTHROPIC_API_KEY", ""))
        except Exception as e:  # noqa: BLE001 — fail-open: alert untriaged
            log(f"  triage error (alerting untriaged): {e}")
        if triage is not None:
            store.conn.execute(
                "UPDATE clusters SET direction=?, triage=? WHERE cluster_id=?",
                (triage.get("direction"), json.dumps(triage), cluster_id))
            passed = (triage.get("actionable")
                      and float(triage.get("confidence") or 0)
                      >= cfg.TRIAGE_MIN_CONFIDENCE)
            log(f"  triage: actionable={triage.get('actionable')} "
                f"dir={triage.get('direction')} "
                f"conf={triage.get('confidence')} :: "
                f"{triage.get('headline','')[:60]}")
            if not passed:
                # not worth an email — keep it visible in the daily digest
                store.set_cluster_scores(cluster_id, nov, imp, "digest")
                c["tier"] = "digest"
                if app_store is None:
                    store.conn.execute(
                        "INSERT OR IGNORE INTO digest_queue VALUES (?,?)",
                        (cluster_id, now_iso()))
                else:
                    _publish_delivery_event(
                        app_store, c, members, None, triage, env, no_email,
                        market_cap_usd)
                return "digest(triaged-out)"

    if action:
        subject, body_text = _fmt_cluster_email(
            c, members, update=(action == "update"), triage=triage)
        if no_email:
            log(f"  [no-email] would send: {subject}")
        elif app_store is not None:
            _publish_delivery_event(
                app_store, c, members, action, triage, env, no_email,
                market_cap_usd)
        else:
            try:
                send_email(subject, body_text, env)
                log(f"  emailed: {subject}")
            except Exception as e:  # noqa: BLE001
                log(f"  email error: {e}")
                return tier
        if not mark_alerted:
            return tier
        store.conn.execute(
            "UPDATE clusters SET alerted_at=?, alerted_impact=?, "
            "update_count=update_count+? WHERE cluster_id=?",
            (now_iso(), imp, 1 if action == "update" else 0, cluster_id))
        store.touch_entity_alerts(c["entities"])
    elif tier == "digest" and not already_alerted and app_store is not None:
        _publish_delivery_event(
            app_store, c, members, None, triage, env, no_email,
            market_cap_usd)
    return tier


def flush_digest(store, env, no_email, force=False, app_store=None):
    """Send the daily digest when the local hour matches, or on demand."""
    if app_store is not None:
        if force and not no_email:
            DeliveryWorker(app_store, env, logger=log).process_digests(
                monitor="sentinel", force=True)
        return
    last = store.kv_get("digest_sent_date")
    today = datetime.now().strftime("%Y-%m-%d")
    if not force:
        if datetime.now().hour < cfg.DIGEST_HOUR_LOCAL or last == today:
            return
    rows = store.conn.execute(
        "SELECT c.* FROM digest_queue q JOIN clusters c USING(cluster_id) "
        "ORDER BY c.impact DESC").fetchall()
    if not rows:
        store.kv_set("digest_sent_date", today)
        return
    lines = [f"Sentinel daily digest — {len(rows)} medium-tier cluster(s)", ""]
    for r in rows:
        lines.append(
            f"- [{r['event_type']}] {r['title'][:70]}  "
            f"impact={r['impact']:.2f} novelty={r['novelty']:.2f} "
            f"items={r['n_items']}")
    subject = f"Sentinel digest: {len(rows)} item(s)"
    if no_email:
        log(f"[no-email] would send digest: {subject}")
    else:
        try:
            send_email(subject, "\n".join(lines), env)
            log(f"sent digest: {subject}")
        except Exception as e:  # noqa: BLE001
            log(f"digest email error: {e}")
            return
    store.conn.execute("DELETE FROM digest_queue")
    store.kv_set("digest_sent_date", today)


# ---------------------------------------------------------------------------
# Heartbeats — silent source death detection
# ---------------------------------------------------------------------------
def business_hours_now():
    et = datetime.now(timezone(timedelta(hours=src._et_offset(datetime.now()))))
    return et.weekday() < 5 and 6 <= et.hour < 22


def heartbeat(store, source, fetch_ok, n_new, env, no_email):
    now = now_iso()
    row = store.conn.execute(
        "SELECT * FROM source_state WHERE source=?", (source,)).fetchone()
    if row is None:
        store.conn.execute(
            "INSERT INTO source_state VALUES (?,?,?,?,0)",
            (source, now if fetch_ok else None, now if n_new else None,
             None if fetch_ok else now))
        return
    if fetch_ok:
        store.conn.execute(
            "UPDATE source_state SET last_fetch_ok=?, error_since=NULL "
            "WHERE source=?", (now, source))
        if n_new:
            store.conn.execute(
                "UPDATE source_state SET last_new_item=?, flagged_dead=0 "
                "WHERE source=?", (now, source))
    elif not row["error_since"]:
        store.conn.execute(
            "UPDATE source_state SET error_since=? WHERE source=?",
            (now, source))

    row = store.conn.execute(
        "SELECT * FROM source_state WHERE source=?", (source,)).fetchone()
    problems = []
    if row["error_since"]:
        dt = (datetime.now(timezone.utc)
              - clu.parse_iso(row["error_since"])).total_seconds()
        if dt > cfg.HEARTBEAT_ERROR_MAX:
            problems.append(f"fetch failing for {dt/60:.0f} min")
    quiet_max = cfg.HEARTBEAT_QUIET_MAX.get(source)
    if quiet_max and row["last_new_item"] and business_hours_now():
        dt = (datetime.now(timezone.utc)
              - clu.parse_iso(row["last_new_item"])).total_seconds()
        if dt > quiet_max:
            problems.append(f"no new items for {dt/3600:.1f} h "
                            f"(max {quiet_max/3600:.0f} h)")
    if problems and not row["flagged_dead"]:
        subject = f"Sentinel WARNING: source '{source}' looks dead"
        body = f"Source '{source}': " + "; ".join(problems)
        log(body)
        if not no_email:
            try:
                admin = env.get("ADMIN_EMAIL_TO")
                if not admin:
                    log("heartbeat warning not emailed: ADMIN_EMAIL_TO is unset")
                else:
                    send_email(subject, body, dict(os.environ), to=admin)
            except Exception as e:  # noqa: BLE001
                log(f"heartbeat email error: {e}")
        store.conn.execute(
            "UPDATE source_state SET flagged_dead=1 WHERE source=?", (source,))


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------
def run_once(store, env, no_email=False, use_llm=True, only_source=None,
             app_store=None):
    api_key = env.get("ANTHROPIC_API_KEY", "")
    cik_map = cik_ticker_map(store)

    # Cold start: an empty DB means everything in the feeds is "new" backlog.
    # Ingest and cluster it silently — no alert storm on first boot.
    cold_start = store.conn.execute(
        "SELECT COUNT(*) FROM items").fetchone()[0] == 0
    # A dry run must NOT consume pending alerts; cold start deliberately must.
    mark_alerted = cold_start or not no_email
    if cold_start:
        log("cold start: ingesting backlog silently (alerts suppressed)")
        no_email = True

    fetchers = {
        "edgar": lambda: src.fetch_edgar(cik_map),
        "nasdaq_halts": src.fetch_nasdaq_halts,
        "nyse_halts": src.fetch_nyse_halts,
    }

    total_new = 0
    for source, fn in fetchers.items():
        if not cfg.SOURCES_ENABLED.get(source):
            continue
        if only_source and source != only_source:
            continue
        try:
            items = fn()
            ok = True
        except Exception as e:  # noqa: BLE001
            log(f"{source}: fetch error: {e}")
            items, ok = [], False

        # dedup within the batch too: one accession can appear twice in one
        # feed page (multi-registrant filings; 8-Ks cross-tagged as 425s)
        seen_batch, fresh = set(), []
        for i in items:
            if i["doc_hash"] in seen_batch or store.have_item(i["doc_hash"]):
                continue
            seen_batch.add(i["doc_hash"])
            fresh.append(i)
        log(f"{source}: {len(items)} item(s), {len(fresh)} new")
        for it in fresh:
            it["simhash"] = clu.simhash(it["title"] + " " + it["body"])
            cluster_id, is_new = assign_cluster(store, it, api_key, use_llm)
            store.insert_item(it, cluster_id)
            tier = score_and_alert(store, cluster_id, is_new, env, no_email,
                                   mark_alerted, use_llm, app_store)
            log(f"  + {it['event_type']:<12} {'NEW' if is_new else 'JOIN':<4} "
                f"c{cluster_id} tier={tier} :: {it['title'][:70]}")
            total_new += 1
        heartbeat(store, source, ok, len(fresh), env, no_email)
        store.commit()

    repair_fragmentation(store)
    if cold_start:
        # backlog items shouldn't fill the first day's digest either
        store.conn.execute("DELETE FROM digest_queue")
    flush_digest(store, env, no_email, app_store=app_store)
    store.commit()
    log(f"pass complete: {total_new} new item(s)")
    return total_new


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sentinel",
        description="Monitor for unscheduled material market events.")
    ap.add_argument("--once", action="store_true",
                    help="run a single pass and exit (idempotent, cron-safe)")
    ap.add_argument("--no-email", action="store_true",
                    help="dry run: score and log, but never send email")
    ap.add_argument("--no-llm", action="store_true",
                    help="skip LLM adjudication (ambiguous pairs -> new cluster)")
    ap.add_argument("--legacy-delivery", action="store_true",
                    help="temporary rollback: send directly to EMAIL_TO")
    ap.add_argument("--source", metavar="NAME",
                    help="poll only this source: edgar, nasdaq_halts, nyse_halts")
    ap.add_argument("--digest-now", action="store_true",
                    help="flush the digest queue immediately and exit")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    ap.add_argument("--app-db", default=DEFAULT_APP_DB,
                    help="shared users, preferences, events, and deliveries SQLite path")
    ap.add_argument("--env", default=os.path.join(HERE, ".env"),
                    help=".env path (shared with sundew)")
    args = ap.parse_args(argv)

    sundew.load_env(args.env)
    env = dict(os.environ)
    if not args.no_llm and not env.get("ANTHROPIC_API_KEY"):
        log("ERROR: ANTHROPIC_API_KEY missing (or pass --no-llm)")
        sys.exit(2)
    if not args.no_email:
        missing = [k for k in ("EMAIL_FROM",)
                   if not env.get(k)]
        if missing:
            log(f"ERROR: missing {', '.join(missing)} (or pass --no-email)")
            sys.exit(2)
        if args.legacy_delivery and not env.get("EMAIL_TO"):
            log("ERROR: legacy delivery requires EMAIL_TO")
            sys.exit(2)
    if cfg.MARKET_CAP_ENABLED and not env.get("FINNHUB_API_KEY"):
        log("WARNING: MARKET_CAP_ENABLED but FINNHUB_API_KEY is unset — market "
            "caps can't be resolved; with fail-open every company passes the "
            f"gate. Set FINNHUB_API_KEY in .env to enforce the "
            f"${cfg.MARKET_CAP_MIN_USD / 1e9:g}B floor.")

    store = Store(args.db)
    app_store = AppStore(args.app_db)
    try:
        app_store.seed_from_env(
            env,
            default_timezone=env.get("APP_DEFAULT_TIMEZONE", "America/Los_Angeles"),
            sentinel_digest_time=f"{cfg.DIGEST_HOUR_LOCAL:02d}:00",
            sentinel_market_cap_floor=cfg.MARKET_CAP_MIN_USD)
        if not args.no_email and app_store.get_settings() is None:
            log("ERROR: no notification user is configured; seed EMAIL_TO once")
            sys.exit(2)
        if args.digest_now:
            flush_digest(store, env, args.no_email, force=True,
                         app_store=None if args.legacy_delivery else app_store)
            return
        if args.once:
            run_once(store, env, args.no_email, not args.no_llm, args.source,
                     None if args.legacy_delivery else app_store)
        else:
            log(f"Sentinel starting forever-loop, poll={cfg.POLL_INTERVAL_SECONDS}s")
            while True:
                try:
                    run_once(store, env, args.no_email, not args.no_llm,
                             args.source,
                             None if args.legacy_delivery else app_store)
                except Exception as e:  # noqa: BLE001
                    log(f"pass error (continuing): {e}")
                time.sleep(cfg.POLL_INTERVAL_SECONDS)
    finally:
        store.conn.close()
        app_store.close()


if __name__ == "__main__":
    main()
