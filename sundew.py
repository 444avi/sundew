#!/usr/bin/env python3
"""
Sundew — a market-awareness monitor for Donald Trump's Truth Social posts.

It is an AWARENESS tool, not a trading signal. It characterizes a post
(category, affected instruments, direction, magnitude) and emails the
market-relevant ones. It never emits fake numeric predictions and contains
no trading logic.

Pipeline:
  poll archive -> dedup (id + content hash) -> cheap keyword pre-filter
  -> LLM classify survivors -> log to SQLite -> email the relevant ones.

Runs as a forever-loop, or `--once` (idempotent, cron-safe), with `--no-email`
for dry runs. Stdlib only; secrets come from .env. See README.md.
"""

import argparse
import hashlib
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

import config
import kalshi
import sesmail
from notification_app import AppStore, sundew_categories

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "sundew.db")
DEFAULT_APP_DB = os.path.join(HERE, "app.db")
DEFAULT_ENV = os.path.join(HERE, ".env")
USER_AGENT = "sundew/0.1 (+https://github.com/) market-awareness-monitor"


# ---------------------------------------------------------------------------
# .env loading (no python-dotenv dependency)
# ---------------------------------------------------------------------------
def load_env(path=DEFAULT_ENV):
    """Read KEY=VALUE lines from .env into os.environ (does not overwrite
    variables already set in the real environment)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


# ---------------------------------------------------------------------------
# Fetch + normalize
# ---------------------------------------------------------------------------
def clean_text(raw):
    """CNN's archive content is plain text but carries HTML entities
    (&amp;, &#39;, ...). Unescape and collapse whitespace."""
    return " ".join(html.unescape(raw or "").split())


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(raw):
    """Map one raw archive record to the fields we use.

    Real archive fields (verified live): id, created_at, content, url, media,
    replies_count, reblogs_count, favourites_count. `content` is plain text
    (no HTML tags) but may be empty (media-only posts) and may contain HTML
    entities.
    """
    text = clean_text(raw.get("content", ""))
    return {
        "id": str(raw.get("id", "")),
        "created_at": raw.get("created_at", ""),
        "text": text,
        "url": raw.get("url", ""),
        "media": raw.get("media") or [],
        "hash": content_hash(text),
    }


def fetch_posts(url=config.ARCHIVE_URL, timeout=config.HTTP_TIMEOUT_SECONDS):
    """Fetch and parse the archive. Returns posts newest-first (as served)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON list, got {type(data).__name__}")
    return [normalize(r) for r in data]


# ---------------------------------------------------------------------------
# Cheap keyword pre-filter (no LLM)
# ---------------------------------------------------------------------------
def _build_prefilter_terms():
    terms = set(t.lower() for t in config.PREFILTER_EXTRA_TERMS)
    terms.update(config.MACRO_TERMS)
    terms.update(config.GEO_TERMS)
    terms.update(config.COMMODITY_TERMS)
    terms.update(config.COMPANY_TICKERS)
    # Drop very short keys that would cause false matches.
    return sorted(t for t in terms if len(t) >= 2)


PREFILTER_TERMS = _build_prefilter_terms()


def prefilter(text):
    """Return (passed, matched_terms). A post passes if it contains any
    gazetteer/trigger term. Matching is substring on a space-padded,
    lowercased string so multi-word terms work and we avoid most partial-word
    hits. Cheap and high-recall by design."""
    if not text:
        return False, []
    hay = " " + text.lower() + " "
    matched = []
    for term in PREFILTER_TERMS:
        needle = term if " " in term else " " + term + " "
        # for single words, require word-ish boundaries via padding;
        # for phrases, plain substring is fine.
        if (" " in term and term in hay) or (
            " " not in term and (needle in hay)
        ):
            matched.append(term)
    return (len(matched) > 0), matched


# ---------------------------------------------------------------------------
# SQLite store (dedup state + log)
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, path=DEFAULT_DB):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL + relaxed sync: far fewer fsyncs, which matters on SD cards
        # (a Raspberry Pi cold start went from hours to seconds).
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                post_id       TEXT PRIMARY KEY,
                content_hash  TEXT NOT NULL,
                created_at    TEXT,
                first_seen_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_seen_hash ON seen(content_hash);

            CREATE TABLE IF NOT EXISTS posts (
                post_id      TEXT PRIMARY KEY,
                created_at   TEXT,
                url          TEXT,
                text         TEXT,
                content_hash TEXT,
                passed_prefilter INTEGER,
                matched_terms    TEXT,
                category     TEXT,
                direction    TEXT,
                magnitude    TEXT,
                is_noise     INTEGER,
                confidence   REAL,
                entities     TEXT,
                rationale    TEXT,
                notified     INTEGER DEFAULT 0,
                processed_at TEXT
            );
            """
        )
        self.conn.commit()

    def id_seen(self, post_id):
        cur = self.conn.execute("SELECT 1 FROM seen WHERE post_id=?", (post_id,))
        return cur.fetchone() is not None

    def hash_seen(self, h):
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE content_hash=? LIMIT 1", (h,)
        )
        return cur.fetchone() is not None

    def mark_seen(self, post):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen(post_id, content_hash, created_at, first_seen_at)"
            " VALUES (?,?,?,?)",
            (post["id"], post["hash"], post["created_at"], _now_iso()),
        )
        self.conn.commit()

    def mark_seen_many(self, posts):
        """Bulk version: one transaction, one commit. Per-row commits are
        ruinously slow on SD cards (every commit is an fsync)."""
        now = _now_iso()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen(post_id, content_hash, created_at, first_seen_at)"
            " VALUES (?,?,?,?)",
            [(p["id"], p["hash"], p["created_at"], now) for p in posts],
        )
        self.conn.commit()

    def count_seen(self):
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def log_post(self, post, passed, matched, classification, notified):
        c = classification or {}
        self.conn.execute(
            """INSERT OR REPLACE INTO posts
               (post_id, created_at, url, text, content_hash, passed_prefilter,
                matched_terms, category, direction, magnitude, is_noise,
                confidence, entities, rationale, notified, processed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                post["id"], post["created_at"], post["url"], post["text"],
                post["hash"], int(passed), json.dumps(matched),
                c.get("category"), c.get("direction"), c.get("magnitude"),
                int(bool(c.get("is_noise"))) if c else None,
                c.get("confidence"),
                json.dumps(c.get("entities", [])) if c else None,
                c.get("rationale"), int(notified), _now_iso(),
            ),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# LLM classifier (Anthropic Messages API via stdlib urllib)
# ---------------------------------------------------------------------------
CLASSIFIER_SYSTEM = """You are a financial-markets awareness classifier. You read a single social-media \
post by Donald Trump and decide whether it is relevant to financial markets \
(macroeconomics, monetary policy, trade/tariffs, geopolitics, specific \
countries, commodities, or specific public companies).

You are an AWARENESS tool, not a trading signal. Characterize the post. Do NOT \
invent numeric price predictions. Do NOT give trading advice.

Respond with STRICT JSON only — no prose, no markdown fences. Schema:
{
  "category": one of ["tariff","monetary","company","geopolitical","commodity","other"],
  "entities": [ {"name": str, "ticker": str|null, "type": one of ["company","country","commodity","index","currency","sector"]} ],
  "direction": one of ["bullish","bearish","unclear"],   // likely directional effect on the affected instruments
  "magnitude": one of ["low","medium","high"],            // how market-moving, qualitatively
  "is_noise": boolean,   // true if this is personal grievance / campaign rhetoric / media attack with no real market content
  "confidence": number between 0 and 1,
  "rationale": one short sentence (<= 25 words) explaining the call
}

Rules:
- If the post is political grievance, insults, campaign slogans, religious or \
personal content with no concrete economic/market hook, set is_noise=true, \
category="other", direction="unclear".
- Attach tickers only when you are confident. Use the provided company->ticker \
hints when applicable; otherwise ticker=null.
- "direction" is the plausible effect on the named instruments, not a guarantee."""


def build_user_prompt(post):
    hints = {
        "company_tickers": config.COMPANY_TICKERS,
        "macro_terms": list(config.MACRO_TERMS.keys()),
        "geo_terms": list(config.GEO_TERMS.keys()),
        "commodity_terms": list(config.COMMODITY_TERMS.keys()),
    }
    return (
        "Reference hints (term -> meaning / ticker):\n"
        + json.dumps(hints, indent=0)
        + "\n\nPost:\n"
        + f"date: {post['created_at']}\n"
        + f"text: {post['text']}\n\n"
        + "Return the JSON object now."
    )


def classify(post, api_key, model=config.ANTHROPIC_MODEL,
             timeout=config.HTTP_TIMEOUT_SECONDS):
    """Call the Anthropic Messages API and return the parsed classification
    dict. Raises on transport errors; returns a fallback dict on bad JSON."""
    body = {
        "model": model,
        "max_tokens": 512,
        "temperature": 0,
        "system": CLASSIFIER_SYSTEM,
        "messages": [{"role": "user", "content": build_user_prompt(post)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    text = "".join(
        b.get("text", "") for b in payload.get("content", [])
        if b.get("type") == "text"
    ).strip()
    return _parse_classification(text)


def _parse_classification(text):
    """Extract the JSON object from the model's reply, tolerating stray
    fences or text around it."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1:
        s = s[start:end + 1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return {
            "category": "other", "entities": [], "direction": "unclear",
            "magnitude": "low", "is_noise": True, "confidence": 0.0,
            "rationale": "classifier returned unparseable JSON",
        }
    obj.setdefault("entities", [])
    obj.setdefault("direction", "unclear")
    obj.setdefault("magnitude", "low")
    obj.setdefault("category", "other")
    obj.setdefault("is_noise", False)
    obj.setdefault("confidence", 0.0)
    obj.setdefault("rationale", "")
    return obj


# ---------------------------------------------------------------------------
# Relevance gate + email
# ---------------------------------------------------------------------------
def is_relevant(c):
    """A classified post earns a notification if it is not noise, not in the
    'other' bucket, and the model has some confidence."""
    if not c or c.get("is_noise"):
        return False
    if c.get("category") == "other":
        return False
    return float(c.get("confidence") or 0) >= 0.4


_DIR_EMOJI = {"bullish": "🟢", "bearish": "🔴", "unclear": "⚪"}


def email_subject(post, c):
    arrow = _DIR_EMOJI.get(c.get("direction"), "⚪")
    cat = (c.get("category") or "other").upper()
    ents = ", ".join(
        e.get("ticker") or e.get("name", "")
        for e in (c.get("entities") or [])
    )[:60]
    mag = (c.get("magnitude") or "").upper()
    tag = f"{arrow} [{cat}/{mag}]"
    return f"Sundew {tag} {ents}".strip()


def email_body(post, c, extra_section=""):
    """The email body. `extra_section` (default "") is appended after the
    disclaimer; with the default the output is byte-for-byte today's."""
    ents = c.get("entities") or []
    ent_lines = "\n".join(
        f"  - {e.get('name')} "
        f"({e.get('ticker') or 'n/a'}, {e.get('type') or '?'})"
        for e in ents
    ) or "  (none identified)"
    body = (
        f"Trump post — {post['created_at']}\n"
        f"{post['url']}\n\n"
        f"{post['text']}\n\n"
        f"{'-' * 60}\n"
        f"Sundew assessment (awareness only — NOT a trade signal):\n"
        f"  category:   {c.get('category')}\n"
        f"  direction:  {c.get('direction')}\n"
        f"  magnitude:  {c.get('magnitude')}\n"
        f"  confidence: {c.get('confidence')}\n"
        f"  entities:\n{ent_lines}\n"
        f"  why: {c.get('rationale')}\n\n"
        f"This characterizes possible market relevance. It is not financial "
        f"advice and contains no price prediction.\n"
    )
    if extra_section:
        body += "\n" + extra_section
    return body


def sundew_recipient(env):
    """The plain-Sundew list. EMAIL_TO_SUNDEW if set, else EMAIL_TO. Keeping this
    separate from EMAIL_TO lets EMAIL_TO stay the shared "everyone" list that
    Sentinel emails, while plain Sundew (EMAIL_TO_SUNDEW) and Sundew.ks (EMAIL_TO_KS)
    address their own subsets. Unset EMAIL_TO_SUNDEW => falls back to EMAIL_TO, so
    an existing single-list setup is unchanged."""
    return env.get("EMAIL_TO_SUNDEW") or env.get("EMAIL_TO")


def send_email(post, c, env, to=None, extra_section=""):
    """Send one email. Recipient defaults to the plain-Sundew list
    (EMAIL_TO_SUNDEW or EMAIL_TO); pass `to` for a different recipient (e.g. the
    Kalshi-augmented EMAIL_TO_KS) and `extra_section` to append a block to the
    body."""
    msg = EmailMessage()
    msg["Subject"] = email_subject(post, c)
    msg["From"] = env["EMAIL_FROM"]
    msg["To"] = to or sundew_recipient(env)
    msg.set_content(email_body(post, c, extra_section))
    sesmail.send(env, msg)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{_now_iso()}] {msg}", flush=True)


def cold_start_backfill(store, posts):
    """On an empty DB, mark all but the newest COLD_START_BACKFILL posts as
    seen so we don't classify the entire historical backlog. Returns the
    number marked."""
    n = config.COLD_START_BACKFILL
    if n <= 0:
        return 0
    # posts are newest-first; skip the newest n, mark the rest seen.
    to_mark = posts[n:]
    store.mark_seen_many(to_mark)
    log(f"cold start: backfilled {len(to_mark)} historical posts as seen; "
        f"will only act on the newest {n}")
    return len(to_mark)


def run_once(store, env, no_email=False, classify_enabled=True, app_store=None):
    """One full pass. Idempotent: already-seen posts are skipped, so running
    twice is a no-op for the second run."""
    posts = fetch_posts()
    log(f"fetched {len(posts)} posts")

    # Whether to also send the Kalshi-augmented copy. Kalshi markets are
    # fetched on demand per relevant post (in the branch below), so there is
    # nothing to prepare here.
    ks_on = bool(config.KS_ENABLED and env.get("EMAIL_TO_KS"))

    if store.count_seen() == 0:
        cold_start_backfill(store, posts)

    new_posts, reposts = [], 0
    # process oldest-first among the unseen so chronological order is natural
    for p in reversed(posts):
        if store.id_seen(p["id"]):
            continue
        if p["text"] and store.hash_seen(p["hash"]):
            # same content under a new id = repost; record but don't re-notify
            store.mark_seen(p)
            reposts += 1
            continue
        new_posts.append(p)

    log(f"new posts: {len(new_posts)} (skipped {reposts} reposts)")

    notified = 0
    for p in new_posts:
        passed, matched = prefilter(p["text"])
        if not passed:
            store.log_post(p, False, matched, None, False)
            store.mark_seen(p)
            continue

        log(f"prefilter PASS {p['id']} terms={matched[:5]} :: {p['text'][:80]!r}")

        classification = None
        did_notify = False
        if classify_enabled:
            try:
                classification = classify(p, env["ANTHROPIC_API_KEY"])
            except urllib.error.HTTPError as e:
                log(f"  classify HTTP {e.code}: {e.read()[:200]!r}")
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                log(f"  classify error: {e}")

            if classification:
                rel = is_relevant(classification)
                log(f"  -> {classification.get('category')}/"
                    f"{classification.get('direction')}/"
                    f"{classification.get('magnitude')} "
                    f"noise={classification.get('is_noise')} "
                    f"conf={classification.get('confidence')} relevant={rel}")
                if rel:
                    subj = email_subject(p, classification)
                    sundew_to = sundew_recipient(env)
                    if not no_email and app_store is not None:
                        result = app_store.publish_event(
                            monitor="sundew",
                            source_event_id=p["id"],
                            event_version=1,
                            event_kind="original",
                            categories=sundew_categories(classification),
                            direction=classification.get("direction") or "unclear",
                            significance=classification.get("magnitude") or "low",
                            canonical_tier="immediate",
                            occurred_at=p.get("created_at") or _now_iso(),
                            subject_data={"subject": subj},
                            body_data={
                                "body": email_body(p, classification),
                                "post": p,
                                "classification": classification,
                            },
                        )
                        counts = result["decisions"]
                        if result["created"]:
                            log(f"  canonical event {result['event_id']} created; "
                                f"delivery decisions={counts}")
                    # Compatibility fallback for callers that have not supplied
                    # app_store. The CLI always supplies it after migration.
                    elif not no_email:
                        try:
                            send_email(p, classification, env, to=sundew_to)
                            did_notify = True
                            notified += 1
                            log(f"  emailed: {subj}")
                        except Exception as e:  # noqa: BLE001
                            log(f"  email error: {e}")
                    else:
                        if app_store is not None:
                            log(f"  [no-email] would create a canonical Sundew event: {subj}")
                        else:
                            log(f"  [no-email] would send -> {sundew_to}: {subj}")

                    # Legacy-only sundew.ks routing. The settings delivery worker
                    # enriches once per event and sends one private To address.
                    if app_store is None and ks_on:
                        section = ""
                        try:
                            section = kalshi.render_section(
                                kalshi.find_markets(classification))
                        except Exception as e:  # noqa: BLE001
                            log(f"  kalshi section error "
                                f"(sending .ks without it): {e}")
                        tag = "with" if section else "no"
                        if not no_email:
                            try:
                                send_email(p, classification, env,
                                           to=env["EMAIL_TO_KS"],
                                           extra_section=section)
                                log(f"  emailed .ks -> {env['EMAIL_TO_KS']} "
                                    f"({tag} Kalshi section)")
                            except Exception as e:  # noqa: BLE001
                                log(f"  .ks email error: {e}")
                        else:
                            log(f"  [no-email] would send .ks -> "
                                f"{env['EMAIL_TO_KS']} ({tag} Kalshi section): "
                                f"{subj}")
                            for ln in section.splitlines():
                                log(f"      | {ln}")

        store.log_post(p, True, matched, classification, did_notify)
        store.mark_seen(p)

    log(f"pass complete: {notified} notification(s) sent")
    return notified


def require_env(env, keys, what):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        log(f"ERROR: missing {what} in .env: {', '.join(missing)}")
        sys.exit(2)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sundew",
        description="Market-awareness monitor for Trump Truth Social posts.",
    )
    ap.add_argument("--once", action="store_true",
                    help="run a single pass and exit (idempotent, cron-safe)")
    ap.add_argument("--no-email", action="store_true",
                    help="dry run: classify and log, but never send email")
    ap.add_argument("--no-classify", action="store_true",
                    help="skip the LLM; only poll + dedup + pre-filter "
                         "(step-1 testing)")
    ap.add_argument("--legacy-delivery", action="store_true",
                    help="temporary rollback: send directly to legacy env lists")
    ap.add_argument("--db", default=DEFAULT_DB, help="SQLite path")
    ap.add_argument("--app-db", default=DEFAULT_APP_DB,
                    help="shared users, preferences, events, and deliveries SQLite path")
    ap.add_argument("--env", default=DEFAULT_ENV, help=".env path")
    args = ap.parse_args(argv)

    load_env(args.env)
    env = dict(os.environ)

    classify_enabled = not args.no_classify
    if classify_enabled:
        require_env(env, ["ANTHROPIC_API_KEY"], "API key")
    if not args.no_email:
        require_env(
            env,
            ["EMAIL_FROM"],
            "email config: EMAIL_FROM (or pass --no-email)",
        )
        if args.legacy_delivery and not sundew_recipient(env):
            log("ERROR: legacy delivery requires EMAIL_TO_SUNDEW or EMAIL_TO")
            sys.exit(2)
    store = Store(args.db)
    app_store = AppStore(args.app_db)
    try:
        app_store.seed_from_env(
            env, default_timezone=env.get("APP_DEFAULT_TIMEZONE", "America/Los_Angeles"))
        if not args.no_email and app_store.get_settings() is None:
            log("ERROR: no notification user is configured; seed EMAIL_TO, "
                "EMAIL_TO_SUNDEW, or EMAIL_TO_KS once")
            sys.exit(2)
        if args.once:
            run_once(store, env, no_email=args.no_email,
                     classify_enabled=classify_enabled,
                     app_store=None if args.legacy_delivery else app_store)
        else:
            log(f"Sundew starting forever-loop, poll={config.POLL_INTERVAL_SECONDS}s")
            while True:
                try:
                    run_once(store, env, no_email=args.no_email,
                             classify_enabled=classify_enabled,
                             app_store=None if args.legacy_delivery else app_store)
                except Exception as e:  # noqa: BLE001 — never die on one bad pass
                    log(f"pass error (continuing): {e}")
                time.sleep(config.POLL_INTERVAL_SECONDS)
    finally:
        store.close()
        app_store.close()


if __name__ == "__main__":
    main()
