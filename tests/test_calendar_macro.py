"""The live macro calendar and the twelve-kind events block (register R-AN-10, R-AN-20, §16.18 A12-D17..D19)."""
import csv
import datetime as dt
import hashlib
import io
from pathlib import Path
from zoneinfo import ZoneInfo

from ga import frozen, tables
from ga.repo import DictView

ROOT = Path(__file__).resolve().parents[1]
MACRO = "analyst/calendars/macro_2026_2027.csv"
BUILD = ROOT / "tests" / "fixtures" / "macro_2026_2027_build.csv"
BUILD_SHA = "68a4c46c76087870b4cd7d6560418457d39daf67994188b6c600740e6c44fbb0"
HEADER = "date,event,time_local,tz,ts_utc,source"
EVENTS = {"PPI", "JOLTS", "GDP", "PCE", "RETAIL", "ISM_MFG", "ISM_SVC", "CLAIMS", "ECB"}
KINDS = ("nfp", "cpi", "fomc", "ppi", "jolts", "gdp", "pce", "retail", "ism_mfg", "ism_svc", "claims", "ecb")
UTC = dt.timezone.utc


def _text(p):
    return (ROOT / p).read_text("utf-8")


def _events(macro_text=None):
    return tables.Events(_text(tables.BLS_PATH), _text(tables.FOMC_PATH),
                         _text(MACRO) if macro_text is None else macro_text)


# ---------------------------------------------------------------- the file (A12-D17, D19)
def test_build_fixture_is_the_verified_scratch_copy():
    assert hashlib.sha256(BUILD.read_bytes()).hexdigest() == BUILD_SHA


def test_file_keeps_every_build_row_and_the_header():
    """Append-only (A12-D19): every line at the build stays, byte for byte."""
    have = _text(MACRO).split("\n")
    build = BUILD.read_text("utf-8").split("\n")
    assert have[0] == build[0] == HEADER
    assert set(l for l in build if l) <= set(l for l in have if l)


def test_file_rules():
    raw = (ROOT / MACRO).read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    assert len(rows) >= 136
    keys = []
    for r in rows:
        assert r["event"] in EVENTS
        assert r["tz"] in ("America/New_York", "Europe/Berlin")
        assert r["source"].startswith("https://") and "," not in r["source"]
        day = dt.date.fromisoformat(r["date"])
        assert day.weekday() < 5
        hh, mm = r["time_local"].split(":")
        t = dt.datetime.combine(day, dt.time(int(hh), int(mm)), tzinfo=ZoneInfo(r["tz"])).astimezone(UTC)
        assert r["ts_utc"] == t.strftime("%Y-%m-%d %H:%M:%S+00:00")
        keys.append((r["ts_utc"], r["event"]))
    assert keys == sorted(keys)
    assert len({(r["date"], r["event"]) for r in rows}) == len(rows)


def test_macro_calendar_is_outside_the_frozen_manifest():
    paths = frozen.frozen_paths(ROOT)
    assert MACRO not in paths
    assert "analyst/calendars/bls_2026_2027.csv" in paths and "analyst/calendars/fomc_2026_2027.csv" in paths
    assert MACRO in frozen.DATA_FILES


# ---------------------------------------------------------------- the events block (A12-D18)
def test_twelve_kinds_in_order():
    assert tables.KINDS == KINDS


def test_block_on_a_day_with_four_releases():
    b = _events().context_block(dt.date(2026, 10, 29))
    assert set(b) == {"today", "next", "today_times_utc", "next_times_utc", "next_day"}
    assert list(b["today"]) == list(KINDS) and list(b["next"]) == list(KINDS)
    assert b["today"] == {"nfp": 0, "cpi": 0, "fomc": 0, "ppi": 0, "jolts": 0, "gdp": 1, "pce": 1, "retail": 0,
                          "ism_mfg": 0, "ism_svc": 0, "claims": 1, "ecb": 1}
    assert b["today_times_utc"] == [{"kind": "claims", "utc": "2026-10-29T12:30:00Z"},
                                    {"kind": "gdp", "utc": "2026-10-29T12:30:00Z"},
                                    {"kind": "pce", "utc": "2026-10-29T12:30:00Z"},
                                    {"kind": "ecb", "utc": "2026-10-29T13:15:00Z"}]
    assert b["next_day"] == "2026-10-30"


def test_next_lists_the_next_weekday_with_labels():
    b = _events().context_block(dt.date(2026, 10, 30))              # Friday -> Monday 2026-11-02 (ISM manufacturing)
    assert b["next_day"] == "2026-11-02"
    assert b["next"]["ism_mfg"] == 1 and b["today"]["ism_mfg"] == 0
    assert b["next_times_utc"] == [{"kind": "ism_mfg", "utc": "2026-11-02T15:00:00Z"}]


def test_old_kinds_are_labelled_too():
    b = _events().context_block(dt.date(2026, 10, 28))              # FOMC 18:00Z
    assert b["today"]["fomc"] == 1
    assert {"kind": "fomc", "utc": "2026-10-28T18:00:00Z"} in b["today_times_utc"]


def test_null_after_a_kinds_last_event():
    e = _events()
    assert e.context_block(dt.date(2026, 12, 1))["today"]["jolts"] == 1
    assert e.context_block(dt.date(2026, 12, 1))["today"]["ism_mfg"] == 1
    later = e.context_block(dt.date(2026, 12, 2))["today"]
    assert later["jolts"] is None and later["ism_mfg"] is None and later["ism_svc"] == 0
    assert e.context_block(dt.date(2026, 12, 4))["today"]["ism_svc"] is None
    assert e.context_block(dt.date(2026, 12, 4))["today"]["claims"] == 0
    assert e.context_block(dt.date(2028, 1, 4))["today"] == {k: None for k in KINDS}


def test_a_refresh_turns_null_into_flags():
    """A12-D19: rows appended later (e.g. JOLTS 2027) give flags from that version of the file on."""
    base = _text(MACRO)
    assert _events(base).context_block(dt.date(2027, 1, 5))["today"]["jolts"] is None
    row = ("2027-01-05,JOLTS,10:00,America/New_York,2027-01-05 15:00:00+00:00,"
           "https://www.bls.gov/schedule/2027/01_sched.htm\n")
    b = _events(base + row).context_block(dt.date(2027, 1, 5))
    assert b["today"]["jolts"] == 1
    assert {"kind": "jolts", "utc": "2027-01-05T15:00:00Z"} in b["today_times_utc"]
    assert _events(base + row).context_block(dt.date(2027, 1, 4))["today"]["jolts"] == 0


def test_a_repeated_row_is_listed_once():
    base = _text(MACRO)
    dup = base + base.split("\n")[1] + "\n"
    assert _events(dup).context_block(dt.date(2026, 10, 29)) == _events(base).context_block(dt.date(2026, 10, 29))


def test_times_sort_by_utc_then_kind():
    for day in (dt.date(2026, 10, 29), dt.date(2026, 11, 6), dt.date(2026, 12, 10), dt.date(2027, 4, 29)):
        b = _events().context_block(day)
        for key in ("today_times_utc", "next_times_utc"):
            got = [(x["utc"], x["kind"]) for x in b[key]]
            assert got == sorted(got) and len(got) == len(set(got))
            assert all(set(x) == {"kind", "utc"} and x["kind"] in KINDS for x in b[key])


def test_load_events_reads_the_three_files():
    files = {p: (ROOT / p).read_bytes() for p in (tables.BLS_PATH, tables.FOMC_PATH, tables.MACRO_PATH)}
    e = tables.load_events(DictView(files))
    assert e.context_block(dt.date(2026, 10, 29))["today"]["ecb"] == 1
