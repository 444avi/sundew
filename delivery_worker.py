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
from notification_app import AppStore, DeliveryWorker


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP_DB = os.path.join(HERE, "app.db")


def log(message):
    print(f"[delivery] {message}", flush=True)


def slack_test_destinations(env, monitor, route=None):
    """(name, webhook) pairs for --test-slack: the default webhook, the ops
    webhook when it is set apart from the default, then every client route; or
    only the client route ``route``. Each webhook appears once."""
    clients = [r for r in slack.load_routes(env, monitor) if r.name != slack.DEFAULT_ROUTE]
    if route:
        pairs = [(r.name, r.webhook) for r in clients if r.name.upper() == route.upper()]
    else:
        pairs = [(slack.DEFAULT_ROUTE, slack.webhook_for(env, monitor)),
                 ("ops", slack.ops_webhook(env, monitor))]
        pairs += [(r.name, r.webhook) for r in clients]
    destinations, seen = [], set()
    for name, hook in pairs:
        if hook and hook not in seen:
            seen.add(hook)
            destinations.append((name, hook))
    return destinations


def test_slack(env, monitor, route=None):
    """Post a contentless labeled test to each of one monitor's Slack
    destinations. Prints one line per destination, never the URL."""
    destinations = slack_test_destinations(env, monitor, route)
    if not destinations:
        if route:
            prefix = f"{slack.ROUTE_PREFIX}{monitor.upper()}_"
            log(f"ERROR: {prefix}{route.upper()} is unset or invalid (see warning above)")
        else:
            log(f"ERROR: no Slack destination for {monitor}: set "
                f"{slack.MONITOR_WEBHOOKS[monitor]}, {slack.OPS_WEBHOOK} or a "
                f"{slack.ROUTE_PREFIX}{monitor.upper()}_<NAME> route "
                f"({slack.HOOK_PREFIX} URLs only)")
        return 2
    failed = 0
    for name, hook in destinations:
        text = f"[TEST] {monitor.title()} Slack test ({name}). No real event occurred."
        if name in ("ops", slack.DEFAULT_ROUTE):
            text += f" Sent by delivery_worker.py on {socket.gethostname()}."
        if slack.post(hook, text):
            log(f"slack test {monitor} {name}: accepted")
        else:
            failed += 1
            log(f"ERROR: slack test {monitor} {name}: rejected (see warning above)")
    return 1 if failed else 0


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
                        help="post a test message to that monitor's Slack channels and exit")
    parser.add_argument("--route", metavar="NAME",
                        help="with --test-slack, post only to SLACK_ROUTE_<MONITOR>_<NAME>")
    args = parser.parse_args(argv)
    if args.route and not args.test_slack:
        parser.error("--route needs --test-slack")

    sundew.load_env(args.env)
    env = dict(os.environ)
    if args.test_slack:
        return test_slack(env, args.test_slack, args.route)
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
