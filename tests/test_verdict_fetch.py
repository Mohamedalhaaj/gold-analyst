"""The settle run's fetch of the judge's verdict sends the package's own User-Agent (§16.18 A13).

The judge's site answers Python's default `Python-urllib/x.y` agent with Cloudflare error 1010 (HTTP 403), so with
the default agent no settle run ever fetched a verdict and the canary paused `judge_silent` from P0 on (2026-10-27).
"""
import io
import json

from ga import hours, settle_run

PINS = {"judge_site_url": "https://judge.example/"}
VERDICT = {"schema": "analyst-verdict/1", "analyst_verdict": "SOUND", "asof_utc": "2026-10-27T09:11:19Z"}


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_verdict_request_carries_the_package_user_agent(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _Resp(json.dumps(VERDICT).encode("utf-8"))

    monkeypatch.setattr(settle_run.urllib.request, "urlopen", fake_urlopen)
    assert settle_run.fetch_verdict(PINS) == VERDICT
    [req] = seen
    assert req.full_url == "https://judge.example/judge_analyst/analyst_verdict.json"
    assert req.get_header("User-agent") == hours.UA


def test_a_refused_fetch_is_still_none(monkeypatch):
    def refuse(req, timeout=None):
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(settle_run.urllib.request, "urlopen", refuse)
    assert settle_run.fetch_verdict(PINS) is None
