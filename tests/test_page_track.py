"""The page (prereg §7, R8; register R-AN-63..R-AN-65): three clocks across the DST changes, banner, text safety,
canary hiding, the tracker's provisional marks."""
import datetime as dt
import html.parser
import json

import pytest

from ga import clock, rec
from ga.repo import DictView
import page
import track

UTC = rec.UTC
EVIL = 'سبب <script>alert(1)</script>‮<img src=x onerror=alert(1)> "q" & [link](http://x)'


def live_view(cid="2026-11-10_W1", status="called", action="buy", mode="live", extra=None):
    w = clock.window_of(cid)
    call = {"plan": {"action": action, "stop_mult": 1.5, "target_mult": 2.0, "confidence": "low", "reason_ar": EVIL,
                     "key_risks_ar": "مخاطر"}, "side": {"buy": 1, "sell": -1, "stay_out": 0}[action], "S": 35.2,
            "T": 46.9, "d_ref": {"D_ref": 23.45}}
    term = {"call_id": cid, "status": status, "entry_utc": rec.iso(w.entry_utc),
            "call": {"sha256": "ab" * 32, "lock_utc": rec.iso(w.deadline_utc - dt.timedelta(minutes=20)),
                     "witness_seq": 2}}
    files = {"analyst/MODE.json": json.dumps({"mode": mode, "live_from": "2026-11-09", "canary_from": "2026-11-01"}),
             "analyst/STATE.json": json.dumps({"state": "TESTING", "n": 3, "E": 1.234, "calls_enabled": True}),
             "analyst/PINS.json": json.dumps({"repo": "Mohamedalhaaj/gold-analyst"}),
             "analyst/PARAMS.json": json.dumps({"L_min": 50}),
             f"analyst/terminal/{cid}.json": json.dumps(term), f"analyst/calls/{cid}.json": json.dumps(call),
             "analyst/witness/000002.json": json.dumps({"comment": {"issue": 1, "id": 77}})}
    files.update(extra or {})
    return DictView(files)


class Texts(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.text, self.scripts = [], [], 0

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.scripts += tag == "script"

    def handle_data(self, data):
        self.text.append(data)


def parse(doc):
    p = Texts()
    p.feed(doc)
    return p


def test_banner_rtl_and_card():
    doc = page.build(live_view(), dt.datetime(2026, 11, 10, 7, 30, tzinfo=UTC))
    assert '<html lang="ar" dir="rtl">' in doc
    assert page.BANNER in doc
    assert "خطة تجريبية غير مثبتة: شراء" in doc
    assert "خسارة الوقف: 35.20 دولارًا لكل أونصة" in doc
    assert "إن لم تستطع الإغلاق عند وقت الخروج فلا تفتح الصفقة" in doc
    assert "انتهت مهلة الدخول، لا تفتح هذه الصفقة" in doc
    assert "احتمال الصدفة لم يُرفض" in doc and "تعليل النموذج، غير مثبت" in doc
    assert "issuecomment-77" in doc


def test_model_text_is_sanitized_text_only():
    doc = page.build(live_view(), dt.datetime(2026, 11, 10, 7, 30, tzinfo=UTC))
    p = parse(doc)
    assert p.scripts == 1                                       # only the page's own script
    assert "img" not in p.tags and "onerror" not in doc and "‮" not in doc and "http://x" not in doc
    assert "alert" not in doc.split("<script>")[0]
    js = doc.split("<script>")[1]
    assert "innerHTML" not in js and "textContent" in js and "eval" not in js


@pytest.mark.parametrize("cid,clocks", [
    ("2026-10-23_W1", ("2026-10-23 10:00", "2026-10-23 09:00", "2026-10-23 08:00")),   # BST / EDT
    ("2026-10-26_W1", ("2026-10-26 11:00", "2026-10-26 10:00", "2026-10-26 08:00")),   # GMT / EDT (mismatch)
    ("2026-11-02_W1", ("2026-11-02 10:00", "2026-11-02 10:00", "2026-11-02 08:00")),   # GMT / EST
    ("2026-10-30_W2", ("2026-10-30 16:30", "2026-10-30 15:30", "2026-10-30 09:30")),   # EDT
    ("2026-11-02_W2", ("2026-11-02 16:30", "2026-11-02 16:30", "2026-11-02 09:30")),   # EST
])
def test_three_clocks_across_dst(cid, clocks):
    w = clock.window_of(cid)
    got = tuple(v for _l, v in page.clocks(w.entry_utc, w.name))
    assert got == clocks
    labels = [l for l, _v in page.clocks(w.entry_utc, w.name)]
    assert labels == ["توقيت المنصة", "طرابلس", "لندن" if w.name == "W1" else "نيويورك"]
    doc = page.build(live_view(cid), w.entry_utc)
    for v in clocks:
        assert v in doc


def test_canary_calls_are_never_shown():
    doc = page.build(live_view(mode="canary"), dt.datetime(2026, 11, 10, 7, 30, tzinfo=UTC))
    assert "لا خطة الآن" in doc and "خطة تجريبية غير مثبتة" not in doc and "تجربة تمهيدية" in doc


def test_no_valid_call_and_paused():
    v = live_view(status="missed:trigger")
    v.files["analyst/STATE.json"] = json.dumps({"paused_display": True}).encode()
    doc = page.build(v, dt.datetime(2026, 11, 10, 9, 0, tzinfo=UTC))
    assert "لا خطة الآن" in doc and "فاتت: لم يبدأ التشغيل" in doc and "PAUSED" in doc


def test_tracker_marks_and_stale_notice():
    tr = {"updated_utc": "2026-11-10T09:55:00Z", "status": "open", "provisional_R": 0.25}
    v = live_view(extra={"site/track/2026-11-10_W1.json": json.dumps(tr)})
    doc = page.build(v, dt.datetime(2026, 11, 10, 10, 0, tzinfo=UTC))
    assert "متابعة أولية بالساعة تتأخر نحو ساعة عن السوق، وليست سعرًا حيًا" in doc
    assert "نتيجة أولية" in doc and "مفتوحة" in doc and "المتابعة الأولية متوقفة" not in doc
    doc = page.build(v, dt.datetime(2026, 11, 10, 12, 50, tzinfo=UTC))          # > 2 h + L since the update
    assert "المتابعة الأولية متوقفة" in doc


def test_track_update_rules():
    call = {"call_id": "2026-11-10_W1", "side": 1, "S": 20.0, "T": 40.0, "entry_utc": "2026-11-10T08:00:00Z",
            "exit_utc": "2026-11-10T12:00:00Z"}
    st = track.new_state(call)
    e_ms = rec.epoch_ms(dt.datetime(2026, 11, 10, 8, tzinfo=UTC))
    x_ms = rec.epoch_ms(dt.datetime(2026, 11, 10, 12, tzinfo=UTC))
    h8 = dt.datetime(2026, 11, 10, 8, tzinfo=UTC)
    track.update(st, h8, [(0, 4000100, 3999900, 1, 1), (60000, 4010100, 4009900, 1, 1)], e_ms, x_ms)
    assert st["P_e"] == 4000.1 and st["status"] == "open"
    assert st["provisional_R"] == pytest.approx((4009.9 - 4000.1) / 20.0)
    h9 = dt.datetime(2026, 11, 10, 9, tzinfo=UTC)
    track.update(st, h9, [(0, 3980000, 3979000, 1, 1)], e_ms, x_ms)
    assert st["status"] == "stop" and st["provisional_R"] == pytest.approx(-1.0)
    assert [h.hour for h in track.hours_to_track(rec.parse_iso(call["entry_utc"]),
                                                  rec.parse_iso(call["exit_utc"]))] == [8, 9, 10, 11]
    # ticks before entry are ignored; a short tracks the ask
    st = track.new_state(dict(call, side=-1))
    track.update(st, dt.datetime(2026, 11, 10, 7, tzinfo=UTC), [(0, 1, 1, 1, 1)], e_ms, x_ms)
    assert st["P_e"] is None
    track.update(st, h8, [(5, 4000100, 3999900, 1, 1), (9, 3959800, 3959600, 1, 1)], e_ms, x_ms)
    assert st["P_e"] == 3999.9 and st["status"] == "target" and st["provisional_R"] == pytest.approx(2.0)


def test_status_csv_only_after_promotion():
    v = live_view()
    assert page.status_csv(v, dt.datetime(2026, 11, 10, 7, 30, tzinfo=UTC)) is None
    v.files["analyst/STATE.json"] = json.dumps({"state": "PROMOTED", "n": 61, "E": 412.3456, "head_sha": "abc"}).encode()
    text = page.status_csv(v, dt.datetime(2026, 11, 10, 7, 30, tzinfo=UTC))
    lines = text.splitlines()
    assert lines[0] == "key,value" and "e_n,412.3456" in lines and "today_action,buy" in lines
    assert "today_stop_usd,35.20" in lines and lines[-1] == f"end,{len(lines) - 1}" and text.endswith("\n")
    text.encode("ascii")
