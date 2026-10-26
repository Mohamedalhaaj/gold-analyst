"""Public-repository git reads are measured, not a probe failure (register §16.18 A8, client approval 2026-10-24).

The first C20 probe session (p20261024T024852Z) found that the claude.ai GitHub proxy serves git reads of public
repositories that are not attached. The session guard (R-AN-85) lets a session run no git command of its own, so the
two probes are recorded and never fail the probe."""
import json
from pathlib import Path

from ga import probes

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ("git.ls_remote_public", "git.ls_remote_public_proxy")


def test_public_reads_are_measured():
    assert all(p in probes.MEASURED for p in PUBLIC)


def test_the_first_probe_session_fails_only_on_the_dukascopy_outage():
    """Under the rules it ran with (A8), the 503 of Saturday's throttling was its only failure; under §16.18 A9 the
    same record fails nothing (Dukascopy is expected refused) and is incomplete: it holds no `net.twelvedata`."""
    rec = json.loads((ROOT / "analyst/canary/checks/PROBE_p20261024T024852Z.json").read_text("utf-8"))
    out = dict(rec["outcomes"])
    assert out["net.dukascopy"] == 503 and rec["pass"]["net.dukascopy"] is False and rec["verdict"] == "fail"
    pm = probes.passes(out, rec["kind"])
    assert pm["git.ls_remote_public"] is None and pm["git.ls_remote_public_proxy"] is None
    assert [p for p, v in pm.items() if v is False] == []
    assert pm["net.dukascopy"] is True and pm["net.twelvedata"] is None and probes.verdict(pm) == "incomplete"


def test_refused_public_reads_still_pass():
    out = {p: "refused" for p in PUBLIC}
    assert all(probes.passes(out, "probe")[p] is None for p in PUBLIC)


def test_a_denied_webhook_list_passes():
    """§16.18 A8, added with the client's approval of 2026-10-24: the session cannot reach the hooks API."""
    for v in ("denied", "scope", 0):
        assert probes.passes({"rest.inbox": "ok", "rest.hooks": v}, "probe")["rest.hooks"] is True
    assert probes.passes({"rest.inbox": "ok", "rest.hooks": 1}, "probe")["rest.hooks"] is False
    assert probes.passes({"rest.inbox": "ok", "rest.hooks": "ok"}, "probe")["rest.hooks"] is None


def test_the_probe_sessions_pass_once_twelvedata_answers():
    """§16.18 A9: with `net.twelvedata` 200 and Dukascopy refused, the three C20 probe records would pass."""
    for tag in ("p20261024T024852Z", "p20261024T110641Z", "p20261026T000513Z"):
        rec = json.loads((ROOT / f"analyst/canary/checks/PROBE_{tag}.json").read_text("utf-8"))
        out = dict(rec["outcomes"], **{"net.twelvedata": 200})
        assert out["net.dukascopy"] != 200, tag
        assert probes.verdict(probes.passes(out, rec["kind"])) == "pass", tag
        out["net.dukascopy"] = 200                             # a Dukascopy answer now fails the probe
        assert probes.verdict(probes.passes(out, rec["kind"])) == "fail", tag
