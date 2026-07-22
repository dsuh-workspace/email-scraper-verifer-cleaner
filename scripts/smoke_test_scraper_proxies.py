"""Quick smoke test for scraper proxy wiring.

Loads .env, resolves SCRAPER_PROXIES_FILE, builds gosom proxy args using the
real app helper, and reports how many proxies will be passed to `-proxies`.
This is config smoke only — it does not launch the scraper binary.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.scraper.run_scraper import _scraper_proxy_args  # noqa: E402


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
    _resolve_env_path("SCRAPER_PROXIES_FILE")

    try:
        args = _scraper_proxy_args()
    except Exception as exc:
        print(f"FAIL: could not build scraper proxy args: {exc}")
        return 1

    if not args:
        print("FAIL: no scraper proxies configured. Set SCRAPER_PROXIES or SCRAPER_PROXIES_FILE.")
        return 1

    if len(args) != 2 or args[0] != "-proxies":
        print(f"FAIL: unexpected scraper proxy args: {args!r}")
        return 1

    proxies = [item for item in args[1].split(",") if item.strip()]
    if not proxies:
        print("FAIL: -proxies built, but proxy list was empty.")
        return 1

    first_proxy = proxies[0]
    redacted_first = first_proxy.split("@")[-1]
    print(f"PASS: scraper will receive {len(proxies)} proxies via -proxies.")
    print(f"First proxy host: {redacted_first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
