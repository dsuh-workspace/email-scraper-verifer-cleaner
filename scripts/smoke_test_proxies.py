"""Quick smoke test for crawler proxy wiring.

Loads .env, resolves crawler proxy settings, makes one request to an IP echo
endpoint through the configured proxy, and prints whether proxy routing works.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.pipeline.extract_emails import _build_crawler_proxies  # noqa: E402

DEFAULT_URL = "https://httpbin.org/ip"
TIMEOUT_SEC = 15


def _resolve_env_path(var_name: str) -> None:
    value = os.getenv(var_name, "").strip()
    if not value:
        return

    candidate = Path(value)
    if candidate.is_absolute() or candidate.exists():
        return

    repo_candidate = ROOT / value
    if repo_candidate.exists():
        os.environ[var_name] = str(repo_candidate)


def main() -> int:
    load_dotenv(ROOT / ".env")
    _resolve_env_path("CRAWLER_PROXY_FILE")

    url = os.getenv("PROXY_SMOKE_URL", DEFAULT_URL)
    proxies = _build_crawler_proxies()
    if not proxies:
        print("FAIL: no crawler proxy configured. Set CRAWLER_PROXY, split vars, or CRAWLER_PROXY_FILE.")
        return 1

    print(f"Testing {url} with crawler proxies for schemes: {', '.join(sorted(proxies))}")
    try:
        response = requests.get(url, proxies=proxies, timeout=TIMEOUT_SEC)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"FAIL: request through proxy failed: {exc}")
        return 1

    try:
        payload = response.json()
    except json.JSONDecodeError:
        print("FAIL: endpoint did not return JSON.")
        print(response.text[:500])
        return 1

    origin = payload.get("origin")
    proxy_hosts = {proxy.split("@")[-1].split(":")[0] for proxy in proxies.values()}

    print(json.dumps(payload, indent=2))
    if origin and any(host in origin for host in proxy_hosts):
        print("PASS: response origin matches configured proxy host.")
        return 0

    print("WARN: request succeeded, but origin did not match proxy host exactly.")
    print(f"Configured proxy hosts: {sorted(proxy_hosts)}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
