"""Role 2: reaction analytics. Reads role 1's timeline.json, writes analysis.json.

For every market: did it react, how fast, how fully, and -- most importantly -- never
call an illiquid market "slow". Method follows docs/team/role2-reaction-analytics.md.

    python -m tradio.reaction bowman20250623 waller20250717
    python -m tradio.reaction --in path/to/timeline.json --out path/to/analysis.json

Choices the role file leaves open (flagged for team review, change only with agreement):
- CUSUM alarms when S > h (strict), the textbook convention.
- Liquidity: 0 trades but quotes moved, spread <= 5 ticks -> "thin" (never "active").
- Polymarket has no trades or spread in our data, so its liquidity is "indicative",
  it never gets candidate_delay (no timing claims), and its status is suffixed
  " (indicative)".
- Active direct/partial market with no alarm, or an alarm whose final move is < 2 ticks
  -> expected_no_reaction, with a reason that says what happened.
- Thin market with alarm and |move| >= 2 ticks -> reacted, reason says timing is rough.
  Dark market -> always cant_tell_illiquid.
- Inconsistency checks only override expected_no_reaction; they never hide a reaction
  or an illiquid verdict. Every market also gets a "flags" list with any check that fired.
- The outcome-sum check runs only on events whose outcome list we know is complete
  (EXHAUSTIVE_EVENTS); a partial ladder legitimately sums to less than 1.
- move_ticks is signed (positive = price went up).
"""

import argparse
import json
import os
import statistics
import sys
from datetime import timedelta

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import DATA_DIR, iso, parse_ts  # noqa: E402

# Thresholds from the role file. Tune only with team agreement.
PRE_MIN, POST_MIN = 60, 90
CUSUM_K, CUSUM_H = 0.5, 2.0          # ticks
MIN_MOVE_TICKS = 2
DELAY_AFTER_MIN = 10
SUM_LO, SUM_HI = 0.97, 1.03
XVENUE_TICKS = 2
INCONSISTENT_FOR_MIN = 5             # must persist MORE than this many minutes
EXHAUSTIVE_EVENTS = {"KXFEDDECISION-25JUL"}
# Same outcome on both venues (Kalshi id suffix -> Polymarket id).
CROSS_VENUE = {"KXFEDDECISION-25JUL-C25": "polymarket:cut25",
               "KXFEDDECISION-25JUL-H0": "polymarket:nochange"}
STATUSES = ("reacted", "expected_no_reaction", "cant_tell_illiquid",
            "candidate_delay", "candidate_inconsistency")


def _ticks(x, tick):
    return round(x / tick, 6)


def _fmt_ticks(x):
    return f"{x:+g}" if x else "0"


def _hhmm(dt):
    return dt.strftime("%H:%M UTC")


def build_series(quotes, t0):
    """Per-minute midpoints in the window, carried forward when imputed.
    Returns a list of (datetime, mid, quote) sorted by time."""
    start, end = t0 - timedelta(minutes=PRE_MIN), t0 + timedelta(minutes=POST_MIN)
    rows = sorted(((parse_ts(q["t"]), q) for q in quotes), key=lambda r: r[0])
    out, last = [], None
    for t, q in rows:
        if t < start or t > end or q.get("mid") is None:
            continue
        if q.get("imputed") and last is not None:
            mid = last
        else:
            mid = q["mid"]
            last = mid
        out.append((t, mid, q))
    return out


def liquidity(series, trades, t0, tick, indicative):
    start = t0 - timedelta(minutes=PRE_MIN)
    pre = [r for r in series if start < r[0] <= t0]
    n_trades = sum(1 for tr in trades if start <= tr["t"] < t0)
    changes = sum(1 for a, b in zip(pre, pre[1:]) if b[1] != a[1])
    spreads = [_ticks(q["ask"] - q["bid"], tick) for _, _, q in pre
               if q.get("bid") is not None and q.get("ask") is not None]
    med_spread = statistics.median(spreads) if spreads else None
    imputed = sum(1 for _, _, q in pre if q.get("imputed"))
    detail = {"trades": n_trades, "quote_change_minutes": changes,
              "median_spread_ticks": med_spread, "imputed_minutes": imputed}
    if indicative:
        return "indicative", detail
    if (med_spread is not None and med_spread > 5) or (n_trades == 0 and changes == 0):
        cls = "dark"
    elif n_trades >= 10 and med_spread is not None and med_spread <= 2:
        cls = "active"
    else:
        cls = "thin"
    return cls, detail


def cusum_alarm(series, t0, tick):
    """Two-sided CUSUM on 1-minute midpoint changes after T0 (in ticks).
    Returns the quote time of the first alarm, or None."""
    base = [r for r in series if r[0] <= t0]
    post = [r for r in series if r[0] > t0]
    if not base or not post:
        return None
    prev, s_hi, s_lo = base[-1][1], 0.0, 0.0
    for t, mid, _ in post:
        d = _ticks(mid - prev, tick)
        prev = mid
        s_hi = max(0.0, s_hi + d - CUSUM_K)
        s_lo = max(0.0, s_lo - d - CUSUM_K)
        if s_hi > CUSUM_H or s_lo > CUSUM_H:
            return t
    return None


def confirming_trade(trades, alarm_t, new_mid, pre_mid, tick):
    """Earliest trade inside the alarm minute at the new level: within 1 tick of the
    alarm-minute mid and closer to it than to the pre-event mid."""
    lo = alarm_t - timedelta(minutes=1)
    for tr in trades:
        if lo < tr["t"] <= alarm_t:
            p = tr["price"]
            if abs(p - new_mid) <= tick + 1e-9 and abs(p - new_mid) < abs(p - pre_mid):
                return tr["raw_t"]
    return None


def absorption(series, t0, tick):
    pre = [m for t, m, _ in series if t0 - timedelta(minutes=10) < t <= t0]
    fin = [m for t, m, _ in series
           if t0 + timedelta(minutes=75) <= t <= t0 + timedelta(minutes=POST_MIN)]
    if not pre or not fin:
        return None, None, None, None
    pre_mid, final_mid = statistics.median(pre), statistics.median(fin)
    move = _ticks(final_mid - pre_mid, tick)
    t50 = t90 = None
    if abs(move) >= MIN_MOVE_TICKS:
        for t, m, _ in series:
            if t <= t0:
                continue
            frac = (m - pre_mid) / (final_mid - pre_mid)
            mins = round((t - t0).total_seconds() / 60, 2)
            if t50 is None and frac >= 0.5:
                t50 = mins
            if t90 is None and frac >= 0.9:
                t90 = mins
                break
    return pre_mid, move, t50, t90


def _persisting_minutes(flags_by_minute):
    """Longest run of consecutive True minutes."""
    best = run = 0
    for f in flags_by_minute:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def inconsistency_flags(markets, series_by_id, t0):
    """market id -> list of human-readable flags."""
    flags = {m["id"]: [] for m in markets}
    by_event = {}
    for m in markets:
        if m["venue"] == "kalshi":
            ticker = m["id"].split(":", 1)[1]
            by_event.setdefault(ticker.rsplit("-", 1)[0], []).append(m["id"])
    for event, ids in by_event.items():
        if event not in EXHAUSTIVE_EVENTS:
            continue
        maps = [{t: mid for t, mid, _ in series_by_id[i]} for i in ids]
        times = sorted(set.intersection(*(set(mp) for mp in maps))) if maps else []
        out = [not (SUM_LO <= sum(mp[t] for mp in maps) <= SUM_HI) for t in times]
        run = _persisting_minutes(out)
        if run > INCONSISTENT_FOR_MIN:
            for i in ids:
                flags[i].append(f"{event} outcome midpoints summed outside "
                                f"{SUM_LO:.2f}-{SUM_HI:.2f} for {run} straight minutes")
    for k_suffix, pm_id in CROSS_VENUE.items():
        k_id = "kalshi:" + k_suffix
        if k_id not in series_by_id or pm_id not in series_by_id:
            continue
        km = {t: mid for t, mid, _ in series_by_id[k_id]}
        pm = {t: mid for t, mid, _ in series_by_id[pm_id]}
        tick = 0.01
        times = sorted(set(km) & set(pm))
        run = _persisting_minutes([abs(_ticks(km[t] - pm[t], tick)) > XVENUE_TICKS for t in times])
        if run > INCONSISTENT_FOR_MIN:
            msg = (f"Kalshi and Polymarket (indicative) differed by more than {XVENUE_TICKS} "
                   f"ticks for {run} straight minutes; settlement rules may differ")
            flags[k_id].append(msg)
            flags[pm_id].append(msg)
    return flags


def analyze_market(m, series, trades, t0, flags):
    tick = m.get("tick", 0.01)
    indicative = bool(m.get("indicative"))
    exposure = m.get("exposure", "none")
    liq, detail = liquidity(series, trades, t0, tick, indicative)
    alarm_t = cusum_alarm(series, t0, tick)
    pre_mid, move, t50, t90 = absorption(series, t0, tick)
    big = move is not None and abs(move) >= MIN_MOVE_TICKS
    alarm_min = (alarm_t - t0).total_seconds() / 60 if alarm_t else None
    confirmed = None
    if alarm_t is not None and pre_mid is not None:
        new_mid = next(mid for t, mid, _ in series if t == alarm_t)
        confirmed = confirming_trade(trades, alarm_t, new_mid, pre_mid, tick)
    mv = _fmt_ticks(move) if move is not None else "n/a"

    if pre_mid is None:
        status, reason = "cant_tell_illiquid", "Not enough quotes around T0 to measure anything."
    elif indicative:
        if alarm_t is not None and big:
            status = "reacted"
            reason = (f"Indicative series moved {mv} ticks after the release; "
                      "Polymarket history is not used for timing.")
        elif exposure in ("weak", "none"):
            status, reason = "expected_no_reaction", "Weak exposure and no move detected."
        else:
            status = "cant_tell_illiquid"
            reason = (f"Indicative series, no depth or trade data; final move {mv} ticks. "
                      "Can't judge speed.")
    elif liq == "dark":
        status = "cant_tell_illiquid"
        reason = (f"Dark before T0 ({detail['trades']} trades, "
                  f"{detail['quote_change_minutes']} quote changes in 60 min); a quiet tape "
                  "here says nothing about speed.")
    elif exposure in ("weak", "none") and alarm_t is None:
        status, reason = "expected_no_reaction", "Weak exposure; no alarm after T0."
    elif alarm_t is not None and big:
        if liq == "active" and exposure in ("direct", "partial") and alarm_min > DELAY_AFTER_MIN:
            status = "candidate_delay"
            reason = (f"Active market, no alarm in the first {DELAY_AFTER_MIN} min; first alarm "
                      f"{_hhmm(alarm_t)} (T0+{alarm_min:g} min), move {mv} ticks. Candidate only.")
        else:
            status = "reacted"
            reason = (f"Alarm {_hhmm(alarm_t)} (T0+{alarm_min:g} min), move {mv} ticks"
                      + (", confirmed by a trade" if confirmed else "")
                      + ("; thin market, timing is rough" if liq == "thin" else "") + ".")
    elif liq == "thin":
        status = "cant_tell_illiquid"
        reason = (f"Thin before T0 ({detail['trades']} trades in 60 min) and no clear "
                  f"reaction (move {mv} ticks); can't tell slow from quiet.")
    elif alarm_t is not None:
        status = "expected_no_reaction"
        reason = (f"Brief alarm {_hhmm(alarm_t)} but final move only {mv} ticks; "
                  "no lasting reaction.")
    else:
        status = "expected_no_reaction"
        reason = f"Active market stayed inside noise; final move {mv} ticks, no alarm."

    if flags and status == "expected_no_reaction":
        status, reason = "candidate_inconsistency", flags[0] + "."
    if indicative:
        status += " (indicative)"

    return {
        "liquidity": liq,
        "pre_mid": round(pre_mid, 4) if pre_mid is not None else None,
        "alarm_t": iso(alarm_t) if alarm_t else None,
        "confirmed_by_trade": confirmed,
        "move_ticks": move,
        "t50_min": t50,
        "t90_min": t90,
        "status": status,
        "reason": reason,
        "placebo_p": None,
        "flags": flags,
        "liquidity_detail": detail,
    }


def analyze(timeline):
    t0 = parse_ts(timeline["t0"])
    trades_by = {}
    for tr in timeline.get("trades", []):
        trades_by.setdefault(tr["market"], []).append(
            {"t": parse_ts(tr["t"]), "raw_t": tr["t"], "price": tr["price"]})
    quotes_by = {}
    for q in timeline.get("quotes", []):
        quotes_by.setdefault(q["market"], []).append(q)
    series = {m["id"]: build_series(quotes_by.get(m["id"], []), t0) for m in timeline["markets"]}
    flags = inconsistency_flags(timeline["markets"], series, t0)
    out = {}
    for m in timeline["markets"]:
        trades = sorted(trades_by.get(m["id"], []), key=lambda x: x["t"])
        out[m["id"]] = analyze_market(m, series[m["id"]], trades, t0, flags[m["id"]])
    return {"event_id": timeline["event_id"], "markets": out}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("events", nargs="*", help="event ids under tradio/data/")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    jobs = [(a.inp, a.out)] if a.inp else [
        (os.path.join(DATA_DIR, e, "timeline.json"), os.path.join(DATA_DIR, e, "analysis.json"))
        for e in a.events]
    if not jobs:
        ap.error("give event ids or --in")
    for inp, outp in jobs:
        with open(inp, encoding="utf-8") as f:
            result = analyze(json.load(f))
        outp = outp or os.path.join(os.path.dirname(inp), "analysis.json")
        with open(outp, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
        print(f"wrote {outp}")
        for mid, r in result["markets"].items():
            print(f"  {mid:40s} {r['liquidity']:10s} {r['status']:32s} "
                  f"alarm={r['alarm_t']} move={r['move_ticks']} t50={r['t50_min']}")


if __name__ == "__main__":
    main()
