"""Lab v2 paper-trade execution and accounting (SYSTEM_V2_PREREG.md §2.2; v1 §3 items 2-3 and Amendment A1 item 8
where v2 is silent). FROZEN (lab v2): accounting.

For a traded signal with close T (minutes of the model day), side s (+1 long, −1 short), hold H·TF minutes and the
RAW F_S (bp) of the entry slot (finite and > 0):
- Minutes: the model day's M1 rows as v1 lab.execution.Minutes (bid, ask and mid o/h/l/c, volume on both sides).
- Entry minute T + 1; exit minute T + H·TF − 1; the path is the minutes [T + 1, T + H·TF) in time order.
- Entry price P_e: the open of the entry minute on the executable side (ask for a long, bid for a short). A
  non-finite executable price counts as no quote (A1 item 8): if the entry minute has no row or no finite executable
  open, P_e is the close of the latest earlier minute of the model day with a finite executable close; with none,
  the trade is `no_quote` (not counted).
- Unit: D = P_e·(1 − exp(−k_S·F_S/1e4)), k_S = 1.0540339626212611 (the S10 distance; the same formula for both
  sides). Disaster stop X = P_e − s·3·D.
- Path, minute by minute on the executable exit side (bid for a long, ask for a short), exactly v1's loop: a minute
  with a finite exit quote (open, close and the low for a long / the high for a short all finite) whose open is through
  X exits at that open (a gap fill, the entry minute included); otherwise if its low (long) / high (short) reaches X
  it exits at X (reason "stop"). The stop is checked before the time exit, so the stop wins a tie in the exit minute.
- No-quote run: a minute without a row, without volume on both sides, or without a finite exit quote adds to a run;
  at 15 consecutive minutes the trade is settled at X (reason "gap", conservative). A run counts from the entry minute.
- Time exit (reason "time"): the close of the exit minute on the exit side; if it has no finite exit quote, the close
  of the last path minute with one (v1's rule); X with reason "gap" if the path has none.
- Money: net$ = s·(P_x − P_e) − 0.10; stress net$ = net$ − 0.25; R = net$/D; R_stress = stress net$/D;
  y = clip(R, −2, 2). cost$ = 0.10 + half the spread at the entry quote + half the spread at the exit quote (the
  open's spread for an open exit, the close's otherwise; 0 when a side is not finite or there is no row); gross$ =
  net$ + cost$.
- P4 twin (§2.2): μ = the gross long R on the mid, no cost and no allowance, with the same entry and exit minutes and
  the same stop distance: P_e = the mid open of the entry minute (or the latest earlier finite mid close), X =
  P_e − 3·D with the trade's D, the same path rules on the mid (finite mid quote; volume on both sides for the run),
  R = (P_x − P_e)/D.
- A row with status "final" never holds a non-finite number (A1 item 8); trade() raises if it would.
"""
import numpy as np

K_S = 1.0540339626212611
STOP_MULT = 3.0
ALLOWANCE = 0.10
STRESS_ADD = 0.25
GAP_MINUTES = 15
CLIP = 2.0


def unit_d(P_e, F_S):
    return float(P_e * (1.0 - np.exp(-K_S * F_S / 1e4)))


def _half_spread(md, row, field):
    if row < 0:
        return 0.0
    a, b = getattr(md, "ask_" + field)[row], getattr(md, "bid_" + field)[row]
    return float((a - b) / 2) if np.isfinite(a) and np.isfinite(b) else 0.0


def trade(md, side, T, hold_min, F_S=None, twin_D=None):
    """Settle one paper trade (twin_D given: the P4 mid twin, side +1, with that D). Returns a dict."""
    twin = twin_D is not None
    if twin:
        side = 1
    elif not (F_S is not None and np.isfinite(F_S) and F_S > 0):
        raise ValueError(f"trade() needs a finite F_S > 0, got {F_S!r}")
    t0, t1 = T + 1, T + hold_min
    out = {"side": side, "entry_min": t0}
    if twin:
        ent_o, ent_c = md.o, md.c
    else:
        ent_o, ent_c = (md.ask_o, md.ask_c) if side > 0 else (md.bid_o, md.bid_c)
    r0 = md.row(t0)
    if r0 >= 0 and np.isfinite(ent_o[r0]):
        e_row, e_field = r0, "o"
        P_e = float(ent_o[r0])
    else:
        before = md.pos[:t0]
        rb = before[before >= 0]
        rb = rb[np.isfinite(ent_c[rb])]
        if len(rb) == 0:
            out.update(P_e=np.nan, status="no_quote")
            return out
        e_row, e_field = int(rb[-1]), "c"
        P_e = float(ent_c[e_row])
    D = float(twin_D) if twin else unit_d(P_e, F_S)
    X = P_e - side * STOP_MULT * D
    rows = md.pos[t0:t1]
    n = len(rows)
    present = rows >= 0
    r = np.where(present, rows, 0)
    if twin:
        o, h, l, c = md.o[r], md.h[r], md.l[r], md.c[r]
    elif side > 0:
        o, h, l, c = md.bid_o[r], md.bid_h[r], md.bid_l[r], md.bid_c[r]
    else:
        o, h, l, c = md.ask_o[r], md.ask_h[r], md.ask_l[r], md.ask_c[r]
    q = present & np.isfinite(o) & np.isfinite(c) & np.isfinite(l if side > 0 else h)
    live = q & md.vol[r]
    ar = np.arange(n)
    run = ar - np.maximum.accumulate(np.where(live, ar, -1))
    with np.errstate(invalid="ignore"):
        if side > 0:
            open_hit, any_hit = q & (o <= X), q & ((o <= X) | (l <= X))
        else:
            open_hit, any_hit = q & (o >= X), q & ((o >= X) | (h >= X))
    event = any_hit | (run >= GAP_MINUTES)
    exit_row, exit_field = -1, "c"
    if event.any():
        i = int(np.argmax(event))
        if any_hit[i]:
            exit_field = "o" if open_hit[i] else "c"
            exit_px, reason, exit_row = (o[i] if open_hit[i] else X), "stop", int(rows[i])
        else:
            exit_px, reason, exit_row = X, "gap", int(rows[i])
        exit_min = t0 + i
    else:
        idx = np.flatnonzero(q)
        if len(idx) == 0:
            exit_px, exit_min, reason = X, t1 - 1, "gap"
        else:
            j = idx[-1]
            exit_px, exit_min, reason, exit_row = c[j], t0 + j, "time", int(rows[j])
    allowance = 0.0 if twin else ALLOWANCE
    net = side * (exit_px - P_e) - allowance
    hs_e = 0.0 if twin else _half_spread(md, e_row, e_field)
    hs_x = 0.0 if twin else _half_spread(md, exit_row, exit_field)
    cost = allowance + hs_e + hs_x
    stress = net - (0.0 if twin else STRESS_ADD)
    out.update(P_e=P_e, status="final", X=float(X), D=float(D), exit_min=int(exit_min), P_x=float(exit_px),
               reason=reason, net_usd=float(net), net_stress_usd=float(stress), cost_usd=float(cost),
               gross_usd=float(net + cost), R=float(net / D), R_stress=float(stress / D),
               y=float(np.clip(net / D, -CLIP, CLIP)))
    nums = ("P_e", "X", "D", "P_x", "net_usd", "net_stress_usd", "cost_usd", "gross_usd", "R", "R_stress", "y")
    if not (D > 0 and all(np.isfinite(out[k]) for k in nums)):
        raise ValueError(f"a final trade would hold a non-finite number: {out}")
    return out
