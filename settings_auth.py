#!/usr/bin/env python3
"""First-party session verification for the Sundew settings service.

Identity and entitlement come *only* from the account service's ``arb_session``
cookie, verified with the shared ``arboretum_auth`` package (asymmetric JWT, no
WorkOS/Stripe/database secret ever reaches Sundew). This module holds the
framework-free glue around that package so it can be unit tested without an HTTP
server:

* :func:`build_auth_config` / :func:`assert_safe_startup` — fail-closed config.
* :class:`SettingsAuthenticator` — verify a request's cookies, enforce a minimum
  plan, and build the central login/refresh redirect with a validated return
  URL (with an anti-loop marker).
* :func:`parse_cookie_header` — a tiny, strict Cookie-header parser.

The Cloudflare Access headers (``Cf-Access-Authenticated-User-Email`` and
``Cf-Access-Jwt-Assertion``) are deliberately not consulted anywhere here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from arboretum_auth import (
    AuthConfig,
    AuthenticatedUser,
    ConfigurationError,
    InvalidSession,
    Plan,
    SessionVerifier,
    VerificationUnavailable,
)
from arboretum_auth.plans import Plan as _Plan
from arboretum_auth.redirects import build_login_url, safe_return_to

# Query marker echoed once through the account service so a browser that returns
# still without a usable cookie is shown a terminal error instead of bouncing
# forever between Sundew and login.
ATTEMPT_PARAM = "_arb_auth_attempt"

# Non-production markers that unlock the development/testing injection path.
_DEV_ENVS = {"development", "dev", "local", "test", "testing"}


class AuthStartupError(Exception):
    """Raised when authentication configuration is unsafe for production."""


def is_production(env) -> bool:
    """True unless ``SETTINGS_ENV`` explicitly names a development environment."""
    return (env.get("SETTINGS_ENV", "production") or "").strip().lower() not in _DEV_ENVS


def build_auth_config(env) -> AuthConfig:
    """Build the verifier config from ARBORETUM_* env (raises ConfigurationError)."""
    return AuthConfig.from_env(env)


def dev_principal_from_env(env) -> AuthenticatedUser | None:
    """Parse a development-only injected principal, or None.

    ``SETTINGS_DEV_AUTH`` is JSON: ``{"id": "...", "email": "...", "plan": "..."}``.
    It exists so the settings UI can be exercised locally without minting a real
    signed cookie. It is honored *only* in a development environment; production
    refuses to start when it (or the legacy ``SETTINGS_DEV_EMAIL``) is present.
    """
    raw = env.get("SETTINGS_DEV_AUTH")
    legacy = env.get("SETTINGS_DEV_EMAIL")
    if not raw and not legacy:
        return None
    if raw:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise AuthStartupError(f"SETTINGS_DEV_AUTH is not valid JSON: {exc}")
        account_id = str(data.get("id") or data.get("account_id") or "").strip()
        email = str(data.get("email") or "").strip()
        plan = str(data.get("plan") or "premium").strip()
    else:  # legacy convenience: an email string, defaulting to premium.
        account_id = f"dev-{legacy.strip().lower()}"
        email = legacy.strip()
        plan = env.get("SETTINGS_DEV_PLAN", "premium").strip()
    if not account_id or not email:
        raise AuthStartupError("SETTINGS_DEV_AUTH requires both id and email")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return AuthenticatedUser(
        id=account_id,
        workos_user_id=account_id,
        email=email,
        plan=_Plan.coerce(plan),
        session_id="dev-session",
        issued_at=now,
        expires_at=now,
    )


def assert_safe_startup(env, config: AuthConfig) -> bool:
    """Fail closed on unsafe production auth configuration; returns ``is_production``.

    Production rejects any development bypass and requires an explicit, validated
    return origin + allow-list so redirects cannot be pointed off-site.
    """
    prod = is_production(env)
    dev_configured = bool(env.get("SETTINGS_DEV_AUTH") or env.get("SETTINGS_DEV_EMAIL"))
    if prod and dev_configured:
        raise AuthStartupError(
            "Refusing to start: a development auth bypass (SETTINGS_DEV_AUTH/"
            "SETTINGS_DEV_EMAIL) is set while SETTINGS_ENV is production.")
    if prod and not config.allowed_return_hosts:
        raise AuthStartupError(
            "Refusing to start: ARBORETUM_ALLOWED_RETURN_HOSTS must be set in production.")
    if prod and not config.return_origin:
        raise AuthStartupError(
            "Refusing to start: ARBORETUM_RETURN_ORIGIN must be set in production.")
    return prod


def parse_cookie_header(header: str | None) -> dict:
    """Parse a raw ``Cookie:`` header into a name->value dict (last write wins).

    Deliberately simple and dependency-free: split on ``;`` then the first ``=``.
    Values are used verbatim (the JWT is URL-safe base64 with dots, which needs
    no unquoting)."""
    cookies: dict[str, str] = {}
    if not header:
        return cookies
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


@dataclass
class AuthDecision:
    """Outcome of authenticating one request.

    ``kind`` is one of: ``ok`` (``user`` set), ``redirect`` (browser 302 to
    ``auth_url``), ``unauthorized`` (API 401 with ``auth_url``), ``forbidden``
    (plan too low, ``user`` set), ``unavailable`` (verifier down, 503), or
    ``loop`` (returned from login still unauthenticated, terminal 401).
    """

    kind: str
    user: AuthenticatedUser | None = None
    auth_url: str | None = None
    status: int | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind == "ok"


def compute_return_target(config: AuthConfig, host, path, query_pairs, *,
                          api: bool, retry: bool) -> str:
    """Build a validated absolute (or safe relative) Sundew return URL.

    For an API caller the browser cannot re-issue the original request after the
    login hop, so it is sent to the tool home; page requests keep their deep
    link. The result always passes the shared open-redirect guard.
    """
    origin = config.return_origin
    if not origin and host:
        hostname = host.split(":")[0].lower()
        if hostname in {h.lower() for h in config.allowed_return_hosts}:
            origin = f"https://{host}"
    if api:
        path = "/"
        pairs = []
    else:
        pairs = [(k, v) for (k, v) in query_pairs if k != ATTEMPT_PARAM]
    if retry:
        pairs = pairs + [(ATTEMPT_PARAM, "1")]
    query = urlencode(pairs)
    suffix = f"{path or '/'}{'?' + query if query else ''}"
    if origin:
        candidate = f"{origin}{suffix}"
        return safe_return_to(candidate, allowed_hosts=config.allowed_return_hosts,
                              default=f"{origin}/")
    return safe_return_to(suffix, allowed_hosts=config.allowed_return_hosts, default="/")


class SettingsAuthenticator:
    """Verifies ``arb_session`` and enforces a minimum plan for the settings app."""

    def __init__(self, config: AuthConfig, verifier: SessionVerifier | None = None,
                 *, min_plan="premium", dev_principal: AuthenticatedUser | None = None,
                 accounts_url: str | None = None):
        self.config = config
        self.verifier = verifier or SessionVerifier(config)
        self.min_plan = Plan.coerce(min_plan)
        self.dev_principal = dev_principal
        self.accounts_url = (accounts_url or config.issuer or "").rstrip("/")

    # -- redirects ---------------------------------------------------------

    def _login_base(self) -> str:
        return self.config.refresh_url or self.config.login_url

    def auth_url(self, host, path, query_pairs, *, api: bool, retry: bool = True) -> str:
        target = compute_return_target(self.config, host, path, query_pairs,
                                       api=api, retry=retry)
        return build_login_url(self._login_base(), target)

    def clean_target(self, host, path, query_pairs) -> str:
        """The current page URL with the anti-loop marker stripped (for a 302)."""
        return compute_return_target(self.config, host, path, query_pairs,
                                     api=False, retry=False)

    def account_url(self) -> str:
        """The central 'manage account' URL carrying the Sundew return URL."""
        return_to = (self.config.return_origin or "").rstrip("/") + "/"
        base = self.accounts_url or self.config.issuer.rstrip("/")
        return f"{base}/account?return_to={quote(return_to, safe='')}"

    def logout_url(self) -> str:
        """The central sign-out URL (mirrors the shared Arboretum tool header)."""
        base = self.accounts_url or self.config.issuer.rstrip("/")
        return f"{base}/logout"

    # -- verification ------------------------------------------------------

    def authenticate(self, *, cookies, host, path, query_pairs, api: bool,
                     has_attempt: bool) -> AuthDecision:
        if self.dev_principal is not None:
            user = self.dev_principal
        else:
            token = cookies.get(self.config.cookie_name, "")
            try:
                user = self.verifier.verify(token)
            except VerificationUnavailable:
                # Transient: keys unreachable. Fail closed for this request but
                # never clear the cookie or conclude the token is bad.
                return AuthDecision("unavailable", status=503,
                                    message="Sign-in verification is temporarily "
                                            "unavailable. Please retry shortly.")
            except InvalidSession:
                auth_url = self.auth_url(host, path, query_pairs, api=api, retry=True)
                if api:
                    return AuthDecision("unauthorized", status=401, auth_url=auth_url)
                if has_attempt:
                    return AuthDecision(
                        "loop", status=401, auth_url=self.account_url(),
                        message="Sign-in did not complete. Check that cookies are "
                                "enabled for this site, then try again.")
                return AuthDecision("redirect", status=302, auth_url=auth_url)
        if not user.has_plan(self.min_plan):
            return AuthDecision("forbidden", status=403, user=user)
        return AuthDecision("ok", user=user)


__all__ = [
    "ATTEMPT_PARAM",
    "AuthDecision",
    "AuthStartupError",
    "SettingsAuthenticator",
    "assert_safe_startup",
    "build_auth_config",
    "compute_return_target",
    "dev_principal_from_env",
    "is_production",
    "parse_cookie_header",
    "ConfigurationError",
]
