"""Slack incoming-webhook sender for the Sundew and Sentinel channels.

Level 1 is operator-only: each monitor posts to one fixed webhook from env, and
notifications are mirrored only for the operator's own addresses
(``SLACK_MIRROR_EMAILS``). Email stays the system of record, so nothing here
may raise into a delivery path: an unset webhook is a silent no-op and every
failure is logged and swallowed.

The webhook URL is a secret (anyone holding it can post to the channel), so it
is never logged or put in exception text. Stdlib only.
"""

import json
import logging
import os
import urllib.error
import urllib.request

HOOK_PREFIX = "https://hooks.slack.com/services/"
SECTION_LIMIT = 3000  # Slack's per-section text limit
TRUNCATED = "\n… (truncated, full text in email)"
MONITOR_WEBHOOKS = {
    "sundew": "SLACK_WEBHOOK_URL_SUNDEW",
    "sentinel": "SLACK_WEBHOOK_URL_SENTINEL",
}

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


def mirror_allowed(recipient, env=None):
    """True if ``recipient`` is listed in SLACK_MIRROR_EMAILS (comma-separated,
    case-insensitive). Unset mirrors nothing."""
    raw = (os.environ if env is None else env).get("SLACK_MIRROR_EMAILS") or ""
    allowed = {addr.strip().lower() for addr in raw.split(",") if addr.strip()}
    return bool(recipient) and recipient.strip().lower() in allowed


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
