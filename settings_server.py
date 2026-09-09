#!/usr/bin/env python3
"""Per-recipient settings API and static UI (stdlib only).

Authentication is delegated to Cloudflare Access, which sits in front of the
tunnel and injects a verified ``Cf-Access-Authenticated-User-Email`` header on
every request. This server trusts that header to identify the caller and scopes
every read and write to that recipient. The origin is bound to loopback so the
header cannot be reached except through the tunnel; the signed
``Cf-Access-Jwt-Assertion`` is intentionally not re-verified here (that lands
with the future first-party login). ``SETTINGS_DEV_EMAIL`` may stand in for the
header for local runs only and must stay unset in production.
"""

import argparse
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import sentinel_config as sentinel_cfg
import sundew
from notification_app import (
    AppStore,
    DeliveryWorker,
    MONITORS,
    ValidationError,
    normalize_email,
)


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_DB = os.path.join(HERE, "app.db")
ACCESS_EMAIL_HEADER = "Cf-Access-Authenticated-User-Email"
DEFAULT_ADMIN_EMAILS = "avi@arboretuminvestments.net"
STATIC_FILES = {
    "/": os.path.join(HERE, "templates", "settings.html"),
    "/settings.css": os.path.join(HERE, "static", "settings.css"),
    "/settings.js": os.path.join(HERE, "static", "settings.js"),
}


def resolve_target(identity, requested, admin_emails):
    """Decide which recipient a request may act on (pure authorization policy).

    Returns ``(email, error)`` where ``error`` is a ``(status, payload)`` tuple
    or None. A caller may always act on themselves; acting on another recipient
    requires the identity to be in ``admin_emails``. Existence of the recipient
    is a separate check made against the store by the caller.
    """
    if not identity:
        return None, (401, {"error": "no_identity",
                            "message": "No authenticated identity; sign in through Access."})
    if requested and requested != identity:
        if identity not in admin_emails:
            return None, (403, {"error": "forbidden",
                                "message": "Only an administrator can manage other recipients."})
        return requested, None
    return identity, None


class SettingsHandler(BaseHTTPRequestHandler):
    server_version = "SundewSettings/1.0"

    def _store(self):
        return AppStore(self.server.app_db)

    def _identity(self):
        """The authenticated caller's email, or None.

        Trusts the Cloudflare Access header; ``SETTINGS_DEV_EMAIL`` is a
        local-only fallback for running without Access in front.
        """
        email = normalize_email(self.headers.get(ACCESS_EMAIL_HEADER, ""))
        if not email:
            email = normalize_email(self.server.dev_email or "")
        return email or None

    def _is_admin(self, email):
        return bool(email) and email in self.server.admin_emails

    def _requested_user(self, payload=None):
        """The recipient the caller is asking to act on, if any (email or None)."""
        if payload and payload.get("user"):
            return normalize_email(payload.get("user"))
        values = parse_qs(urlparse(self.path).query).get("user")
        return normalize_email(values[0]) if values else None

    def _resolve_target(self, requested):
        """Apply :func:`resolve_target` to this request's authenticated identity."""
        return resolve_target(self._identity(), requested, self.server.admin_emails)

    def _test_email_gate(self, email):
        """Per-recipient cooldown so the test button cannot hammer SMTP."""
        cooldown = self.server.test_cooldown
        now = time.monotonic()
        with self.server.test_lock:
            last = self.server.test_email_times.get(email, 0.0)
            remaining = cooldown - (now - last)
            if remaining > 0:
                return False, int(remaining) + 1
            self.server.test_email_times[email] = now
            return True, 0

    def _headers(self, status, content_type="application/json; charset=utf-8",
                 length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _read_json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValidationError({"request": "Content-Type must be application/json."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValidationError({"request": "Invalid content length."})
        if length < 1 or length > 64 * 1024:
            raise ValidationError({"request": "Expected a small JSON request body."})
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError({"request": "Request body must be valid JSON."})

    def _serve_file(self, path):
        target = STATIC_FILES.get(path)
        if not target or not os.path.isfile(target):
            self._json(404, {"error": "not_found"})
            return
        with open(target, "rb") as handle:
            body = handle.read()
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type == "application/javascript":
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/settings":
            self._get_settings()
            return
        self._serve_file(path)

    def _get_settings(self):
        target, error = self._resolve_target(self._requested_user())
        if error:
            self._json(*error)
            return
        identity = self._identity()
        admin = self._is_admin(identity)
        store = self._store()
        try:
            envelope = {
                "is_admin": admin,
                "identity": identity,
                "users": store.list_users() if admin else [],
            }
            user = store.get_user(target)
            if user is None:
                # Authenticated but not a seeded recipient. Admins still get the
                # roster to pick from; everyone else simply is not on the list.
                envelope["user"] = None
                envelope["viewing"] = None
                envelope["not_recipient"] = True
                if not admin and target == identity:
                    envelope["message"] = "You are signed in, but not on the recipient list."
                self._json(200, envelope)
                return
            settings = store.get_settings(target)
            settings.update(envelope)
            settings["viewing"] = target
            self._json(200, settings)
        finally:
            store.close()

    def do_PUT(self):
        path = urlparse(self.path).path
        monitor = path.rsplit("/", 1)[-1]
        if path not in {"/api/settings/sundew", "/api/settings/sentinel"}:
            self._json(404, {"error": "not_found"})
            return
        try:
            payload = self._read_json()
        except ValidationError as exc:
            self._json(422, {"error": "validation", "fields": exc.errors})
            return
        target, error = self._resolve_target(self._requested_user(payload))
        if error:
            self._json(*error)
            return
        payload.pop("user", None)  # transport-only field, not a preference
        store = self._store()
        try:
            if store.get_user(target) is None:
                self._json(403, {"error": "not_recipient",
                                 "message": "This recipient is not on the list."})
                return
            saved = store.update_preference(monitor, payload, email=target)
            self._json(200, {"preference": saved})
        except ValidationError as exc:
            self._json(422, {"error": "validation", "fields": exc.errors})
        finally:
            store.close()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/settings/test-email":
                self._send_test_email()
                return
            if path == "/api/unsubscribe":
                self._unsubscribe()
                return
            self._json(404, {"error": "not_found"})
        except ValidationError as exc:
            self._json(422, {"error": "validation", "fields": exc.errors})

    def _send_test_email(self):
        payload = self._read_json()
        monitor = payload.get("monitor")
        if monitor not in MONITORS:
            self._json(422, {"error": "validation",
                             "fields": {"monitor": "Choose Sundew or Sentinel."}})
            return
        target, error = self._resolve_target(self._requested_user(payload))
        if error:
            self._json(*error)
            return
        store = self._store()
        try:
            user = store.get_user(target)
        finally:
            store.close()
        if user is None:
            self._json(403, {"error": "not_recipient",
                             "message": "This recipient is not on the list."})
            return
        allowed, retry_after = self._test_email_gate(user["email"])
        if not allowed:
            self._json(429, {"error": "rate_limited",
                             "message": f"Please wait {retry_after}s before sending another test."})
            return
        worker = DeliveryWorker(self._store(), self.server.env, logger=self._worker_log)
        try:
            result = worker.send_test(user["email"], monitor)
        except Exception as exc:  # SMTP or configuration failure
            self.log_message("test email to %s failed: %s", user["email"], exc)
            self._json(502, {"error": "send_failed",
                             "message": "Could not send the test email. Check SMTP settings."})
            return
        finally:
            worker.store.close()
        self._json(200, result)

    def _unsubscribe(self):
        payload = self._read_json()
        target, error = self._resolve_target(self._requested_user(payload))
        if error:
            self._json(*error)
            return
        store = self._store()
        try:
            if store.get_user(target) is None:
                self._json(403, {"error": "not_recipient",
                                 "message": "This recipient is not on the list."})
                return
            store.unsubscribe(email=target)
            self._json(200, {"status": "unsubscribed"})
        finally:
            store.close()

    def _worker_log(self, message):
        print("[settings] " + str(message), flush=True)

    def log_message(self, fmt, *args):
        print("[settings] " + fmt % args, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Serve the Sundew notification settings UI.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="listen address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--app-db", default=DEFAULT_APP_DB)
    parser.add_argument("--env", default=os.path.join(HERE, ".env"))
    args = parser.parse_args(argv)

    sundew.load_env(args.env)
    env = dict(os.environ)
    store = AppStore(args.app_db)
    try:
        store.seed_from_env(
            env,
            default_timezone=env.get("APP_DEFAULT_TIMEZONE", "America/Los_Angeles"),
            sentinel_digest_time=f"{sentinel_cfg.DIGEST_HOUR_LOCAL:02d}:00",
            sentinel_market_cap_floor=sentinel_cfg.MARKET_CAP_MIN_USD)
    finally:
        store.close()

    server = ThreadingHTTPServer((args.host, args.port), SettingsHandler)
    server.app_db = args.app_db
    server.env = env
    server.admin_emails = {normalize_email(value) for value
                           in env.get("SETTINGS_ADMIN_EMAILS", DEFAULT_ADMIN_EMAILS).split(",")
                           if normalize_email(value)}
    # Local-only stand-in for the Access header; must be unset in production.
    server.dev_email = env.get("SETTINGS_DEV_EMAIL")
    server.test_cooldown = int(env.get("SETTINGS_TEST_EMAIL_COOLDOWN", "20"))
    server.test_email_times = {}
    server.test_lock = threading.Lock()
    print(f"Settings UI listening on http://{args.host}:{args.port}", flush=True)
    print(f"Admin recipients: {', '.join(sorted(server.admin_emails)) or '(none)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
