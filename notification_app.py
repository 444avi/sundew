#!/usr/bin/env python3
"""Shared notification preferences, canonical events, and delivery worker.

The monitors remain responsible for fetching and classifying.  This module is
deliberately downstream-only: every decision is made from fields already stored
on a canonical event, and no evaluator path calls a source, market-data API, or
LLM.
"""

import hashlib
import json
import re
import smtplib
import sqlite3
from datetime import datetime, time, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MONITORS = {"vavi", "sentinel"}
CATEGORIES = {"company", "commodity", "country", "macro"}
DIRECTIONS = {"bullish", "bearish", "unclear"}
SIGNIFICANCE = {"low": 0, "medium": 1, "high": 2}
DELIVERY_MODES = {"vavi": {"immediate", "digest"},
                  "sentinel": {"immediate", "smart", "digest"}}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DEFAULT_SENTINEL_MARKET_CAP_USD = 1_000_000_000
MAX_SENTINEL_MARKET_CAP_USD = 1_000_000_000_000


class ValidationError(ValueError):
    def __init__(self, errors):
        super().__init__("invalid settings")
        self.errors = errors


def utcnow():
    return datetime.now(timezone.utc)


def iso_utc(value=None):
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def normalize_email(value):
    return (value or "").strip().lower()


def parse_recipients(value):
    return sorted({normalize_email(v) for v in (value or "").split(",")
                   if normalize_email(v)})


def validate_timezone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValidationError({"timezone": "Use a valid IANA timezone, such as America/Los_Angeles."})


def parse_local_time(value, field="time"):
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        raise ValidationError({field: "Use a 24-hour time in HH:MM format."})
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour, minute)


def is_quiet_at(now, timezone_name, start_local, end_local):
    """Whether an instant falls inside a wall-clock interval.

    Converting the instant to the user's ZoneInfo first makes the comparison
    daylight-saving aware. Equal start/end is treated as a full-day interval.
    """
    local_clock = now.astimezone(ZoneInfo(timezone_name)).time().replace(tzinfo=None)
    start = parse_local_time(start_local, "quiet_start_local")
    end = parse_local_time(end_local, "quiet_end_local")
    if start == end:
        return True
    if start < end:
        return start <= local_clock < end
    return local_clock >= start or local_clock < end


def _normalized_wall_time(day, wall_time, zone):
    """Resolve a local schedule through DST gaps/folds deterministically.

    A nonexistent spring-forward time is normalized to the first corresponding
    valid local time. An ambiguous fall-back time uses the first occurrence.
    """
    candidate = datetime.combine(day, wall_time, tzinfo=zone).replace(fold=0)
    return candidate.astimezone(timezone.utc).astimezone(zone)


def next_digest_at(now, timezone_name, digest_time_local):
    zone = ZoneInfo(timezone_name)
    wall = parse_local_time(digest_time_local, "digest_time_local")
    local_now = now.astimezone(zone)
    candidate = _normalized_wall_time(local_now.date(), wall, zone)
    if candidate <= local_now:
        candidate = _normalized_wall_time(local_now.date() + timedelta(days=1), wall, zone)
    return candidate.astimezone(timezone.utc)


def vavi_categories(classification):
    labels = set()
    category = (classification.get("category") or "").lower()
    if category == "company":
        labels.add("company")
    if category == "commodity":
        labels.add("commodity")
    if category == "geopolitical":
        labels.add("country")
    if category in {"monetary", "tariff"}:
        labels.add("macro")
    for entity in classification.get("entities") or []:
        kind = (entity.get("type") or "").lower()
        if kind in {"company", "commodity", "country"}:
            labels.add(kind)
        elif kind in {"index", "currency", "sector"}:
            labels.add("macro")
    return sorted(labels)


def sentinel_categories(event_type, entities=None):
    event_type = (event_type or "").upper()
    if event_type.startswith("HALT:MWC"):
        return ["macro"]
    return ["company"]


def sentinel_significance(impact):
    impact = float(impact)
    if impact >= 0.85:
        return "high"
    if impact >= 0.70:
        return "medium"
    return "low"


class AppStore:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def _schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email_verified_at TEXT,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','unsubscribed','disabled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notification_preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            monitor TEXT NOT NULL CHECK(monitor IN ('vavi','sentinel')),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            delivery_mode TEXT NOT NULL
                CHECK(delivery_mode IN ('immediate','smart','digest')),
            minimum_significance TEXT NOT NULL DEFAULT 'low'
                CHECK(minimum_significance IN ('low','medium','high')),
            minimum_market_cap_usd INTEGER
                CHECK(minimum_market_cap_usd IS NULL OR minimum_market_cap_usd >= 0),
            quiet_hours_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(quiet_hours_enabled IN (0,1)),
            quiet_start_local TEXT NOT NULL DEFAULT '22:00'
                CHECK(quiet_start_local GLOB '[0-2][0-9]:[0-5][0-9]'
                      AND CAST(substr(quiet_start_local,1,2) AS INTEGER) BETWEEN 0 AND 23
                      AND CAST(substr(quiet_start_local,4,2) AS INTEGER) BETWEEN 0 AND 59),
            quiet_end_local TEXT NOT NULL DEFAULT '07:00'
                CHECK(quiet_end_local GLOB '[0-2][0-9]:[0-5][0-9]'
                      AND CAST(substr(quiet_end_local,1,2) AS INTEGER) BETWEEN 0 AND 23
                      AND CAST(substr(quiet_end_local,4,2) AS INTEGER) BETWEEN 0 AND 59),
            digest_time_local TEXT NOT NULL DEFAULT '17:00'
                CHECK(digest_time_local GLOB '[0-2][0-9]:[0-5][0-9]'
                      AND CAST(substr(digest_time_local,1,2) AS INTEGER) BETWEEN 0 AND 23
                      AND CAST(substr(digest_time_local,4,2) AS INTEGER) BETWEEN 0 AND 59),
            kalshi_enabled INTEGER NOT NULL DEFAULT 0 CHECK(kalshi_enabled IN (0,1)),
            update_emails_enabled INTEGER NOT NULL DEFAULT 0
                CHECK(update_emails_enabled IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, monitor),
            CHECK(monitor = 'sentinel' OR delivery_mode != 'smart'),
            CHECK(monitor = 'vavi' OR kalshi_enabled = 0),
            CHECK(monitor = 'sentinel' OR update_emails_enabled = 0)
        );

        CREATE TABLE IF NOT EXISTS preference_categories (
            preference_id INTEGER NOT NULL REFERENCES notification_preferences(id)
                ON DELETE CASCADE,
            category TEXT NOT NULL
                CHECK(category IN ('company','commodity','country','macro')),
            PRIMARY KEY(preference_id, category)
        );

        CREATE TABLE IF NOT EXISTS preference_directions (
            preference_id INTEGER NOT NULL REFERENCES notification_preferences(id)
                ON DELETE CASCADE,
            direction TEXT NOT NULL
                CHECK(direction IN ('bullish','bearish','unclear')),
            PRIMARY KEY(preference_id, direction)
        );

        CREATE TABLE IF NOT EXISTS notification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor TEXT NOT NULL CHECK(monitor IN ('vavi','sentinel')),
            source_event_id TEXT NOT NULL,
            event_version INTEGER NOT NULL CHECK(event_version >= 1),
            event_kind TEXT NOT NULL CHECK(event_kind IN ('original','update')),
            categories TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('bullish','bearish','unclear')),
            significance TEXT NOT NULL CHECK(significance IN ('low','medium','high')),
            canonical_tier TEXT NOT NULL CHECK(canonical_tier IN ('immediate','digest')),
            market_cap_usd REAL CHECK(market_cap_usd IS NULL OR market_cap_usd >= 0),
            occurred_at TEXT NOT NULL,
            subject_data TEXT NOT NULL,
            body_data TEXT NOT NULL,
            kalshi_data TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(monitor, source_event_id, event_version)
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_id INTEGER NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
            status TEXT NOT NULL
                CHECK(status IN ('pending','queued_digest','sending','sent','suppressed','failed')),
            delivery_method TEXT NOT NULL CHECK(delivery_method IN ('immediate','digest')),
            suppression_reason TEXT,
            scheduled_for TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_delivery_work
            ON deliveries(status, delivery_method, scheduled_for);
        CREATE INDEX IF NOT EXISTS idx_event_source
            ON notification_events(monitor, source_event_id, event_kind);
        """)
        # CREATE TABLE IF NOT EXISTS does not evolve existing installations.
        # Keep these additive migrations local and idempotent so the settings
        # service and workers can safely start in either order after deploy.
        preference_columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(notification_preferences)")}
        if "minimum_market_cap_usd" not in preference_columns:
            self.conn.execute(
                "ALTER TABLE notification_preferences "
                "ADD COLUMN minimum_market_cap_usd INTEGER")
        event_columns = {row[1] for row in self.conn.execute(
            "PRAGMA table_info(notification_events)")}
        if "market_cap_usd" not in event_columns:
            self.conn.execute(
                "ALTER TABLE notification_events ADD COLUMN market_cap_usd REAL")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def _ensure_preference(self, user_id, monitor, kalshi=False,
                           digest_time="17:00",
                           minimum_market_cap_usd=DEFAULT_SENTINEL_MARKET_CAP_USD):
        now = iso_utc()
        mode = "immediate" if monitor == "vavi" else "smart"
        updates = 1 if monitor == "sentinel" else 0
        self.conn.execute(
            """INSERT OR IGNORE INTO notification_preferences
               (user_id, monitor, enabled, delivery_mode,
                minimum_significance, minimum_market_cap_usd, quiet_hours_enabled,
                quiet_start_local, quiet_end_local, digest_time_local,
                kalshi_enabled, update_emails_enabled, created_at, updated_at)
               VALUES (?,?,1,?,'low',?,0,'22:00','07:00',?,?,?,?,?)""",
            (user_id, monitor, mode,
             minimum_market_cap_usd if monitor == "sentinel" else None,
             digest_time, int(kalshi), updates, now, now))
        pref = self.conn.execute(
            "SELECT id FROM notification_preferences WHERE user_id=? AND monitor=?",
            (user_id, monitor)).fetchone()
        if monitor == "sentinel":
            self.conn.execute(
                """UPDATE notification_preferences
                   SET minimum_market_cap_usd=?
                   WHERE id=? AND minimum_market_cap_usd IS NULL""",
                (minimum_market_cap_usd, pref["id"]))
        self.conn.executemany(
            "INSERT OR IGNORE INTO preference_categories VALUES (?,?)",
            [(pref["id"], value) for value in sorted(CATEGORIES)])
        self.conn.executemany(
            "INSERT OR IGNORE INTO preference_directions VALUES (?,?)",
            [(pref["id"], value) for value in sorted(DIRECTIONS)])

    def seed_from_env(self, env, default_timezone="America/Los_Angeles",
                      sentinel_digest_time="17:00",
                      sentinel_market_cap_floor=DEFAULT_SENTINEL_MARKET_CAP_USD):
        """Idempotently migrate legacy lists into verified active users."""
        validate_timezone(default_timezone)
        sentinel = set(parse_recipients(env.get("EMAIL_TO")))
        plain = set(parse_recipients(env.get("EMAIL_TO_VAVI")))
        if not plain:
            plain = set(sentinel)
        kalshi_users = set(parse_recipients(env.get("EMAIL_TO_KS")))
        now = iso_utc()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for email in sorted(sentinel | plain | kalshi_users):
                self.conn.execute(
                    """INSERT OR IGNORE INTO users
                       (email,email_verified_at,timezone,status,created_at,updated_at)
                       VALUES (?,?,?,'active',?,?)""",
                    (email, now, default_timezone, now, now))
                user = self.conn.execute(
                    "SELECT id FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
                if email in sentinel:
                    self._ensure_preference(user["id"], "sentinel",
                                            digest_time=sentinel_digest_time,
                                            minimum_market_cap_usd=sentinel_market_cap_floor)
                if email in plain or email in kalshi_users:
                    self._ensure_preference(user["id"], "vavi",
                                            kalshi=email in kalshi_users)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def _preference_dict(self, row):
        if row is None:
            return None
        pref = dict(row)
        pref_id = pref.pop("id")
        pref.pop("user_id", None)
        pref.pop("created_at", None)
        pref.pop("updated_at", None)
        for key in ("enabled", "quiet_hours_enabled", "kalshi_enabled",
                    "update_emails_enabled"):
            pref[key] = bool(pref[key])
        pref["categories"] = [r[0] for r in self.conn.execute(
            "SELECT category FROM preference_categories WHERE preference_id=? ORDER BY category",
            (pref_id,))]
        pref["directions"] = [r[0] for r in self.conn.execute(
            "SELECT direction FROM preference_directions WHERE preference_id=? ORDER BY direction",
            (pref_id,))]
        if pref["monitor"] == "vavi":
            pref.pop("update_emails_enabled", None)
            pref.pop("minimum_market_cap_usd", None)
        else:
            pref.pop("kalshi_enabled", None)
        return pref

    def _resolve_user(self, email=None):
        """Resolve the acting user row.

        A concrete ``email`` scopes to that recipient (the production path, where
        the HTTP layer supplies the authenticated identity). ``None`` falls back
        to the first seeded user, which keeps single-user callers and tests
        working; authorization is enforced at the request boundary, not here.
        """
        if email:
            return self.conn.execute(
                "SELECT * FROM users WHERE email=? COLLATE NOCASE",
                (normalize_email(email),)).fetchone()
        return self.conn.execute(
            "SELECT * FROM users ORDER BY id LIMIT 1").fetchone()

    def get_user(self, email):
        return self.conn.execute(
            "SELECT * FROM users WHERE email=? COLLATE NOCASE",
            (normalize_email(email),)).fetchone()

    def list_users(self):
        return [{"email": row["email"], "status": row["status"]}
                for row in self.conn.execute(
                    "SELECT email, status FROM users ORDER BY email")]

    def get_settings(self, email=None):
        user = self._resolve_user(email)
        if user is None:
            return None
        preferences = {}
        for row in self.conn.execute(
                "SELECT * FROM notification_preferences WHERE user_id=? ORDER BY monitor",
                (user["id"],)):
            value = self._preference_dict(row)
            preferences[value["monitor"]] = value
        return {
            "user": {"email": user["email"], "verified": bool(user["email_verified_at"]),
                     "timezone": user["timezone"], "status": user["status"]},
            "preferences": preferences,
        }

    def update_preference(self, monitor, payload, email=None):
        if monitor not in MONITORS:
            raise ValidationError({"monitor": "Unknown monitor."})
        if not isinstance(payload, dict):
            raise ValidationError({"request": "Expected a JSON object."})
        shared = {"enabled", "delivery_mode", "minimum_significance",
                  "quiet_hours_enabled", "quiet_start_local", "quiet_end_local",
                  "digest_time_local", "categories", "directions", "timezone"}
        applicable = shared | ({"kalshi_enabled"} if monitor == "vavi"
                               else {"update_emails_enabled",
                                     "minimum_market_cap_usd"})
        errors = {}
        for field in sorted(set(payload) - applicable):
            errors[field] = f"{field} does not apply to {monitor.title()}."
        user = self._resolve_user(email)
        if user is None:
            errors["user"] = "No migrated user is available."
        if errors:
            raise ValidationError(errors)
        row = self.conn.execute(
            "SELECT * FROM notification_preferences WHERE user_id=? AND monitor=?",
            (user["id"], monitor)).fetchone()
        if row is None:
            raise ValidationError({"monitor": "This monitor has not been seeded for the user."})
        current = self._preference_dict(row)
        proposed = dict(current)
        proposed.update(payload)

        for key in ("enabled", "quiet_hours_enabled"):
            if not isinstance(proposed.get(key), bool):
                errors[key] = "Choose on or off."
        monitor_flag = "kalshi_enabled" if monitor == "vavi" else "update_emails_enabled"
        if not isinstance(proposed.get(monitor_flag), bool):
            errors[monitor_flag] = "Choose on or off."
        if proposed.get("delivery_mode") not in DELIVERY_MODES[monitor]:
            errors["delivery_mode"] = "Choose a delivery mode available for this monitor."
        if proposed.get("minimum_significance") not in SIGNIFICANCE:
            errors["minimum_significance"] = "Choose Low, Medium, or High."
        if monitor == "sentinel":
            market_cap = proposed.get("minimum_market_cap_usd")
            if isinstance(market_cap, bool) or not isinstance(market_cap, int):
                errors["minimum_market_cap_usd"] = "Choose a market-cap threshold."
            elif not (DEFAULT_SENTINEL_MARKET_CAP_USD <= market_cap
                      <= MAX_SENTINEL_MARKET_CAP_USD):
                errors["minimum_market_cap_usd"] = "Choose a threshold from $1B to $1T."
        for field in ("quiet_start_local", "quiet_end_local", "digest_time_local"):
            try:
                parse_local_time(proposed.get(field), field)
            except ValidationError as exc:
                errors.update(exc.errors)
        timezone_name = payload.get("timezone", user["timezone"])
        try:
            validate_timezone(timezone_name)
        except ValidationError as exc:
            errors.update(exc.errors)
        categories = proposed.get("categories")
        directions = proposed.get("directions")
        if not isinstance(categories, list) or any(v not in CATEGORIES for v in categories):
            errors["categories"] = "Choose only the available categories."
        elif proposed.get("enabled") and not categories:
            errors["categories"] = "Select at least one category while the monitor is enabled."
        if not isinstance(directions, list) or any(v not in DIRECTIONS for v in directions):
            errors["directions"] = "Choose only Bullish, Bearish, or Unclear."
        elif proposed.get("enabled") and not directions:
            errors["directions"] = "Select at least one direction while the monitor is enabled."
        if errors:
            raise ValidationError(errors)

        now = iso_utc()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE users SET timezone=?, updated_at=? WHERE id=?",
                (timezone_name, now, user["id"]))
            self.conn.execute(
                """UPDATE notification_preferences SET
                   enabled=?, delivery_mode=?, minimum_significance=?,
                   minimum_market_cap_usd=?,
                   quiet_hours_enabled=?, quiet_start_local=?, quiet_end_local=?,
                   digest_time_local=?, kalshi_enabled=?, update_emails_enabled=?,
                   updated_at=? WHERE id=?""",
                (int(proposed["enabled"]), proposed["delivery_mode"],
                 proposed["minimum_significance"],
                 proposed.get("minimum_market_cap_usd"),
                 int(proposed["quiet_hours_enabled"]),
                 proposed["quiet_start_local"], proposed["quiet_end_local"],
                 proposed["digest_time_local"],
                 int(proposed.get("kalshi_enabled", False)),
                 int(proposed.get("update_emails_enabled", False)), now, row["id"]))
            self.conn.execute("DELETE FROM preference_categories WHERE preference_id=?",
                              (row["id"],))
            self.conn.executemany("INSERT INTO preference_categories VALUES (?,?)",
                                  [(row["id"], v) for v in sorted(set(categories))])
            self.conn.execute("DELETE FROM preference_directions WHERE preference_id=?",
                              (row["id"],))
            self.conn.executemany("INSERT INTO preference_directions VALUES (?,?)",
                                  [(row["id"], v) for v in sorted(set(directions))])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        saved = self.conn.execute(
            "SELECT * FROM notification_preferences WHERE id=?", (row["id"],)).fetchone()
        return self._preference_dict(saved)

    def unsubscribe(self, email=None):
        user = self._resolve_user(email)
        if user is None:
            raise ValidationError({"user": "No migrated user is available."})
        now = iso_utc()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE users SET status='unsubscribed', updated_at=? WHERE id=?",
                (now, user["id"]))
            self.conn.execute(
                """UPDATE deliveries SET status='suppressed',
                   suppression_reason='user_inactive', updated_at=?
                   WHERE user_id=? AND status IN ('pending','queued_digest','failed')""",
                (now, user["id"]))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _selected(self, table, column, preference_id):
        return {r[0] for r in self.conn.execute(
            f"SELECT {column} FROM {table} WHERE preference_id=?", (preference_id,))}

    def _original_was_sent(self, user_id, monitor, source_event_id):
        return self.conn.execute(
            """SELECT 1 FROM deliveries d JOIN notification_events e ON e.id=d.event_id
               WHERE d.user_id=? AND e.monitor=? AND e.source_event_id=?
                 AND e.event_kind='original' AND d.status='sent' LIMIT 1""",
            (user_id, monitor, source_event_id)).fetchone() is not None

    def _decision(self, user, pref, event, now):
        if user["status"] != "active":
            return "suppressed", "immediate", "user_inactive", None
        if not user["email_verified_at"]:
            return "suppressed", "immediate", "user_inactive", None
        if not pref["enabled"]:
            return "suppressed", "immediate", "monitor_disabled", None
        if event["event_kind"] == "update":
            if not pref["update_emails_enabled"]:
                return "suppressed", "immediate", "updates_disabled", None
            if not self._original_was_sent(user["id"], event["monitor"],
                                           event["source_event_id"]):
                return "suppressed", "immediate", "original_not_delivered", None
        event_categories = set(json.loads(event["categories"]))
        selected_categories = self._selected(
            "preference_categories", "category", pref["id"])
        if not event_categories & selected_categories:
            return "suppressed", "immediate", "category_filtered", None
        selected_directions = self._selected(
            "preference_directions", "direction", pref["id"])
        if event["direction"] not in selected_directions:
            return "suppressed", "immediate", "direction_filtered", None
        if SIGNIFICANCE[event["significance"]] < SIGNIFICANCE[pref["minimum_significance"]]:
            return "suppressed", "immediate", "below_significance", None
        if (event["monitor"] == "sentinel"
                and event["market_cap_usd"] is not None
                and event["market_cap_usd"] < pref["minimum_market_cap_usd"]):
            return "suppressed", "immediate", "below_market_cap", None

        mode, tier = pref["delivery_mode"], event["canonical_tier"]
        if event["monitor"] == "sentinel" and mode == "immediate" and tier == "digest":
            return "suppressed", "immediate", "immediate_only", None
        if mode == "digest" or (mode == "smart" and tier == "digest"):
            scheduled = next_digest_at(now, user["timezone"], pref["digest_time_local"])
            return "queued_digest", "digest", None, iso_utc(scheduled)
        if pref["quiet_hours_enabled"] and is_quiet_at(
                now, user["timezone"], pref["quiet_start_local"],
                pref["quiet_end_local"]):
            scheduled = next_digest_at(now, user["timezone"], pref["digest_time_local"])
            return "queued_digest", "digest", None, iso_utc(scheduled)
        return "pending", "immediate", None, iso_utc(now)

    def publish_event(self, *, monitor, source_event_id, event_version,
                      event_kind, categories, direction, significance,
                      canonical_tier, occurred_at, subject_data, body_data,
                      market_cap_usd=None, now=None, supersede_previous=False):
        now = now or utcnow()
        if monitor not in MONITORS or direction not in DIRECTIONS \
                or significance not in SIGNIFICANCE \
                or canonical_tier not in {"immediate", "digest"} \
                or event_kind not in {"original", "update"}:
            raise ValueError("invalid canonical event")
        category_set = sorted(set(categories))
        if not category_set or any(v not in CATEGORIES for v in category_set):
            raise ValueError("canonical event needs valid categories")
        if (market_cap_usd is not None
                and (isinstance(market_cap_usd, bool)
                     or not isinstance(market_cap_usd, (int, float))
                     or market_cap_usd < 0)):
            raise ValueError("market_cap_usd must be a non-negative number or null")
        created_at = iso_utc(now)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO notification_events
                   (monitor,source_event_id,event_version,event_kind,categories,
                    direction,significance,canonical_tier,occurred_at,
                    subject_data,body_data,kalshi_data,created_at,market_cap_usd)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (monitor, str(source_event_id), int(event_version), event_kind,
                 json.dumps(category_set), direction, significance, canonical_tier,
                 occurred_at or created_at, json.dumps(subject_data),
                 json.dumps(body_data), created_at, market_cap_usd))
            row = self.conn.execute(
                "SELECT * FROM notification_events WHERE monitor=? AND source_event_id=? AND event_version=?",
                (monitor, str(source_event_id), int(event_version))).fetchone()
            if cur.rowcount == 0:
                self.conn.commit()
                return {"event_id": row["id"], "created": False, "decisions": {}}
            if supersede_previous:
                self.conn.execute(
                    """UPDATE deliveries SET status='suppressed',
                       suppression_reason='duplicate', updated_at=?
                       WHERE event_id IN (
                           SELECT id FROM notification_events
                           WHERE monitor=? AND source_event_id=? AND event_version < ?)
                         AND status IN ('pending','queued_digest','failed')""",
                    (created_at, monitor, str(source_event_id), int(event_version)))
            decisions = {}
            for joined in self.conn.execute(
                    """SELECT u.id AS uid,u.email,u.email_verified_at,u.timezone,u.status,
                              p.*
                       FROM users u JOIN notification_preferences p ON p.user_id=u.id
                       WHERE p.monitor=?""", (monitor,)).fetchall():
                user = {"id": joined["uid"], "email": joined["email"],
                        "email_verified_at": joined["email_verified_at"],
                        "timezone": joined["timezone"], "status": joined["status"]}
                pref = dict(joined)
                status, method, reason, scheduled = self._decision(user, pref, row, now)
                self.conn.execute(
                    """INSERT INTO deliveries
                       (user_id,event_id,status,delivery_method,suppression_reason,
                        scheduled_for,attempt_count,last_error,sent_at,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,0,NULL,NULL,?,?)""",
                    (user["id"], row["id"], status, method, reason, scheduled,
                     created_at, created_at))
                decisions[status] = decisions.get(status, 0) + 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"event_id": row["id"], "created": True, "decisions": decisions}

    def delivery_counts(self, event_id):
        return {row["status"]: row["n"] for row in self.conn.execute(
            "SELECT status,COUNT(*) n FROM deliveries WHERE event_id=? GROUP BY status",
            (event_id,))}

    def latest_event_version(self, monitor, source_event_id):
        row = self.conn.execute(
            """SELECT MAX(event_version) AS version FROM notification_events
               WHERE monitor=? AND source_event_id=?""",
            (monitor, str(source_event_id))).fetchone()
        return int(row["version"] or 0)

    def _candidate_immediate(self, monitor=None, event_id=None, limit=50):
        query = """SELECT d.id delivery_id,d.*,u.email,u.timezone,
                          e.monitor,e.source_event_id,e.event_version,e.event_kind,
                          e.subject_data,e.body_data,e.kalshi_data,
                          e.created_at AS event_created_at,p.kalshi_enabled
                   FROM deliveries d JOIN users u ON u.id=d.user_id
                   JOIN notification_events e ON e.id=d.event_id
                   JOIN notification_preferences p ON p.user_id=d.user_id AND p.monitor=e.monitor
                   WHERE d.delivery_method='immediate' AND d.status IN ('pending','failed')
                     AND u.status='active' AND u.email_verified_at IS NOT NULL
                     AND d.attempt_count < 5 AND d.scheduled_for <= ?"""
        args = [iso_utc()]
        if monitor:
            query += " AND e.monitor=?"
            args.append(monitor)
        if event_id:
            query += " AND e.id=?"
            args.append(event_id)
        query += " ORDER BY p.kalshi_enabled,d.scheduled_for,d.id LIMIT ?"
        args.append(limit)
        return self.conn.execute(query, args).fetchall()

    def claim_delivery(self, delivery_id):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                """UPDATE deliveries SET status='sending',attempt_count=attempt_count+1,
                   updated_at=? WHERE id=? AND status IN ('pending','failed')""",
                (iso_utc(), delivery_id))
            self.conn.commit()
            return cur.rowcount == 1
        except Exception:
            self.conn.rollback()
            raise

    def due_digest_groups(self, monitor=None):
        query = """SELECT d.user_id,e.monitor,d.scheduled_for
                   FROM deliveries d JOIN notification_events e ON e.id=d.event_id
                   JOIN users u ON u.id=d.user_id
                   WHERE d.delivery_method='digest'
                     AND d.status IN ('queued_digest','failed')
                     AND u.status='active' AND u.email_verified_at IS NOT NULL
                     AND d.attempt_count < 5 AND d.scheduled_for <= ?"""
        args = [iso_utc()]
        if monitor:
            query += " AND e.monitor=?"
            args.append(monitor)
        query += " GROUP BY d.user_id,e.monitor,d.scheduled_for ORDER BY d.scheduled_for"
        return self.conn.execute(query, args).fetchall()

    def digest_rows(self, user_id, monitor, scheduled_for):
        return self.conn.execute(
            """SELECT d.id delivery_id,d.*,u.email,u.timezone,
                      e.monitor,e.source_event_id,e.event_version,e.event_kind,
                      e.subject_data,e.body_data,e.kalshi_data,
                      e.created_at AS event_created_at,p.kalshi_enabled
               FROM deliveries d JOIN users u ON u.id=d.user_id
               JOIN notification_events e ON e.id=d.event_id
               JOIN notification_preferences p ON p.user_id=d.user_id AND p.monitor=e.monitor
               WHERE d.user_id=? AND e.monitor=? AND d.scheduled_for=?
                 AND u.status='active' AND u.email_verified_at IS NOT NULL
                 AND d.delivery_method='digest'
                 AND d.status IN ('queued_digest','failed') AND d.attempt_count < 5
               ORDER BY e.occurred_at,e.id""", (user_id, monitor, scheduled_for)).fetchall()

    def claim_digest(self, delivery_ids):
        if not delivery_ids:
            return False
        marks = ",".join("?" for _ in delivery_ids)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                f"""UPDATE deliveries SET status='sending',attempt_count=attempt_count+1,
                    updated_at=? WHERE id IN ({marks})
                    AND status IN ('queued_digest','failed')""",
                [iso_utc()] + list(delivery_ids))
            if cur.rowcount != len(delivery_ids):
                self.conn.rollback()
                return False
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def finish_deliveries(self, delivery_ids, success, error=None):
        if not delivery_ids:
            return
        now = iso_utc()
        if success:
            values = ("sent", None, now, now)
            sql_tail = "status=?,last_error=?,sent_at=?,updated_at=?"
        else:
            attempts = self.conn.execute(
                "SELECT MAX(attempt_count) FROM deliveries WHERE id IN (%s)" %
                ",".join("?" for _ in delivery_ids), delivery_ids).fetchone()[0] or 1
            delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
            values = ("failed", (error or "delivery failed")[:500],
                      iso_utc(utcnow() + timedelta(seconds=delay)), now)
            sql_tail = "status=?,last_error=?,scheduled_for=?,updated_at=?"
        marks = ",".join("?" for _ in delivery_ids)
        self.conn.execute(
            f"UPDATE deliveries SET {sql_tail} WHERE id IN ({marks}) AND status='sending'",
            list(values) + list(delivery_ids))
        self.conn.commit()

    def recover_stale_claims(self, minutes=15):
        """Return abandoned send claims to their original queue.

        SMTP cannot provide a true distributed transaction with SQLite, so the
        stable Message-ID remains the duplicate guard if a process dies after
        SMTP accepts a message but before the local commit.
        """
        cutoff = iso_utc(utcnow() - timedelta(minutes=minutes))
        cur = self.conn.execute(
            """UPDATE deliveries
               SET status=CASE delivery_method
                    WHEN 'digest' THEN 'queued_digest' ELSE 'pending' END,
                   last_error='recovered abandoned delivery claim', updated_at=?
               WHERE status='sending' AND updated_at < ?""",
            (iso_utc(), cutoff))
        self.conn.commit()
        return cur.rowcount

    def claim_kalshi(self, event_id):
        marker = json.dumps({"status": "fetching", "at": iso_utc()})
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT kalshi_data FROM notification_events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                self.conn.commit()
                return "missing", None
            if row["kalshi_data"] is not None:
                self.conn.commit()
                return "existing", row["kalshi_data"]
            self.conn.execute("UPDATE notification_events SET kalshi_data=? WHERE id=?",
                              (marker, event_id))
            self.conn.commit()
            return "claimed", None
        except Exception:
            self.conn.rollback()
            raise

    def save_kalshi(self, event_id, markets, error=None):
        value = {"status": "failed" if error else "ready", "markets": markets}
        if error:
            value["error"] = str(error)[:200]
        self.conn.execute("UPDATE notification_events SET kalshi_data=? WHERE id=?",
                          (json.dumps(value), event_id))
        self.conn.commit()
        return value


class DeliveryWorker:
    def __init__(self, store, env, logger=print):
        self.store = store
        self.env = env
        self.log = logger

    def _links(self):
        base = self.env.get("SETTINGS_PUBLIC_URL", "http://localhost:8787").rstrip("/")
        return ("\n\nManage preferences: " + base + "/\n"
                "Unsubscribe: " + base + "/?action=unsubscribe")

    def _send(self, to, subject, body, message_id):
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.env["EMAIL_FROM"]
        msg["To"] = to
        msg["Message-ID"] = message_id
        msg.set_content(body + self._links())
        host = self.env.get("SMTP_HOST", "smtp.gmail.com")
        port = int(self.env.get("SMTP_PORT", "587"))
        user = self.env.get("SMTP_USER", self.env["EMAIL_FROM"])
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, self.env["SMTP_PASSWORD"])
            smtp.send_message(msg)

    def send_test(self, to, monitor):
        """Send a representative test email to ``to`` for one monitor.

        Uses the same SMTP path as real delivery so a success genuinely proves
        the channel works. The body is the static sample, clearly labeled, and
        the Message-ID is unique per send so it is never treated as a duplicate.
        """
        preview = static_preview(monitor)
        subject = "[TEST] " + preview["subject"]
        body = "\n".join([
            preview["eyebrow"], "",
            preview["headline"], "",
            preview["summary"], "",
            " · ".join(preview["meta"]), "",
            "This is a test email you triggered from the Notification Desk. "
            "No real event occurred and no delivery preferences were changed.",
        ])
        digest = hashlib.sha256(to.encode()).hexdigest()[:12]
        message_id = f"<test-{monitor}-{digest}-{int(utcnow().timestamp())}@notifications.vavi>"
        self._send(to, subject, body, message_id)
        return {"status": "sent", "to": to, "monitor": monitor}

    def _kalshi_value(self, row):
        if row["monitor"] != "vavi" or not row["kalshi_enabled"]:
            return {"status": "ready", "markets": []}
        if row["kalshi_data"]:
            value = json.loads(row["kalshi_data"])
            if value.get("status") in {"ready", "failed"}:
                self.log(f"kalshi cache hit for event {row['event_id']}")
            return None if value.get("status") == "fetching" else value
        status, existing = self.store.claim_kalshi(row["event_id"])
        if status == "existing":
            value = json.loads(existing)
            return None if value.get("status") == "fetching" else value
        if status != "claimed":
            return {"status": "failed", "markets": []}
        body_data = json.loads(row["body_data"])
        try:
            import kalshi
            self.log(f"kalshi fetch for event {row['event_id']}")
            markets = kalshi.find_markets(body_data.get("classification") or {})
            return self.store.save_kalshi(row["event_id"], markets)
        except Exception as exc:  # fail-open; cache the failure for this event
            self.log(f"kalshi enrichment failed for event {row['event_id']}: {exc}")
            return self.store.save_kalshi(row["event_id"], [], error=exc)

    def _render_one(self, row, kalshi_value=None):
        subject = json.loads(row["subject_data"])["subject"]
        data = json.loads(row["body_data"])
        body = data["body"]
        markets = (kalshi_value or {}).get("markets") or []
        if markets:
            import kalshi
            body += "\n" + kalshi.render_section(markets)
        return subject, body

    @staticmethod
    def _message_id(kind, monitor, user_id, identity):
        digest = hashlib.sha256(str(identity).encode()).hexdigest()[:20]
        return f"<{kind}-{monitor}-{user_id}-{digest}@notifications.vavi>"

    def process_immediate(self, monitor=None, event_id=None):
        sent = 0
        for row in self.store._candidate_immediate(monitor, event_id):
            enrichment = self._kalshi_value(row)
            if enrichment is None:  # another worker is enriching this event
                continue
            if not self.store.claim_delivery(row["delivery_id"]):
                continue
            try:
                subject, body = self._render_one(row, enrichment)
                message_id = self._message_id(
                    "event", row["monitor"], row["user_id"],
                    f"{row['source_event_id']}:{row['event_version']}")
                self._send(row["email"], subject, body, message_id)
                self.store.finish_deliveries([row["delivery_id"]], True)
                sent += 1
                created = datetime.fromisoformat(row["event_created_at"])
                latency = max(0, int((utcnow() - created).total_seconds()))
                self.log(f"delivered {row['monitor']} event {row['event_id']} "
                         f"to user {row['user_id']} latency_seconds={latency}")
            except Exception as exc:
                self.store.finish_deliveries([row["delivery_id"]], False, exc)
                self.log(f"delivery failed for event {row['event_id']}: {exc}")
        return sent

    def process_digests(self, monitor=None, force=False):
        sent = 0
        groups = self.store.due_digest_groups(monitor)
        if force:
            query_monitor = " AND e.monitor=?" if monitor else ""
            args = [monitor] if monitor else []
            groups = self.store.conn.execute(
                """SELECT d.user_id,e.monitor,d.scheduled_for FROM deliveries d
                   JOIN notification_events e ON e.id=d.event_id
                   JOIN users u ON u.id=d.user_id
                   WHERE d.delivery_method='digest'
                     AND d.status IN ('queued_digest','failed') AND d.attempt_count < 5
                     AND u.status='active' AND u.email_verified_at IS NOT NULL"""
                + query_monitor + " GROUP BY d.user_id,e.monitor,d.scheduled_for ORDER BY d.scheduled_for",
                args).fetchall()
        for group in groups:
            rows = self.store.digest_rows(
                group["user_id"], group["monitor"], group["scheduled_for"])
            if not rows:
                continue
            enrichments = []
            blocked = False
            for row in rows:
                value = self._kalshi_value(row)
                if value is None:
                    blocked = True
                    break
                enrichments.append(value)
            if blocked:
                continue
            ids = [row["delivery_id"] for row in rows]
            if not self.store.claim_digest(ids):
                continue
            monitor_name = group["monitor"].title()
            subject = f"{monitor_name} digest: {len(rows)} item(s)"
            parts = [f"{monitor_name} daily digest — {len(rows)} event(s)", ""]
            for index, (row, enrichment) in enumerate(zip(rows, enrichments), 1):
                item_subject, item_body = self._render_one(row, enrichment)
                parts.extend([f"{index}. {item_subject}", "", item_body, "", "—" * 48, ""])
            try:
                message_id = self._message_id(
                    "digest", group["monitor"], group["user_id"], group["scheduled_for"])
                self._send(rows[0]["email"], subject, "\n".join(parts), message_id)
                self.store.finish_deliveries(ids, True)
                sent += 1
                self.log(f"sent {group['monitor']} digest with {len(rows)} item(s) to user {group['user_id']}")
            except Exception as exc:
                self.store.finish_deliveries(ids, False, exc)
                self.log(f"digest delivery failed for user {group['user_id']}: {exc}")
        return sent

    def run_due(self, monitor=None, event_id=None, force_digests=False):
        recovered = self.store.recover_stale_claims()
        if recovered:
            self.log(f"recovered {recovered} abandoned delivery claim(s)")
        return {
            "immediate": self.process_immediate(monitor, event_id),
            "digests": self.process_digests(monitor, force_digests),
        }


def static_preview(monitor):
    if monitor == "vavi":
        return {
            "eyebrow": "VAVI / TARIFF · HIGH",
            "subject": "Vavi 🔴 [TARIFF/HIGH] NVDA, TSM",
            "headline": "New semiconductor tariff announced",
            "summary": "A market-relevant post names chip imports and a new tariff timetable.",
            "meta": ["Direction · Bearish", "Category · Company + Macro", "Confidence · 0.91"],
        }
    if monitor == "sentinel":
        return {
            "eyebrow": "SENTINEL / 8-K:5.02 · MEDIUM",
            "subject": "Sentinel 🔴 [8-K:5.02] ACME — CFO departs immediately",
            "headline": "Unscheduled executive departure",
            "summary": "An 8-K reports an immediate CFO departure; the event cleared global impact and novelty gates.",
            "meta": ["Direction · Bearish", "Category · Company", "Impact · 0.78"],
        }
    raise ValidationError({"monitor": "Choose Vavi or Sentinel."})
