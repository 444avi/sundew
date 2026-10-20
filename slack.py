"""Slack incoming-webhook sender for the Sundew and Sentinel channels.

Each monitor has a list of routes, each one a set of recipient addresses and
one webhook. A delivery is mirrored to every route of its monitor that lists
the recipient, and to no other route:

- ``default`` is the operator's own channel: ``SLACK_WEBHOOK_URL_<MONITOR>``
  with the addresses in ``SLACK_MIRROR_EMAILS``.
- ``SLACK_ROUTE_<MONITOR>_<NAME>=<email>[,<email>...]|<webhook URL>`` adds a
  client route, e.g. ``SLACK_ROUTE_SUNDEW_ACME``.

Ops notices go only to ``SLACK_OPS_WEBHOOK_URL``, or to the monitor's default
webhook when that is unset, never to a client route.

Email stays the system of record, so nothing here may raise into a delivery
path: an unset webhook is a silent no-op and every failure is logged and
swallowed. A webhook URL is a secret (anyone holding it can post to the
channel), so it is never logged or put in exception text; warnings name the
variable only. Stdlib only.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from collections import namedtuple

HOOK_PREFIX = "https://hooks.slack.com/services/"
SECTION_LIMIT = 3000  # Slack's per-section text limit
TRUNCATED = "\n… (truncated, full text in email)"
MONITOR_WEBHOOKS = {
    "sundew": "SLACK_WEBHOOK_URL_SUNDEW",
    "sentinel": "SLACK_WEBHOOK_URL_SENTINEL",
}
ROUTE_PREFIX = "SLACK_ROUTE_"
OPS_WEBHOOK = "SLACK_OPS_WEBHOOK_URL"
DEFAULT_ROUTE = "default"

# One Slack destination: ``emails`` is a lower-cased frozenset of recipients.
Route = namedtuple("Route", "name emails webhook")

log = logging.getLogger("slack")


def webhook_from_env(name, env=None):
    """Return the webhook in env var ``name``, or None if unset or not a Slack
    incoming-webhook URL. ``env`` defaults to ``os.environ``."""
    value = ((os.environ if env is None else env).get(name) or "").strip()
    if not value:
        return None
    if not value.startswith(HOOK_PREFIX):
        log.warning("slack: ignoring %s: not a %s URL", name, HOOK_PREFIX)
        return None
    return value


def webhook_for(env, monitor):
    """The channel webhook for one monitor, or None when unset/invalid."""
    name = MONITOR_WEBHOOKS.get(monitor)
    return webhook_from_env(name, env) if name else None


def _emails(raw):
    return frozenset(addr.strip().lower() for addr in (raw or "").split(",") if addr.strip())


def _route_prefix(monitor):
    return f"{ROUTE_PREFIX}{monitor.upper()}_"


def load_routes(env, monitor):
    """The mirror routes for one monitor: ``default`` first (when its webhook
    is set), then each valid ``SLACK_ROUTE_<MONITOR>_<NAME>`` sorted by name.
    A route with no ``|``, no emails or a non-Slack URL is skipped with a
    warning naming the variable. An empty value is skipped silently."""
    env = os.environ if env is None else env
    routes = []
    default_hook = webhook_for(env, monitor)
    if default_hook:
        routes.append(Route(DEFAULT_ROUTE, _emails(env.get("SLACK_MIRROR_EMAILS")),
                            default_hook))
    prefix = _route_prefix(monitor)
    for variable in sorted(key for key in env if key.startswith(prefix)):
        name = variable[len(prefix):]
        value = (env.get(variable) or "").strip()
        if not value:
            continue
        raw_emails, bar, url = value.partition("|")
        emails, url = _emails(raw_emails), url.strip()
        if not name:
            problem = "no route name"
        elif not bar:
            problem = "expected <email>[,<email>...]|<webhook URL>"
        elif not emails:
            problem = "no recipient emails"
        elif not url.startswith(HOOK_PREFIX):
            problem = f"not a {HOOK_PREFIX} URL"
        else:
            routes.append(Route(name, emails, url))
            continue
        log.warning("slack: ignoring %s: %s", variable, problem)
    if default_hook:
        for route in routes[1:]:
            if route.emails & routes[0].emails:
                log.warning("slack: SLACK_MIRROR_EMAILS and %s%s share an address; "
                            "its deliveries post to both channels", prefix, route.name)
    return routes


def webhooks_for(routes, recipient):
    """Webhooks of every route listing ``recipient`` (case-insensitive), in
    route order with duplicates removed. Empty when none match."""
    address = (recipient or "").strip().lower()
    hooks = []
    for route in routes:
        if address and address in route.emails and route.webhook not in hooks:
            hooks.append(route.webhook)
    return hooks


def ops_webhook(env, monitor):
    """Where ops notices for ``monitor`` go: SLACK_OPS_WEBHOOK_URL, else the
    monitor's default webhook. Never a client route. None when neither is set."""
    return webhook_from_env(OPS_WEBHOOK, env) or webhook_for(env, monitor)


def unknown_route_variables(env):
    """``SLACK_ROUTE_*`` names that belong to no monitor (e.g. a missing
    ``SUNDEW_``/``SENTINEL_``), so a typo is reported instead of ignored."""
    env = os.environ if env is None else env
    known = tuple(_route_prefix(monitor) for monitor in MONITOR_WEBHOOKS)
    return sorted(key for key in env
                  if key.startswith(ROUTE_PREFIX) and not key.startswith(known))


def escape(text):
    """Escape Slack mrkdwn control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fit(escaped, room):
    """Truncate already-escaped text to ``room`` chars without splitting an
    entity (every ``&`` in escaped text starts one)."""
    if len(escaped) <= room:
        return escaped
    cut = escaped[:room - len(TRUNCATED)]
    if cut.rfind("&") > cut.rfind(";"):
        cut = cut[:cut.rfind("&")]
    return cut + TRUNCATED


def plain_block(title, body):
    """Blocks mirroring a plain-text email: a bold title, then the body in a
    code block so its column alignment survives."""
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "*" + _fit(escape(title), SECTION_LIMIT - 2) + "*"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "```" + _fit(escape(body), SECTION_LIMIT - 6) + "```"}},
    ]


def post(webhook, text, blocks=None):
    """POST one message. Returns True only when Slack answers 200 ``ok``.
    Never raises; logs status, Slack's error string or the exception type, but
    never the URL."""
    if not webhook:
        return False
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        request = urllib.request.Request(
            webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            status = response.status
            reply = response.read(200).decode("utf-8", "replace").strip()
        if status == 200 and reply == "ok":
            return True
        log.warning("slack: post rejected: status=%s error=%s", status, reply[:100])
    except urllib.error.HTTPError as exc:
        try:
            reply = exc.read(200).decode("utf-8", "replace").strip()
        except Exception:
            reply = ""
        log.warning("slack: post rejected: status=%s error=%s", exc.code, reply[:100])
    except Exception as exc:
        log.warning("slack: post failed: %s", type(exc).__name__)
    return False
