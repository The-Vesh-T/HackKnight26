"""Role 2: reaction analytics. Reads role 1's timeline.json, writes analysis.json.

For every market: did it react, how fast, how fully, and -- most importantly -- never
call an illiquid market "slow". Method follows docs/team/role2-reaction-analytics.md.

    python -m tradio.reaction bowman20250623 waller20250717 warsh20260828
    python -m tradio.reaction --all
    python -m tradio.reaction --in path/to/timeline.json --out path/to/analysis.json

Reaction time (deviation from the role file, agreed by the team lead 2026-10-09):
- The role file's CUSUM on 1-minute midpoint CHANGES (k=0.5, h=2 ticks) never alarms on
  real Bowman data: the July cut moved 0.145 -> 0.215 as single 1-tick steps with flat
  minutes between, and each flat minute drains the sum. So `alarm_t` now uses a
  band-exit rule: the first minute after T0 where the midpoint is at least 1 tick
  outside the pre-event range (T0-60..T0) and stays outside for 2 straight minutes.
  It passes the role file's three synthetic tests. The role file's CUSUM result is still
  reported as `cusum_alarm_t` so nobody has to take the change on trust.

Choices the role file leaves open (flagged for team review):
- CUSUM alarms when S > h (strict), the textbook convention.
- Liquidity: 0 trades but quotes moved, spread <= 5 ticks -> "thin" (never "active").
- Polymarket has no trades or spread in our data, so its liquidity is "indicative", and
  per CLAUDE.md rule 2 it gets NO timing at all (alarm_t, t50/t90, cusum all null) and
  never candidate_delay. The role file says to suffix its status with "(indicative)",
  but CLAUDE.md's shared contract fixes status to five values the UI matches on, so
  status stays clean, every market carries `"indicative": true/false`, and indicative
  reasons start with "Indicative:".
- Active direct/partial market with no alarm, or an alarm whose final move is < 2 ticks
  -> expected_no_reaction, with a reason that says what happened.
- Thin market with alarm and |move| >= 2 ticks -> reacted, reason says timing is rough.
  Dark market -> always cant_tell_illiquid.
- Inconsistency checks only override expected_no_reaction; they never hide a reaction
  or an illiquid verdict. Every market also gets a "flags" list with any check that fired.
- The outcome-sum check runs only on Fed-decision events whose five legs are all in the
  timeline; a partial ladder (e.g. Warsh's Oct 2026 H25/H0) legitimately sums to < 1.
- move_ticks is signed (positive = price went up).
- Placebo (tradio/placebo.py): once tradio/data/<event>/placebo.json exists, a reacted or
  candidate_delay market whose placebo p >= 0.05 becomes expected_no_reaction with the
  counts in its reason; p from fewer than 50 usable days is shown but changes nothing.
"""

import argparse
import json
import os
import statistics
import sys
from datetime import timedelta

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tradio.common import DATA_DIR, WEB_DATA_DIR, iso, parse_ts  # noqa: E402

# Thresholds from the role file. Tune only with team agreement.
PRE_MIN, POST_MIN = 60, 90
CUSUM_K, CUSUM_H = 0.5, 2.0          # ticks
BAND_EXIT_TICKS = 1                  # how far outside the pre-event range counts
BAND_PERSIST_MIN = 2                 # consecutive minutes outside to alarm
MIN_MOVE_TICKS = 2
DELAY_AFTER_MIN = 10
PLACEBO_P = 0.05                     # reacted needs placebo p below this, once it exists
SUM_LO, SUM_HI = 0.97, 1.03
XVENUE_TICKS = 2
INCONSISTENT_FOR_MIN = 5             # must persist MORE than this many minutes
# A Fed-decision event's outcome list is complete only if every leg is in the timeline.
FED_DECISION_PREFIX = "KXFEDDECISION-"
FED_DECISION_LEGS = {"H26", "H25", "H0", "C25", "C26"}
# Same outcome on both venues: Kalshi decision leg -> Polymarket id. Only applied to the
# meeting with "direct" exposure, which is the one the Polymarket series cover.
CROSS_VENUE = {"C25": "polymarket:cut25", "H0": "polymarket:nochange"}
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


def band_exit_alarm(series, t0, tick):
    """First minute after T0 where the midpoint sits >= BAND_EXIT_TICKS outside the
    pre-event (T0-60..T0) range for BAND_PERSIST_MIN straight minutes. Returns
    (alarm time or None, (band_lo, band_hi) or None)."""
    pre = [mid for t, mid, _ in series if t0 - timedelta(minutes=PRE_MIN) < t <= t0]
    if not pre:
        return None, None
    lo, hi = min(pre), max(pre)
    run_start, run = None, 0
    for t, mid, _ in series:
        if t <= t0:
            continue
        outside = (_ticks(mid - hi, tick) >= BAND_EXIT_TICKS
                   or _ticks(lo - mid, tick) >= BAND_EXIT_TICKS)
        if outside:
            run_start = run_start or t
            run += 1
            if run >= BAND_PERSIST_MIN:
                return run_start, (lo, hi)
        else:
            run_start, run = None, 0
    return None, (lo, hi)


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
        legs = {i.rsplit("-", 1)[-1] for i in ids}
        if not (event.startswith(FED_DECISION_PREFIX) and FED_DECISION_LEGS <= legs):
            continue
        maps = [{t: mid for t, mid, _ in series_by_id[i]} for i in ids]
        times = sorted(set.intersection(*(set(mp) for mp in maps))) if maps else []
        out = [not (SUM_LO <= sum(mp[t] for mp in maps) <= SUM_HI) for t in times]
        run = _persisting_minutes(out)
        if run > INCONSISTENT_FOR_MIN:
            for i in ids:
                flags[i].append(f"{event} outcome midpoints summed outside "
                                f"{SUM_LO:.2f}-{SUM_HI:.2f} for {run} straight minutes")
    pairs = []
    for m in markets:
        ticker = m["id"].split(":", 1)[1]
        leg = ticker.rsplit("-", 1)[-1]
        if (m["venue"] == "kalshi" and ticker.startswith(FED_DECISION_PREFIX)
                and m.get("exposure") == "direct" and leg in CROSS_VENUE):
            pairs.append((m["id"], CROSS_VENUE[leg]))
    for k_id, pm_id in pairs:
        if pm_id not in series_by_id:
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


def analyze_market(m, series, trades, t0, flags, placebo=None):
    tick = m.get("tick", 0.01)
    indicative = bool(m.get("indicative"))
    exposure = m.get("exposure", "none")
    liq, detail = liquidity(series, trades, t0, tick, indicative)
    pre_mid, move, t50, t90 = absorption(series, t0, tick)
    big = move is not None and abs(move) >= MIN_MOVE_TICKS
    mv = _fmt_ticks(move) if move is not None else "n/a"
    alarm_t, band = band_exit_alarm(series, t0, tick)
    cusum_t = cusum_alarm(series, t0, tick)
    confirmed = None

    if indicative:
        # CLAUDE.md rule 2: no timing claims from Polymarket history.
        alarm_t = cusum_t = t50 = t90 = None
        if pre_mid is None:
            status, reason = "cant_tell_illiquid", "Indicative: not enough points around T0."
        elif big:
            status = "reacted"
            reason = (f"Indicative: moved {mv} ticks from before T0 to T0+75..90 min; "
                      "Polymarket history is not used for timing.")
        elif exposure in ("weak", "none"):
            status, reason = "expected_no_reaction", "Indicative: weak exposure, no move."
        else:
            status = "cant_tell_illiquid"
            reason = (f"Indicative: final move {mv} ticks; no depth or trade data, "
                      "so no judgement on speed.")
    else:
        alarm_min = (alarm_t - t0).total_seconds() / 60 if alarm_t else None
        if alarm_t is not None and pre_mid is not None:
            new_mid = next(mid for t, mid, _ in series if t == alarm_t)
            confirmed = confirming_trade(trades, alarm_t, new_mid, pre_mid, tick)
        if pre_mid is None:
            status, reason = "cant_tell_illiquid", "Not enough quotes around T0 to measure anything."
        elif liq == "dark":
            status = "cant_tell_illiquid"
            reason = (f"Dark before T0 ({detail['trades']} trades, "
                      f"{detail['quote_change_minutes']} quote changes in 60 min); a quiet "
                      "tape here says nothing about speed.")
        elif exposure in ("weak", "none") and alarm_t is None:
            status, reason = "expected_no_reaction", "Weak exposure; no alarm after T0."
        elif alarm_t is not None and big:
            if (liq == "active" and exposure in ("direct", "partial")
                    and alarm_min > DELAY_AFTER_MIN):
                status = "candidate_delay"
                reason = (f"Active market, no alarm in the first {DELAY_AFTER_MIN} min; "
                          f"first alarm {_hhmm(alarm_t)} (T0+{alarm_min:g} min), move {mv} "
                          "ticks. Candidate only.")
            else:
                status = "reacted"
                reason = (f"Alarm {_hhmm(alarm_t)} (T0+{alarm_min:g} min), move {mv} ticks"
                          + (", confirmed by a trade" if confirmed else "")
                          + ("; thin market, timing is rough" if liq == "thin" else "")
                          + ".")
        elif liq == "thin":
            status = "cant_tell_illiquid"
            reason = (f"Thin before T0 ({detail['trades']} trades in 60 min) and no clear "
                      f"reaction (move {mv} ticks); can't tell slow from quiet.")
        elif alarm_t is not None:
            status = "expected_no_reaction"
            reason = (f"Brief alarm {_hhmm(alarm_t)} but final move only {mv} ticks; "
                      "no lasting reaction.")
        elif big:
            status = "expected_no_reaction"
            reason = (f"Drifted {mv} ticks by T0+75..90 min without ever leaving the "
                      "pre-event range for 2 minutes; no reaction time to report.")
        else:
            status = "expected_no_reaction"
            reason = f"Active market stayed inside its pre-event range; final move {mv} ticks."

    placebo_p = None
    if placebo is not None and placebo.get("n") and not indicative:
        placebo_p = placebo.get("p")
        if placebo_p is not None and status in ("reacted", "candidate_delay"):
            if placebo.get("low_n"):
                reason += f" Placebo has too few ordinary days (n={placebo['n']}) to count."
            elif placebo_p >= PLACEBO_P:
                status = "expected_no_reaction"
                reason = (f"Moved {mv} ticks, but {placebo['n_ge']} of {placebo['n']} ordinary "
                          f"days at this hour moved as much (placebo p={placebo_p:g}); not "
                          "distinguishable from a normal day.")
            else:
                reason += (f" Bigger than {placebo['n'] - placebo['n_ge']} of {placebo['n']} "
                           f"ordinary days (placebo p={placebo_p:g}).")

    if flags and status == "expected_no_reaction":
        status, reason = "candidate_inconsistency", flags[0] + "."

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
        "placebo_p": placebo_p,
        "indicative": indicative,
        "cusum_alarm_t": iso(cusum_t) if cusum_t else None,
        "pre_band": [round(band[0], 4), round(band[1], 4)] if band else None,
        "flags": flags,
        "liquidity_detail": detail,
    }


def load_placebo(event_id):
    path = os.path.join(DATA_DIR, event_id, "placebo.json")
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("markets", {})


def analyze(timeline, use_placebo=True):
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
    placebo = load_placebo(timeline["event_id"]) if use_placebo else {}
    out = {}
    for m in timeline["markets"]:
        trades = sorted(trades_by.get(m["id"], []), key=lambda x: x["t"])
        out[m["id"]] = analyze_market(m, series[m["id"]], trades, t0, flags[m["id"]],
                                      placebo.get(m["id"]))
    return {"event_id": timeline["event_id"], "markets": out}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("events", nargs="*", help="event ids under tradio/data/")
    ap.add_argument("--in", dest="inp")
    ap.add_argument("--out")
    ap.add_argument("--all", action="store_true", help="every event with a timeline.json")
    a = ap.parse_args(argv)
    if a.all:
        a.events = sorted(e for e in os.listdir(DATA_DIR)
                          if os.path.isfile(os.path.join(DATA_DIR, e, "timeline.json")))
    jobs = [(a.inp, a.out)] if a.inp else [
        (os.path.join(DATA_DIR, e, "timeline.json"), os.path.join(DATA_DIR, e, "analysis.json"))
        for e in a.events]
    if not jobs:
        ap.error("give event ids or --in")
    for inp, outp in jobs:
        with open(inp, encoding="utf-8") as f:
            result = analyze(json.load(f))
        outp = outp or os.path.join(os.path.dirname(inp), "analysis.json")
        targets = [outp]
        # Same copy role 1's build_timeline.py makes, so the page sees fresh results
        # without re-running ingest.
        web_dir = os.path.join(WEB_DATA_DIR, result["event_id"])
        if not a.inp and os.path.isdir(web_dir):
            targets.append(os.path.join(web_dir, "analysis.json"))
        for path in targets:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=1)
            print(f"wrote {path}")
        for mid, r in result["markets"].items():
            print(f"  {mid:40s} {r['liquidity']:10s} {r['status']:32s} "
                  f"alarm={r['alarm_t']} move={r['move_ticks']} t50={r['t50_min']}")


if __name__ == "__main__":
    main()
