import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import sentinel
import settings_server
import sundew
from notification_app import (
    AppStore,
    DeliveryWorker,
    ValidationError,
    is_quiet_at,
    next_digest_at,
    sentinel_categories,
    sentinel_significance,
    sundew_categories,
)


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


if __name__ == "__main__":
    unittest.main()
