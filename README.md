# Sundew

A market-**awareness** monitor for Donald Trump's Truth Social posts. It watches
CNN's free archive, decides whether a post is relevant to markets
(macro / monetary / tariffs / geopolitics / countries / commodities / specific
companies), and emails you the relevant ones with the signal in the subject line.

**It is not a trading signal.** Sundew characterizes a post — category, affected
instruments, likely direction, and a qualitative magnitude (low/med/high). It
never emits fake numeric predictions like "SPY −1.2%" and contains no trading
logic.

## How it works

```
poll archive (~2 min)
  → dedup by post id + content hash   (he deletes/reposts; don't re-notify dupes)
  → cheap keyword pre-filter (NO LLM) (drops ~90% — political grievance, etc.)
  → LLM classify survivors only       (Anthropic claude-sonnet-4-6, strict JSON)
  → log everything to SQLite
  → store one canonical event
  → apply saved user preferences      (no new source, market-data, or LLM work)
  → send now or queue a private digest
       └─ optional Kalshi section      (fetched once per event and cached)
```

The keyword pre-filter is deliberately high-recall and cheap, so the only posts
that cost an API call are the ones with a plausible market hook. On the live
archive it lets through ~9% of posts; the LLM then marks campaign/grievance
survivors as `is_noise` and they are suppressed.

Each Sundew preference can optionally append a short list of live, on-topic
**Kalshi** prediction markets. See [Kalshi email content](#kalshi-email-content)
below. Enrichment is downstream of every filter, cached once on the canonical
event, and never changes the normal notification when it fails.

## Data source

CNN's free archive: `https://ix.cnn.io/data/truth-social/truth_archive.json`
(refreshes ~every 5 minutes; there is no official Truth Social API). It is a
JSON **list**, newest-first, ~34k posts. Verified fields:

| field              | type        | notes                                            |
|--------------------|-------------|--------------------------------------------------|
| `id`               | string      | unique, stable                                   |
| `created_at`       | string      | ISO-8601 UTC, e.g. `2026-06-30T00:23:48.147Z`    |
| `content`          | string      | **plain text** (no HTML tags) but carries HTML entities (`&amp;`); may be **empty** for media-only posts |
| `url`              | string      | canonical truthsocial.com permalink              |
| `media`            | list of str | attachment URLs (may be empty)                   |
| `replies_count`    | int         | engagement                                       |
| `reblogs_count`    | int         | engagement                                       |
| `favourites_count` | int         | engagement                                       |

## Install

Stdlib-only — **no `pip install` required**, even for the Anthropic call (it uses
`urllib`). Python 3.9+ is required for the standard-library IANA timezone data.

```bash
cd sundew
cp .env.example .env      # then fill in your keys (see below)
```

`.env` keys:

- `ANTHROPIC_API_KEY` — required unless you run `--no-classify`.
- `EMAIL_FROM`, `SMTP_PASSWORD` (+ optional `SMTP_HOST`, `SMTP_PORT`,
  `SMTP_USER`) — required unless you run `--no-email`. Defaults target Gmail; for
  Gmail use a **16-char App Password**, not your account password.
- **Legacy recipient lists** (each may be a comma-separated list of addresses):
  - `EMAIL_TO` — the shared **"everyone"** list. **Sentinel** emails it, and
    plain Sundew **falls back** to it when `EMAIL_TO_SUNDEW` is unset.
  - `EMAIL_TO_SUNDEW` — the **plain-Sundew** subset (no Kalshi section). Optional;
    unset ⇒ plain Sundew uses `EMAIL_TO`.
  - `EMAIL_TO_KS` — the **sundew.ks** subset (gets the augmented copy). Optional;
    unset ⇒ no second copy is ever sent.

  These lists seed verified users and default preferences into `app.db` on the
  first run. Saved settings are authoritative afterward and startup never
  overwrites a user's choices. For
  *"everyone gets Sentinel; each person gets sundew OR sundew.ks"*, set `EMAIL_TO` =
  the union, `EMAIL_TO_SUNDEW` = the plain folk, `EMAIL_TO_KS` = the Kalshi folk.
  For the original single-list behavior, just set `EMAIL_TO` and leave the other
  two empty. Also set `APP_DEFAULT_TIMEZONE` to the seeded user's IANA timezone,
  `SETTINGS_PUBLIC_URL` to the operator-controlled settings URL, and
  `ADMIN_EMAIL_TO` to the separate address for Sentinel source-health warnings.

## Run

All commands must be run **from the directory containing the scripts** — `cd`
into it first. Running `python3 sundew.py` from your home directory gives
`No such file or directory`.

```bash
cd ~/sundew

# Step-by-step testing (what was used to build it):
python3 sundew.py --once --no-classify --no-email   # poll + dedup + pre-filter only
python3 sundew.py --once --no-email                  # + LLM classify, dry run (no send)
python3 sundew.py --once                             # full pass incl. email

# Forever-loop (the 24/7 mode):
python3 sundew.py
```

Flags (`python3 sundew.py --help`):

| flag | effect |
|------|--------|
| `--once` | single pass and exit. **Idempotent / cron-safe**: already-seen posts are skipped, so a second run is a no-op |
| `--no-email` | classify and log, but never send (dry run) |
| `--no-classify` | poll + dedup + pre-filter only; no API calls, no cost |
| `--legacy-delivery` | temporary rollback to direct environment-list delivery |
| `--db PATH` | override the SQLite path (default `sundew.db` next to the script) |
| `--app-db PATH` | override the shared settings/delivery database (default `app.db`) |
| `--env PATH` | override the `.env` path (default next to the script) |

> **If systemd (or cron) is already running the forever-loop, never start a
> second copy by hand.** Two instances would poll and email in parallel and
> write the same database. For a manual pass always use `--once --no-email`.
> See **Command reference** below.

**Cold start:** on a fresh DB the entire ~34k backlog would otherwise be
classified. Instead Sundew marks all but the newest `COLD_START_BACKFILL`
(default 5) posts as already-seen, then only acts on genuinely new posts going
forward. Tune in `config.py`.

## Editing the gazetteers

Everything tunable lives in **`config.py`**: poll interval, the keyword
pre-filter terms, the macro / geo / commodity gazetteers, and the
company→ticker map. That's the one file to edit to broaden or narrow coverage.

## Notification settings

Start the local settings service, then open `http://127.0.0.1:8787` through an
operator-controlled tunnel:

```bash
python3 settings_server.py
```

The single page controls Sundew and Sentinel independently: enabled state,
categories, delivery mode, quiet hours, direction, minimum significance,
digest time, Sundew's Kalshi section, and Sentinel update emails. It also shows
static previews and supports a local unsubscribe. There is intentionally no
public login/session layer in the current single-user phase; keep the listener
on loopback or behind the same operator-only boundary used for `.env`.

`app.db` is opened in WAL mode by both monitors and contains verified users,
preferences, canonical notification events, and auditable per-user delivery
records. Settings apply only when a new canonical event is created; changing a
setting never replays history. `delivery_worker.py` is the single shared process
for immediate work, retries, once-per-event Kalshi enrichment, and timezone-aware
digests; the monitor processes never do per-user network work.

## Kalshi email content

Optional augmentation lives in `kalshi.py`. After a post clears the relevance
gate and user filters, the shared worker checks whether any matched recipient
has the Kalshi content option enabled. If so, it fetches at most once for that
canonical event, stores the result on `app.db`, and reuses it for every matching
delivery. It reuses the classification already in memory, so it costs **no
extra LLM call**. Kalshi's read API is public; no key is needed.

**How it finds markets — category-scoped, fetch-on-demand.** A Trump post only
falls into a handful of market buckets (macro/monetary, tariff/trade, commodity,
country/geopolitical, company), so there is no need to index all of Kalshi
(100k+ open markets, mostly auto-generated sports parlays). Instead:

1. The post's `category` + `entities` are mapped to a small set of curated
   Kalshi **series** via a hand-built seed map in `kalshi.py` (e.g.
   monetary → `KXFEDDECISION`, oil → `KXWTIW`, Iran → `KXIRANCRUDE`).
2. Only those series' open markets are listed (one public API call each),
   cached in-process for `KS_SERIES_TTL_SECONDS` so a burst of same-category
   posts doesn't refetch. Because relevant posts are rare, this is a few API
   calls a day, and prices are fresh at send time.
3. The pooled markets are ranked by title relevance (then nearest close, then
   probability) and the top `KS_MAX_MARKETS` are rendered — each with its
   current Yes/No price, close date, and a link.

**Fail-open and isolated.** All Kalshi work is wrapped and the result, including
an empty or failed result, is cached for the event. A Kalshi outage simply sends
the normal Sundew email without the section.

**Recipient migration.** See the three legacy lists (`EMAIL_TO`,
`EMAIL_TO_SUNDEW`, `EMAIL_TO_KS`) under [Install](#install). They select the
initial preferences only. After seeding, the UI and `app.db` are authoritative.

**Tuning (`config.py`).** `KS_ENABLED` (master switch), `KS_MAX_MARKETS` (3),
`KS_SERIES_TTL_SECONDS` (900), `KS_MARKETS_PER_SERIES`, `KS_FETCH_PACE_SECONDS`,
`KALSHI_BASE_URL`. The curated series seed map (`CATEGORY_SERIES` / `TERM_SERIES`)
lives in `kalshi.py` — add series there to broaden coverage. Standalone check:
`python3 kalshi.py --demo`.

> **Known gap:** the `company` category returns nothing today — Kalshi has no
> liquid per-company markets to map — so a company post gets the `.ks` email
> with no section (correct fail behavior). macro, country, and commodity are
> well covered.

## Running 24/7 on AWS (EC2)

The services use **local SQLite state that must
persist** and must never run as two copies at once — so a single small EC2
instance running the existing systemd units is the natural home: no code
changes, and the dedup databases sit on the instance's EBS volume.

**1. Launch an instance.**

- **Type:** `t4g.nano` (Graviton / arm64) is plenty — two 2-minute pollers sit
  idle almost all the time; step up to `t4g.micro` for headroom. ~$3–5/mo
  on-demand.
- **AMI:** Amazon Linux 2023 (ships Python 3.9+ and systemd; the app is
  stdlib-only, so there's no `pip` step). Ubuntu works too.
- **Storage:** the default gp3 root volume is fine — `sundew.db`, `sentinel.db`,
  and `app.db` live on it and survive stop/start and reboot. **Don't
  terminate the instance** without copying the DBs off first; take an
  occasional EBS snapshot if you want to keep the history.
- Leave IMDSv2 set to *required* (the default on new launches).

**2. Access it without opening inbound ports.** The app needs **no inbound**
traffic. Attach an IAM instance role with `AmazonSSMManagedInstanceCore` and
use **SSM Session Manager** for a shell — then the security group can be
outbound-only. (Prefer plain SSH? Open port 22 to your IP instead.) Outbound
needs are just **443** (Anthropic, Finnhub, SEC, Nasdaq/NYSE) and **587** for
Gmail SMTP submission — use 587, not 25, which AWS throttles.

**3. Install the code and the services.**

```bash
# on the instance (user 'ec2-user' on Amazon Linux):
sudo dnf install -y git             # or rsync the code up from your Mac instead
git clone <your-repo> ~/sundew && cd ~/sundew
cp .env.example .env                # then fill in the keys

sudo cp sundew.service sentinel.service delivery.service settings.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sundew sentinel delivery settings
journalctl -u sundew -u sentinel -u delivery -u settings -f
```

The bundled units already target `User=ec2-user` and `/home/ec2-user/sundew`;
edit them if you install elsewhere. They auto-restart on crash and start on
boot, so the instance recovers on its own after a stop or a maintenance reboot.

**Alternatively**, skip the forever-loop and run `--once` from cron — it's
idempotent, so a 2-minute schedule is safe:

```cron
*/2 * * * * cd /home/ec2-user/sundew && /usr/bin/python3 sundew.py --once >> sundew.log 2>&1
```

**Don't simplify the bulk insert.** SQLite runs in WAL mode with batched
writes on purpose — per-row commits fsync on every row and can hang the
~34k-row cold start for minutes. Keep the batched insert.

The master copy of the code stays at `/Users/avi/sundew`; deploy it to the
instance and restart the units (see **Update the code** below).

## Command reference — services

Four services run side by side: **`sundew`** (Trump posts), **`sentinel`**
(unscheduled events), **`delivery`** (shared outbound worker), and **`settings`**
(loopback UI/API). They share one `.env` and folder; only the two monitors poll
external sources.

**Two rules that cover the common mistakes:**

1. **`cd` into the project directory before running anything by hand.** The
   scripts aren't in your home directory.
2. **systemd is already running the forever-loops.** Never launch a bare
   `python3 sundew.py` / `python3 sentinel.py` — you would get a second instance
   polling, emailing, and writing the same database in parallel. Manual passes
   always take `--once --no-email`.

### Service control

Every command takes one or more unit names.

Run these on the host (or wrap them in your SSH invocation):

```bash
systemctl is-active sundew sentinel delivery settings
systemctl status sundew sentinel delivery settings
sudo systemctl stop sundew sentinel delivery settings
sudo systemctl start sundew sentinel delivery settings
sudo systemctl restart sundew sentinel delivery settings
sudo systemctl disable --now sentinel  # off, and stays off after reboot
sudo systemctl enable --now sentinel   # on, auto-start at boot
```

### Watching them work

```bash
journalctl -u sundew -f                  # live Sundew log (Ctrl-C stops watching, not the service)
journalctl -u sentinel -f              # live Sentinel log
journalctl -u delivery -f              # sends, digests, retries, Kalshi cache
journalctl -u settings -f              # settings API requests
journalctl -u sentinel --no-pager -n 50        # last 50 lines, no follow
journalctl -u sundew --since "1 hour ago"        # time-bounded
journalctl -u sentinel -p err --no-pager       # errors only
journalctl -u delivery | grep "delivered"      # immediate deliveries
```

### Manual passes (safe alongside the running services)

```bash
cd ~/sundew
python3 sundew.py --once --no-email        # Sundew: one pass, no send
python3 sentinel.py --once --no-email    # Sentinel: one pass, no send
python3 sentinel.py --once --no-email --no-llm --source edgar   # cheapest possible check
python3 sentinel.py --digest-now         # send the pending digest now
python3 delivery_worker.py --once        # process currently due work once
python3 delivery_worker.py --force-digests  # flush all non-empty due/future groups
```

These are safe because both databases dedup: anything the service already
processed is skipped, so a manual `--once` is a no-op rather than a
double-alert. A Sentinel dry run also does **not** consume pending alerts —
`--no-email` logs what *would* be sent without marking the cluster alerted,
so the running service still emails it for real. (Cold start is the
deliberate exception: the initial backlog is marked alerted so it never
fires.)

### Health check (one paste)

```bash
systemctl is-active sundew sentinel delivery settings
journalctl -u sundew --no-pager -n 2 -o cat
journalctl -u sentinel --no-pager -n 2 -o cat
journalctl -u delivery --no-pager -n 2 -o cat
free -m | head -2; df -h / | tail -1
```

(Wrap the four commands in your SSH invocation to run them remotely.) WAL
journaling protects both databases across an unclean shutdown, but a clean
`systemctl stop` or `shutdown` is gentler on the underlying storage.

### Update the code

Edit files in the master copy (`/Users/avi/sundew`), then get them onto the
instance and restart. If you cloned the repo on the box, `git pull` on it
(works over SSM Session Manager, no SSH port needed). To push straight from the
Mac over SSH instead:

```bash
rsync -a --exclude '__pycache__' --exclude '*.db*' \
  /Users/avi/sundew/ ec2-user@<instance>:~/sundew/
ssh ec2-user@<instance> sudo systemctl restart sundew sentinel delivery settings
```

(`--exclude '*.db*'` matters: never overwrite the instance's dedup databases.)
Restart only the service whose code you touched if you prefer — editing
`config.py` affects `sundew`, `sentinel_*.py` affects `sentinel`.

## Storage

`sundew.db` (SQLite) holds two tables:

- `seen` — dedup state (post_id, content_hash) so reposts/re-fetches don't
  re-notify.
- `posts` — a log of every processed survivor: pre-filter result, the full
  classification, and whether it was emailed. Useful for tuning.

`app.db` is the shared delivery-facing store. It contains users, monitor
preferences, category/direction selections, canonical events, and per-user
delivery state. Keep it on the same persistent volume and include it in backups;
never copy a development DB over the live file.

## Scope

This is step 1: an awareness monitor. No backtest, no trading, no real-time
latency claims.

---

# Sentinel (second service, same folder)

A live monitor for **unscheduled, unexpected material events**. Not a news
agent: if it was on a calendar, it's not news — suppress it. It surfaces and
triages for a human; it does not predict prices or trade.

Files: `sentinel.py` (orchestrator), `sentinel_sources.py` (fetchers),
`sentinel_cluster.py` (SimHash clustering + scoring), `sentinel_config.py`
(ALL tunables), `sentinel.service` (systemd). Shares `.env` with Sundew;
own database `sentinel.db`.

## Pipeline

```
fetch sources -> dedup by doc_hash -> normalize
  -> cluster: block on (shared entity, trailing 48h), score = SimHash lexical
     + entity overlap; >=0.72 same-event_type merges, <0.35 new cluster,
     between -> LLM adjudicates ("same event or not", strict JSON)
  -> novelty (new? suppressed? repeated lately? late?) and impact
     (event-type table + multi-source corroboration) scored SEPARATELY
  -> tiers: email CANDIDATE / daily digest / log only
  -> LLM TRIAGE of email candidates: fetches the ACTUAL filing text from
     EDGAR (primary doc + ex99 press release), judges actionability and
     stock direction; non-actionable candidates drop to the digest
  -> per-source heartbeats (fetch failing, or quiet too long in business hours)
```

An email therefore requires ALL of: impact >= `TIER_EMAIL`, novelty >=
`NOVELTY_MIN_ALERT`, entity not in cooldown, the company's market cap >=
`MARKET_CAP_MIN_USD` ($1B — unknown caps pass, fail-open), AND the triage LLM
judging it actionable with confidence >= `TRIAGE_MIN_CONFIDENCE`. Triage exists because
8-K item numbers can't separate "CEO terminated" from "routine director
election" — both are Item 5.02; only the filing text can tell them apart. On
live data (2026-07-31) triage killed **90%** of the 8-K email flood while
keeping restatements, forced exits, and dilutive raises. Its verdict,
direction, and headline are stored on the cluster (`direction`, `triage`
columns) for tuning.

The email subject carries the direction: 🟢 bullish / 🔴 bearish / ⚪ unclear,
plus a one-line headline of what actually happened. The direction is a
characterization of the news, not advice and not a price prediction.

Guards built in: cluster **fragmentation** (delayed repair pass re-merges
near-identical same-type clusters), cluster **collapse** (auto-merge requires
event_type agreement, not just entity overlap), **cold start** (first run on
an empty DB ingests the backlog silently), per-entity **cooldown**, UPDATE
emails only when impact rises materially.

## Market-cap gate

Sentinel only notifies about companies with a market cap at or above
`MARKET_CAP_MIN_USD` (default **$1B**). The cap is resolved per ticker from
Finnhub (`stock/profile2`; free key `FINNHUB_API_KEY` in `.env`) and cached in
`sentinel.db` (`market_cap` table, refreshed weekly like the CIK map). The
lookup runs **only for events that would otherwise notify** (email / update /
digest), so the low-tier `log` flood never touches the API; with weekly
caching the request volume is a handful of tickers a day.

Each user can raise that floor from **$1B to $1T** with the Sentinel market-cap
slider in notification settings. Sentinel resolves the cap once and stores it
on the canonical event before the shared evaluator applies each user's saved
threshold, so recipients do not multiply Finnhub requests. A user threshold
can narrow the global feed but cannot opt into companies below the production
floor.

**Fail-open:** a company whose cap can't be resolved — a filer with no mapped
ticker, a symbol Finnhub doesn't cover, a transient API error, or no key set —
is allowed through, so a lookup miss never silently drops a real event. Only a
*known* sub-$1B cap suppresses: the cluster drops to `log` (tier
`log(sub-cap)`) and is pulled from the digest queue. Multi-company clusters
(e.g. an M&A 425 naming acquirer and target) qualify if **any** party is >=
$1B. Ingestion, clustering, and novelty/impact scoring are untouched — this
gates notification only. Toggle with `MARKET_CAP_ENABLED`; without
`FINNHUB_API_KEY` Sentinel logs a startup warning and the gate is inert
(everything passes). Finnhub reports caps in millions of the reporting
currency, treated as USD (all sources are US listings).

## Sources live today

| source | feed | format (verified live 2026-07-28) |
|--------|------|-----------------------------------|
| `edgar` | `sec.gov/cgi-bin/browse-edgar?action=getcurrent...&output=atom` per form (8-K, SC 13D, 425) | Atom; title `FORM - Company (CIK)`, summary HTML carries `Item N.NN` lines inline; `<updated>` = ET acceptance time; accession no. in `<id>`. One accession can appear twice (multi-registrant, 8-K↔425 cross-tags) — batch dedup handles it. SEC requires a contact User-Agent. |
| `nasdaq_halts` | `nasdaqtrader.com/rss.aspx?feed=tradehalts` | RSS + `ndaq:` namespace; ReasonCode (T12/H10/H11/LUDP...); **snapshot of current halts**, not a stream — items persist while halted; company names abbreviated. |
| `nyse_halts` | `nyse.com/api/trade-halts/current/download` | CSV; prose reasons mapped to codes; covers Nasdaq-listed names too (overlap with the RSS is the clustering test case). |

CIK→ticker via `sec.gov/files/company_tickers.json` (~800KB, cached in the
DB, weekly refresh). Scaffolded but not yet built: OFAC SDN, BIS Entity List,
Federal Register public inspection, USTR, FDA, CourtListener.

## 8-K item triage (edit in `sentinel_config.py`)

High value: 1.01 agreements, 4.02 restatements, 5.02 officer changes,
8.01 other material, 1.03 bankruptcy, 3.01 delisting. Discounted: 2.02
earnings (scheduled — suppressed via `SUPPRESSED_EVENT_TYPES`), 9.01
exhibits. Halts: T12 news-pending ≈ the strongest breaking signal; LUDP
means the move already happened.

The `calendar` table in `sentinel.db` suppresses entity-specific scheduled
dates: `INSERT INTO calendar VALUES ('TICKER:XYZ','2026-08-15','8-K:2.02','earnings');`

## Run / test

Same rule as Sundew: `cd ~/sundew` first, and never start a second copy by hand
while the systemd service is running.

```bash
cd ~/sundew

python3 sentinel.py --once --no-email --no-llm        # ingestion only, no API calls
python3 sentinel.py --once --no-email                 # + LLM adjudication, dry run
python3 sentinel.py --once                            # full pass incl. email
python3 sentinel.py --once --no-email --source edgar  # one source only
python3 sentinel.py --digest-now                      # flush the digest queue and exit
python3 sentinel.py                                   # forever-loop (the 24/7 mode)
```

Flags (`python3 sentinel.py --help`):

| flag | effect |
|------|--------|
| `--once` | single pass and exit; idempotent / cron-safe |
| `--no-email` | score and log, but never send (dry run) |
| `--no-llm` | skip LLM adjudication entirely; ambiguous pairs become new clusters. No API cost |
| `--source NAME` | poll only one source: `edgar`, `nasdaq_halts`, `nyse_halts` |
| `--digest-now` | send the queued medium-tier digest immediately, then exit |
| `--db PATH` | override the SQLite path (default `sentinel.db`) |
| `--env PATH` | override the `.env` path (shared with Sundew) |

**Cold start:** the first run on an empty DB ingests the whole backlog
*silently* — clusters are built and scored, but no email is sent and the
digest queue is cleared, so a fresh database never causes an alert storm.

### Installing the service

```bash
sudo cp sentinel.service /etc/systemd/system/sentinel.service
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel
journalctl -u sentinel -f
```

## Storage (`sentinel.db`)

| table | holds |
|-------|-------|
| `items` | every normalized item ever ingested, with its `doc_hash` (dedup key), simhash, entities, and the cluster it joined |
| `clusters` | one row per event: event_type, merged entity set, `novelty`, `impact`, `tier`, and whether/when it was alerted |
| `calendar` | manual suppression — entity + date + event_type that is *expected*, so it never counts as news |
| `entity_alerts` | last alert time per entity, for the cooldown |
| `source_state` | per-source heartbeat: last successful fetch, last new item, error streak, dead flag |
| `llm_log` | every adjudication the LLM was asked to make, with its verdict — read this to tune `SIM_MERGE`/`SIM_NEW` |
| `digest_queue` | medium-tier clusters waiting for the daily digest |
| `kv` | cached CIK→ticker map and digest bookkeeping |
| `market_cap` | per-ticker market cap (USD) + resolve time; NULL cap = confirmed no-data (cached so it isn't re-queried); weekly refresh |

Useful inspection queries:

```bash
cd ~/sundew
# what got emailed today, highest impact first
sqlite3 sentinel.db "SELECT cluster_id,event_type,impact,novelty,title FROM clusters
  WHERE alerted_at IS NOT NULL ORDER BY impact DESC LIMIT 20;"

# tier distribution — are the thresholds sane?
sqlite3 sentinel.db "SELECT tier,COUNT(*) FROM clusters GROUP BY tier;"

# every LLM adjudication and its verdict
sqlite3 sentinel.db "SELECT at,cluster_id,verdict,raw FROM llm_log ORDER BY at DESC LIMIT 20;"

# are all sources alive?
sqlite3 sentinel.db "SELECT * FROM source_state;"
```

(`sqlite3` may need `sudo apt install sqlite3`; otherwise use
`python3 -c "import sqlite3; ..."`.)

## Tuning knobs that matter

`SIM_MERGE`/`SIM_NEW` (the two thresholds), `TIER_EMAIL`/`TIER_DIGEST`,
`NOVELTY_MIN_ALERT` (0.45 — raised from 0.30 after stale halts kept
alerting), `TRIAGE_MIN_CONFIDENCE`, `TRIAGE_ENABLED` (kill switch for the
whole gate), `DERIVATIVE_ISSUE_DISCOUNT` (warrants/rights/units halting
alongside their common stock), `ENTITY_COOLDOWN_HOURS`, `ITEM_IMPACT_8K`,
`HALT_REASON_IMPACT`, `HEARTBEAT_QUIET_MAX`, `MARKET_CAP_MIN_USD` (the $1B
notification floor) / `MARKET_CAP_ENABLED` (gate kill switch). Novelty, impact, and every
triage verdict are logged per cluster in `sentinel.db` precisely so these can
be tuned independently against real history:

```bash
# review triage verdicts — is it killing things you wanted?
sqlite3 sentinel.db "SELECT cluster_id,tier,direction,
  json_extract(triage,'$.actionable') act,
  json_extract(triage,'$.headline') headline
  FROM clusters WHERE triage IS NOT NULL ORDER BY cluster_id DESC LIMIT 20;"
```

If email volume is still wrong, the order to try: (1) `TRIAGE_MIN_CONFIDENCE`
0.5→0.6, (2) `TIER_EMAIL` 0.70→0.75 (drops bare 5.02/1.01 from candidacy
entirely), (3) tighten the `TRIAGE_SYSTEM` prompt's definition of
actionable in `sentinel.py`.
