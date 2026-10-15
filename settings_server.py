#!/usr/bin/env python3
"""Per-recipient settings API and static UI (stdlib HTTP + shared auth client).

Authentication is first-party: every request is identified by the account
service's ``arb_session`` cookie, verified with the shared ``arboretum_auth``
package (asymmetric JWT — Sundew holds only public verification material, never a
WorkOS/Stripe/database/signing secret). Identity (stable account id + verified
email) and entitlement (plan) come *only* from that verified token.

The legacy Cloudflare Access headers ``Cf-Access-Authenticated-User-Email`` and
``Cf-Access-Jwt-Assertion`` are no longer trusted for identity. A requested
``user`` email is only a selector: it never grants authority, and acting on
another recipient still requires the verified caller to be a configured
administrator. Sundew requires at least the Premium plan; the settings page and
every settings/test-email/unsubscribe action enforce it. The origin stays bound
to loopback and is reached only through the operator-controlled tunnel.

A development-only injected principal (``SETTINGS_DEV_AUTH``) can stand in for a
signed cookie for local runs; production fails closed if that — or any other
unsafe/incomplete auth configuration — is present (see :mod:`settings_auth`).
"""

import argparse
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlparse

import sentinel_config as sentinel_cfg
import sundew
from notification_app import (
    AccountConflict,
    AppStore,
    DeliveryWorker,
    MONITORS,
    ValidationError,
    normalize_email,
)
from settings_auth import (
    ATTEMPT_PARAM,
    AuthStartupError,
    ConfigurationError,
    SettingsAuthenticator,
    assert_safe_startup,
    build_auth_config,
    dev_principal_from_env,
    is_production,
    parse_cookie_header,
)


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_DB = os.path.join(HERE, "app.db")
DEFAULT_ADMIN_EMAILS = "avi@arboretuminvestments.net"
# Public static assets (styling/behavior only — never identity-bearing).
PUBLIC_FILES = {
    "/settings.css": os.path.join(HERE, "static", "settings.css"),
    "/settings.js": os.path.join(HERE, "static", "settings.js"),
}
SETTINGS_HTML = os.path.join(HERE, "templates", "settings.html")


def resolve_target(identity, requested, admin_emails):
    """Decide which recipient a request may act on (pure authorization policy).

    Returns ``(email, error)`` where ``error`` is a ``(status, payload)`` tuple
    or None. A caller may always act on themselves; acting on another recipient
    requires the identity to be in ``admin_emails``. Existence of the recipient
    is a separate check made against the store by the caller. ``identity`` is the
    verified caller's email; a requested email is only a selector.
    """
    if not identity:
        return None, (401, {"error": "no_identity",
                            "message": "No authenticated identity; sign in through Arboretum."})
    if requested and requested != identity:
        if identity not in admin_emails:
            return None, (403, {"error": "forbidden",
                                "message": "Only an administrator can manage other recipients."})
        return requested, None
    return identity, None


def _upgrade_page(account_url, plan):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sundew · Premium required</title><link rel="stylesheet" href="/settings.css">
</head><body class="gate-page"><main class="gate">
<p class="gate-kicker">Sundew · Notification Desk</p>
<h1>Premium is required</h1>
<p>Your Arboretum plan is <strong>{plan}</strong>. Sundew's notification desk is
available on the <strong>Premium</strong> and <strong>Max</strong> plans.</p>
<p><a class="gate-cta" href="{account_url}">Upgrade your Arboretum plan →</a></p>
<p class="gate-foot">Once your plan is upgraded, return here and the desk will open.</p>
</main></body></html>"""


def _unavailable_page():
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sundew · Temporarily unavailable</title><link rel="stylesheet" href="/settings.css">
</head><body class="gate-page"><main class="gate">
<p class="gate-kicker">Sundew · Notification Desk</p>
<h1>Sign-in check is temporarily unavailable</h1>
<p>We could not reach the Arboretum sign-in service to verify your session. Your
session has not been cleared. Please retry in a moment.</p>
<p><a class="gate-cta" href="/">Retry →</a></p>
</main></body></html>"""


def _loop_page(account_url):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sundew · Sign-in did not complete</title><link rel="stylesheet" href="/settings.css">
</head><body class="gate-page"><main class="gate">
<p class="gate-kicker">Sundew · Notification Desk</p>
<h1>Sign-in did not complete</h1>
<p>You were returned from Arboretum sign-in without a usable session. This is
usually a blocked third-party cookie. Enable cookies for this site, then try
again.</p>
<p><a class="gate-cta" href="{account_url}">Manage your Arboretum account →</a></p>
</main></body></html>"""


class SettingsHandler(BaseHTTPRequestHandler):
    server_version = "SundewSettings/2.0"

    # -- request context ---------------------------------------------------

    def _store(self):
        return AppStore(self.server.app_db)

    def _cookies(self):
        return parse_cookie_header(self.headers.get("Cookie"))

    def _host(self):
        return self.headers.get("Host", "")

    def _query_pairs(self):
        return parse_qsl(urlparse(self.path).query, keep_blank_values=True)

    def _has_attempt(self):
        return any(k == ATTEMPT_PARAM and v == "1" for k, v in self._query_pairs())

    def _is_admin(self, email):
        return bool(email) and email in self.server.admin_emails

    def _requested_user(self, payload=None):
        """The recipient the caller is asking to act on, if any (email or None)."""
        if payload and payload.get("user"):
            return normalize_email(payload.get("user"))
        values = parse_qs(urlparse(self.path).query).get("user")
        return normalize_email(values[0]) if values else None

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

    # -- low-level responses ----------------------------------------------

    def _headers(self, status, content_type="application/json; charset=utf-8",
                 length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._headers(status, length=len(body))
        self.wfile.write(body)

    def _html(self, status, html):
        body = html.encode("utf-8")
        self._headers(status, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _redirect(self, location, status=302):
        self._headers(status, "text/html; charset=utf-8", 0,
                      extra={"Location": location})

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

    # -- authentication ----------------------------------------------------

    def _gate(self, api):
        """Verify the session and plan. Returns the AuthenticatedUser or None.

        On any non-OK outcome the appropriate response (redirect / 401 / 403 /
        503 / terminal 401) has already been written when None is returned.
        """
        auth = self.server.authenticator
        path = urlparse(self.path).path
        pairs = self._query_pairs()
        decision = auth.authenticate(
            cookies=self._cookies(), host=self._host(), path=path,
            query_pairs=pairs, api=api, has_attempt=self._has_attempt())
        if decision.ok:
            self.principal = decision.user
            if not api and self._has_attempt():
                # Returned from login authenticated: drop the marker from the URL.
                self._redirect(auth.clean_target(self._host(), path, pairs))
                return None
            return decision.user
        if decision.kind == "redirect":
            self._redirect(decision.auth_url)
        elif decision.kind == "unauthorized":
            self._json(401, {"error": "auth_required",
                             "message": "Sign in to continue.",
                             "auth_url": decision.auth_url})
        elif decision.kind == "forbidden":
            self._forbidden(api, decision.user)
        elif decision.kind == "unavailable":
            if api:
                self._json(503, {"error": "verification_unavailable",
                                 "message": decision.message})
            else:
                self._html(503, _unavailable_page())
        elif decision.kind == "loop":
            if api:
                self._json(401, {"error": "auth_required", "message": decision.message,
                                 "auth_url": decision.auth_url})
            else:
                self._html(401, _loop_page(decision.auth_url))
        else:  # pragma: no cover - defensive
            self._json(500, {"error": "auth_error"})
        return None

    def _forbidden(self, api, user):
        account_url = self.server.authenticator.account_url()
        plan = user.plan.value if user else "free"
        if api:
            self._json(403, {"error": "plan_required", "required_plan": "premium",
                             "plan": plan, "account_url": account_url,
                             "message": "Sundew requires a Premium or Max plan."})
        else:
            self._html(403, _upgrade_page(account_url, plan.title()))

    def _prepare(self, api, requested):
        """Gate, self-provision the caller, and resolve the acting recipient.

        Returns ``(store, identity, admin, target)`` with an *open* store on
        success (the caller closes it), or None when a response was already sent.
        """
        principal = self._gate(api=api)
        if principal is None:
            return None
        store = self._store()
        try:
            caller = store.resolve_account_user(
                principal.id, principal.email,
                default_timezone=self.server.new_user_timezone,
                sentinel_digest_time=self.server.sentinel_digest_time,
                sentinel_market_cap_floor=self.server.sentinel_market_cap_floor)
        except AccountConflict as exc:
            store.close()
            self.log_message("account link conflict: %s", exc.code)
            self._json(409, {"error": "account_conflict", "code": exc.code,
                             "message": "This account could not be linked to a "
                                        "Sundew profile. Please contact support."})
            return None
        except Exception:
            store.close()
            raise
        identity = caller["email"]
        admin = self._is_admin(identity)
        target, error = resolve_target(identity, requested, self.server.admin_emails)
        if error:
            store.close()
            self._json(*error)
            return None
        return store, identity, admin, target

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok", "service": "sundew-settings"})
            return
        if path in PUBLIC_FILES:
            self._serve_static(path)
            return
        if path == "/api/settings":
            self._get_settings()
            return
        if path == "/":
            self._serve_home()
            return
        self._json(404, {"error": "not_found"})

    def _serve_static(self, path):
        target = PUBLIC_FILES.get(path)
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

    def _serve_home(self):
        principal = self._gate(api=False)
        if principal is None:
            return  # redirect / 403 / 503 / loop already sent
        # Self-provision on first visit so a newly paid customer can use the page
        # immediately, and surface a linkage conflict instead of a broken UI.
        store = self._store()
        try:
            store.resolve_account_user(
                principal.id, principal.email,
                default_timezone=self.server.new_user_timezone,
                sentinel_digest_time=self.server.sentinel_digest_time,
                sentinel_market_cap_floor=self.server.sentinel_market_cap_floor)
        except AccountConflict as exc:
            self.log_message("account link conflict on home: %s", exc.code)
            self._html(409, _loop_page(self.server.authenticator.account_url()))
            return
        finally:
            store.close()
        if not os.path.isfile(SETTINGS_HTML):
            self._json(404, {"error": "not_found"})
            return
        with open(SETTINGS_HTML, "rb") as handle:
            body = handle.read()
        self._headers(200, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _get_settings(self):
        prepared = self._prepare(api=True, requested=self._requested_user())
        if prepared is None:
            return
        store, identity, admin, target = prepared
        try:
            envelope = {
                "is_admin": admin,
                "identity": identity,
                "plan": self.principal.plan.value,
                "account_url": self.server.authenticator.account_url(),
                "logout_url": self.server.authenticator.logout_url(),
                "users": store.list_users() if admin else [],
            }
            user = store.get_user(target)
            if user is None:
                # Authenticated but the requested recipient has no row (only an
                # admin can reach this — a caller always self-provisions).
                envelope["user"] = None
                envelope["viewing"] = None
                envelope["not_recipient"] = True
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
            # Body is validated before auth only to bound request size; identity
            # is still required and enforced below via _prepare.
            self._json(422, {"error": "validation", "fields": exc.errors})
            return
        prepared = self._prepare(api=True, requested=self._requested_user(payload))
        if prepared is None:
            return
        store, identity, admin, target = prepared
        try:
            payload.pop("user", None)  # transport-only field, not a preference
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
        prepared = self._prepare(api=True, requested=self._requested_user(payload))
        if prepared is None:
            return
        store, identity, admin, target = prepared
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
        prepared = self._prepare(api=True, requested=self._requested_user(payload))
        if prepared is None:
            return
        store, identity, admin, target = prepared
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
    default_timezone = env.get("APP_DEFAULT_TIMEZONE", "America/Los_Angeles")
    sentinel_digest_time = f"{sentinel_cfg.DIGEST_HOUR_LOCAL:02d}:00"
    sentinel_market_cap_floor = sentinel_cfg.MARKET_CAP_MIN_USD

    store = AppStore(args.app_db)
    try:
        store.seed_from_env(
            env,
            default_timezone=default_timezone,
            sentinel_digest_time=sentinel_digest_time,
            sentinel_market_cap_floor=sentinel_market_cap_floor)
    finally:
        store.close()

    # Fail closed on incomplete or unsafe authentication configuration.
    try:
        config = build_auth_config(env)
        prod = assert_safe_startup(env, config)
        dev_principal = None if prod else dev_principal_from_env(env)
    except (ConfigurationError, AuthStartupError) as exc:
        print(f"[settings] refusing to start: {exc}", flush=True)
        raise SystemExit(2)

    authenticator = SettingsAuthenticator(
        config, min_plan="premium", dev_principal=dev_principal,
        accounts_url=env.get("ARBORETUM_ACCOUNTS_URL"))

    server = ThreadingHTTPServer((args.host, args.port), SettingsHandler)
    server.app_db = args.app_db
    server.env = env
    server.authenticator = authenticator
    server.admin_emails = {normalize_email(value) for value
                           in env.get("SETTINGS_ADMIN_EMAILS", DEFAULT_ADMIN_EMAILS).split(",")
                           if normalize_email(value)}
    server.test_cooldown = int(env.get("SETTINGS_TEST_EMAIL_COOLDOWN", "20"))
    server.test_email_times = {}
    server.test_lock = threading.Lock()
    server.new_user_timezone = default_timezone
    server.sentinel_digest_time = sentinel_digest_time
    server.sentinel_market_cap_floor = sentinel_market_cap_floor

    print(f"Settings UI listening on http://{args.host}:{args.port}", flush=True)
    print(f"Environment: {'production' if prod else 'development'}; "
          f"dev-auth injection: {'ON' if dev_principal else 'off'}", flush=True)
    print(f"Admin recipients: {', '.join(sorted(server.admin_emails)) or '(none)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
