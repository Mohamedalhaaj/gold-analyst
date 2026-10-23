"""The client page /analyst/ (prereg §7, R8; register R-AN-63..R-AN-65). Display only (OPS), not in the frozen set.

- Arabic, right to left; a fixed banner; one card per recent live window, newest first; «لا خطة الآن» when the
  current window has no valid call. Canary calls are never shown.
- Entry and forced exit are shown in three clocks (the platform server clock = New York + 7 h, Tripoli, and London for
  W1 / New York for W2), computed with zoneinfo, never typed.
- Model text (reason_ar, key_risks_ar) passes ga.plan.sanitize and is written as escaped text; the page script only
  sets textContent from numbers and ISO times held in data attributes. No model text reaches the script.
- The lock time shown is the server time (activity / witness run), never an agent-written time.
"""
import datetime as dt
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

import ga  # noqa: E402,F401
from ga import clock, paths, plan, rec  # noqa: E402

BANNER = ("تجربة على الورق. لم يثبت أن لهذا المحلل ميزة، والأرجح أن نتائجه صدفة. من ينفّذ خططه بمال حقيقي يتحمل "
          "الخسارة كاملة.")
ACTION_AR = {"buy": "شراء", "sell": "بيع", "stay_out": "خارج السوق"}
TRACK_AR = {"open": "مفتوحة", "stop": "ضُرب الوقف", "target": "تحقق الهدف", "time": "خروج بالوقت"}
STATUS_AR = {"no_call:data": "لا خطة: بيانات ناقصة", "no_call:d_ref": "لا خطة: وحدة التذبذب غير متاحة",
             "no_call:invalid": "لا خطة: خطة غير صالحة", "no_call:refusal": "لا خطة: امتنع النموذج",
             "no_call:late": "لا خطة: قُفلت متأخرة", "no_call:run": "لا خطة: لم يكتمل التشغيل",
             "no_call:model": "لا خطة: نموذج آخر", "missed:trigger": "فاتت: لم يبدأ التشغيل",
             "missed:push": "فاتت: تعذّر الإرسال", "skipped:holiday": "عطلة"}
CLOCK_LABEL = {"server": "توقيت المنصة", "tripoli": "طرابلس", "W1": "لندن", "W2": "نيويورك"}
CARDS = 8


def esc(s):
    return html.escape(str(s), quote=True)


def clocks(t, name):
    """[(label, 'YYYY-MM-DD HH:MM')] in the three clocks."""
    return [(CLOCK_LABEL["server"], clock.to_server(t).strftime("%Y-%m-%d %H:%M")),
            (CLOCK_LABEL["tripoli"], t.astimezone(clock.TRIPOLI).strftime("%Y-%m-%d %H:%M")),
            (CLOCK_LABEL[name], t.astimezone(clock.LOCAL_TZ[name]).strftime("%Y-%m-%d %H:%M"))]


def _clock_rows(label, t, name):
    cells = "".join(f'<div class="clk"><span class="clk-l">{esc(lab)}</span><span class="num">{esc(v)}</span></div>'
                    for lab, v in clocks(t, name))
    return f'<div class="row"><span class="k">{esc(label)}</span><div class="clks">{cells}</div></div>'


def _fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def record_block(st, ledger_rows):
    n = st.get("n") or 0
    ct = [r for r in ledger_rows if r.get("trade_no")]
    net_R = sum(float(r["R"]) for r in ct) if ct else 0.0
    st_R = sum(float(r["R_stress"]) for r in ct) if ct else 0.0
    E = st.get("E")
    promoted = st.get("state") == "PROMOTED"
    mu = {}
    for W in ("W1", "W2"):
        d = [float(r["d_W"]) for r in ledger_rows if r["window"] == W and r["class"] == "counted"
             and r.get("twin_status") == "final" and r.get("d_W")]
        mu[W] = sum(d) / len(d) if d else None
    dn = None
    if ct and all(mu[r["window"]] is not None for r in ct):
        dn = sum(float(r["net_usd"]) - int(r["side"]) * mu[r["window"]] for r in ct)
    lines = [("صفقات محسوبة", str(n)), ("صافي R", _fmt(net_R)), ("صافي R بعد كلفة التحفظ", _fmt(st_R)),
             ("مؤشر الدليل E مقابل 400", f"{_fmt(E)} / 400"),
             ("المجموع بعد طرح انحراف السوق (دولار للأونصة)", _fmt(dn))]
    items = "".join(f'<div class="row"><span class="k">{esc(a)}</span><span class="num">{esc(b)}</span></div>'
                    for a, b in lines)
    note = (f"اجتاز اختبار الصدفة على الورق في {n} صفقة. هذا لا يضمن ربحًا قادمًا." if promoted
            else "احتمال الصدفة لم يُرفض")
    side = '<p class="warn">ربح من انحياز اتجاهي، لا قراءة مثبتة</p>' if st.get("side_label") else ""
    return f'<div class="record">{items}<p class="note">{esc(note)}</p>{side}</div>'


def track_block(tr, now, L_min, exit_utc):
    if not tr:
        return ""
    upd = rec.parse_iso(tr["updated_utc"])
    stale = now - upd > dt.timedelta(hours=2, minutes=L_min) and now < exit_utc + dt.timedelta(hours=2)
    label = TRACK_AR.get(tr.get("status"), "")
    r = tr.get("provisional_R")
    out = [f'<div class="track"><p class="note">متابعة أولية بالساعة تتأخر نحو ساعة عن السوق، وليست سعرًا حيًا</p>',
           f'<div class="row"><span class="k">الحالة الأولية</span><span>{esc(label)}</span></div>',
           f'<div class="row"><span class="k">نتيجة أولية</span><span class="num">{esc(_fmt(r))} R</span></div>',
           f'<div class="row"><span class="k">آخر تحديث (توقيت المنصة)</span><span class="num">'
           f'{esc(clock.to_server(upd).strftime("%Y-%m-%d %H:%M"))}</span></div>']
    if stale:
        out.append('<p class="warn">المتابعة الأولية متوقفة</p>')
    out.append("</div>")
    return "".join(out)


def card(view, t, call, st, ledger_rows, track, pins, now, L_min):
    w = clock.window_of(t["call_id"])
    status = t["status"]
    head = f'<h2>{esc(w.day.isoformat())} · {"لندن" if w.name == "W1" else "نيويورك"}</h2>'
    parts = [f'<article class="card" data-entry="{rec.epoch_ms(w.entry_utc)}" data-exit="{rec.epoch_ms(w.exit_utc)}">',
             head]
    if status in ("called", "stay_out"):
        p = call["plan"]
        parts.append(f'<p class="plan">خطة تجريبية غير مثبتة: {esc(ACTION_AR[p["action"]])}</p>')
    else:
        parts.append(f'<p class="plan">لا خطة الآن</p><p class="note">{esc(STATUS_AR.get(status, status))}</p>')
    parts.append(_clock_rows("الدخول", w.entry_utc, w.name))
    parts.append(_clock_rows("الخروج الإجباري", w.exit_utc, w.name))
    if status == "called":
        p = call["plan"]
        s = call["side"]
        S, T = call["S"], call["T"]
        parts.append('<div class="row"><span class="k">الحالة</span><span class="state" aria-live="polite">'
                     '</span><span class="num countdown"></span></div>')
        parts.append('<p class="warn late" hidden>انتهت مهلة الدخول، لا تفتح هذه الصفقة</p>')
        parts.append(f'<div class="row"><span class="k">الوقف</span><span class="num">{_fmt(S)} دولار من سعر '
                     f'التنفيذ</span></div><div class="row"><span class="k">الهدف</span><span class="num">{_fmt(T)}'
                     f' دولار من سعر التنفيذ</span></div>')
        parts.append(f'<p>خسارة الوقف: {esc(_fmt(S))} دولارًا لكل أونصة</p>')
        parts.append(f'<div class="calc" data-side="{s}" data-s="{S}" data-t="{T}"><label>سعر تنفيذك'
                     f'<input type="number" inputmode="decimal" step="0.01" class="fill"></label>'
                     f'<div class="row"><span class="k">الوقف SL</span><span class="num sl">—</span></div>'
                     f'<div class="row"><span class="k">الهدف TP</span><span class="num tp">—</span></div></div>')
        parts.append('<p class="warn">إن لم تستطع الإغلاق عند وقت الخروج فلا تفتح الصفقة</p>')
        parts.append(track_block(track, now, L_min, w.exit_utc))
    if status in ("called", "stay_out"):
        p = call["plan"]
        parts.append(f'<div class="reason"><span class="k">تعليل النموذج، غير مثبت</span>'
                     f'<p>{esc(plan.sanitize(p.get("reason_ar")))}</p>'
                     f'<span class="k">مخاطر</span><p>{esc(plan.sanitize(p.get("key_risks_ar")))}</p></div>')
        parts.append(record_block(st, ledger_rows))
        info = t.get("call") or {}
        lock_s = rec.parse_iso(info["lock_utc"]) if info.get("lock_utc") else None
        link = ""
        wseq = info.get("witness_seq")
        wit = view.read_json(paths.witness_path("live", wseq)) if wseq else None
        if wit and wit.get("comment") and pins.get("repo"):
            link = (f'<a href="https://github.com/{esc(pins["repo"])}/issues/{esc(wit["comment"]["issue"])}'
                    f'#issuecomment-{esc(wit["comment"]["id"])}" rel="noopener">شاهد القفل</a>')
        lock_txt = clock.to_server(lock_s).strftime("%Y-%m-%d %H:%M") if lock_s else "—"
        parts.append(f'<div class="row small"><span class="k">وقت القفل لدى GitHub (توقيت المنصة)</span>'
                     f'<span class="num">{esc(lock_txt)}</span></div><div class="row small"><span class="k">بصمة '
                     f'الخطة</span><span class="num hash">{esc((info.get("sha256") or "")[:16])}</span></div>{link}')
    parts.append("</article>")
    return "".join(parts)


def load_ledger(view):
    import csv
    import io
    b = view.read(paths.LEDGER_PATH)
    return list(csv.DictReader(io.StringIO(b.decode("utf-8")))) if b else []


def build(view, now, track_dir=None):
    mode_doc = paths.read_mode(view)
    st = view.read_json(paths.STATE_PATH) or {}
    pins = paths.read_pins(view) or {}
    params = paths.read_params(view) or {"L_min": 50}
    ledger_rows = load_ledger(view)
    cards = []
    if mode_doc["mode"] == "live" or (mode_doc["mode"] == "off" and mode_doc.get("live_from")):
        terms = []
        for p in view.list("analyst/terminal/"):
            t = view.read_json(p)
            if t:
                terms.append(t)
        terms.sort(key=lambda t: clock.window_key(t["call_id"]), reverse=True)
        for t in terms[:CARDS]:
            call = view.read_json(paths.call_path("live", t["call_id"])) if t.get("call") else None
            tr = view.read_json(f"site/track/{t['call_id']}.json")
            cards.append(card(view, t, call, st, ledger_rows, tr, pins, now, int(params["L_min"])))
    body = "".join(cards) if cards else '<article class="card"><p class="plan">لا خطة الآن</p></article>'
    canary = ('<p class="note">المحلل في مرحلة تجربة تمهيدية؛ خطط هذه المرحلة لا تُعرض ولا تُحسب.</p>'
              if mode_doc["mode"] == "canary" else "")
    paused = '<p class="warn">متوقف مؤقتًا (PAUSED)</p>' if st.get("paused_display") or (
        st.get("calls_enabled") is False) else ""
    asof = clock.to_server(now).strftime("%Y-%m-%d %H:%M") + " (توقيت المنصة)"
    return PAGE.format(banner=esc(BANNER), canary=canary, paused=paused, body=body, asof=esc(asof),
                       power=esc("بلوغ عتبة الدليل 400 يحتاج ميزة كبيرة جدًا؛ الاعتماد خلال سنة غير مرجّح."))


PAGE = """<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>المحلل التجريبي</title>
<style>
:root {{ --bg:#f7f6f2; --fg:#1d1d1b; --muted:#6b6a64; --card:#ffffff; --line:#e3e1da; --warn:#9a3412;
  --accent:#8a6d1f; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#141412; --fg:#ecebe6; --muted:#a3a29b; --card:#1e1e1b;
  --line:#33322d; --warn:#f0a070; --accent:#d9b65a; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 "Noto Naskh Arabic","Geeza Pro","Segoe UI",
  Tahoma,sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:16px; }}
.banner {{ border:1px solid var(--warn); color:var(--warn); padding:12px 14px; border-radius:8px; margin:0 0 16px;
  font-weight:600; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:0 0 14px; }}
h1 {{ font-size:1.3rem; margin:8px 0 12px; }}
h2 {{ font-size:1.05rem; margin:0 0 8px; color:var(--accent); }}
.plan {{ font-size:1.15rem; font-weight:700; margin:4px 0 10px; }}
.row {{ display:flex; flex-wrap:wrap; gap:6px 12px; align-items:baseline; padding:4px 0; border-top:1px dashed var(--line); }}
.k {{ color:var(--muted); min-width:7.5em; }}
.clks {{ display:flex; flex-wrap:wrap; gap:4px 14px; }}
.clk-l {{ color:var(--muted); margin-inline-end:4px; font-size:.9em; }}
.num {{ font-variant-numeric:tabular-nums; direction:ltr; unicode-bidi:isolate; }}
.warn {{ color:var(--warn); font-weight:600; }}
.note {{ color:var(--muted); }}
.small {{ font-size:.85em; }}
.hash {{ font-family:ui-monospace,Menlo,monospace; }}
.calc label {{ display:flex; gap:8px; align-items:center; margin:8px 0; }}
.calc input {{ width:9em; padding:6px 8px; border:1px solid var(--line); border-radius:6px; background:var(--bg);
  color:var(--fg); font:inherit; direction:ltr; }}
.calc input:focus-visible, a:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
.reason p {{ margin:4px 0 8px; white-space:pre-line; }}
a {{ color:var(--accent); }}
footer {{ color:var(--muted); font-size:.85em; margin:20px 0; }}
</style>
</head>
<body>
<main>
<p class="banner">{banner}</p>
<h1>المحلل التجريبي للذهب</h1>
{canary}{paused}
{body}
<footer><p>{power}</p><p>آخر بناء للصفحة: {asof}</p></footer>
</main>
<script>
(function () {{
  "use strict";
  function two(n) {{ return (n < 10 ? "0" : "") + n; }}
  function fmt(ms) {{ var s = Math.max(0, Math.floor(ms / 1000)); var d = Math.floor(s / 86400);
    var h = Math.floor((s % 86400) / 3600); var m = Math.floor((s % 3600) / 60);
    return (d > 0 ? d + "d " : "") + two(h) + ":" + two(m) + ":" + two(s % 60); }}
  function tick() {{
    var now = Date.now();
    document.querySelectorAll(".card[data-entry]").forEach(function (c) {{
      var e = Number(c.getAttribute("data-entry")), x = Number(c.getAttribute("data-exit"));
      var st = c.querySelector(".state"), cd = c.querySelector(".countdown"), late = c.querySelector(".late");
      if (!st) return;
      if (now < e) {{ st.textContent = "قادمة"; cd.textContent = fmt(e - now); }}
      else if (now < x) {{ st.textContent = "نافذة الدخول"; cd.textContent = fmt(x - now); }}
      else {{ st.textContent = "منتهية"; cd.textContent = ""; }}
      if (late) late.hidden = !(now > e + 5 * 60 * 1000);
    }});
  }}
  document.querySelectorAll(".calc").forEach(function (c) {{
    var side = Number(c.getAttribute("data-side")), S = Number(c.getAttribute("data-s")),
        T = Number(c.getAttribute("data-t"));
    var inp = c.querySelector(".fill"), sl = c.querySelector(".sl"), tp = c.querySelector(".tp");
    inp.addEventListener("input", function () {{
      var f = Number(inp.value);
      if (!isFinite(f) || f <= 0) {{ sl.textContent = "—"; tp.textContent = "—"; return; }}
      sl.textContent = (f - side * S).toFixed(2); tp.textContent = (f + side * T).toFixed(2);
    }});
  }});
  tick(); setInterval(tick, 1000);
}})();
</script>
</body>
</html>
"""


def status_csv(view, now):
    """VolMapAnalyst.csv (prereg §7; register R-AN-64): exists only after a promotion. ASCII, LF, final newline."""
    st = view.read_json(paths.STATE_PATH) or {}
    if st.get("state") != "PROMOTED":
        return None
    today = None
    for p in sorted(view.list("analyst/terminal/"), reverse=True):
        t = view.read_json(p)
        if t and t["call_id"].startswith(now.date().isoformat()):
            today = t
            break
    win = act = stop = tgt = ""
    win, act = "none", "none"
    if today:
        win = today["call_id"][-2:]
        call = view.read_json(paths.call_path("live", today["call_id"])) if today.get("call") else None
        if today["status"] in ("called", "stay_out") and call:
            act = call["plan"]["action"]
            if act != "stay_out":
                stop, tgt = f"{call['S']:.2f}", f"{call['T']:.2f}"
    lines = ["key,value", "version,1", f"asof_utc,{rec.iso(now)}", f"head_sha,{st.get('head_sha') or ''}",
             f"state,{st['state']}", f"n,{st.get('n') or 0}", f"e_n,{(st.get('E') or 0.0):.4f}", "alpha,0.0025",
             f"today_window,{win}", f"today_action,{act}", f"today_stop_usd,{stop}", f"today_target_usd,{tgt}"]
    lines.append(f"end,{len(lines)}")
    text = "\n".join(lines) + "\n"
    text.encode("ascii")
    return text


def main(argv=None):
    import argparse
    from ga.repo import DirView
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(HERE.parents[1]))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    out = Path(a.out or Path(a.root) / "site" / "analyst" / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(rec.UTC).replace(microsecond=0)
    view = DirView(a.root)
    out.write_text(build(view, now), "utf-8")
    csv_text = status_csv(view, now)
    if csv_text is not None:
        (out.parent.parent / "VolMapAnalyst.csv").write_bytes(csv_text.encode("ascii"))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
