"""Tests for block detection, the proxy health ledger, and pacing."""

import json

import pytest

from app.scraper import pacing, proxy_health
from app.scraper.block_detect import classify_yield
from app.scraper.proxy_health import (
    ProxyPoolExhausted,
    filter_cooling,
    proxy_id,
    record_block,
    record_success,
)
from app.scraper.run_scraper import _select_scraper_proxies, _session_offset

P1 = "http://u1:pw1@p1.example.com:8080"
P2 = "http://u2:pw2@p2.example.com:8080"
P3 = "http://u3:pw3@p3.example.com:8080"


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SCRAPER_PROXIES", raising=False)
    monkeypatch.delenv("SCRAPER_PROXIES_FILE", raising=False)
    monkeypatch.delenv("SCRAPER_PROXY_LIMIT", raising=False)
    monkeypatch.delenv("SCRAPER_PACING_SEC", raising=False)
    for name in (
        "PROXY_COOLDOWN_SEC",
        "PROXY_RETIRE_AFTER_STRIKES",
        "PROXY_RETIRE_SEC",
        "BLOCK_DETECT_ENABLED",
        "BLOCK_DETECT_ZERO_YIELD",
        "BLOCK_DETECT_MIN_HISTORY",
        "BLOCK_DETECT_LOW_YIELD_RATIO",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROXY_HEALTH_FILE", str(tmp_path / "proxy_health.json"))
    # Waiting is off by default here so exhaustion tests fail fast instead of
    # sitting through a real cooldown; TestWaitForCapacity opts back in.
    monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "0")


class TestClassifyYield:
    def test_zero_yield_is_a_block(self):
        assert classify_yield(0, []) == "zero-yield"

    def test_zero_yield_rule_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("BLOCK_DETECT_ZERO_YIELD", "0")
        assert classify_yield(0, []) is None

    def test_thin_yield_without_history_is_not_flagged(self):
        # A brand-new market legitimately has no baseline to compare against.
        assert classify_yield(2, [50, 60]) is None

    def test_low_yield_against_median_is_flagged(self):
        reason = classify_yield(2, [50, 60, 55])
        assert reason is not None and reason.startswith("low-yield")

    def test_normal_yield_is_not_flagged(self):
        assert classify_yield(40, [50, 60, 55]) is None

    def test_ratio_boundary_is_exclusive(self):
        # median 40, ratio 0.25 -> threshold 10; 10 is not "below" 10.
        assert classify_yield(10, [40, 40, 40]) is None
        assert classify_yield(9, [40, 40, 40]) is not None

    def test_all_zero_history_does_not_flag(self):
        # Median 0 would make every yield "low"; guard against that.
        assert classify_yield(3, [0, 0, 0]) is None


class TestProxyIdRedaction:
    def test_password_is_not_part_of_identity(self):
        pid = proxy_id(P1)
        assert pid == "u1@p1.example.com:8080"
        assert "pw1" not in pid

    def test_ledger_file_never_contains_passwords(self, tmp_path):
        record_block([P1, P2], "zero-yield")
        contents = (tmp_path / "proxy_health.json").read_text()
        assert "pw1" not in contents and "pw2" not in contents


class TestCooldownLedger:
    def test_blocked_proxy_is_filtered_out(self):
        record_block([P1], "zero-yield")
        usable, cooling = filter_cooling([P1, P2])
        assert usable == [P2]
        assert list(cooling) == [proxy_id(P1)]

    def test_second_strike_parks_far_longer(self, tmp_path):
        record_block([P1], "zero-yield")
        first = json.loads((tmp_path / "proxy_health.json").read_text())[proxy_id(P1)]
        record_block([P1], "zero-yield")
        second = json.loads((tmp_path / "proxy_health.json").read_text())[proxy_id(P1)]

        assert first["strikes"] == 1
        assert second["strikes"] == 2
        assert second["cooldown_until"] > first["cooldown_until"]

    def test_success_decays_one_strike_and_clears_cooldown(self, tmp_path):
        record_block([P1], "zero-yield")
        record_block([P1], "zero-yield")
        record_success([P1])

        entry = json.loads((tmp_path / "proxy_health.json").read_text())[proxy_id(P1)]
        assert entry["strikes"] == 1
        assert "cooldown_until" not in entry
        assert filter_cooling([P1])[0] == [P1]

    def test_success_on_unknown_proxy_is_a_noop(self, tmp_path):
        record_success([P1])
        assert not (tmp_path / "proxy_health.json").exists()

    def test_corrupt_ledger_is_ignored(self, tmp_path):
        (tmp_path / "proxy_health.json").write_text("{not json")
        assert filter_cooling([P1]) == ([P1], {})


class TestPoolExhaustion:
    def test_raises_rather_than_scraping_unproxied(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2}")
        record_block([P1], "zero-yield")
        record_block([P2], "zero-yield")

        # An empty list would silently send traffic from the host IP.
        with pytest.raises(ProxyPoolExhausted, match="cooling down"):
            _select_scraper_proxies(session_key="HVAC")

    def test_disable_proxy_still_short_circuits(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2}")
        record_block([P1], "zero-yield")
        record_block([P2], "zero-yield")

        assert _select_scraper_proxies(disable_proxy=True, session_key="HVAC") == []


class TestStickyAssignment:
    def test_offset_is_stable_for_a_key(self):
        assert len({_session_offset("HVAC", 3) for _ in range(20)}) == 1

    def test_offset_is_zero_without_a_key(self):
        assert _session_offset(None, 3) == 0
        assert _session_offset("", 3) == 0

    def test_same_variant_gets_same_proxy_every_time(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2},{P3}")
        picks = {
            tuple(_select_scraper_proxies(proxy_limit=1, session_key="HVAC"))
            for _ in range(10)
        }
        assert len(picks) == 1

    def test_different_variants_spread_across_the_pool(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2},{P3}")
        keys = ["HVAC", "Plumbing", "Air conditioning repair", "Water heater repair"]
        picks = {
            _select_scraper_proxies(proxy_limit=1, session_key=k)[0] for k in keys
        }
        assert len(picks) > 1, "sticky assignment should not pin every variant to one proxy"

    def test_cooling_proxy_is_excluded_from_rotation(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2},{P3}")
        record_block([P2], "zero-yield")
        for key in ("HVAC", "Plumbing", "Drain cleaning"):
            assert P2 not in _select_scraper_proxies(session_key=key)

    def test_limit_applies_after_rotation(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2},{P3}")
        assert len(_select_scraper_proxies(proxy_limit=2, session_key="HVAC")) == 2


class TestPacing:
    def test_off_when_unset(self):
        assert pacing.pacing_range() is None
        assert pacing.pace("next variant") == 0.0

    def test_parses_min_max(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PACING_SEC", "10:20")
        assert pacing.pacing_range() == (10.0, 20.0)

    def test_parses_fixed_value(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PACING_SEC", "7")
        assert pacing.pacing_range() == (7.0, 7.0)

    @pytest.mark.parametrize("raw", ["abc", "10:20:30", "-5", "20:10", "0"])
    def test_invalid_values_disable_rather_than_raise(self, monkeypatch, raw):
        monkeypatch.setenv("SCRAPER_PACING_SEC", raw)
        assert pacing.pacing_range() is None

    def test_pace_sleeps_within_the_window(self, monkeypatch):
        slept = []
        monkeypatch.setenv("SCRAPER_PACING_SEC", "10:20")
        monkeypatch.setattr(pacing.time, "sleep", lambda s: slept.append(s))

        delay = pacing.pace("next ZIP")

        assert slept and 10.0 <= slept[0] <= 20.0
        assert delay == slept[0]


class TestWaitForCapacity:
    """A fully-parked pool waits out the shortest cooldown instead of failing.

    Real sleeps are never taken here — `time.sleep` is captured so the chosen
    duration is asserted directly.
    """

    @pytest.fixture(autouse=True)
    def _slept(self, monkeypatch):
        calls = []
        monkeypatch.setattr(proxy_health.time, "sleep", lambda s: calls.append(s))
        return calls

    def test_no_wait_while_any_proxy_is_usable(self, monkeypatch, _slept):
        monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "900")
        record_block([P1], "zero-yield")

        assert proxy_health.earliest_expiry([P1, P2]) is None
        assert proxy_health.wait_for_capacity([P1, P2]) == 0.0
        assert not _slept

    def test_waits_the_shortest_cooldown_when_all_are_parked(self, monkeypatch, _slept):
        monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "900")
        monkeypatch.setenv("PROXY_COOLDOWN_SEC", "600")
        record_block([P1], "zero-yield")
        monkeypatch.setenv("PROXY_COOLDOWN_SEC", "60")
        record_block([P2], "zero-yield")

        slept = proxy_health.wait_for_capacity([P1, P2])

        # P2's 60s cooldown, not P1's 600s: we only need one proxy back.
        assert 55 <= slept <= 65
        assert _slept == [slept]

    def test_refuses_to_wait_longer_than_the_cap(self, monkeypatch, _slept):
        monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "60")
        monkeypatch.setenv("PROXY_COOLDOWN_SEC", "600")
        record_block([P1, P2], "zero-yield")

        assert proxy_health.wait_for_capacity([P1, P2]) == 0.0
        assert not _slept

    def test_zero_cap_disables_waiting(self, monkeypatch, _slept):
        monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "0")
        record_block([P1, P2], "zero-yield")

        assert proxy_health.wait_for_capacity([P1, P2]) == 0.0
        assert not _slept

    def test_selection_retries_after_a_successful_wait(self, monkeypatch):
        """The point of waiting: a parked pool still yields proxies afterward."""
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2}")
        record_block([P1, P2], "zero-yield")

        # Stand in for the cooldown elapsing while we sleep.
        def fake_wait(proxies):
            proxy_health.save_state({})
            return 601.0

        monkeypatch.setattr("app.scraper.run_scraper.wait_for_capacity", fake_wait)

        assert _select_scraper_proxies(
            disable_proxy=False, proxy_limit=3, session_key="Plumbing"
        )

    def test_exhaustion_still_raises_when_waiting_cannot_help(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_PROXIES", f"{P1},{P2}")
        monkeypatch.setenv("PROXY_WAIT_MAX_SEC", "0")
        record_block([P1, P2], "zero-yield")

        with pytest.raises(ProxyPoolExhausted):
            _select_scraper_proxies(
                disable_proxy=False, proxy_limit=3, session_key="Plumbing"
            )


class TestLedgerWriteFailures:
    def test_unwritable_ledger_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("PROXY_HEALTH_FILE", "/nonexistent-root/nope/ph.json")
        record_block([P1], "zero-yield")  # warns, does not raise
        assert proxy_health.load_state() == {}
