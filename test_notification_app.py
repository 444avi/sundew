import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import sentinel
import settings_auth
import settings_server
import sundew
from arboretum_auth import AuthConfig, VerificationUnavailable
from notification_app import (
    AccountConflict,
    AppStore,
    DeliveryWorker,
    ValidationError,
    is_quiet_at,
    next_digest_at,
    sentinel_categories,
    sentinel_significance,
    sundew_categories,
)


# ---------------------------------------------------------------------------
# Signed-token helpers (self-contained: only PyJWT + cryptography, no network).
# ---------------------------------------------------------------------------
ISSUER = "https://accounts.test"
AUDIENCE = "arboretum-tools"


def _make_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


# One "real" keypair (its public half is configured on the verifier) and one
# "attacker" keypair used to forge a token the verifier must reject.
_PRIVATE_PEM, _PUBLIC_PEM = _make_keypair()
_ATTACKER_PRIVATE_PEM, _ATTACKER_PUBLIC_PEM = _make_keypair()


def mint(*, sub="acct_1", email="a@example.com", plan="premium",
         issuer=ISSUER, audience=AUDIENCE, ttl=900, now=None,
         private_pem=_PRIVATE_PEM, kid="k1", drop=()):
    issued = int(time.time()) if now is None else now
    payload = {
        "iss": issuer, "sub": sub, "aud": audience,
        "iat": issued, "exp": issued + ttl,
        "email": email, "plan": plan,
        "wos_user_id": sub, "sid": "sess_1",
    }
    for claim in drop:
        payload.pop(claim, None)
    return pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


def _test_config(return_origin="https://sundew.test", allowed=("sundew.test",)):
    return AuthConfig(
        issuer=ISSUER, login_url=ISSUER + "/login", refresh_url=ISSUER + "/refresh",
        audience=AUDIENCE, public_key=_PUBLIC_PEM,
        allowed_return_hosts=allowed, return_origin=return_origin)


class NotificationAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AppStore(os.path.join(self.temp.name, "app.db"))
        self.store.seed_from_env({"EMAIL_TO": "owner@example.com"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def publish(self, monitor="sundew", **overrides):
        values = {
            "monitor": monitor,
            "source_event_id": overrides.pop("source_event_id", "event-1"),
            "event_version": overrides.pop("event_version", 1),
            "event_kind": overrides.pop("event_kind", "original"),
            "categories": overrides.pop("categories", ["company"]),
            "direction": overrides.pop("direction", "bullish"),
            "significance": overrides.pop("significance", "medium"),
            "canonical_tier": overrides.pop("canonical_tier", "immediate"),
            "occurred_at": overrides.pop("occurred_at", "2026-09-05T12:00:00+00:00"),
            "subject_data": overrides.pop("subject_data", {"subject": "Example"}),
            "body_data": overrides.pop("body_data", {"body": "Example body", "classification": {}}),
            "now": overrides.pop("now", datetime(2026, 9, 5, 12, tzinfo=timezone.utc)),
        }
        values.update(overrides)
        return self.store.publish_event(**values)

    def delivery(self, event_id):
        return self.store.conn.execute(
            "SELECT * FROM deliveries WHERE event_id=?", (event_id,)).fetchone()

    def test_category_and_significance_mapping_is_local(self):
        labels = sundew_categories({
            "category": "tariff",
            "entities": [
                {"type": "country", "name": "China"},
                {"type": "company", "name": "Acme"},
                {"type": "currency", "name": "USD"},
            ],
        })
        self.assertEqual(labels, ["company", "country", "macro"])
        self.assertEqual(sentinel_categories("HALT:MWC2"), ["macro"])
        self.assertEqual(sentinel_categories("8-K:5.02"), ["company"])
        self.assertEqual([sentinel_significance(v) for v in (.55, .70, .85)],
                         ["low", "medium", "high"])

    def test_quiet_hours_cross_midnight_and_digest_uses_timezone(self):
        during = datetime(2026, 9, 6, 6, 30, tzinfo=timezone.utc)  # 23:30 PDT
        after = datetime(2026, 9, 6, 15, 30, tzinfo=timezone.utc)  # 08:30 PDT
        self.assertTrue(is_quiet_at(during, "America/Los_Angeles", "22:00", "07:00"))
        self.assertFalse(is_quiet_at(after, "America/Los_Angeles", "22:00", "07:00"))
        due = next_digest_at(
            datetime(2026, 11, 1, 8, 45, tzinfo=timezone.utc),
            "America/Los_Angeles", "01:30")
        self.assertEqual(due.tzinfo, timezone.utc)
        self.assertGreater(due, datetime(2026, 11, 1, 8, 45, tzinfo=timezone.utc))
        spring = next_digest_at(
            datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc),
            "America/Los_Angeles", "02:30")
        self.assertEqual(spring, datetime(2026, 3, 8, 10, 30, tzinfo=timezone.utc))

    def test_disabled_category_direction_and_significance_filters_are_audited(self):
        self.store.update_preference("sundew", {
            "categories": ["macro"], "directions": ["bearish"],
            "minimum_significance": "high",
        })
        category = self.publish(source_event_id="cat")
        self.assertEqual(self.delivery(category["event_id"])["suppression_reason"],
                         "category_filtered")
        direction = self.publish(source_event_id="dir", categories=["macro"])
        self.assertEqual(self.delivery(direction["event_id"])["suppression_reason"],
                         "direction_filtered")
        significance = self.publish(
            source_event_id="sig", categories=["macro"], direction="bearish")
        self.assertEqual(self.delivery(significance["event_id"])["suppression_reason"],
                         "below_significance")
        self.store.update_preference("sundew", {"enabled": False})
        disabled = self.publish(source_event_id="off")
        self.assertEqual(self.delivery(disabled["event_id"])["suppression_reason"],
                         "monitor_disabled")

    def test_modes_and_quiet_hours_only_reorganize_eligible_events(self):
        self.store.update_preference("sentinel", {"delivery_mode": "immediate"})
        digest = self.publish("sentinel", canonical_tier="digest")
        self.assertEqual(self.delivery(digest["event_id"])["suppression_reason"],
                         "immediate_only")
        self.store.update_preference("sentinel", {"delivery_mode": "smart"})
        smart = self.publish("sentinel", source_event_id="event-2",
                             canonical_tier="digest")
        self.assertEqual(self.delivery(smart["event_id"])["status"], "queued_digest")
        self.store.update_preference("sundew", {
            "quiet_hours_enabled": True,
            "quiet_start_local": "22:00", "quiet_end_local": "07:00",
            "digest_time_local": "17:00",
        })
        quiet = self.publish(
            source_event_id="quiet",
            now=datetime(2026, 9, 6, 6, 30, tzinfo=timezone.utc))
        self.assertEqual(self.delivery(quiet["event_id"])["status"], "queued_digest")

    def test_sentinel_market_cap_threshold_filters_known_caps_and_fails_open(self):
        settings = self.store.get_settings()["preferences"]
        self.assertEqual(settings["sentinel"]["minimum_market_cap_usd"], 1_000_000_000)
        self.assertNotIn("minimum_market_cap_usd", settings["sundew"])
        self.store.update_preference(
            "sentinel", {"minimum_market_cap_usd": 10_000_000_000})

        below = self.publish(
            "sentinel", source_event_id="small-cap", market_cap_usd=5_000_000_000)
        self.assertEqual(
            self.delivery(below["event_id"])["suppression_reason"],
            "below_market_cap")
        above = self.publish(
            "sentinel", source_event_id="large-cap", market_cap_usd=25_000_000_000)
        self.assertEqual(self.delivery(above["event_id"])["status"], "pending")
        unknown = self.publish(
            "sentinel", source_event_id="unknown-cap", market_cap_usd=None)
        self.assertEqual(self.delivery(unknown["event_id"])["status"], "pending")

        with self.assertRaises(ValidationError):
            self.store.update_preference(
                "sundew", {"minimum_market_cap_usd": 10_000_000_000})

    def test_duplicate_event_and_multi_category_create_one_delivery(self):
        first = self.publish(categories=["company", "macro"])
        second = self.publish(categories=["macro", "company"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        count = self.store.conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE event_id=?", (first["event_id"],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_promoted_sentinel_original_supersedes_an_unsent_digest_version(self):
        first = self.publish("sentinel", source_event_id="cluster-promote",
                             canonical_tier="digest")
        promoted = self.publish(
            "sentinel", source_event_id="cluster-promote", event_version=2,
            canonical_tier="immediate", supersede_previous=True)
        self.assertEqual(self.delivery(first["event_id"])["status"], "suppressed")
        self.assertEqual(self.delivery(first["event_id"])["suppression_reason"],
                         "duplicate")
        self.assertEqual(self.delivery(promoted["event_id"])["status"], "pending")

    def test_updates_require_a_sent_original_and_respect_update_toggle(self):
        original = self.publish("sentinel", source_event_id="cluster-9")
        update = self.publish("sentinel", source_event_id="cluster-9",
                              event_version=2, event_kind="update")
        self.assertEqual(self.delivery(update["event_id"])["suppression_reason"],
                         "original_not_delivered")
        self.store.conn.execute(
            "UPDATE deliveries SET status='sent' WHERE event_id=?", (original["event_id"],))
        self.store.conn.commit()
        allowed = self.publish("sentinel", source_event_id="cluster-9",
                               event_version=3, event_kind="update")
        self.assertEqual(self.delivery(allowed["event_id"])["status"], "pending")
        self.store.update_preference("sentinel", {"update_emails_enabled": False})
        blocked = self.publish("sentinel", source_event_id="cluster-9",
                               event_version=4, event_kind="update")
        self.assertEqual(self.delivery(blocked["event_id"])["suppression_reason"],
                         "updates_disabled")

    def test_validation_is_atomic_and_monitor_specific(self):
        before = self.store.get_settings()
        with self.assertRaises(ValidationError):
            self.store.update_preference("sundew", {
                "categories": [], "kalshi_enabled": True, "update_emails_enabled": True,
            })
        self.assertEqual(before, self.store.get_settings())

    def test_legacy_seed_never_overwrites_saved_preferences(self):
        self.store.update_preference("sundew", {"kalshi_enabled": False,
                                              "delivery_mode": "digest"})
        self.store.seed_from_env({"EMAIL_TO_KS": "owner@example.com"})
        saved = self.store.get_settings()["preferences"]["sundew"]
        self.assertFalse(saved["kalshi_enabled"])
        self.assertEqual(saved["delivery_mode"], "digest")

    def test_unsubscribe_suppresses_work_that_has_not_been_sent(self):
        event = self.publish(now=datetime(2020, 9, 5, 12, tzinfo=timezone.utc))
        self.assertEqual(self.delivery(event["event_id"])["status"], "pending")
        self.store.unsubscribe()
        delivery = self.delivery(event["event_id"])
        self.assertEqual(delivery["status"], "suppressed")
        self.assertEqual(delivery["suppression_reason"], "user_inactive")
        self.assertEqual(self.store._candidate_immediate(), [])

    def test_kalshi_fetches_once_for_multiple_private_deliveries(self):
        self.store.close()
        self.store = AppStore(os.path.join(self.temp.name, "kalshi.db"))
        self.store.seed_from_env({"EMAIL_TO_KS": "one@example.com,two@example.com"})
        event = self.publish(
            now=datetime(2020, 9, 5, 12, tzinfo=timezone.utc),
            body_data={"body": "Example", "classification": {
                "category": "monetary", "entities": []}})
        worker = DeliveryWorker(self.store, {
            "EMAIL_FROM": "sender@example.com", "SMTP_PASSWORD": "unused"})
        recipients = []
        worker._send = lambda to, subject, body, message_id: recipients.append(to)
        with mock.patch("kalshi.find_markets", return_value=[]) as find_markets:
            sent = worker.process_immediate(event_id=event["event_id"])
        self.assertEqual(sent, 2)
        self.assertEqual(sorted(recipients), ["one@example.com", "two@example.com"])
        find_markets.assert_called_once()

    def test_digests_are_isolated_by_user_and_monitor(self):
        self.store.close()
        self.store = AppStore(os.path.join(self.temp.name, "digests.db"))
        self.store.seed_from_env({"EMAIL_TO": "one@example.com,two@example.com"})
        self.store.conn.execute(
            "UPDATE notification_preferences SET delivery_mode='digest' WHERE monitor='sentinel'")
        self.store.conn.commit()
        self.publish("sentinel", now=datetime(2026, 9, 5, 22, tzinfo=timezone.utc))
        worker = DeliveryWorker(self.store, {
            "EMAIL_FROM": "sender@example.com", "SMTP_PASSWORD": "unused"})
        recipients = []
        worker._send = lambda to, subject, body, message_id: recipients.append(to)
        sent = worker.process_digests(monitor="sentinel", force=True)
        self.assertEqual(sent, 2)
        self.assertEqual(sorted(recipients), ["one@example.com", "two@example.com"])

    def test_failed_send_is_recorded_for_retry(self):
        event = self.publish(now=datetime(2020, 9, 5, 12, tzinfo=timezone.utc))
        worker = DeliveryWorker(self.store, {"EMAIL_FROM": "sender@example.com"},
                                logger=lambda message: None)

        def refuse(*args):
            raise ConnectionError("smtp down")
        worker._send = refuse
        self.assertEqual(worker.process_immediate(), 0)
        delivery = self.delivery(event["event_id"])
        self.assertEqual(delivery["status"], "failed")
        self.assertEqual(delivery["attempt_count"], 1)
        self.assertEqual(delivery["last_error"], "smtp down")

    def test_sundew_classifies_once_then_creates_two_user_decisions(self):
        self.store.close()
        self.store = AppStore(os.path.join(self.temp.name, "sundew-app.db"))
        self.store.seed_from_env({"EMAIL_TO": "one@example.com,two@example.com"})
        self.store.conn.execute(
            "UPDATE notification_preferences SET delivery_mode='digest' WHERE monitor='sundew'")
        self.store.conn.commit()
        monitor_store = sundew.Store(os.path.join(self.temp.name, "sundew-monitor.db"))
        post = {
            "id": "post-1", "created_at": "2026-09-05T12:00:00+00:00",
            "text": "A new tariff on semiconductor imports", "url": "https://example.test/post",
            "media": [], "hash": sundew.content_hash("A new tariff on semiconductor imports"),
        }
        classification = {
            "category": "tariff", "entities": [{"name": "Nvidia", "ticker": "NVDA", "type": "company"}],
            "direction": "bearish", "magnitude": "high", "is_noise": False,
            "confidence": .92, "rationale": "Tariffs can raise import costs.",
        }
        try:
            with mock.patch("sundew.fetch_posts", return_value=[post]), \
                    mock.patch("sundew.classify", return_value=classification) as classify:
                sundew.run_once(monitor_store, {"ANTHROPIC_API_KEY": "unused"},
                              app_store=self.store)
            classify.assert_called_once()
            self.assertEqual(self.store.conn.execute(
                "SELECT COUNT(*) FROM notification_events WHERE monitor='sundew'").fetchone()[0], 1)
            self.assertEqual(self.store.conn.execute(
                "SELECT COUNT(*) FROM deliveries").fetchone()[0], 2)
        finally:
            monitor_store.close()

    def test_sentinel_adapter_preserves_global_gate_then_publishes(self):
        self.store.close()
        self.store = AppStore(os.path.join(self.temp.name, "sentinel-app.db"))
        self.store.seed_from_env({"EMAIL_TO": "one@example.com,two@example.com"})
        self.store.conn.execute(
            "UPDATE notification_preferences SET delivery_mode='digest' WHERE monitor='sentinel'")
        self.store.conn.commit()
        monitor_store = sentinel.Store(os.path.join(self.temp.name, "sentinel-monitor.db"))
        observed = datetime.now(timezone.utc).isoformat()
        item = {
            "doc_hash": "doc-1", "source": "nasdaq_halts", "observed_at": observed,
            "event_time": observed, "title": "HALT ACME (T12: news pending)",
            "body": "Trading halt pending news.", "url": "https://example.test/halt",
            "entities": ["TICKER:ACME", "NAME:ACME"], "event_type": "HALT:T12",
            "simhash": 1, "impact_hint": .9, "raw": {},
        }
        cluster_id = monitor_store.new_cluster(item)
        monitor_store.insert_item(item, cluster_id)
        monitor_store.commit()
        try:
            with mock.patch("sentinel.cluster_market_cap",
                            return_value=48_000_000_000) as market_cap:
                sentinel.score_and_alert(
                    monitor_store, cluster_id, True, {}, False,
                    mark_alerted=True, use_llm=False, app_store=self.store)
            market_cap.assert_called_once()
            event = self.store.conn.execute(
                "SELECT * FROM notification_events WHERE monitor='sentinel'").fetchone()
            self.assertIsNotNone(event)
            self.assertEqual(event["significance"], "high")
            self.assertEqual(event["direction"], "unclear")
            self.assertEqual(event["market_cap_usd"], 48_000_000_000)
            self.assertEqual(self.store.conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE event_id=?", (event["id"],)).fetchone()[0], 2)
        finally:
            monitor_store.conn.close()


class PerRecipientScopingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AppStore(os.path.join(self.temp.name, "scope.db"))
        self.store.seed_from_env({"EMAIL_TO": "aa@example.com,bb@example.com"})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_updates_are_scoped_to_the_named_recipient(self):
        self.store.update_preference("sundew", {"delivery_mode": "digest"}, email="aa@example.com")
        aa = self.store.get_settings("aa@example.com")["preferences"]["sundew"]
        bb = self.store.get_settings("bb@example.com")["preferences"]["sundew"]
        self.assertEqual(aa["delivery_mode"], "digest")
        self.assertEqual(bb["delivery_mode"], "immediate")

    def test_email_lookup_is_case_insensitive(self):
        settings = self.store.get_settings("AA@Example.com")
        self.assertEqual(settings["user"]["email"], "aa@example.com")

    def test_unknown_recipient_has_no_settings_and_cannot_write(self):
        self.assertIsNone(self.store.get_settings("nobody@example.com"))
        self.assertIsNone(self.store.get_user("nobody@example.com"))
        with self.assertRaises(ValidationError):
            self.store.update_preference("sundew", {"delivery_mode": "digest"},
                                         email="nobody@example.com")

    def test_unsubscribe_only_affects_the_named_recipient(self):
        self.store.unsubscribe(email="aa@example.com")
        self.assertEqual(self.store.get_settings("aa@example.com")["user"]["status"],
                         "unsubscribed")
        self.assertEqual(self.store.get_settings("bb@example.com")["user"]["status"],
                         "active")

    def test_list_users_returns_every_recipient(self):
        self.assertEqual(self.store.list_users(),
                         [{"email": "aa@example.com", "status": "active"},
                          {"email": "bb@example.com", "status": "active"}])

    def test_send_test_email_uses_the_real_mailer_path(self):
        worker = DeliveryWorker(self.store, {
            "EMAIL_FROM": "sender@example.com", "SMTP_PASSWORD": "unused"})
        captured = {}
        worker._send = lambda to, subject, body, message_id: captured.update(
            to=to, subject=subject, body=body, message_id=message_id)
        result = worker.send_test("aa@example.com", "sundew")
        self.assertEqual(result, {"status": "sent", "to": "aa@example.com", "monitor": "sundew"})
        self.assertEqual(captured["to"], "aa@example.com")
        self.assertTrue(captured["subject"].startswith("[TEST]"))
        self.assertIn("test email", captured["body"].lower())
        with self.assertRaises(ValidationError):
            worker.send_test("aa@example.com", "not-a-monitor")


class AuthorizationPolicyTests(unittest.TestCase):
    ADMINS = {"avi@arboretuminvestments.net"}

    def test_caller_may_always_act_on_self(self):
        target, error = settings_server.resolve_target(
            "bob@example.com", "bob@example.com", self.ADMINS)
        self.assertEqual((target, error), ("bob@example.com", None))

    def test_missing_target_defaults_to_self(self):
        target, error = settings_server.resolve_target(
            "bob@example.com", None, self.ADMINS)
        self.assertEqual((target, error), ("bob@example.com", None))

    def test_admin_may_act_on_another_recipient(self):
        target, error = settings_server.resolve_target(
            "avi@arboretuminvestments.net", "bob@example.com", self.ADMINS)
        self.assertEqual((target, error), ("bob@example.com", None))

    def test_non_admin_cannot_act_on_another_recipient(self):
        target, error = settings_server.resolve_target(
            "bob@example.com", "carol@example.com", self.ADMINS)
        self.assertIsNone(target)
        self.assertEqual(error[0], 403)

    def test_unauthenticated_request_is_rejected(self):
        target, error = settings_server.resolve_target(None, None, self.ADMINS)
        self.assertIsNone(target)
        self.assertEqual(error[0], 401)


class AccountLinkingTests(unittest.TestCase):
    """The verified-account -> user resolution (self-provision / claim / conflict)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = AppStore(os.path.join(self.temp.name, "link.db"))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _prefs_count(self, user_id):
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM notification_preferences WHERE user_id=?",
            (user_id,)).fetchone()[0]

    def test_new_paid_account_creates_one_user_and_one_pref_per_monitor(self):
        user = self.store.resolve_account_user("acct_new", "new@example.com")
        self.assertEqual(user["arboretum_account_id"], "acct_new")
        self.assertEqual(user["status"], "active")
        self.assertIsNotNone(user["email_verified_at"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        monitors = [r[0] for r in self.store.conn.execute(
            "SELECT monitor FROM notification_preferences WHERE user_id=? ORDER BY monitor",
            (user["id"],))]
        self.assertEqual(monitors, ["sentinel", "sundew"])
        # Defaults are populated so the settings page is usable immediately.
        settings = self.store.get_settings("new@example.com")["preferences"]
        self.assertTrue(settings["sundew"]["categories"])
        self.assertEqual(settings["sentinel"]["minimum_market_cap_usd"], 1_000_000_000)

    def test_repeated_first_access_is_idempotent(self):
        first = self.store.resolve_account_user("acct_x", "x@example.com")
        second = self.store.resolve_account_user("acct_x", "x@example.com")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self._prefs_count(first["id"]), 2)

    def test_concurrent_first_access_creates_exactly_one_user(self):
        path = os.path.join(self.temp.name, "concurrent.db")
        AppStore(path).close()  # ensure schema exists
        barrier = threading.Barrier(8)
        errors = []

        def worker():
            store = AppStore(path)
            try:
                barrier.wait()
                store.resolve_account_user("acct_race", "race@example.com")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        check = AppStore(path)
        try:
            self.assertEqual(check.conn.execute(
                "SELECT COUNT(*) FROM users WHERE arboretum_account_id='acct_race'").fetchone()[0], 1)
            user = check.get_user_by_account("acct_race")
            self.assertEqual(check.conn.execute(
                "SELECT COUNT(*) FROM notification_preferences WHERE user_id=?",
                (user["id"],)).fetchone()[0], 2)
        finally:
            check.close()

    def test_legacy_recipient_is_claimed_preserving_everything(self):
        self.store.seed_from_env({"EMAIL_TO_KS": "legacy@example.com"})
        legacy = self.store.get_user("legacy@example.com")
        # Customize a preference and record delivery history to prove preservation.
        self.store.update_preference("sundew", {"delivery_mode": "digest",
                                                "kalshi_enabled": False},
                                     email="legacy@example.com")
        event = self.store.publish_event(
            monitor="sundew", source_event_id="hist-1", event_version=1,
            event_kind="original", categories=["company"], direction="bullish",
            significance="medium", canonical_tier="immediate",
            occurred_at="2026-09-05T12:00:00+00:00",
            subject_data={"subject": "x"}, body_data={"body": "y", "classification": {}},
            now=datetime(2026, 9, 5, 12, tzinfo=timezone.utc))
        deliveries_before = self.store.conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE user_id=?", (legacy["id"],)).fetchone()[0]

        linked = self.store.resolve_account_user("acct_legacy", "legacy@example.com")
        self.assertEqual(linked["id"], legacy["id"])  # same row, id preserved
        self.assertEqual(linked["arboretum_account_id"], "acct_legacy")
        self.assertEqual(linked["timezone"], legacy["timezone"])
        prefs = self.store.get_settings("legacy@example.com")["preferences"]
        self.assertEqual(prefs["sundew"]["delivery_mode"], "digest")  # preserved
        self.assertFalse(prefs["sundew"]["kalshi_enabled"])
        self.assertNotIn("sentinel", prefs)  # not silently given a new monitor
        deliveries_after = self.store.conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE user_id=?", (legacy["id"],)).fetchone()[0]
        self.assertEqual(deliveries_after, deliveries_before)  # history intact
        self.assertEqual(event["event_id"], event["event_id"])

    def test_unsubscribed_and_disabled_users_are_not_reactivated(self):
        self.store.seed_from_env({"EMAIL_TO": "gone@example.com,off@example.com"})
        self.store.unsubscribe(email="gone@example.com")
        self.store.conn.execute("UPDATE users SET status='disabled' WHERE email='off@example.com'")
        self.store.conn.commit()

        gone = self.store.resolve_account_user("acct_gone", "gone@example.com")
        off = self.store.resolve_account_user("acct_off", "off@example.com")
        self.assertEqual(gone["status"], "unsubscribed")
        self.assertEqual(off["status"], "disabled")
        # Re-resolving an already-linked, unsubscribed user still does not resubscribe.
        again = self.store.resolve_account_user("acct_gone", "gone@example.com")
        self.assertEqual(again["status"], "unsubscribed")

    def test_email_is_kept_current_for_a_linked_account(self):
        self.store.resolve_account_user("acct_move", "old@example.com")
        moved = self.store.resolve_account_user("acct_move", "new-address@example.com")
        self.assertEqual(moved["email"], "new-address@example.com")
        self.assertIsNone(self.store.get_user("old@example.com"))
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_account_id_never_reassigned_when_email_bound_elsewhere(self):
        self.store.resolve_account_user("acct_a", "shared@example.com")
        before = self.store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        with self.assertRaises(AccountConflict) as ctx:
            self.store.resolve_account_user("acct_b", "shared@example.com")
        self.assertEqual(ctx.exception.code, "email_bound_to_other_account")
        after = self.store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        self.assertEqual(before, after)  # nothing created

    def test_email_change_into_a_taken_address_fails_closed(self):
        self.store.resolve_account_user("acct_a", "a@example.com")
        self.store.resolve_account_user("acct_b", "b@example.com")
        with self.assertRaises(AccountConflict) as ctx:
            # acct_a tries to move onto b@, which belongs to acct_b -> conflict.
            self.store.resolve_account_user("acct_a", "b@example.com")
        self.assertEqual(ctx.exception.code, "email_in_use")
        self.assertEqual(self.store.get_user("a@example.com")["arboretum_account_id"], "acct_a")

    def test_users_never_merged(self):
        self.store.seed_from_env({"EMAIL_TO": "one@example.com,two@example.com"})
        self.store.resolve_account_user("acct_one", "one@example.com")
        with self.assertRaises(AccountConflict):
            self.store.resolve_account_user("acct_one", "two@example.com")
        # Both rows still independent; two@ never absorbed.
        self.assertIsNone(self.store.get_user("two@example.com")["arboretum_account_id"])


class SettingsAuthHelpersTests(unittest.TestCase):
    """Framework-free auth glue: config, startup guard, redirects, cookies."""

    PROD_ENV = {
        "ARBORETUM_ACCOUNTS_URL": "https://accounts.arboretuminvestments.net",
        "ARBORETUM_ISSUER": "https://accounts.arboretuminvestments.net",
        "ARBORETUM_AUDIENCE": "arboretum-tools",
        "ARBORETUM_JWKS_URL": "https://accounts.arboretuminvestments.net/.well-known/jwks.json",
        "ARBORETUM_ALLOWED_RETURN_HOSTS": "sundew.arboretuminvestments.net",
        "ARBORETUM_RETURN_ORIGIN": "https://sundew.arboretuminvestments.net",
        "ARBORETUM_COOKIE_NAME": "arb_session",
    }

    def test_build_auth_config_from_required_production_env(self):
        config = settings_auth.build_auth_config(self.PROD_ENV)
        self.assertEqual(config.issuer, "https://accounts.arboretuminvestments.net")
        self.assertEqual(config.audience, "arboretum-tools")
        self.assertEqual(config.cookie_name, "arb_session")
        self.assertEqual(config.jwks_url,
                         "https://accounts.arboretuminvestments.net/.well-known/jwks.json")
        self.assertIn("sundew.arboretuminvestments.net", config.allowed_return_hosts)
        self.assertEqual(config.return_origin, "https://sundew.arboretuminvestments.net")

    def test_incomplete_config_fails_closed(self):
        with self.assertRaises(settings_auth.ConfigurationError):
            settings_auth.build_auth_config({})

    def test_production_rejects_dev_bypass(self):
        config = settings_auth.build_auth_config(self.PROD_ENV)
        env = dict(self.PROD_ENV, SETTINGS_DEV_AUTH='{"id":"x","email":"x@x","plan":"max"}')
        with self.assertRaises(settings_auth.AuthStartupError):
            settings_auth.assert_safe_startup(env, config)
        env2 = dict(self.PROD_ENV, SETTINGS_DEV_EMAIL="x@x")
        with self.assertRaises(settings_auth.AuthStartupError):
            settings_auth.assert_safe_startup(env2, config)

    def test_production_requires_return_origin_and_hosts(self):
        loose = AuthConfig(issuer=ISSUER, login_url=ISSUER + "/login",
                           public_key=_PUBLIC_PEM)  # no allowed hosts / origin
        with self.assertRaises(settings_auth.AuthStartupError):
            settings_auth.assert_safe_startup(dict(self.PROD_ENV), loose)

    def test_development_allows_injection(self):
        env = {"SETTINGS_ENV": "development",
               "SETTINGS_DEV_AUTH": '{"id":"dev1","email":"dev@x","plan":"premium"}'}
        config = _test_config()
        self.assertFalse(settings_auth.assert_safe_startup(env, config))  # not production
        principal = settings_auth.dev_principal_from_env(env)
        self.assertEqual(principal.email, "dev@x")
        self.assertEqual(principal.plan.value, "premium")
        self.assertTrue(principal.has_plan("premium"))

    def test_legacy_dev_email_defaults_to_premium(self):
        principal = settings_auth.dev_principal_from_env({"SETTINGS_DEV_EMAIL": "me@x"})
        self.assertEqual(principal.email, "me@x")
        self.assertEqual(principal.plan.value, "premium")
        self.assertIsNone(settings_auth.dev_principal_from_env({}))

    def test_is_production_default_true(self):
        self.assertTrue(settings_auth.is_production({}))
        self.assertFalse(settings_auth.is_production({"SETTINGS_ENV": "development"}))

    def test_parse_cookie_header(self):
        cookies = settings_auth.parse_cookie_header("arb_session=abc.def.ghi; other=1")
        self.assertEqual(cookies["arb_session"], "abc.def.ghi")
        self.assertEqual(cookies["other"], "1")
        self.assertEqual(settings_auth.parse_cookie_header(None), {})

    def test_return_target_preserves_deep_link_and_marks_retry(self):
        config = _test_config()
        target = settings_auth.compute_return_target(
            config, "sundew.test", "/", [("tab", "sentinel")], api=False, retry=True)
        self.assertTrue(target.startswith("https://sundew.test/?"))
        self.assertIn("tab=sentinel", target)
        self.assertIn(settings_auth.ATTEMPT_PARAM + "=1", target)

    def test_return_target_for_api_goes_home(self):
        config = _test_config()
        target = settings_auth.compute_return_target(
            config, "sundew.test", "/api/settings", [("user", "x")], api=True, retry=True)
        self.assertEqual(target, "https://sundew.test/?" + settings_auth.ATTEMPT_PARAM + "=1")

    def test_account_url_contains_sundew_return(self):
        auth = settings_auth.SettingsAuthenticator(_test_config(), accounts_url=ISSUER)
        url = auth.account_url()
        self.assertTrue(url.startswith(ISSUER + "/account?return_to="))
        self.assertIn("https%3A%2F%2Fsundew.test%2F", url)


class _QuietHandler(settings_server.SettingsHandler):
    def log_message(self, *args, **kwargs):
        pass

    def _worker_log(self, message):
        pass


class SettingsServerHTTPTests(unittest.TestCase):
    """End-to-end HTTP behavior of the settings server (verify -> gate -> act)."""

    def start(self, *, admin_emails=(), dev_principal=None, verifier=None, seed=None):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "app.db")
        store = AppStore(self.db_path)
        try:
            store.seed_from_env(seed or {})
        finally:
            store.close()
        config = _test_config()
        authenticator = settings_auth.SettingsAuthenticator(
            config, verifier=verifier, min_plan="premium",
            dev_principal=dev_principal, accounts_url=ISSUER)
        httpd = settings_server.ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
        httpd.app_db = self.db_path
        httpd.env = {"EMAIL_FROM": "sender@example.com"}
        httpd.authenticator = authenticator
        httpd.admin_emails = {settings_server.normalize_email(e) for e in admin_emails}
        httpd.test_cooldown = 0
        httpd.test_email_times = {}
        httpd.test_lock = threading.Lock()
        httpd.new_user_timezone = "America/Los_Angeles"
        httpd.sentinel_digest_time = "17:00"
        httpd.sentinel_market_cap_floor = 1_000_000_000
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def req(self, method, path, *, token=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = dict(headers or {})
        if token is not None:
            hdrs["Cookie"] = f"arb_session={token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        status = resp.status
        headers_out = {k.lower(): v for k, v in resp.getheaders()}
        parsed = None
        if "application/json" in headers_out.get("content-type", ""):
            parsed = json.loads(raw.decode())
        return status, headers_out, raw, parsed

    def store(self):
        return AppStore(self.db_path)

    # -- health & public assets -------------------------------------------

    def test_health_is_public(self):
        self.start()
        status, _, _, data = self.req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_static_assets_are_public(self):
        self.start()
        for path in ("/settings.css", "/settings.js"):
            status, headers, _, _ = self.req("GET", path)
            self.assertEqual(status, 200, path)

    # -- missing / bad sessions -------------------------------------------

    def test_api_without_cookie_returns_401_with_auth_url(self):
        self.start()
        status, headers, raw, data = self.req("GET", "/api/settings")
        self.assertEqual(status, 401)
        self.assertTrue(data["auth_url"].startswith(ISSUER + "/refresh?return_to="))
        self.assertNotIn("set-cookie", headers)

    def test_page_without_cookie_redirects_through_login_preserving_deep_link(self):
        self.start()
        status, headers, _, _ = self.req("GET", "/?tab=sentinel")
        self.assertEqual(status, 302)
        location = headers["location"]
        self.assertTrue(location.startswith(ISSUER + "/refresh?return_to="))
        from urllib.parse import unquote
        self.assertIn("tab=sentinel", unquote(location))
        self.assertIn("sundew.test", unquote(location))

    def test_forged_token_is_rejected(self):
        self.start()
        token = mint(private_pem=_ATTACKER_PRIVATE_PEM)  # signed by wrong key
        status, _, _, data = self.req("GET", "/api/settings", token=token)
        self.assertEqual(status, 401)

    def test_malformed_expired_and_wrong_claims_are_rejected(self):
        self.start()
        cases = {
            "malformed": "not-a-jwt",
            "expired": mint(now=int(time.time()) - 5000),
            "wrong_issuer": mint(issuer="https://evil.test"),
            "wrong_audience": mint(audience="someone-else"),
            "missing_sub": mint(drop=("sub",)),
        }
        for name, token in cases.items():
            status, _, _, _ = self.req("GET", "/api/settings", token=token)
            self.assertEqual(status, 401, name)

    def test_jwks_unavailable_returns_503_without_clearing_cookie(self):
        self.start(verifier=_AlwaysUnavailable())
        status, headers, _, data = self.req("GET", "/api/settings", token=mint())
        self.assertEqual(status, 503)
        self.assertEqual(data["error"], "verification_unavailable")
        self.assertNotIn("set-cookie", headers)
        # And the browser page variant is a 503 HTML, not a redirect/login loop.
        pstatus, _, praw, _ = self.req("GET", "/", token=mint())
        self.assertEqual(pstatus, 503)
        self.assertIn(b"unavailable", praw.lower())

    # -- plan gating -------------------------------------------------------

    def test_free_plan_is_denied_with_upgrade_path(self):
        self.start()
        status, _, _, data = self.req("GET", "/api/settings", token=mint(plan="free", email="f@example.com"))
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "plan_required")
        self.assertIn("account", data["account_url"])
        # Browser variant is an HTML upgrade page.
        pstatus, pheaders, praw, _ = self.req("GET", "/", token=mint(plan="free", email="f@example.com"))
        self.assertEqual(pstatus, 403)
        self.assertIn("text/html", pheaders["content-type"])
        self.assertIn(b"Premium", praw)

    def test_premium_and_max_can_access(self):
        self.start()
        for plan in ("premium", "max"):
            email = f"{plan}@example.com"
            status, _, _, data = self.req(
                "GET", "/api/settings", token=mint(sub=f"acct_{plan}", plan=plan, email=email))
            self.assertEqual(status, 200, plan)
            self.assertEqual(data["plan"], plan)
            self.assertIn("account_url", data)
            self.assertTrue(data["logout_url"].endswith("/logout"))

    # -- self-provisioning -------------------------------------------------

    def test_new_premium_user_self_provisions_exactly_once(self):
        self.start()
        token = mint(sub="acct_fresh", email="fresh@example.com", plan="premium")
        for _ in range(3):  # repeated first access stays idempotent
            status, _, _, data = self.req("GET", "/api/settings", token=token)
            self.assertEqual(status, 200)
        store = self.store()
        try:
            self.assertEqual(store.conn.execute(
                "SELECT COUNT(*) FROM users WHERE arboretum_account_id='acct_fresh'").fetchone()[0], 1)
            user = store.get_user_by_account("acct_fresh")
            self.assertEqual(store.conn.execute(
                "SELECT COUNT(*) FROM notification_preferences WHERE user_id=?",
                (user["id"],)).fetchone()[0], 2)
        finally:
            store.close()

    def test_page_returned_from_login_authenticated_clears_marker(self):
        self.start()
        token = mint(email="loop@example.com")
        status, headers, _, _ = self.req(
            "GET", "/?" + settings_auth.ATTEMPT_PARAM + "=1", token=token)
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "https://sundew.test/")

    def test_no_redirect_loop_when_returning_still_unauthenticated(self):
        self.start()
        status, headers, raw, _ = self.req("GET", "/?" + settings_auth.ATTEMPT_PARAM + "=1")
        self.assertEqual(status, 401)  # terminal, not another 302
        self.assertNotIn("location", headers)
        self.assertIn(b"did not complete", raw)

    # -- authorization scoping --------------------------------------------

    def test_non_admin_cannot_act_on_another_recipient(self):
        self.start(seed={"EMAIL_TO": "aa@example.com,bb@example.com"})
        aa = mint(sub="acct_aa", email="aa@example.com", plan="premium")
        status, _, _, _ = self.req("GET", "/api/settings?user=bb@example.com", token=aa)
        self.assertEqual(status, 403)
        pstatus, _, _, _ = self.req(
            "PUT", "/api/settings/sundew?user=bb@example.com", token=aa,
            body={"delivery_mode": "digest"})
        self.assertEqual(pstatus, 403)

    def test_requested_email_is_only_a_selector_not_identity(self):
        # A premium non-admin naming someone else gets their OWN row, never theirs.
        self.start(seed={"EMAIL_TO": "victim@example.com"})
        token = mint(sub="acct_self", email="self@example.com", plan="premium")
        status, _, _, data = self.req("GET", "/api/settings?user=victim@example.com", token=token)
        # self@ is not admin and victim@ != self, so access to victim is refused.
        self.assertEqual(status, 403)

    def test_admin_uses_verified_identity_to_manage_others(self):
        self.start(admin_emails={"admin@sundew.test"},
                   seed={"EMAIL_TO": "member@example.com"})
        admin = mint(sub="acct_admin", email="admin@sundew.test", plan="premium")
        status, _, _, data = self.req("GET", "/api/settings?user=member@example.com", token=admin)
        self.assertEqual(status, 200)
        self.assertTrue(data["is_admin"])
        self.assertEqual(data["viewing"], "member@example.com")
        # The admin can save the member's preferences.
        pstatus, _, _, pdata = self.req(
            "PUT", "/api/settings/sundew?user=member@example.com", token=admin,
            body={"delivery_mode": "digest"})
        self.assertEqual(pstatus, 200)
        self.assertEqual(pdata["preference"]["delivery_mode"], "digest")
        # A non-admin premium user with the same request is refused (identity, not
        # the selector, decides authority).
        member = mint(sub="acct_member", email="member@example.com", plan="premium")
        rstatus, _, _, _ = self.req(
            "PUT", "/api/settings/sundew?user=admin@sundew.test", token=member)
        self.assertIn(rstatus, (403, 422))

    def test_home_serves_settings_page_with_account_link(self):
        self.start()
        status, headers, raw, _ = self.req("GET", "/", token=mint(email="home@example.com"))
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        body = raw.decode()
        self.assertIn("accounts.arboretuminvestments.net/account", body)
        self.assertIn("return_to=https%3A%2F%2Fsundew.arboretuminvestments.net%2F", body)
        # Header carries the same options as the shared Arboretum tool header.
        self.assertIn(">Account<", body)
        self.assertIn(">Log out<", body)
        self.assertIn("accounts.arboretuminvestments.net/logout", body)

    def test_legacy_recipient_links_over_http_and_keeps_preferences(self):
        self.start(seed={"EMAIL_TO_KS": "keep@example.com"})
        store = self.store()
        try:
            store.update_preference("sundew", {"delivery_mode": "digest"}, email="keep@example.com")
            original_id = store.get_user("keep@example.com")["id"]
        finally:
            store.close()
        token = mint(sub="acct_keep", email="keep@example.com", plan="premium")
        status, _, _, data = self.req("GET", "/api/settings", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data["preferences"]["sundew"]["delivery_mode"], "digest")
        store = self.store()
        try:
            linked = store.get_user("keep@example.com")
            self.assertEqual(linked["id"], original_id)
            self.assertEqual(linked["arboretum_account_id"], "acct_keep")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        finally:
            store.close()

    def test_test_email_is_scoped_and_uses_mailer(self):
        self.start(seed={"EMAIL_TO": "user@example.com"})
        token = mint(sub="acct_user", email="user@example.com", plan="premium")
        with mock.patch("sesmail.send") as send:
            status, _, _, data = self.req(
                "POST", "/api/settings/test-email", token=token, body={"monitor": "sundew"})
        self.assertEqual(status, 200)
        self.assertEqual(data["to"], "user@example.com")
        send.assert_called_once()

    def test_unsubscribe_does_not_reactivate_and_is_scoped(self):
        self.start()
        token = mint(sub="acct_unsub", email="unsub@example.com", plan="premium")
        self.req("GET", "/api/settings", token=token)  # provision
        status, _, _, data = self.req("POST", "/api/unsubscribe", token=token, body={})
        self.assertEqual(status, 200)
        # Re-authenticating must not silently resubscribe.
        status2, _, _, data2 = self.req("GET", "/api/settings", token=token)
        self.assertEqual(status2, 200)
        self.assertEqual(data2["user"]["status"], "unsubscribed")

    def test_dev_injection_bypasses_cookie_in_development(self):
        from arboretum_auth import AuthenticatedUser, Plan
        now = datetime.now(timezone.utc)
        principal = AuthenticatedUser(
            id="dev-acct", workos_user_id="dev-acct", email="dev@example.com",
            plan=Plan.PREMIUM, session_id="s", issued_at=now, expires_at=now)
        self.start(dev_principal=principal)
        status, _, _, data = self.req("GET", "/api/settings")  # no cookie at all
        self.assertEqual(status, 200)
        self.assertEqual(data["identity"], "dev@example.com")


class _AlwaysUnavailable:
    """A verifier stand-in that simulates an unreachable JWKS endpoint."""

    def verify(self, token):
        raise VerificationUnavailable("jwks unreachable")


if __name__ == "__main__":
    unittest.main()
