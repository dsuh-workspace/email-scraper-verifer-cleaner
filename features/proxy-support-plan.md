# Context
User wants written plan for adding proxy support to this repo, using upstream `gosom/google-maps-scraper` proxy capability. Current pipeline has two outbound network surfaces: scraper subprocess in `app/scraper/run_scraper.py` and website crawler in `app/pipeline/extract_emails.py`. Upstream scraper supports built-in proxy rotation via `-proxies` with comma-separated proxy URLs. Local crawler uses plain `requests.get(...)` and no proxy config today. Goal: add practical proxy support with low churn, clear config, and good testability.

# Recommended approach
Use explicit env-driven proxy config with separate knobs for scraper binary and Python website crawler. Do not build full shared networking framework yet. Let upstream gosom scraper own proxy rotation through its native `-proxies` flag. Keep Python crawler simpler: support one explicit HTTP/HTTPS proxy mapping via `requests` `proxies=`. Keep geocoding and dormant verification path out of first pass unless user asks for repo-wide proxying.

Why this approach:
- Matches current code style: env vars + local helpers, not central config objects.
- Small blast radius: only scraper wrapper, crawler, docs, tests.
- Avoids overengineering around session factories and per-component toggles before need proven.
- Uses upstream proxy feature where it already exists instead of re-implementing rotation locally.

# Critical files to modify
- `app/scraper/run_scraper.py`
- `app/pipeline/extract_emails.py`
- `README.md`
- `tests/test_extract_emails.py`
- new scraper-focused test file if needed, likely `tests/test_run_scraper.py`

# Implementation plan
1. Add scraper proxy helper in `app/scraper/run_scraper.py`.
   - Read `SCRAPER_PROXIES` from env.
   - Validate minimal shape: split on commas, trim whitespace, reject empty segments, allow schemes upstream documents (`http`, `https`, `socks5`, `socks5h`).
   - Return extra CLI args `['-proxies', value]` when configured.
   - Append helper result into existing `cmd` list before `subprocess.run(...)`.
   - Log count of configured proxies, never raw proxy URLs with credentials.

2. Add crawler proxy helper in `app/pipeline/extract_emails.py`.
   - Read `CRAWLER_HTTP_PROXY`, `CRAWLER_HTTPS_PROXY`, fallback `CRAWLER_PROXY`.
   - Build explicit `requests` proxy dict or `None`.
   - Keep first pass to HTTP/HTTPS proxy URLs only unless user explicitly wants SOCKS for crawler too.
   - Fail early on malformed values with clear message.

3. Thread crawler proxy config into fetch path.
   - Update `_crawl_business(...)` to accept optional proxy dict or close over helper result.
   - Pass `proxies=...` into existing `requests.get(...)` call.
   - Keep `ThreadPoolExecutor`, per-host locks, and request flow unchanged.

4. Keep scope intentionally narrow.
   - Do not refactor to shared `requests.Session` yet.
   - Do not add proxy rotation or health checks for crawler.
   - Do not wire proxy support into `verify_emails.py` in first pass because main pipeline does not call it.
   - Do not change geocoding unless user wants all outbound traffic proxied.

5. Document config in `README.md`.
   - Add env vars:
     - `SCRAPER_PROXIES`
     - `CRAWLER_PROXY`
     - `CRAWLER_HTTP_PROXY`
     - `CRAWLER_HTTPS_PROXY`
   - Explain split behavior:
     - scraper uses upstream gosom `-proxies` and can rotate
     - crawler uses single explicit proxy mapping, no local rotation
   - Add example values with auth syntax.
   - Note credentials should stay in `.env`, never logs.

6. Add unit tests.
   - In crawler tests (`tests/test_extract_emails.py` or small new file):
     - no env -> helper returns `None`
     - `CRAWLER_PROXY` only -> both `http` and `https`
     - split vars override fallback
     - malformed proxy raises clear error
   - In scraper tests (`tests/test_run_scraper.py` recommended):
     - no env -> no extra args
     - valid single/multi proxy string -> `['-proxies', value]`
     - malformed empty segment -> clear error
     - unsupported scheme -> clear error
   - Mock subprocess, do not invoke real scraper binary.

# Verification
- Run unit tests for proxy helpers and existing affected tests.
- Manual no-proxy run: confirm current behavior unchanged.
- Manual scraper-only config: confirm command contains `-proxies` and crawler stays direct.
- Manual crawler-only config: confirm `requests.get(...)` receives explicit `proxies` mapping.
- Manual invalid config: confirm early actionable error, not deep thread/subprocess failure.

# Notes / tradeoffs
- Upstream gosom already supports proxy rotation. Reuse it.
- Crawler SOCKS support likely needs extra dependency (`requests[socks]` / `PySocks`). Skip first pass unless requested.
- Full central config layer possible later, but not needed for this feature unless user wants repo-wide proxy policy across scraper, crawler, geocoder, and verifier.
