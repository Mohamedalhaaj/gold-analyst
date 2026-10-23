"""Dukascopy hour files: parsing, status, transport, budget (prereg R2; register R-AN-16)."""
import datetime as dt
import lzma
import struct

from ga import hours, rec
from synth import FakeClock, tick_bi5

H = dt.datetime(2026, 11, 10, 5, tzinfo=rec.UTC)


def good():
    return tick_bi5([(0, 4000100, 3999900, 1.0, 1.0), (1500, 4000300, 4000100, 1.0, 1.0),
                     (3_599_999, 4000200, 4000000, 0.5, 0.5)])


def test_url():
    assert hours.url("XAUUSD", H) == "https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/10/10/05h_ticks.bi5"
    assert hours.url("EURUSD", dt.datetime(2027, 1, 4, 23, tzinfo=rec.UTC)).endswith("/EURUSD/2027/00/04/23h_ticks.bi5")


def test_present_and_tick_mid():
    st, ticks = hours.classify(200, good())
    assert st == "present" and len(ticks) == 3
    assert hours.tick_mid("XAUUSD", ticks[0][1], ticks[0][2]) == 4000.0
    assert hours.tick_mid("EURUSD", 110002, 109998) == 1.1


def test_empty_invalid_error():
    assert hours.classify(404, b"")[0] == "empty"
    assert hours.classify(200, b"")[0] == "empty"
    raw = lzma.decompress(good(), format=lzma.FORMAT_ALONE)
    cases = {
        "not_lzma": b"not lzma",
        "len": lzma.compress(raw + b"\x00", format=lzma.FORMAT_ALONE),
        "no_record": lzma.compress(b"", format=lzma.FORMAT_ALONE),
        "ms_range": tick_bi5([(3_600_000, 4000100, 3999900, 1.0, 1.0)]),
        "decreasing": tick_bi5([(10, 4000100, 3999900, 1.0, 1.0), (9, 4000100, 3999900, 1.0, 1.0)]),
        "zero_bid": tick_bi5([(10, 4000100, 0, 1.0, 1.0)]),
    }
    for name, body in cases.items():
        assert hours.classify(200, body)[0] == "invalid", name
    for code in (0, 429, 500, 503, 403, 410):
        assert hours.classify(code, b"")[0] == "error"
    # equal ms are allowed (non-decreasing)
    assert hours.classify(200, tick_bi5([(5, 1, 1, 0.0, 0.0), (5, 2, 2, 0.0, 0.0)]))[0] == "present"


def make(responses, clk=None, budget=120.0):
    calls = []
    clk = clk or FakeClock()
    seq = list(responses)

    def get(url, timeout):
        calls.append((clk.mono(), url, timeout))
        clk.t += 0.4
        return seq.pop(0) if seq else (200, good())

    f = hours.Fetcher(get=get, clock=clk.mono, sleep=clk.sleep, now=lambda: H + dt.timedelta(hours=2),
                      budget_s=budget)
    return f, calls, clk


def test_retries_only_transient_and_at_most_three_tries():
    f, calls, _ = make([(0, None), (503, b""), (200, good())])
    prov, ticks = f.fetch("XAUUSD", H)
    assert prov["tries"] == 3 and prov["status"] == "present" and prov["http"] == 200 and len(ticks) == 3
    f, calls, _ = make([(429, b""), (429, b""), (429, b""), (200, good())])
    prov, _ = f.fetch("XAUUSD", H)
    assert prov["tries"] == 3 and prov["status"] == "error" and prov["http"] == 429
    f, calls, _ = make([(404, b"")])
    prov, _ = f.fetch("XAUUSD", H)
    assert prov["tries"] == 1 and prov["status"] == "empty"
    f, calls, _ = make([(403, b"")])
    prov, _ = f.fetch("XAUUSD", H)
    assert prov["tries"] == 1 and prov["status"] == "error"
    assert all(c[2] == 20 for c in calls)


def test_pacing_and_provenance():
    f, calls, clk = make([])
    for k in range(4):
        prov, _ = f.fetch("XAUUSD", H + dt.timedelta(hours=k))
    gaps = [calls[i + 1][0] - (calls[i][0] + 0.4) for i in range(3)]
    assert all(g >= 1.5 - 1e-9 for g in gaps)
    assert set(prov) == {"sym", "hour_utc", "url", "http", "bytes", "sha256", "ticks", "status", "fetched_utc",
                         "tries"}
    assert prov["sha256"] == rec.sha256_bytes(good()) and prov["bytes"] == len(good()) and prov["ticks"] == 3
    assert prov["fetched_utc"] == "2026-11-10T07:00:00Z"


def test_budget_120s_from_first_request():
    clk = FakeClock()
    f, calls, clk = make([], clk=clk)
    out = []
    for k in range(200):
        out.append(f.fetch("XAUUSD", H + dt.timedelta(hours=k))[0])
    started = [c[0] for c in calls]
    assert started and max(started) - started[0] < 120.0
    assert out[-1]["status"] == "error" and out[-1]["tries"] == 0 and out[-1]["http"] == 0
    assert sum(1 for p in out if p["status"] == "present") == len(calls)
