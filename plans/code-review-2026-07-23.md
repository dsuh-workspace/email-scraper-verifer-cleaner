# Code Review — 2026-07-23

Workflow-backed review (xhigh effort). 6 finders × cross-angle candidates,
independent verifier per (file, line), 15 findings survived after refutation.

Scope: uncommitted + recent changes since 2026-07-21 review (commit 393a10c),
centered on the new industry-aware harvest-queries plumbing in
`run_pipeline.py` and its tests.

## Executive summary

One major functional regression, a family of tightly-related classifier
defects, and a handful of correctness gaps + cleanup items:

- **Full-harvest Pass 2 silently degrades** from an 8-query sweep to a
  single query for any industry outside plumbing/HVAC — no exception, only
  a `logger.warning` that is easy to miss. This is where the +39% coverage
  advantage over grid comes from; losing it is invisible from the final
  "Full-harvest complete" line.
- **Industry classifier has 3 co-located defects** — over-broad `'leak'`
  keyword misroutes HVAC queries, plumbing-first ordering makes HVAC
  unreachable for combined queries, and HVAC keywords miss common shorthand
  (`AC`, `A/C`, `boiler`, `refrigeration`) — including entries from the
  tool's own `DEFAULT_HVAC_HARVEST_QUERIES`.
- **R4/R5 hardening is uneven** — `--min-contacts != 500` uses a magic
  literal as a proxy for "user explicitly set the flag"; `--max-depth`
  has no analogous warning despite the same "single-centroid only"
  scope; the R5-mandated warning is emitted but never asserted in tests.
- **Cleanup**: side-effect warning inside a pure lookup helper, dead
  `min_contacts` parameter for non-single-centroid strategies, inconsistent
  Pass 3 log format, and R0 (release-blocker NameError) removed from
  CLAUDE.md without a matching CHANGELOG entry.

Verified findings arranged most-severe first.

---

## Findings

### 1. 🔴 `run_pipeline.py:514` — Full-harvest Pass 2 drops to 1-query for non-plumbing/HVAC industries

**Category**: correctness · **Verdict**: CONFIRMED

Full-harvest Pass 2 loses its 8-query variant sweep for any industry that
is not plumbing or HVAC (previously always ran `DEFAULT_HARVEST_QUERIES`).

**Failure scenario**: User runs
`python run_pipeline.py "Electrician" "Denver, CO" --strategy full-harvest`
(no `--queries`). Previously Pass 2 always ran the 8-element
`DEFAULT_HARVEST_QUERIES` sweep (per commit history + CHANGELOG this is
where the +39% coverage over grid comes from). After this diff,
`_default_harvest_queries("Electrician")` returns `("Electrician",)`, so
Pass 2 runs a single query and only logs a warning. If the operator misses
the warning (log-level filters, cron output tail-only, etc.) they get
roughly Pass-1-only coverage while still paying full-harvest wall time —
no exception, no early exit, no visible signal in the final
"Full-harvest complete" line.

---

### 2. 🔴 `run_pipeline.py:62` — `'leak'` keyword misroutes HVAC queries to plumbing set

**Category**: correctness · **Verdict**: CONFIRMED
(same root cause also at `run_pipeline.py:58`)

`'leak'` in `_PLUMBING_KEYWORDS` misroutes `'AC leak repair'` and
`'refrigerant leak'` HVAC queries to the plumbing variant set, because
plumbing keywords are checked before HVAC keywords.

**Failure scenario**: User runs `--query 'AC leak repair' --strategy full-harvest`.
`_default_harvest_queries` matches `'leak'` first and returns
`DEFAULT_HARVEST_QUERIES` (Plumbing, Plumber, Drain cleaning, Sewer
service, …). Pass 2 slow multi-query scrape at centroid searches for
plumbers instead of HVAC leak-repair businesses; HVAC leads for that
market are drastically undercounted.

---

### 3. 🔴 `run_pipeline.py:68` — Plumbing-first ordering makes HVAC branch unreachable for combined queries

**Category**: correctness · **Verdict**: CONFIRMED

Plumbing check runs before HVAC check, so any query containing both
classes of keywords (e.g., `'HVAC & plumbing'`, `'plumbing and heating'`)
is always classified as plumbing — the HVAC branch is unreachable for
such input.

**Failure scenario**: Contractor sells combined services and runs
`--query 'Plumbing and HVAC' --strategy full-harvest`.
`_default_harvest_queries` returns plumbing set only; Pass 2 emits 8
plumbing variants and no HVAC variants. HVAC-only businesses in that
metro are never surfaced in Pass 2.

---

### 4. 🔴 `run_pipeline.py:71` — `_HVAC_KEYWORDS` misses common HVAC shorthand

**Category**: correctness · **Verdict**: CONFIRMED
(same root cause also at `run_pipeline.py:59`)

`_HVAC_KEYWORDS` misses common HVAC shorthand (`'AC'`, `'A/C'`,
`'boiler'`, `'refrigeration'`); those queries fall through to the
`(query,)` fallback and Pass 2 degenerates to a single-query slow scrape
at the centroid.

**Failure scenario**: User runs `--query 'AC repair' --strategy full-harvest`.
`'ac'` does not match `'air condition'`, `'heating'`, `'cooling'`,
`'furnace'`, `'heat pump'`, or `'hvac'`. Function warns "no built-in
harvest query set" and returns `('AC repair',)`. Pass 2 runs a
single-query slow scrape instead of the intended 8-variant multi-query
sweep, cutting HVAC coverage roughly to Pass-1-only levels for that
market.

---

### 5. 🟠 `run_pipeline.py:667` — Magic-literal `500` hides min-contacts R4 warning

**Category**: correctness · **Verdict**: CONFIRMED
(same root cause also at `run_pipeline.py:664`)

`args.min_contacts != 500` uses a hardcoded literal as a stand-in for
"user explicitly set `--min-contacts`"; passing the same value as the
default (or drifting the default in `parse_args`) silences the "ignored by
grid/full-harvest" warning that R4 is supposed to give.

**Failure scenario**: User expecting the same cap as before passes
`--min-contacts 500 --strategy grid`. The equality check fails, no warning
fires, and the grid strategy still ignores `min_contacts`. Grid runs to
completion crawling the whole bbox rather than stopping near 500
contacts — user thinks the cap is being honored.

**Fix hint**: Use `parser.set_defaults(min_contacts=None)` and check
`is not None`, or compare against `parser.get_default("min_contacts")`.

---

### 6. 🟠 `run_pipeline.py:667` — No warning when `--max-depth` used with grid/full-harvest

**Category**: correctness · **Verdict**: CONFIRMED

R4 fix warns when `--min-contacts` is combined with a non-single-centroid
strategy, but the equally moot `--max-depth` on grid/full-harvest is not
warned about — asymmetric warning coverage for two flags that share the
exact same "single-centroid only" scope. `--max-depth`'s help text was
also not updated with the analogous "single-centroid only" note that
`--min-contacts` received on line 251.

**Failure scenario**: User runs
`python run_pipeline.py --strategy full-harvest --max-depth 3 --query Plumbing --location "San Jose, CA"`.
They believe the grid + multi-query + ZIP top-up passes are being capped
by `max-depth=3`. Full-harvest actually runs Pass 1 at depth 3, Pass 2 at
depth 10, Pass 3 at depth 3 unconditionally — `max_depth` is only read
inside the single-centroid `while` loop. No warning fires, unlike the
equivalent situation with `--min-contacts`, and the user's coverage-limiting
intent is silently ignored.

---

### 7. 🟠 `run_pipeline.py:680` — `--queries` silently dropped for non-full-harvest strategies

**Category**: correctness · **Verdict**: PLAUSIBLE

`if args.queries and strategy == 'full-harvest'` silently drops
`--queries` for grid/single-centroid, and there is no earlier hard error —
the warning at ~line 662 is easy to miss, so users who intended custom
variants get default behavior with no visible failure.

**Failure scenario**: User runs
`--query Plumbing --strategy grid --queries "Plumber,Leak repair,Drain cleaning"`,
thinking grid mode will use their custom queries. The warning about
ignored queries scrolls past in the log; grid strategy proceeds with the
single base query. User is unaware their variants were dropped and treats
the smaller grid harvest as authoritative.

**Fix hint**: `parser.error()` when `--queries` is passed with
non-full-harvest strategy, or accept and thread `--queries` into grid too.

---

### 8. 🟠 `run_pipeline.py:81` — Fallback returns raw None/empty query without guard

**Category**: correctness · **Verdict**: PLAUSIBLE

`_default_harvest_queries` fallback returns the raw (possibly `None`/empty)
query — bypasses the `(query or "").lower()` guard used two lines above,
so `_default_harvest_queries(None)` yields `(None,)` and
`_default_harvest_queries("")` yields `("",)`.

**Failure scenario**: A programmatic caller (or bug shortcutting argparse)
that invokes `run_end_to_end_pipeline(query=None, strategy="full-harvest", ...)`
reaches line 514 `queries or _default_harvest_queries(query)`, gets
`[None]`, and Pass 2 calls
`execute_scrape_and_ingest(..., queries=[None], ...)` — passing `None` as a
Google Maps search term produces a crash on the first `str` method
(e.g. `None.strip()`, `None.lower()`) inside the scraper, or, if the
scraper is defensive, silently scrapes an empty query and returns garbage
that gets ingested as real leads.

---

### 9. 🟠 `run_pipeline.py:514` — Empty tuple `queries=()` silently reverts to industry defaults

**Category**: correctness · **Verdict**: PLAUSIBLE

`queries or _default_harvest_queries(query)` treats an empty tuple as
"not provided" — classic Python falsy-container pitfall (`()` is falsy
just like `None`), so an explicit `queries=()` from a programmatic caller
silently reverts to industry defaults instead of running with no variants.

**Failure scenario**: A caller writes
`run_end_to_end_pipeline(query="HVAC", strategy="full-harvest", queries=())`
intending "skip the multi-query pass expansion, use only the base query".
Because `() or X == X`, Pass 2 instead runs the 8-query
`DEFAULT_HVAC_HARVEST_QUERIES` set — 8× the Playwright cost the user asked
to avoid, plus lead attribution wrong (rows come from queries the caller
thought they had disabled).

**Fix hint**: Explicit `if queries is None: queries = _default_harvest_queries(query)`.

---

### 10. 🟠 `tests/test_run_pipeline.py:685` — Test never asserts R5-mandated warning fires

**Category**: correctness · **Verdict**: CONFIRMED

`test_default_harvest_queries_unknown_falls_back_to_single` asserts the
fallback return value but never captures/checks that the R5-mandated
`logger.warning("No built-in harvest query set for %r; ...")` was emitted.
The whole point of R5 was "tell the user to pass `--queries` when we
don't know the industry" — that user-visible side effect is now untested
and can be silently deleted without any test failure.

**Failure scenario**: A future refactor drops the `logger.warning(...)`
call inside `_default_harvest_queries` (e.g. someone "cleans up side
effects in pure lookup helpers" per the already-flagged R7-style
critique). Every existing test still passes because none uses `caplog` to
assert the warning fires. R5's intended user feedback silently disappears;
a user running full-harvest on `--query Roofing` gets no hint that they
should pass `--queries`, Pass 2 quietly degenerates to a single-query slow
scrape, and the regression ships.

**Fix hint**: Add `caplog.set_level(logging.WARNING)` and assert
`"No built-in harvest query set"` in `caplog.text`.

---

### 11. 🟡 `run_pipeline.py:514` — Warning fires even when Pass 2 will be skipped

**Category**: cleanup · **Verdict**: CONFIRMED

`query_variants` is computed (and its warning emitted) before the Pass 2
skip check, so the warning fires even when Pass 2 will be skipped.

**Failure scenario**: Full-harvest run with `query='Roofing'` on a location
where Nominatim returns `lat=None`: line 514 calls
`_default_harvest_queries('Roofing')`, which logs "multi-query pass will
run with only the base query". Then line 555 hits the `else` branch and
logs "Skipping PASS 2". The user sees a warning telling them Pass 2 will
run with degraded queries, immediately followed by "PASS 2 skipped" —
two contradictory log lines.

**Fix hint**: Move the assignment inside the `if lat is not None and lon is not None:` block.

---

### 12. 🟡 `run_pipeline.py:75` — `logger.warning` side-effect inside pure lookup helper

**Category**: cleanup · **Verdict**: CONFIRMED
(same root cause also at `run_pipeline.py:514`)

`_default_harvest_queries` embeds a `logger.warning` side effect inside an
otherwise pure lookup helper.

**Failure scenario**: Callers can't opt out of the warning, and unit tests
can't easily assert "warning was emitted" without patching the module
logger. `test_default_harvest_queries_unknown_falls_back_to_single` at
`test_run_pipeline.py:679` asserts only the return value; the warning
goes unverified.

**Fix hint**: Return the choice + a flag (or raise a typed "no default"
marker) and let `run_end_to_end_pipeline` log at the site that has full
context (which pass, whether Pass 2 will actually run).

---

### 13. 🟡 `run_pipeline.py:686` — `min_contacts` dead code outside single-centroid branch

**Category**: cleanup · **Verdict**: CONFIRMED

`min_contacts` is threaded into `run_end_to_end_pipeline` for every
strategy but is dead code outside the single-centroid branch.

**Failure scenario**: Grid and full-harvest branches no longer log or gate
on `min_contacts` after this commit (lines 505, 590 both dropped the
`(target %d)` clause). Yet `main()` at line 686 still passes
`min_contacts=args.min_contacts` for all strategies, and
`run_end_to_end_pipeline`'s signature at line 420 still accepts it with
`default=500`. Anyone reading the function signature would still think
grid honors `min_contacts`, and the R4 mitigation is bolted on as a
warning at `main()` rather than pushed into the mechanism.

**Fix hint**: Per-strategy config object, or explicit `min_contacts=None`
for non-single-centroid callers, so the signature reflects reality.

---

### 14. 🟡 `run_pipeline.py:568` — Pass 3 log format inconsistent within same loop

**Category**: correctness · **Verdict**: CONFIRMED

Pass 3 log format is inconsistent within the same loop: the new success
line (line ~568) is `"  [%d/%d] scraping %s"` where `%s` is the full
joined `zip_loc` (e.g. `"San Jose, CA 95112"`), but the neighboring
geocode-failure line (line ~565) is
`"  [%d/%d zip %s] geocode failed, skipping."` where `%s` is the raw ZIP
alone. Two lines emitted from the same for-loop iteration follow two
different format conventions, breaking log-parser regexes and grep
patterns keyed on either shape.

**Failure scenario**: Ops writes a log-parser regex
`r"\[(\d+)/(\d+)] scraping (.+)"` to compute per-ZIP throughput. The
parser silently drops the `[i/N zip 95120] geocode failed, skipping.` lines
because they use a different bracket format, then double-drops those same
ZIPs from the denominator when counting outcomes — success rate looks
artificially high because failed ZIPs never appear in the numerator or
denominator.

**Fix hint**: A consistent shape (e.g. include the raw ZIP in both lines,
or the joined `zip_loc` in both) would make the loop machine-parseable.

---

### 15. 🟡 `CHANGELOG.md:6` — R0 blocker removed from CLAUDE.md without matching CHANGELOG entry

**Category**: cleanup · **Verdict**: CONFIRMED

R0 (release-blocker `NameError`) removed from CLAUDE.md without a matching
CHANGELOG entry — traceability gap on a blocker item.

**Failure scenario**: CLAUDE.md previously flagged R0 as "Blocks release"
with a specific reproducer (kwargs at lines ~116–119 of `run_pipeline.py`).
Commit `986b640` deletes the entry entirely; `CHANGELOG.md`'s "Recently
closed" list covers R2/R3/R4/R5 but never mentions R0. R0 was actually
fixed by commit `d8df29f` ("Add scraper tuning controls") — a commit
message that gives no hint the blocker was addressed. A future reviewer
who greps CHANGELOG for R0 will find nothing and reasonably suspect
"closed on paper only."

**Fix hint**: Add an R0 entry citing `d8df29f` as the fix commit.

---

## Stats

- **Effort**: xhigh
- **Finders**: 6 (one per correctness angle + one cleanup sweep)
- **Candidates surfaced**: 28
- **Verifier agents**: 22 (independent per unique file:line)
- **Verified**: 28 · **Refuted**: 4 · **Reported**: 15
- **Wall time**: ~34 min · **Total subagent tokens**: ~1.79 M

## Suggested fix order

1. **Fix classifier defects together** (#2, #3, #4) — they share the same
   `_default_harvest_queries` function. Reorder HVAC-before-plumbing,
   remove `'leak'` from plumbing keywords, add `AC`/`A/C`/`boiler`/
   `refrigeration` to HVAC keywords. Add tests using `pytest.mark.parametrize`
   over `[("AC leak repair", "hvac"), ("plumbing and heating", …), …]`.
2. **Fix the Pass 2 regression** (#1) — either restore
   `DEFAULT_HARVEST_QUERIES` as fallback for unknown industries, or
   `parser.error()` early when full-harvest + non-plumbing/HVAC + no
   `--queries`. Silent degradation is the worst mode.
3. **Fix the "did the user set this flag" plumbing** (#5, #6, #7) — use
   `parser.set_defaults(min_contacts=None)` + `is not None`; add symmetric
   `--max-depth` warning; decide whether `--queries` should hard-error for
   non-full-harvest.
4. **Test hardening** (#10) — add `caplog` assertion for R5 warning.
5. **Cleanup pass** (#8, #9, #11, #12, #13, #14, #15) — tighten
   `_default_harvest_queries` contract, remove dead `min_contacts`, add
   R0 CHANGELOG entry, unify Pass 3 log format.
