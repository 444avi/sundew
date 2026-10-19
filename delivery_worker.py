#!/usr/bin/env python3
"""Shared per-user email and digest worker for Sundew and Sentinel."""

import argparse
import os
import socket
import sys
import time

import sentinel_config as sentinel_cfg
import slack
import sundew
from notification_app import AppStore, DeliveryWorker, static_preview


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_DB = os.path.join(HERE, "app.db")


def log(message):
    print(f"[delivery] {message}", flush=True)


def test_slack(env, monitor):
    """Post a labeled test message to one monitor's channel."""
    variable = slack.MONITOR_WEBHOOKS[monitor]
    hook = slack.webhook_for(env, monitor)
    if not hook:
        log(f"ERROR: {variable} is unset or not a {slack.HOOK_PREFIX} URL")
        return 2
    preview = static_preview(monitor)
    title = "[TEST] " + preview["subject"]
    body = "\n".join([
        preview["eyebrow"], "",
        preview["headline"], "",
        preview["summary"], "",
        f"Slack test from delivery_worker.py on {socket.gethostname()}. "
        "No real event occurred.",
    ])
    if slack.post(hook, title, slack.plain_block(title, body)):
        log(f"slack test accepted for {monitor}")
        return 0
    log(f"ERROR: slack test rejected for {monitor} (see warning above)")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deliver pending Sundew and Sentinel notifications.")
    parser.add_argument("--once", action="store_true",
                        help="process currently due work and exit")
    parser.add_argument("--force-digests", action="store_true",
                        help="send non-empty queued digests now")
    parser.add_argument("--monitor", choices=("sundew", "sentinel"))
    parser.add_argument("--interval", type=int, default=15,
                        help="seconds between delivery scans")
    parser.add_argument("--app-db", default=DEFAULT_APP_DB)
    parser.add_argument("--env", default=os.path.join(HERE, ".env"))
    parser.add_argument("--test-slack", choices=("sundew", "sentinel"),
                        help="post a test message to that monitor's Slack channel and exit")
    args = parser.parse_args(argv)

    sundew.load_env(args.env)
    env = dict(os.environ)
    if args.test_slack:
        return test_slack(env, args.test_slack)
    missing = [key for key in ("EMAIL_FROM",) if not env.get(key)]
    if missing:
        log(f"ERROR: missing {', '.join(missing)}")
        return 2
    if args.interval < 1:
        log("ERROR: --interval must be at least one second")
        return 2

    store = AppStore(args.app_db)
    try:
        store.seed_from_env(
            env,
            default_timezone=env.get("APP_DEFAULT_TIMEZONE", "America/Los_Angeles"),
            sentinel_digest_time=f"{sentinel_cfg.DIGEST_HOUR_LOCAL:02d}:00",
            sentinel_market_cap_floor=sentinel_cfg.MARKET_CAP_MIN_USD)
        if store.get_settings() is None:
            log("ERROR: no notification user is configured; seed a legacy recipient once")
            return 2
        worker = DeliveryWorker(store, env, logger=log)
        while True:
            result = worker.run_due(
                monitor=args.monitor, force_digests=args.force_digests)
            if result["immediate"] or result["digests"]:
                log(f"pass complete: immediate={result['immediate']} digests={result['digests']}")
            if args.once or args.force_digests:
                return 0
            time.sleep(args.interval)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
