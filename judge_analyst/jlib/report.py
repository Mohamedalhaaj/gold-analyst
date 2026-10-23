"""The judge's outputs under reports/judge_analyst/ (prereg §8; R-AN-69, R-AN-70; R-JV-1..4 analogues).

Files (all written by the judge workflow only, except ADJUDICATIONS.csv, which is the owner's):
- FINDINGS.csv     append-only `first_seen_utc,severity,check,key`: the first time each finding key was seen.
- ADJUDICATIONS.csv append-only (owner) `utc,ref,check,subject,decision,note`:
    ref `latch`,   decision `clear`  → clears a VIOLATION key (utc must be >= the key's first_seen_utc);
    ref `warning`, decision `judge_error` | `spec_gap` | `accepted` | `ops` → adjudicates a WARNING key;
    ref `warning`, decision `system_error` → a VIOLATION in force (J5 analogue) until a later row for the same subject
    with decision `resolved`.
- VERDICTS.csv     append-only `judge_utc,head_sha,analyst_verdict,n_violation_keys,n_warning_keys,code_sha8`; its last
                   head_sha is the anchor of the next run's A7 check.
- evidence.csv     append-only, R-AN-69's columns, one row per item first seen.
- hours.csv        append-only, the judge's hour-file cache (sources.HOURS_HEADER).
- analyst_verdict.json (`analyst-verdict/1`) and REPORT.md: snapshots.

VIOLATION keys latch: a key once seen stays in force until its clear row, even when a later run cannot re-find it
(an API entry that aged out, say). WARNINGs are recomputed every run and count while unadjudicated. A0 findings
(unverifiable inputs) are never adjudicated: they make that run WARNING at best (never SOUND).
"""
from __future__ import annotations

import csv
import io
import json
import os

from .common import VIOLATION, WARNING

FINDINGS_HEADER = ["first_seen_utc", "severity", "check", "key"]
ADJ_HEADER = ["utc", "ref", "check", "subject", "decision", "note"]
VERDICTS_HEADER = ["judge_utc", "head_sha", "analyst_verdict", "n_violation_keys", "n_warning_keys", "code_sha8"]
WARNING_DECISIONS = {"judge_error", "spec_gap", "accepted", "ops"}


def read_csv(path, header):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames != header:
            raise ValueError(f"{os.path.basename(path)}: header differs")
        return list(rd)


def append_csv(path, header, rows):
    if not rows and os.path.exists(path):
        return
    new = not os.path.exists(path)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if new:
        w.writerow(header)
    for r in rows:
        w.writerow(["" if r.get(k) is None else r.get(k) for k in header])
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


def last_anchor(out_dir):
    rows = read_csv(os.path.join(out_dir, "VERDICTS.csv"), VERDICTS_HEADER)
    return rows[-1]["head_sha"] if rows else None


def decide(findings, out_dir, now_utc):
    """Apply latching and adjudications. Returns (verdict, violation_keys, cleared_keys, warning_keys,
    adjudicated_warnings, new_finding_rows)."""
    frows = read_csv(os.path.join(out_dir, "FINDINGS.csv"), FINDINGS_HEADER)
    first_seen = {r["key"]: r for r in frows}
    new_rows = []
    for f in findings.items:
        if f.check == "A0":
            continue
        if f.key not in first_seen:
            row = {"first_seen_utc": now_utc, "severity": f.severity, "check": f.check, "key": f.key}
            first_seen[f.key] = row
            new_rows.append(row)
    adj = read_csv(os.path.join(out_dir, "ADJUDICATIONS.csv"), ADJ_HEADER)
    cleared = set()
    warn_done = set()
    sys_err = {}
    for a in adj:
        subj = a.get("subject", "")
        fs = first_seen.get(subj)
        if a.get("ref") == "latch" and a.get("decision") == "clear" and fs and fs["severity"] == VIOLATION and \
                a.get("check") == fs["check"] and a.get("utc", "") >= fs["first_seen_utc"]:
            cleared.add(subj)
        elif a.get("ref") == "warning" and fs and fs["severity"] == WARNING and a.get("check") == fs["check"]:
            if a.get("decision") in WARNING_DECISIONS:
                warn_done.add(subj)
            elif a.get("decision") == "system_error":
                sys_err[subj] = True
                warn_done.add(subj)
            elif a.get("decision") == "resolved":
                sys_err.pop(subj, None)
    latched = {k for k, r in first_seen.items() if r["severity"] == VIOLATION}
    violation_keys = sorted(latched - cleared) + sorted(f"system_error:{k}" for k in sys_err)
    current_warn = sorted({f.key for f in findings.items if f.severity == WARNING and f.key not in warn_done})
    if violation_keys:
        verdict = "VIOLATION"
    elif current_warn:
        verdict = "WARNING"
    else:
        verdict = "SOUND"
    return verdict, violation_keys, sorted(cleared), current_warn, sorted(warn_done), new_rows, first_seen, adj


def write_outputs(result, out_dir, now_utc, code_sha8, hour_source=None):
    os.makedirs(out_dir, exist_ok=True)
    adj_path = os.path.join(out_dir, "ADJUDICATIONS.csv")
    if not os.path.exists(adj_path):
        append_csv(adj_path, ADJ_HEADER, [])
    verdict, vkeys, ckeys, wkeys, wdone, new_rows, first_seen, adj = decide(result.findings, out_dir, now_utc)
    append_csv(os.path.join(out_dir, "FINDINGS.csv"), FINDINGS_HEADER, new_rows)
    from .judge import EVIDENCE_HEADER
    append_csv(os.path.join(out_dir, "evidence.csv"), EVIDENCE_HEADER, result.evidence_new)
    if hour_source is not None:
        hour_source.flush()
    append_csv(os.path.join(out_dir, "VERDICTS.csv"), VERDICTS_HEADER,
               [{"judge_utc": now_utc, "head_sha": result.head, "analyst_verdict": verdict,
                 "n_violation_keys": len(vkeys), "n_warning_keys": len(wkeys), "code_sha8": code_sha8}])
    vobj = {"schema": "analyst-verdict/1", "asof_utc": now_utc, "head_sha": result.head, "analyst_verdict": verdict,
            "violation_keys": vkeys, "cleared_keys": ckeys}
    with open(os.path.join(out_dir, "analyst_verdict.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(vobj, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    with open(os.path.join(out_dir, "REPORT.md"), "w", encoding="utf-8") as f:
        f.write(render_report(result, verdict, vkeys, ckeys, wkeys, wdone, first_seen, adj, now_utc, code_sha8))
    return vobj


def render_report(result, verdict, vkeys, ckeys, wkeys, wdone, first_seen, adj, now_utc, code_sha8):
    L = []
    L.append("# Analyst judge report")
    L.append("")
    L.append(f"- asof_utc: {now_utc}")
    L.append(f"- head_sha (gold-analyst main checked): {result.head}")
    L.append(f"- judge code_sha8: {code_sha8}")
    L.append(f"- **analyst_verdict: {verdict}**")
    L.append("")
    L.append("Built blind from ANALYST_V1_PREREG.md section 8 (DRAFT v0.5) and IMPLEMENTATION_REGISTER section 16. "
             "Model text is "
             "never printed here: findings carry enums, field names, hashes and numbers only. Findings marked "
             "(owner-attested) rest on the owner's weekly runs export (R-AN-60).")
    L.append("")
    L.append(f"## VIOLATION keys in force ({len(vkeys)})")
    L.append("")
    cur = {f.key: f for f in result.findings.items}
    for k in vkeys:
        f = cur.get(k)
        note = f" ({f.detail})" if f and f.detail else (" (latched; not re-found this run)" if f is None else "")
        oa = " (owner-attested)" if f and f.owner_attested else ""
        L.append(f"- `{k}`{oa}{note}")
    if not vkeys:
        L.append("- none")
    L.append("")
    L.append(f"## Cleared VIOLATION keys ({len(ckeys)})")
    L.append("")
    for k in ckeys:
        rows = [a for a in adj if a.get("subject") == k and a.get("decision") == "clear"]
        L.append(f"- `{k}` cleared {rows[0]['utc'] if rows else ''}")
    if not ckeys:
        L.append("- none")
    L.append("")
    L.append(f"## WARNINGs needing an ADJUDICATIONS row or a fix ({len(wkeys)})")
    L.append("")
    for k in wkeys:
        f = cur.get(k)
        oa = " (owner-attested)" if f and f.owner_attested else ""
        L.append(f"- `{k}`{oa}{(' (' + f.detail + ')') if f and f.detail else ''}")
    if not wkeys:
        L.append("- none")
    L.append("")
    L.append(f"## Adjudicated WARNINGs ({len(wdone)})")
    L.append("")
    for k in wdone:
        L.append(f"- `{k}`")
    if not wdone:
        L.append("- none")
    L.append("")
    L.append("## Run summary")
    L.append("")
    for k in sorted(result.summary):
        L.append(f"- {k}: {result.summary[k]}")
    L.append("")
    return "\n".join(L)
