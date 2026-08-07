#!/usr/bin/env python
"""Report Webshare proxy bandwidth/requests for a time window.

Cost modelling needs a per-city number, and the only honest way to get one is
to bracket a real run. Two ways to use it:

    # what a run just cost — bracket it
    scripts/webshare_usage.py --last-minutes 45

    # explicit window
    scripts/webshare_usage.py --since 2026-08-07T08:00:00Z \\
                              --until  2026-08-07T09:30:00Z

    # the last month
    scripts/webshare_usage.py --days 30

Reads WEBSHARE_API_KEY from .env. The key is never printed, and this only
calls the read-only stats endpoint.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

load_dotenv(_ROOT / ".env")

STATS_URL = "https://proxy.webshare.io/api/v2/stats/aggregate/"
TIMEOUT_SEC = 30


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def fetch(api_key: str, since: datetime, until: datetime) -> dict:
    response = requests.get(
        STATS_URL,
        params={
            "timestamp__gte": since.isoformat(),
            "timestamp__lte": until.isoformat(),
        },
        headers={"Authorization": f"Token {api_key}"},
        timeout=TIMEOUT_SEC,
    )
    if response.status_code == 401:
        raise SystemExit("Webshare rejected WEBSHARE_API_KEY (401).")
    response.raise_for_status()
    return response.json()


def report(data: dict, since: datetime, until: datetime) -> None:
    total = int(data.get("bandwidth_total") or 0)
    ok = int(data.get("requests_successful") or 0)
    failed = int(data.get("requests_failed") or 0)
    requests_total = ok + failed
    hours = max((until - since).total_seconds() / 3600, 1e-9)

    print(f"window   : {since.isoformat()}  ->  {until.isoformat()}  ({hours:.2f} h)")
    print(f"bandwidth: {_human_bytes(total)}")
    print(f"requests : {requests_total:,}  ({ok:,} ok, {failed:,} failed"
          f"{f', {failed / requests_total:.1%} failure rate' if requests_total else ''})")

    if requests_total:
        print(f"avg/req  : {_human_bytes(total / requests_total)}")

    breakdown = [
        ("target unavailable", data.get("error_target_website_unavailable")),
        ("forbidden host", data.get("error_forbidden_target_website")),
        ("timeout", data.get("error_timeout")),
        ("proxy auth", data.get("error_proxy_authentication")),
    ]
    shown = [(label, int(value)) for label, value in breakdown if value]
    if shown:
        print("failures :")
        for label, value in sorted(shown, key=lambda kv: -kv[1]):
            print(f"           {value:>7,}  {label}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--last-minutes", type=float,
                       help="Window ending now, N minutes wide.")
    group.add_argument("--days", type=float, default=None,
                       help="Window ending now, N days wide (API caps the "
                            "span, so very long windows 400).")
    parser.add_argument("--since", help="ISO8601 start (with --until).")
    parser.add_argument("--until", help="ISO8601 end (defaults to now).")
    args = parser.parse_args()

    api_key = (os.getenv("WEBSHARE_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("WEBSHARE_API_KEY is not set in .env.")

    now = datetime.now(timezone.utc)
    if args.days:
        since, until = now - timedelta(days=args.days), now
    elif args.last_minutes:
        since, until = now - timedelta(minutes=args.last_minutes), now
    elif args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        until = (
            datetime.fromisoformat(args.until.replace("Z", "+00:00"))
            if args.until else now
        )
    else:
        since, until = now - timedelta(hours=1), now

    report(fetch(api_key, since, until), since, until)


if __name__ == "__main__":
    main()
