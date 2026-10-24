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
    rec = json.loads((ROOT / "analyst/canary/checks/PROBE_p20261024T024852Z.json").read_text("utf-8"))
    out = dict(rec["outcomes"])
    pm = probes.passes(out, rec["kind"])
    assert pm["git.ls_remote_public"] is None and pm["git.ls_remote_public_proxy"] is None
    assert [p for p, v in pm.items() if v is False] == ["net.dukascopy"]       # the 503 of Saturday's throttling


def test_refused_public_reads_still_pass():
    out = {p: "refused" for p in PUBLIC}
    assert all(probes.passes(out, "probe")[p] is None for p in PUBLIC)
