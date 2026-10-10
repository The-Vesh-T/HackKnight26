/* Tradio replay terminal.
 *
 * Reads the cached files written by roles 1-3 and replays them with a client-side clock:
 *   data/tradio/<event>/timeline.json   (role 1 + role 3's speech paragraphs)
 *   data/tradio/<event>/analysis.json   (role 2)
 *   data/tradio/<event>/evidence.json   (role 1, raw rows behind the numbers)
 *   data/tradio/<event>/backtest.json   (tradio/playbook.py: what the playbook would have traded)
 *   data/tradio/model.json              (tradio/learner.py: out-of-sample predictions)
 * The LIVE tab reads the same files from data/tradio/live/, which tradio/live.py rewrites
 * every few seconds during a speech.
 *
 * Rules this page follows (CLAUDE.md): no network calls besides those local files (plus the
 * Start/Stop API when the page is served by tradio/serve.py), no analytics in the browser
 * (statuses, alarms, moves, orders and P&L come from the files), a prepared text appears all
 * at once at T0, Polymarket is labelled indicative and gets no timing markers.
 * The only arithmetic done here is for display: cents formatting and "price now minus the
 * pre-speech price", both numbers straight from the data files.
 */
(() => {
  "use strict";

  const DATA_ROOT = "data/tradio/";
  const EVENTS = [
    // focus = the contract the story is about (a presentation choice; numbers still come from the files)
    { id: "bowman20250623", short: "Bowman", sub: "Jun 23 2025 · hawk turns dovish", focus: "kalshi:KXFEDDECISION-25JUL-C25" },
    { id: "waller20250717", short: "Waller", sub: "Jul 17 2025 · dove stays dovish", focus: "kalshi:KXFEDDECISION-25JUL-C25" },
    { id: "warsh20260828", short: "Warsh", sub: "Aug 28 2026 · Jackson Hole" },
  ];
  const COMPARE = { id: "compare", short: "Bowman vs Waller", sub: "same contracts, same clock" };
  // LIVE reads data/tradio/live/, which tradio/live.py rewrites every few seconds during a speech.
  const LIVE = { id: "live", short: "● LIVE", sub: "Fed speech happening now" };
  const LIVE_POLL_MS = 3000;
  const STALE_MS = 90 * 1000;
  const COMPARE_PAIR = ["bowman20250623", "waller20250717"];
  const COMPARE_SUFFIXES = [["-C25", "July cut 25 bp"], ["-H0", "July hold"]];
  const SPEEDS = [1, 10, 30, 60];
  const ALARM_STATUSES = new Set(["reacted", "alarm", "candidate_delay", "candidate_inconsistency"]);
  const MIN = 60000;
  const START_REL = -5 * MIN;

  const STATUS_TEXT = {
    reacted: "Reacted",
    expected_no_reaction: "No reaction expected",
    cant_tell_illiquid: "Can't tell · too thin",
    candidate_delay: "Possible delay",
    candidate_inconsistency: "Possible inconsistency",
    // live only, before the 90-minute window closes (see tradio/live.py provisional())
    alarm: "Alarm · moving",
    watching: "Watching",
  };
  const STATUS_HELP = {
    reacted: "The price left its pre-speech range soon after the text came out, and the move held.",
    alarm: "Live only: the price has left its pre-speech range for 2+ minutes. The final verdict comes when the 90-minute window closes.",
    candidate_delay: "A busy market that did move, but later than expected. A candidate to look at, not a conclusion.",
    candidate_inconsistency: "Related prices disagree (outcome odds don't add up, or Kalshi and Polymarket differ). A candidate, not a conclusion.",
    expected_no_reaction: "No lasting move out of its pre-speech range, which is what you'd expect for this market.",
    watching: "Live only: no alarm so far. The final verdict comes when the 90-minute window closes.",
    cant_tell_illiquid: "Too little trading to tell a slow market from a quiet one, so Tradio says it can't tell instead of calling it slow.",
  };
  const STATUS_ORDER = ["reacted", "alarm", "candidate_delay", "candidate_inconsistency", "expected_no_reaction", "watching", "cant_tell_illiquid"];
  const KNOWN_STATUS = new Set(STATUS_ORDER);
  const LIQ_TEXT = { active: "busy", thin: "thin", dark: "very little", indicative: "n/a (indicative only)" };

  const GROUPS = [
    { id: "reacted", title: "Reacted", color: "var(--green)", open: true, match: (m) => m.status === "reacted" },
    { id: "alarm", title: "Alarm · moving", color: "var(--green)", open: true, match: (m) => m.status === "alarm" },
    { id: "flagged", title: "Worth a closer look", color: "var(--orange)", open: true, match: (m) => m.status === "candidate_delay" || m.status === "candidate_inconsistency" },
    { id: "quiet", title: "No reaction expected", color: "#a9c1cd", open: true, match: (m) => m.status === "expected_no_reaction" },
    { id: "watching", title: "Watching", color: "var(--faint)", open: true, match: (m) => m.status === "watching" },
    { id: "thin", title: "Can't tell · too thin", color: "var(--hawk)", open: false, note: STATUS_HELP.cant_tell_illiquid, match: (m) => m.status === "cant_tell_illiquid" },
    { id: "none", title: "Not analysed yet", color: "var(--faint)", open: true, match: (m) => !m.status || !KNOWN_STATUS.has(m.status) },
  ];

  const state = {
    view: null,          // event id, "compare" or "live"
    events: {},          // id -> prepared data
    rel: START_REL,      // replay clock, ms relative to T0 (shared across events so switching keeps position)
    playing: false,
    speed: 30,
    focus: {},           // event id -> focused market id
    focusPinned: {},     // view -> true once the viewer picks a market (LIVE then stops following the headline)
    fired: new Set(),    // alarm pulses already shown
    lastDraw: 0,
    lastFit: 0,
    touched: false,      // the viewer has used a control: hides the "press play" prompt
    speechOpen: false,   // full speech text expanded
    showAllParas: false, // include paragraphs that aren't about policy
    contextOpen: false,  // the paragraph around the key sentence is shown
    groupOpen: {},       // "<view>:<group id>" -> bool, set once the viewer toggles a group
    deskSkipsOpen: false, // the desk's "skipped by the playbook" list is expanded
    sig: {},             // last-rendered signature per section, reset whenever the page shell is rebuilt
    follow: true,        // LIVE view: keep the clock pinned to the newest data
    liveTimer: null,
    liveShellKey: null,
    model: null,         // data/tradio/model.json, loaded once
  };

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- time helpers ----------
  // Data timestamps can carry microseconds; trim to milliseconds before Date.parse.
  const parseT = (s) => (s ? Date.parse(String(s).replace(/(\.\d{3})\d+/, "$1")) : NaN);
  const etFmt = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const etShort = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", hourCycle: "h23" });
  const fmtEt = (ms) => etFmt.format(new Date(ms));
  const fmtEtShort = (ms) => etShort.format(new Date(ms));
  const fmtUtc = (ms) => new Date(ms).toISOString().slice(11, 19);
  function fmtRel(ms) {
    const sign = ms < 0 ? "−" : "+";
    const a = Math.abs(Math.round(ms / 1000));
    const h = Math.floor(a / 3600), m = Math.floor((a % 3600) / 60), s = a % 60;
    const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
    return `T${sign}${h ? h + ":" + mm : mm}:${ss}`;
  }
  const relMin = (ms) => `+${Math.round(ms / MIN)}m`;
  function agoText(ms) {
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    return m < 120 ? `${m} min ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`;
  }

  // ---------- number / label helpers ----------
  // Prices are stored in dollars (0.155); show cents.
  function cents(x) {
    if (x == null || Number.isNaN(x)) return "—";
    const c = x * 100;
    return (Math.abs(c - Math.round(c)) < 1e-6 ? c.toFixed(0) : c.toFixed(1)) + "¢";
  }
  function sgn(x, d = 1, trim = false) {
    if (x == null || Number.isNaN(+x)) return "—";
    const v = +x;
    let t = Math.abs(v).toFixed(d);
    if (trim) t = t.replace(/\.0+$/, "");
    return +t === 0 ? t : (v > 0 ? "+" : "−") + t;
  }
  function deltaC(d) {
    if (d == null || Number.isNaN(d)) return "";
    const c = d * 100, a = Math.abs(c);
    if (a < 0.05) return `<span class="flat">unchanged</span>`;
    const t = (Math.abs(a - Math.round(a)) < 0.05 ? a.toFixed(0) : a.toFixed(1)) + "¢";
    return `<span class="${c > 0 ? "up" : "dn"}">${c > 0 ? "▲" : "▼"} ${t}</span>`;
  }
  const money = (x) => (x == null ? "—" : (x >= 0 ? "+$" : "−$") + Math.abs(x).toFixed(2));
  const moveText = (m, ticks) => (ticks == null ? "—" : sgn(ticks, 1, true) + (m.tick === 0.01 ? "¢" : " ticks"));
  const niceName = (s) => (s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  // the LIVE banner already says "LIVE" / "SIMULATED LIVE", so the cards show just the event name
  const cardTitle = (E) => (E.live ? String(E.tl.title || "").replace(/^(SIMULATED )?LIVE · /, "") : E.tl.title);
  const lastName = (s) => (niceName(s) || "the speaker").split(" ").pop();
  const displayLabel = (m) => String(m.label || m.id).replace(/\s*\(Polymarket\)\s*$/, "");
  function marketSub(m) {
    const v = m.venue === "kalshi" ? "Kalshi" : "Polymarket";
    const ex = m.exposure === "direct" ? "tied directly to the decision" : m.exposure === "partial" ? "only indirectly related to the decision" : m.exposure;
    return `${v} · ${ex}`;
  }
  function stanceWords(s) {
    if (s == null) return "";
    if (s <= -0.75) return "very dovish";
    if (s <= -0.25) return "leaning dovish";
    if (s < 0.25) return "neutral";
    if (s < 0.75) return "leaning hawkish";
    return "very hawkish";
  }

  // ---------- loading ----------
  async function fetchJson(path, optional) {
    try {
      const r = await fetch(path, { cache: "no-store" });
      if (!r.ok) throw new Error(`${r.status} ${path}`);
      return await r.json();
    } catch (e) {
      if (optional) return null;
      throw e;
    }
  }

  async function loadEvent(id, fresh) {
    if (state.events[id] && !fresh) return state.events[id];
    const base = DATA_ROOT + id + "/";
    const [tl, an, ev, bt] = await Promise.all([
      fetchJson(base + "timeline.json"),
      fetchJson(base + "analysis.json", true),
      fetchJson(base + "evidence.json", true),
      id === "live" ? null : fetchJson(base + "backtest.json", true),
    ]);
    const prepared = prep(tl, an, ev);
    prepared.desk = prepDesk(tl.desk || (bt && backtestDesk(bt)), prepared);
    state.events[id] = prepared;
    return prepared;
  }

  let prepCount = 0;
  function prep(tl, an, ev) {
    const t0 = parseT(tl.t0);
    const ws = parseT(tl.window.start), we = parseT(tl.window.end);
    const evById = {};
    for (const e of (ev && ev.markets) || []) evById[e.market] = e;

    const markets = tl.markets.map((m) => ({
      ...m,
      a: (an && an.markets && an.markets[m.id]) || null,
      evd: evById[m.id] || null,
      q: [],
      tr: [],
    }));
    const byId = Object.fromEntries(markets.map((m) => [m.id, m]));
    for (const q of tl.quotes) {
      const m = byId[q.market];
      if (m) m.q.push({ t: parseT(q.t), bid: q.bid, ask: q.ask, mid: q.mid, imp: !!q.imputed, vol: q.volume });
    }
    for (const t of tl.trades) {
      const m = byId[t.market];
      if (m) m.tr.push({ t: parseT(t.t), p: t.price, s: t.size, side: t.taker_side });
    }
    for (const m of markets) {
      m.q.sort((a, b) => a.t - b.t);
      m.tr.sort((a, b) => a.t - b.t);
      let lo = Infinity, hi = -Infinity;
      for (const q of m.q) for (const v of [q.bid, q.ask, q.mid]) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      for (const t of m.tr) { lo = Math.min(lo, t.p); hi = Math.max(hi, t.p); }
      if (m.a && m.a.pre_band) { lo = Math.min(lo, ...m.a.pre_band); hi = Math.max(hi, ...m.a.pre_band); }
      if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
      m.lo = lo; m.hi = hi;
      m.status = m.a ? m.a.status : null;
      // Only surface an alarm when role 2's verdict backs it; a detector blip on an illiquid
      // or expected-no-reaction market is noise by their own definition.
      m.alarm = m.a && m.a.alarm_t && ALARM_STATUSES.has(m.a.status) ? parseT(m.a.alarm_t) : null;
      m.isKalshi = m.venue === "kalshi";
    }
    // Order inside each group: Kalshi before Polymarket, then size of move.
    const rank = (m) => (m.status ? STATUS_ORDER.indexOf(m.status) : 9);
    markets.sort((a, b) => rank(a) - rank(b) || (b.isKalshi - a.isKalshi) || Math.abs(b.a?.move_ticks || 0) - Math.abs(a.a?.move_ticks || 0));

    // Headline market: biggest Kalshi reaction; while live, the first alarm; else the biggest move.
    const kal = markets.filter((m) => m.isKalshi && m.a);
    const headline =
      kal.filter((m) => m.status === "reacted").sort((a, b) => Math.abs(b.a.move_ticks) - Math.abs(a.a.move_ticks))[0] ||
      kal.filter((m) => m.status === "alarm").sort((a, b) => a.alarm - b.alarm)[0] ||
      kal.filter((m) => m.a.move_ticks).sort((a, b) => Math.abs(b.a.move_ticks) - Math.abs(a.a.move_ticks))[0] ||
      kal.slice().sort((a, b) => b.tr.length - a.tr.length)[0] ||   // nothing has moved: most-traded contract
      markets[0];

    const sp = tl.speech || {};
    // A prepared text is released whole at T0; a live transcript arrives passage by passage,
    // each with its own time `t`.
    const paragraphs = (sp.paragraphs || []).map((p) => ({ ...p, _t: p.t ? parseT(p.t) : t0 }));
    const scored = paragraphs.filter((p) => p.relevant && p.z != null);
    const top =
      paragraphs.find((p) => p.id === sp.top_paragraph && p.z != null) ||
      scored.slice().sort((a, b) => Math.abs(b.z) - Math.abs(a.z))[0] ||
      null;
    const live = tl.live || null;
    const liveNow = live ? parseT(live.now) : null;
    // While live, leave room to the right of "now" so the line grows into the chart.
    const xe = live ? Math.max(we, t0 + 30 * MIN, (liveNow || we) + 10 * MIN) : we;
    return {
      tl, an, t0, ws, we, xe, markets, byId, headline, paragraphs, top, baseline: sp.baseline || null,
      hasAnalysis: !!an, live, liveNow, overTime: !!sp.arrives_over_time, rev: ++prepCount,
    };
  }

  // ---------- desk: orders and P&L (all numbers come from the files; nothing is computed here) ----------
  function backtestDesk(bt) {
    return { mode: "backtest", backtest: true, trades: bt.trades || [], skipped: bt.skipped || [],
             learned_from: bt.learned_from || [], total_pnl: bt.total_pnl, capital: bt.capital,
             note: `rules learned from ${(bt.learned_from || []).join(", ") || "no other events"} only (leave-one-out), real Kalshi prices, fees included` };
  }

  function prepDesk(d, E) {
    if (!d) return null;
    const trades = [], skipped = (d.skipped || []).map((x) => ({ label: x.label || x.market, why: x.why }));
    for (const t of d.trades || []) {
      if (t.skipped) {
        skipped.push({ label: t.title || t.market, why: String(t.status || "").replace(/^skipped: (playbook: )?/, ""), t: parseT(t.t) });
        continue;
      }
      const mid = t.market_id || t.market;
      trades.push({
        t: parseT(t.t || t.entry_t), mid, label: (E.byId[mid] || {}).label || t.label || t.title || t.market,
        side: t.side, qty: t.qty, cost: t.cost, exitT: t.exit_t ? parseT(t.exit_t) : null,
        pnlFinal: t.pnl != null ? t.pnl : t.pnl_at_exit, pnlNow: t.pnl_now, value: t.value,
        status: t.status, sideFixed: t.side_fixed || (t.playbook && t.playbook.side_fixed), fees: t.fees,
      });
    }
    return { mode: d.mode, backtest: !!d.backtest, note: d.note, trades, skipped, total: d.total_pnl, capital: d.capital };
  }

  // Latest quote whose candle has ENDED by time t (quote.t is the candle end), so the replay never peeks ahead.
  function quoteAt(m, t) {
    const q = m.q;
    let lo = 0, hi = q.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (q[mid].t <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans >= 0 ? q[ans] : null;
  }

  // When the replay may show a market's verdict and final move. A reaction with an alarm is shown
  // when its alarm passes; Polymarket (indicative, measured T0+75..90 min by role 2) only near the
  // end of that window; everything else from T0. Before that the market just says "watching".
  function verdictAt(E, m) {
    if (m.alarm) return m.alarm;
    if (m.indicative) return E.t0 + 75 * MIN;
    return E.t0;
  }

  // ---------- SVG helpers ----------
  const svgEl = (w, h, inner) => `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none">${inner}</svg>`;

  // Step path: a candle-end value holds until the next candle ends.
  function stepPath(points, x, y, until) {
    let d = "", prevY = null;
    for (const p of points) {
      if (p.t > until) break;
      const px = x(p.t), py = y(p.v);
      if (prevY == null) d += `M${px.toFixed(1)},${py.toFixed(1)}`;
      else d += `H${px.toFixed(1)}V${py.toFixed(1)}`;
      prevY = py;
    }
    if (prevY != null) d += `H${x(until).toFixed(1)}`;
    return d;
  }

  function bandPath(qs, x, y, until) {
    const pts = qs.filter((q) => q.t <= until && q.bid != null && q.ask != null);
    if (pts.length < 1) return "";
    let top = "", bot = [];
    pts.forEach((q, i) => {
      const xa = x(q.t), xb = x(i + 1 < pts.length ? pts[i + 1].t : until);
      top += `${i ? "L" : "M"}${xa.toFixed(1)},${y(q.ask).toFixed(1)}L${xb.toFixed(1)},${y(q.ask).toFixed(1)}`;
      bot.push(`L${xb.toFixed(1)},${y(q.bid).toFixed(1)}L${xa.toFixed(1)},${y(q.bid).toFixed(1)}`);
    });
    return top + bot.reverse().join("") + "Z";
  }

  function yScale(lo, hi, tick, top, bottom, minTicks = 6) {
    let span = hi - lo;
    const minSpan = (tick || 0.01) * minTicks;
    if (span < minSpan) { const c = (hi + lo) / 2; lo = c - minSpan / 2; hi = c + minSpan / 2; span = minSpan; }
    lo -= span * 0.08; hi += span * 0.08;
    const f = (v) => bottom - ((v - lo) / (hi - lo)) * (bottom - top);
    f.lo = lo; f.hi = hi;
    return f;
  }

  function yTicks(y, n = 5) {
    const span = y.hi - y.lo;
    const raw = span / n;
    const steps = [0.005, 0.01, 0.02, 0.025, 0.05, 0.1, 0.2, 0.25];
    const step = steps.find((s) => s >= raw) || 0.25;
    const out = [];
    for (let v = Math.ceil(y.lo / step) * step; v <= y.hi + 1e-9; v += step) out.push(+v.toFixed(4));
    return out;
  }

  // ---------- main chart (focused market) ----------
  function drawMainChart(E, m, now) {
    const wrap = $("chartWrap");
    if (!wrap) return;
    const W = Math.max(320, wrap.clientWidth), H = Math.max(240, wrap.clientHeight);
    const L = 54, R = 24, B = 28;
    const x = (t) => L + ((t - E.ws) / (E.xe - E.ws)) * (W - L - R);
    const until = Math.min(now, E.we);
    const mono = "ui-monospace,Menlo,monospace";
    let g = "";

    // Labelled marks: the release, plus analysis.json's reaction / halfway / 90% times. Their labels
    // sit in lanes above the plot so they never cover the price line; lanes are worked out from every
    // mark (revealed or not) so the plot doesn't resize as the replay reveals them.
    const marks = [{ t: E.t0, color: "#e8f0f3", label: `${E.overTime ? "Speech starts" : "Text released"} ${fmtEtShort(E.t0)} ET`, dash: "4 3", always: true }];
    if (m.alarm && !m.indicative) marks.push({ t: m.alarm, color: "#84f04f", label: `Reaction ${relMin(m.alarm - E.t0)}`, dash: "0" });
    if (m.a && m.a.t50_min != null && !m.indicative) marks.push({ t: E.t0 + m.a.t50_min * MIN, color: "#9bafb8", label: `Halfway +${m.a.t50_min}m`, dash: "2 3" });
    if (m.a && m.a.t90_min != null && !m.indicative) marks.push({ t: E.t0 + m.a.t90_min * MIN, color: "#9bafb8", label: `90% done +${m.a.t90_min}m`, dash: "2 3" });
    for (const k of marks) {
      k.w = k.label.length * 6.4 + 14;
      k.x0 = x(k.t) + 6;
      if (k.x0 + k.w > W - R) k.x0 = x(k.t) - 6 - k.w;
    }
    const laneEnds = [];
    for (const k of marks.slice().sort((a, b) => a.x0 - b.x0)) {
      let lane = laneEnds.findIndex((end) => end + 6 < k.x0);
      if (lane < 0) lane = laneEnds.length;
      laneEnds[lane] = k.x0 + k.w;
      k.lane = lane;
    }
    const T = 10 + Math.max(1, laneEnds.length) * 22;
    const y = yScale(m.lo, m.hi, m.tick, T, H - B);
    const pill = (k) => {
      const py = 4 + k.lane * 22;
      return `<rect x="${k.x0.toFixed(1)}" y="${py}" width="${k.w.toFixed(1)}" height="18" rx="4" fill="rgba(11,17,22,.9)" stroke="${k.color}" stroke-opacity=".6"/>` +
        `<text x="${(k.x0 + 7).toFixed(1)}" y="${py + 13}" fill="${k.color}" font-size="11.5">${esc(k.label)}</text>`;
    };

    // grid + y labels
    for (const v of yTicks(y)) {
      g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#1d2a33"/>`;
      g += `<text x="${L - 8}" y="${y(v) + 4}" fill="#9bafb8" font-size="12" text-anchor="end" font-family="${mono}">${cents(v)}</text>`;
    }
    // x labels in ET: every 15 min when there's room, else 30 or 60
    const pxPer15 = (W - L - R) / ((E.xe - E.ws) / (15 * MIN));
    const stepMin = pxPer15 >= 56 ? 15 : pxPer15 * 2 >= 56 ? 30 : 60;
    for (let t = Math.ceil(E.ws / (stepMin * MIN)) * stepMin * MIN; t <= E.xe; t += stepMin * MIN) {
      g += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" stroke="#16222a"/>`;
      g += `<text x="${x(t)}" y="${H - 8}" fill="#8aa0aa" font-size="12" text-anchor="middle" font-family="${mono}">${fmtEtShort(t)}</text>`;
    }

    // pre-event noise band from analysis (explained in the legend)
    if (m.a && m.a.pre_band) {
      const [a, b] = m.a.pre_band;
      const yt = y(Math.max(a, b)), yb = y(Math.min(a, b));
      g += `<rect x="${L}" y="${yt - 1}" width="${W - L - R}" height="${Math.max(2, yb - yt + 2)}" fill="rgba(142,162,171,.14)"/>`;
    }

    // bid-ask band + midpoint
    const color = m.isKalshi ? "#22d3ee" : "#f0b429";
    if (m.isKalshi) g += `<path d="${bandPath(m.q, x, y, until)}" fill="rgba(34,211,238,.15)"/>`;
    const mids = m.q.filter((q) => q.mid != null).map((q) => ({ t: q.t, v: q.mid }));
    g += `<path d="${stepPath(mids, x, y, until)}" fill="none" stroke="${color}" stroke-width="2.4" ${m.indicative ? 'stroke-dasharray="6 4"' : ""}/>`;

    // trades, sized by contracts
    const maxS = Math.max(1, ...m.tr.map((t) => t.s));
    for (const t of m.tr) {
      if (t.t > until) break;
      const r = 1.8 + 6 * Math.sqrt(t.s / maxS);
      g += `<circle cx="${x(t.t).toFixed(1)}" cy="${y(t.p).toFixed(1)}" r="${r.toFixed(1)}" fill="${t.side === "no" ? "rgba(239,91,91,.55)" : "rgba(132,240,79,.55)"}"/>`;
    }

    // marks: the release is always shown, analysis.json's times only once the replay clock reaches them
    for (const k of marks) {
      if (!k.always && k.t > now) continue;
      g += `<line x1="${x(k.t).toFixed(1)}" x2="${x(k.t).toFixed(1)}" y1="${4 + k.lane * 22 + 18}" y2="${H - B}" stroke="${k.color}" stroke-dasharray="${k.dash}" opacity=".85"/>`;
      g += pill(k);
    }

    // live transcript: a tick on the top edge of the plot for every passage that was a surprise for this speaker
    if (E.overTime) {
      for (const p of E.paragraphs) {
        if (p._t > now || !p.relevant || p.z == null || Math.abs(p.z) < 2) continue;
        const c = p.z < 0 ? "#4f9dff" : "#f0b429";
        g += `<path d="M${x(p._t) - 5},${T - 2}L${x(p._t) + 5},${T - 2}L${x(p._t)},${T + 7}Z" fill="${c}"><title>z ${sgn(p.z, 1)}: ${esc((p.summary || p.text || "").slice(0, 80))}</title></path>`;
      }
    }

    // desk orders on this contract: entry (and planned exit) in YES-price terms
    for (const t of (E.desk ? E.desk.trades : [])) {
      if (t.mid !== m.id || t.t > now || t.cost == null) continue;
      const side = String(t.side || "");
      const yp = side === "yes" ? t.cost : 1 - t.cost;
      const cx = x(t.t), cy = y(yp);
      g += `<path d="M${cx},${cy - 8}L${cx + 7},${cy}L${cx},${cy + 8}L${cx - 7},${cy}Z" fill="#ffffff" stroke="#0b1116" stroke-width="1.5"><title>BUY ${esc(side.toUpperCase())} ×${esc(t.qty)} @ ${cents(t.cost)}</title></path>`;
      g += `<text x="${cx + 10}" y="${cy - 10}" fill="#ffffff" font-size="11.5" font-weight="600">BUY ${esc(side.toUpperCase())} ×${esc(t.qty)}</text>`;
      if (t.exitT && now >= t.exitT && t.value != null) {
        const ex = x(t.exitT), ey = y(side === "yes" ? t.value : 1 - t.value);
        g += `<line x1="${cx}" y1="${cy}" x2="${ex}" y2="${ey}" stroke="#ffffff" stroke-dasharray="3 3" opacity=".7"/>`;
        g += `<rect x="${ex - 5}" y="${ey - 5}" width="10" height="10" fill="#ffffff"/>`;
        g += `<text x="${ex + 8}" y="${ey + 4}" fill="#ffffff" font-size="11.5" font-weight="600">exit ${money(t.pnlFinal)}</text>`;
      }
    }

    // playhead: a line at "now" and a dot on the price
    if (now >= E.ws && now <= E.xe) {
      g += `<line x1="${x(now)}" x2="${x(now)}" y1="${T}" y2="${H - B}" stroke="#22d3ee" opacity=".35"/>`;
      const q = quoteAt(m, now);
      if (q && q.mid != null) g += `<circle cx="${x(until).toFixed(1)}" cy="${y(q.mid).toFixed(1)}" r="5" fill="${color}" stroke="#0b1116" stroke-width="2"/>`;
    }

    wrap.innerHTML = svgEl(W, H, g);
  }

  function sparkline(E, m, now, w = 260, h = 24) {
    const x = (t) => ((t - E.ws) / (E.xe - E.ws)) * w;
    const y = yScale(m.lo, m.hi, m.tick, 2, h - 2, 4);
    const mids = m.q.filter((q) => q.mid != null).map((q) => ({ t: q.t, v: q.mid }));
    const until = Math.min(now, E.we);
    const c = m.isKalshi ? "#22d3ee" : "#f0b429";
    const ne = 'vector-effect="non-scaling-stroke"';
    let g = `<line x1="${x(E.t0)}" x2="${x(E.t0)}" y1="0" y2="${h}" stroke="#3a4b56" stroke-dasharray="2 2" ${ne}/>`;
    g += `<path d="${stepPath(mids, x, y, until)}" fill="none" stroke="${c}" stroke-width="1.6" ${ne} ${m.indicative ? 'stroke-dasharray="4 3"' : ""}/>`;
    if (m.alarm && m.alarm <= now && !m.indicative) g += `<line x1="${x(m.alarm)}" x2="${x(m.alarm)}" y1="0" y2="${h}" stroke="#84f04f" stroke-width="1.6" ${ne}/>`;
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${g}</svg>`;
  }

  // ---------- 1 · speech ----------
  const tint = (z) => {
    if (z == null) return "var(--line-2)";
    const k = Math.min(1, Math.abs(z) / 3);
    return z < 0 ? `rgba(79,157,255,${0.25 + 0.75 * k})` : `rgba(240,180,41,${0.25 + 0.75 * k})`;
  };

  function highlightKey(p) {
    let text = esc(p.text);
    if (p.key_sentence) {
      const k = esc(p.key_sentence);
      if (text.includes(k)) text = text.replace(k, () => `<mark class="key">${k}</mark>`);
    }
    return text;
  }

  // Where a speech's policy passages sit relative to the speaker's usual stance.
  function gaugeSvg(E, top, list) {
    const b = E.baseline;
    const W = 400, H = 98, L = 14, R = 14, axisY = 70;
    const clamp = (s) => Math.max(-1, Math.min(1, s));
    const x = (s) => L + ((clamp(s) + 1) / 2) * (W - L - R);
    let g = "";
    if (b && b.mean != null && b.stdev != null) {
      const band = (k, fill) => `<rect x="${x(b.mean - k * b.stdev).toFixed(1)}" y="34" width="${(x(b.mean + k * b.stdev) - x(b.mean - k * b.stdev)).toFixed(1)}" height="${axisY - 34}" rx="3" fill="${fill}"/>`;
      g += band(2, "rgba(142,162,171,.16)") + band(1, "rgba(142,162,171,.30)");
      g += `<line x1="${x(b.mean).toFixed(1)}" x2="${x(b.mean).toFixed(1)}" y1="34" y2="${axisY}" stroke="#9bafb8" stroke-dasharray="3 3"/>`;
    }
    g += `<line x1="${L}" x2="${W - R}" y1="${axisY}" y2="${axisY}" stroke="#3a4b56"/>`;
    // the other policy passages: dots stacked where stances coincide
    const stack = {};
    for (const p of list || E.paragraphs) {
      if (!p.relevant || p.stance == null || p === top) continue;
      const key = Math.round(clamp(p.stance) / 0.04);
      const k = (stack[key] = (stack[key] || 0) + 1) - 1;
      const row = k % 5, col = Math.floor(k / 5);
      const dx = col === 0 ? 0 : (col % 2 ? 1 : -1) * Math.ceil(col / 2) * 6;
      g += `<circle cx="${(x(p.stance) + dx).toFixed(1)}" cy="${(axisY - 6 - row * 6.5).toFixed(1)}" r="2.8" fill="${p.stance < 0 ? "#4f9dff" : "#f0b429"}" opacity=".75"/>`;
    }
    // the most unusual passage
    const tx = x(top.stance);
    const anchor = tx < 70 ? "start" : tx > W - 70 ? "end" : "middle";
    const tcol = top.stance < 0 ? "#4f9dff" : "#f0b429";
    g += `<line x1="${tx.toFixed(1)}" x2="${tx.toFixed(1)}" y1="26" y2="${axisY}" stroke="#e8f0f3" stroke-width="1.5"/>`;
    g += `<circle cx="${tx.toFixed(1)}" cy="22" r="6" fill="${tcol}" stroke="#e8f0f3" stroke-width="2"/>`;
    g += `<text x="${(anchor === "start" ? tx - 6 : anchor === "end" ? tx + 6 : tx).toFixed(1)}" y="10" fill="#e8f0f3" font-size="12" font-weight="600" text-anchor="${anchor}">this passage</text>`;
    // axis labels
    g += `<text x="${L}" y="90" fill="#9bafb8" font-size="12" text-anchor="start">← more dovish</text>`;
    g += `<text x="${x(0).toFixed(1)}" y="90" fill="#9bafb8" font-size="12" text-anchor="middle">neutral</text>`;
    g += `<text x="${W - R}" y="90" fill="#9bafb8" font-size="12" text-anchor="end">more hawkish →</text>`;
    return `<svg class="gauge" viewBox="0 0 ${W} ${H}" role="img" aria-label="Stance of this speech's policy passages compared with the speaker's usual range">${g}</svg>`;
  }

  // "Scored −1.00 (very dovish) against Bowman's usual +0.15 …": every number is from timeline.json.
  function whyHtml(E, top, last) {
    const b = E.baseline;
    const dir = top.z < 0 ? "dovish" : "hawkish";
    return b && b.mean != null
      ? `Scored <b class="num">${sgn(top.stance, 2)}</b> (${stanceWords(top.stance)}) against ${esc(last)}'s usual <b class="num">${sgn(b.mean, 2)}</b>${b.n != null ? ` (${esc(b.n)} past passages)` : ""}: <b class="num">${Math.abs(top.z).toFixed(1)} standard deviations</b> more ${dir} than normal (z = ${sgn(top.z, 1)}).`
      : `Scored <b class="num">${sgn(top.stance, 2)}</b> (${stanceWords(top.stance)}); z = ${sgn(top.z, 1)} against ${esc(last)}'s own history.`;
  }

  function gaugeBlock(E, top, last, list, dotsLabel) {
    const b = E.baseline;
    if (!(b && b.mean != null && b.stdev != null)) return "";
    return `<div class="gauge-col">
        <div class="label">How it compares with ${esc(last)}'s usual stance</div>
        ${gaugeSvg(E, top, list)}
        <div class="gauge-legend">
          <span><i class="sw"></i>usual range (within 1 and 2 standard deviations)</span>
          <span><i class="dt"></i>${esc(dotsLabel)}</span>
        </div>
      </div>`;
  }

  // A prepared text, released whole at T0.
  function speechHtml(E, released) {
    const tl = E.tl, sp = tl.speech || {};
    const speaker = niceName(tl.speaker);
    const last = lastName(tl.speaker);
    let h = `<div class="card-head"><span class="step">1</span><h2>What was said</h2><span class="meta">${esc(cardTitle(E))}</span></div>`;
    if (!released) {
      return h + `<div class="embargo">
        <div class="embargo-main"><b>The text isn't public yet.</b> ${esc(speaker || "The speaker")}'s prepared remarks come out at <b class="num">${fmtEtShort(E.t0)} ET</b> (${fmtUtc(E.t0)} UTC).</div>
        <div class="embargo-clock"><span class="num" id="embargoClock"></span><small>until release</small></div>
        <div class="embargo-sub">Until then, markets are trading on expectations only. ${E.live ? "The text appears here the moment it's released." : "Press ▶ Play to watch the moment it lands."}</div>
      </div>`;
    }
    const src = sp.source_url;
    h += `<div class="released-line"><span class="dot"></span><span>Text released <b class="num" id="releasedAgo"></b></span>${src ? `<a href="${esc(src)}" target="_blank" rel="noopener">Read it on federalreserve.gov ↗</a>` : ""}</div>`;
    const ps = E.paragraphs, top = E.top;
    if (!ps.length || !top) {
      return h + `<div class="pending-note">Paragraph scores aren't in <span class="mono">timeline.json</span> yet. <span class="mono">surprise.py</span> fills <span class="mono">speech.paragraphs</span> (stance, and z vs. the speaker's own baseline). Nothing is shown here until then, so no number on this screen is invented.</div>`;
    }
    const hasKey = !!(top.key_sentence && top.text.includes(top.key_sentence));
    h += `<div class="speech-body">
      <div class="quote-col">
        <div class="label">Most unusual passage for ${esc(last)}</div>
        <blockquote class="keyq ${top.z < 0 ? "dove" : "hawk"}">“${esc(hasKey ? top.key_sentence : top.text)}”</blockquote>
        ${hasKey ? `<button class="linkbtn" data-act="context-toggle" aria-expanded="${state.contextOpen}">${state.contextOpen ? "Hide" : "Show"} the paragraph around it ${state.contextOpen ? "▴" : "▾"}</button>${state.contextOpen ? `<p class="paragraph-ctx">${highlightKey(top)}</p>` : ""}` : ""}
        <div class="why">${whyHtml(E, top, last)}</div>
      </div>
      ${gaugeBlock(E, top, last, ps, "this speech's policy paragraphs")}
    </div>`;
    const nRel = ps.filter((p) => p.relevant).length;
    const nOther = ps.length - nRel;
    h += `<button class="linkbtn" data-act="speech-toggle" aria-expanded="${state.speechOpen}">${state.speechOpen ? "Hide" : "Read"} the full speech · ${nRel} paragraphs about policy ${state.speechOpen ? "▴" : "▾"}</button>`;
    if (state.speechOpen) {
      const list = ps.filter((p) => state.showAllParas || p.relevant);
      h += `<div class="fulltext">`;
      if (nOther) h += `<button class="linkbtn" data-act="paras-toggle">${state.showAllParas ? "Hide" : "Also show"} ${nOther} paragraphs that aren't about policy</button>`;
      h += `<div class="para-key">Blue = more dovish, amber = more hawkish; darker = further from ${esc(last)}'s usual.</div>`;
      h += list.map((p) => {
        const z = p.relevant && p.z != null ? `z ${sgn(p.z, 1)}` : "not about policy";
        return `<div class="para ${p.relevant ? "" : "irrelevant"} ${p === top ? "top" : ""}" style="border-left-color:${p.relevant ? tint(p.z) : "var(--line)"}" title="${p.stance != null ? `stance ${sgn(p.stance, 2)} (${stanceWords(p.stance)})` : ""}"><span class="z">${esc(z)}</span>${highlightKey(p)}</div>`;
      }).join("");
      h += `</div>`;
    }
    return h;
  }

  // A live transcript: passages arrive over time, each scored as it's heard (tradio/live.py).
  function liveSpeechHtml(E, heard) {
    const sp = E.tl.speech || {};
    const who = E.tl.speaker || sp.speaker;
    const last = lastName(who);
    let h = `<div class="card-head"><span class="step">1</span><h2>What's being said</h2><span class="meta">${esc(cardTitle(E))}</span></div>
      <div class="live-meta">Transcribed and scored as it's spoken by MarketPulse (<span class="mono">live.py</span>), z against ${esc(last)}'s own history${sp.baseline && sp.baseline.n != null ? ` (n = ${esc(sp.baseline.n)})` : ""}.${sp.status ? ` Status: <b>${esc(sp.status)}</b>.` : ""}</div>`;
    if (!heard.length) {
      return h + `<div class="pending-note">No speech passages yet. Start the live scorer on the laptop:<br><span class="mono">python live.py --speaker &lt;name&gt; --no-prewarm --mode dry</span><br>Passages appear here as they're scored, newest on top. The market chart updates either way.</div>`;
    }
    const scored = heard.filter((p) => p.relevant && p.z != null);
    const surprises = scored.filter((p) => Math.abs(p.z) >= 2);
    const top = scored.slice().sort((a, b) => Math.abs(b.z) - Math.abs(a.z))[0];
    h += `<div class="released-line"><span class="dot"></span><span><b class="num">${heard.length}</b> passages heard · <b class="num">${surprises.length}</b> surprise${surprises.length === 1 ? "" : "s"} (|z| ≥ 2)</span></div>`;
    if (top) {
      h += `<div class="speech-body">
        <div class="quote-col">
          <div class="label">Biggest surprise so far · ${fmtEt(top._t)} ET</div>
          <blockquote class="keyq ${top.z < 0 ? "dove" : "hawk"}">“${esc(top.text)}”</blockquote>
          <div class="why">${whyHtml(E, top, last)}</div>
        </div>
        ${gaugeBlock(E, top, last, heard, "passages heard so far")}
      </div>`;
    }
    h += `<div class="label live-list-label">Transcript, newest first</div>
      <div class="para-key">Blue = more dovish, amber = more hawkish, grey = reporter or not scored.</div>
      <div class="fulltext">` +
      heard.slice().sort((a, b) => b._t - a._t).map((p) => {
        const z = p.z != null ? `z ${sgn(p.z, 1)}` : (p.role && p.role !== "speaker" ? p.role : "not scored");
        return `<div class="para ${p.relevant ? "" : "irrelevant"} ${p === top ? "top" : ""}" style="border-left-color:${p.relevant ? tint(p.z) : "var(--line)"}" title="${p.stance != null ? `stance ${sgn(p.stance, 2)} (${stanceWords(p.stance)})` : ""}"><span class="z">${esc(z)}</span><span class="t">${fmtEt(p._t)} ET</span> ${esc(p.text)}</div>`;
      }).join("") + `</div>`;
    return h;
  }

  function renderSpeech(E, now) {
    const pane = $("speechCard");
    if (!pane) return;
    if (E.overTime) {
      const heard = E.paragraphs.filter((p) => p._t <= now);
      const sig = ["live", heard.length, E.paragraphs.length, (E.tl.speech || {}).status].join();
      if (state.sig.speech !== sig) {
        state.sig.speech = sig;
        pane.innerHTML = liveSpeechHtml(E, heard);
      }
      return;
    }
    const released = now >= E.t0;
    // re-render only when what's shown changes, so the open transcript keeps its scroll position
    const sig = [released, E.paragraphs.length, E.top && E.top.id, state.speechOpen, state.showAllParas, state.contextOpen].join();
    if (state.sig.speech !== sig) {
      state.sig.speech = sig;
      pane.innerHTML = speechHtml(E, released);
    }
    const cd = $("embargoClock");
    if (cd) cd.textContent = fmtRel(now - E.t0);
    const ago = $("releasedAgo");
    if (ago) ago.textContent = agoText(now - E.t0);
  }

  // ---------- 2 · focused market ----------
  function legendHtml(E, m) {
    const items = [];
    if (m.isKalshi) {
      items.push(`<span><i style="background:#22d3ee"></i>price (midpoint of buy/sell offers)</span>`);
      items.push(`<span><i style="background:rgba(34,211,238,.28)"></i>buy–sell gap</span>`);
      items.push(`<span><i class="dot" style="background:rgba(132,240,79,.75)"></i><i class="dot" style="background:rgba(239,91,91,.75)"></i>trades (green Yes · red No · bigger = more contracts)</span>`);
    } else {
      items.push(`<span><i style="background:#f0b429"></i>Polymarket price (indicative, dashed)</span>`);
    }
    if (m.a && m.a.pre_band) items.push(`<span><i style="background:rgba(142,162,171,.3)"></i>usual range before the speech</span>`);
    if (!m.indicative && m.a && (m.alarm || m.a.t50_min != null)) items.push(`<span><i style="background:#84f04f"></i>reaction detected · <i style="background:#9bafb8;width:3px"></i>halfway / 90% of the final move</span>`);
    if (E.desk && E.desk.trades.some((t) => t.mid === m.id)) items.push(`<span><i class="diamond"></i>desk order · <i class="sq"></i>its planned exit</span>`);
    if (E.overTime) items.push(`<span><i class="tri"></i>a passage that was a surprise for this speaker (|z| ≥ 2)</span>`);
    return items.join("");
  }
  const sourceNote = (E, m) => `${m.isKalshi ? (E.live && E.live.kind === "live" ? "Live Kalshi tape · closed 1-minute candles" : "Real Kalshi prices · 1-minute candles") : "Polymarket history"} · times in ET`;

  function verdictHtml(E, m, now) {
    const a = m.a;
    const startWord = E.overTime ? "the speech starts" : "the text is released";
    if (!a) return `<span class="chip watching">no analysis</span><span class="why-text">analysis.json isn't available for this event, so there is no verdict to show.</span>`;
    if (now < E.t0) return `<span class="chip watching">before the speech</span><span class="why-text">The verdict appears once the clock reaches the moment ${startWord}.</span>`;
    if (now < verdictAt(E, m)) {
      return `<span class="chip watching">watching…</span><span class="why-text">${m.indicative
        ? "Polymarket prices are indicative, so this market gets no timing claims: its verdict appears near the end of the window."
        : "Waiting to see whether this price leaves its pre-speech range and stays out."}</span>`;
    }
    const extra = m.indicative ? " Polymarket price history changed depending on the query window, so it's shown for direction only." : "";
    const facts = [];
    if (m.alarm && !m.indicative) facts.push(`Reaction detected: <b>${Math.round((m.alarm - E.t0) / MIN)} min after ${E.overTime ? "the start" : "release"}</b>`);
    if (a.t50_min != null && !m.indicative && now >= E.t0 + a.t50_min * MIN) facts.push(`Halfway there: <b>${a.t50_min} min</b>`);
    if (a.t90_min != null && !m.indicative && now >= E.t0 + a.t90_min * MIN) facts.push(`90% of the move: <b>${a.t90_min} min</b>`);
    if (a.move_ticks != null) facts.push(`Move over the analysed window: <b>${moveText(m, a.move_ticks)}</b>`);
    if (a.liquidity) facts.push(`Trading before the speech: <b>${esc(LIQ_TEXT[a.liquidity] || a.liquidity)}</b>`);
    return `<span class="chip ${esc(a.status)}" title="${esc(STATUS_HELP[a.status] || "")}">${esc(STATUS_TEXT[a.status] || a.status)}</span>` +
      `<span class="why-text">${esc(a.reason || "")}${esc(extra)}</span>` +
      (facts.length ? `<div class="facts">${facts.map((f) => `<span>${f}</span>`).join("")}</div>` : "");
  }

  function renderMarketHead(E, m, now) {
    const head = $("mkHead");
    if (!head) return;
    const q = quoteAt(m, now), a = m.a;
    const cur = q ? q.mid : null;
    const pre = a ? a.pre_mid : null;
    const after = now >= E.t0;
    const ind = m.indicative ? `<span class="indic-banner">INDICATIVE · DIRECTION ONLY, NO TIMING CLAIMS</span>` : "";
    let price = `<div class="row"><span class="now num">${cur != null ? cents(cur) : "—"}</span>`;
    if (after && cur != null && pre != null) {
      price += `<span class="delta num">${deltaC(cur - pre)}</span></div><span class="before">vs ${cents(pre)} just before the speech</span>`;
    } else {
      price += `</div>${q && q.bid != null ? `<span class="before">bid ${cents(q.bid)} · ask ${cents(q.ask)}</span>` : ""}`;
    }
    head.innerHTML = `
      <div class="mk-id"><div class="mk-title">${esc(displayLabel(m))}</div><div class="mk-sub"><span>${esc(marketSub(m))}</span>${ind}</div></div>
      <div class="mk-price" title="A price is the market's implied chance of this outcome: 22¢ means roughly 22%.">${price}</div>`;
    const evBtn = $("btnEvidence");
    if (evBtn) evBtn.dataset.id = m.id;
    $("mkMeta").textContent = sourceNote(E, m);
    $("legend").innerHTML = legendHtml(E, m);
    // the learner's pre-T0 odds for this contract sit under the verdict (it was made before the event)
    const mp = modelPrediction(E, m);
    $("verdict").innerHTML = verdictHtml(E, m, now) + (mp ? modelLine(mp) : "");
  }

  // The desk: what the playbook traded (live) or would have traded (backtest). Orders, skips and
  // P&L are read from the files; the only sum here is the closed total over the rows shown.
  function renderDesk(E, now) {
    const el = $("desk");
    if (!el) return;
    const d = E.desk;
    $("belowChart").classList.toggle("has-desk", !!d); // verdict and desk sit side by side when both exist
    if (!d) { el.hidden = true; state.sig.desk = ""; return; }
    const mode = d.backtest ? `<span class="chip indic">PLAYBOOK BACKTEST</span>`
      : d.mode === "demo" ? `<span class="chip reacted">KALSHI DEMO · FAKE MONEY</span>`
      : d.mode === "live" ? `<span class="chip candidate_inconsistency">LIVE · REAL MONEY</span>`
      : `<span class="chip watching">DRY RUN</span>`;
    const shown = d.trades.filter((t) => t.t <= now);
    let rows = "";
    for (const t of shown) {
      const closed = t.exitT && now >= t.exitT;
      const pnl = closed && t.pnlFinal != null ? t.pnlFinal : (d.backtest ? null : t.pnlNow);
      const pnlTxt = pnl == null ? `<span class="muted">open${t.exitT ? `, exit ${fmtEtShort(t.exitT)}` : ""}</span>`
        : `<b class="${pnl >= 0 ? "up" : "down"}">${money(pnl)}</b>${closed ? "" : ' <span class="muted">so far</span>'}`;
      rows += `<div class="order" role="button" tabindex="0" data-act="order" data-id="${esc(t.mid)}" title="Show this contract on the chart">
        <span class="mono t">${fmtEtShort(t.t)}</span>
        <span>BUY <b>${esc(String(t.side).toUpperCase())}</b> ×${esc(t.qty)} ${esc(t.label)} @ <span class="num">${cents(t.cost)}</span></span>
        <span class="pnl">${pnlTxt}</span>
        ${t.sideFixed ? `<div class="fix">side corrected by the playbook: ${esc(t.sideFixed)}</div>` : ""}
      </div>`;
    }
    const sk = d.skipped.filter((x) => !x.t || x.t <= now);
    const done = shown.filter((t) => t.exitT && now >= t.exitT && t.pnlFinal != null);
    const total = done.length ? done.reduce((a, t) => a + t.pnlFinal, 0) : null;
    const started = now >= E.t0;
    const html = `
      <div class="desk-head"><b>Desk</b>${mode}<span class="px">${total != null ? `<span class="${total >= 0 ? "up" : "down"}">${money(total)}</span> <span class="muted">closed</span>` : ""}</span></div>
      ${rows || `<div class="muted">${started ? "No orders: the playbook hasn't found a trade." : "Waiting for the speech."}</div>`}
      ${sk.length && started ? `<details ${state.deskSkipsOpen ? "open" : ""}><summary>${sk.length} skipped by the playbook</summary>${sk.map((x) => `<div class="skip"><b>${esc(x.label)}</b>: ${esc(x.why)}</div>`).join("")}</details>` : ""}
      <div class="muted small">${esc(d.note || "")}</div>`;
    // only touch the DOM when the content changes, so the open "skipped" list isn't reset every frame
    if (state.sig.desk !== html) { state.sig.desk = html; el.innerHTML = html; }
    el.hidden = false;
  }

  // ---------- 3 · markets list ----------
  const groupKey = (g) => `${state.view}:${g.id}`;
  function isGroupOpen(g) {
    const k = groupKey(g);
    return k in state.groupOpen ? state.groupOpen[k] : g.open;
  }

  function marketRow(E, m, now, focus) {
    const a = m.a || {};
    const q = quoteAt(m, now);
    const after = now >= E.t0;
    let chip;
    if (!m.status) chip = `<span class="chip watching">no analysis</span>`;
    else if (!after) chip = "";
    else if (now < verdictAt(E, m)) chip = `<span class="chip watching">watching…</span>`;
    else chip = `<span class="chip ${esc(m.status)}">${esc(STATUS_TEXT[m.status] || m.status)}</span>`;
    let fired = false;
    if (m.alarm && now >= m.alarm) {
      const key = state.view + m.id;
      if (!state.fired.has(key)) { state.fired.add(key); fired = now - m.alarm < 2 * MIN; }
    }
    const chg = after && q && a.pre_mid != null ? deltaC(q.mid - a.pre_mid) : "";
    const tags = [m.venue === "kalshi" ? "Kalshi" : "Polymarket"];
    if (m.indicative) tags.push("indicative");
    else if (m.exposure === "partial") tags.push("indirect");
    const isFocus = m.id === focus;
    const mp = modelPrediction(E, m);
    return `<div class="mrow ${isFocus ? "focus" : ""} ${fired ? "fired" : ""}" role="button" tabindex="0" data-act="focus" data-id="${esc(m.id)}" aria-pressed="${isFocus}" title="${esc(m.status && STATUS_HELP[m.status] ? STATUS_HELP[m.status] : "")}">
      <div class="mrow-top"><span class="lab">${esc(displayLabel(m))}</span><span class="tag">${esc(tags.join(" · "))}</span><span class="px num">${q ? cents(q.mid) : "—"}</span></div>
      <div class="mrow-bot">${sparkline(E, m, now)}<span class="chg num">${chg}</span>${chip}</div>
      ${mp ? modelLine(mp) : ""}
    </div>`;
  }

  function renderMarkets(E, now) {
    const host = $("marketsList");
    if (!host) return;
    const minuteKey = Math.floor((now - E.ws) / MIN);
    const released = now >= E.t0;
    const focus = state.focus[state.view];
    const openSig = GROUPS.map((g) => (isGroupOpen(g) ? 1 : 0)).join("");
    const sig = [E.rev, minuteKey, released, focus, openSig].join("|");
    if (state.sig.markets === sig) return;
    state.sig.markets = sig;
    let html = "";
    for (const g of GROUPS) {
      const ms = E.markets.filter((m) => g.match(m));
      if (!ms.length) continue;
      const open = isGroupOpen(g);
      // a collapsed group still shows the market that's on the chart, so the selection never disappears
      const shown = open ? ms : ms.filter((m) => m.id === focus);
      const count = open || !shown.length ? ms.length : `${shown.length} of ${ms.length}`;
      html += `<div class="group ${open ? "open" : ""}">
        <button class="group-head" data-act="group" data-id="${g.id}" aria-expanded="${open}"><span class="chev">▶</span><span class="group-dot" style="background:${g.color}"></span><span>${esc(g.title)}</span><span class="count">${count}</span></button>
        ${g.note ? `<p class="group-note">${esc(g.note)}</p>` : ""}
        ${shown.map((m) => marketRow(E, m, now, focus)).join("")}
      </div>`;
    }
    host.innerHTML = html;
  }

  // ---------- single-event page shell (history replays and the LIVE view) ----------
  function renderReplayShell(E) {
    state.sig = {};
    $("view").innerHTML = `
      <main class="stage">
        <div class="live-banner" id="liveBanner" hidden></div>
        <div class="layout">
          <section class="card" id="speechCard"></section>
          <section class="card" id="marketCard">
            <div class="card-head"><span class="step">2</span><h2>How the market reacted</h2>
              <span class="head-right"><span class="meta" id="mkMeta"></span>
                <button class="btn small" id="btnEvidence" data-act="evidence" title="Open the raw rows behind this market's numbers">See the data ›</button></span></div>
            <div class="mk-head" id="mkHead"></div>
            <div class="chart-stage">
              <div class="chart-wrap" id="chartWrap"></div>
              <button class="cta" id="cta" data-act="cta" hidden>
                <span class="big-play">▶</span>
                <span><b>Watch the replay</b><small>It starts ${Math.round(-START_REL / MIN)} minutes before the text is released</small></span>
              </button>
            </div>
            <div class="legend" id="legend"></div>
            <div class="below-chart" id="belowChart">
              <div class="verdict" id="verdict"></div>
              <div class="desk" id="desk" hidden></div>
            </div>
          </section>
          <aside class="card markets">
            <div class="card-head"><span class="step">3</span><h2>All markets tracked<span class="count">${E.markets.length}</span></h2></div>
            <p class="hint">Click one to put it on the chart.</p>
            <div id="marketsList"></div>
          </aside>
        </div>
      </main>`;
  }

  // LIVE only: is this a real session, a simulated one, or has the feed gone quiet?
  function renderLiveBanner(E) {
    const el = $("liveBanner");
    if (!el) return;
    if (!E.live) { el.hidden = true; return; }
    const sim = E.live.kind === "simulated";
    const age = Date.now() - parseT(E.live.updated_at);
    const stale = !sim && age > STALE_MS;
    const badge = sim
      ? `<span class="chip indic">SIMULATED LIVE · ${esc(E.live.source || "")}</span>`
      : stale ? `<span class="chip candidate_inconsistency">STALE · last update ${Math.round(age / 1000)}s ago, is tradio/live.py running?</span>`
              : `<span class="chip live-chip"><span class="dot"></span>LIVE</span>`;
    const pending = E.markets.some((m) => m.a && m.a.final_status_pending);
    el.innerHTML = `${badge}<b>${esc(E.tl.title)}</b>` +
      (pending ? `<span class="muted">Verdicts so far are provisional; final verdicts come when the 90-minute window closes (T0+90m).</span>` : "") +
      (E.hasAnalysis ? "" : `<span class="chip cant_tell_illiquid">analysis.json missing</span>`);
    el.hidden = false;
  }

  // ---------- compare view ----------
  function renderCompareShell() {
    $("view").innerHTML = `
      <main class="stage">
        <section class="card">
          <div class="card-head"><h2>Bowman vs Waller</h2><span class="meta">Same Kalshi July 2025 contracts · same scale on every chart</span></div>
          <p class="cmp-lede">Both clocks are aligned at the moment each speech text was released. Press ▶ Play and watch the same contracts after each speech.</p>
        </section>
        <div class="compare-grid" id="cmp"></div>
      </main>`;
  }

  function cmpSpeech(E, now) {
    if (now < E.t0) return `<div class="cmp-speech muted">The text isn't public yet.</div>`;
    const top = E.top;
    if (!top) return `<div class="cmp-speech muted">Paragraph scores aren't available yet.</div>`;
    const last = lastName(E.tl.speaker);
    const b = E.baseline;
    const quote = top.key_sentence || top.text.slice(0, 180);
    return `<div class="cmp-speech"><div class="label">Most unusual passage</div><q>${esc(quote)}</q>
      <div class="why">z = <b class="num">${sgn(top.z, 1)}</b> against ${esc(last)}'s own history · stance <b class="num">${sgn(top.stance, 2)}</b> (${stanceWords(top.stance)})${b && b.mean != null ? ` vs usual <b class="num">${sgn(b.mean, 2)}</b>` : ""}</div></div>`;
  }

  function drawCompare(nowRel) {
    const host = $("cmp");
    if (!host) return;
    const evs = COMPARE_PAIR.map((id) => state.events[id]).filter(Boolean);
    if (evs.length < 2) return;
    const rows = COMPARE_SUFFIXES.map(([suf, name]) => ({ name, ms: evs.map((E) => E.markets.find((m) => m.isKalshi && m.id.endsWith(suf))) }));
    // Common vertical span (in dollars) per row so equal moves look equal on both sides.
    for (const r of rows) r.span = Math.max(0.06, ...r.ms.filter(Boolean).map((m) => m.hi - m.lo));
    const W = 560, H = 190, L = 48, R = 10, T = 14, B = 22;
    let html = "";
    evs.forEach((E, i) => {
      const meta = EVENTS.find((e) => state.events[e.id] === E);
      const now = E.t0 + nowRel;
      html += `<div class="cmp-col"><h3>${esc(E.tl.title)}</h3><div class="sub">${esc(meta ? meta.sub : "")} · text released ${fmtEtShort(E.t0)} ET</div>${cmpSpeech(E, now)}`;
      for (const r of rows) {
        const m = r.ms[i];
        if (!m) { html += `<div class="cmp-chart">missing ${esc(r.name)}</div>`; continue; }
        const ws = E.t0 - 30 * MIN, we = E.t0 + 90 * MIN;
        const x = (t) => L + ((t - ws) / (we - ws)) * (W - L - R);
        const c = (m.hi + m.lo) / 2;
        const y = yScale(c - r.span / 2, c + r.span / 2, m.tick, T, H - B, 2);
        let g = "";
        for (const v of yTicks(y, 4)) {
          g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#1d2a33"/><text x="${L - 6}" y="${y(v) + 4}" fill="#9bafb8" font-size="11" text-anchor="end" font-family="ui-monospace,Menlo,monospace">${cents(v)}</text>`;
        }
        for (const mm of [-30, 0, 30, 60, 90]) {
          g += `<text x="${x(E.t0 + mm * MIN)}" y="${H - 5}" fill="#8aa0aa" font-size="11" text-anchor="middle" font-family="ui-monospace,Menlo,monospace">${mm === 0 ? "release" : (mm > 0 ? "+" : "−") + Math.abs(mm) + "m"}</text>`;
        }
        g += `<line x1="${x(E.t0)}" x2="${x(E.t0)}" y1="${T}" y2="${H - B}" stroke="#e8f0f3" stroke-dasharray="3 3"/>`;
        const until = Math.min(now, we);
        g += `<path d="${bandPath(m.q.filter((q) => q.t >= ws), x, y, until)}" fill="rgba(34,211,238,.15)"/>`;
        g += `<path d="${stepPath(m.q.filter((q) => q.t >= ws && q.mid != null).map((q) => ({ t: q.t, v: q.mid })), x, y, until)}" fill="none" stroke="#22d3ee" stroke-width="2.2"/>`;
        if (m.alarm && m.alarm <= now) g += `<line x1="${x(m.alarm)}" x2="${x(m.alarm)}" y1="${T}" y2="${H - B}" stroke="#84f04f"/><text x="${x(m.alarm) + 4}" y="${T + 11}" fill="#84f04f" font-size="11">reaction detected ${relMin(m.alarm - E.t0)}</text>`;
        const q = quoteAt(m, now);
        // same reveal rule as the single-event view: no verdict or final move before its time
        const shown = m.status && now >= verdictAt(E, m);
        const chip = shown ? `<span class="chip ${esc(m.status)}" title="${esc(STATUS_HELP[m.status] || "")}">${esc(STATUS_TEXT[m.status] || m.status)}</span>`
          : `<span class="chip watching">${now < E.t0 ? "before speech" : "watching…"}</span>`;
        const mv = shown && m.a ? ` · move <b class="num">${moveText(m, m.a.move_ticks)}</b>` : "";
        html += `<div class="cmp-chart"><div class="hd"><b>${esc(r.name)}</b>${chip}<span style="color:var(--muted);font-size:12.5px">before ${cents(m.a?.pre_mid)}${mv}</span><span class="px">${q ? cents(q.mid) : "—"}</span></div>${svgEl(W, H, g)}</div>`;
      }
      html += `</div>`;
    });
    html += `<div class="cmp-note">Verdicts, reaction times and moves come from <span class="mono">analysis.json</span>; prices are Kalshi 1-minute candles (time = candle end). Waller spoke at 6:30 p.m. ET, when these contracts traded thinly, so a flat line there is labelled "can't tell" rather than "slow".</div>`;
    host.innerHTML = html;
  }

  // ---------- drawer: evidence and playbook ----------
  let drawerReturnFocus = null;
  function showDrawer(title, html) {
    $("drawerTitle").textContent = title;
    $("drawerBody").innerHTML = html;
    if (!$("drawer").classList.contains("open")) drawerReturnFocus = document.activeElement;
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("drawerClose").focus();
  }
  function closeDrawer() {
    const d = $("drawer");
    if (!d.classList.contains("open")) return;
    d.classList.remove("open");
    d.setAttribute("aria-hidden", "true");
    if (drawerReturnFocus && document.contains(drawerReturnFocus)) drawerReturnFocus.focus();
    drawerReturnFocus = null;
  }

  function quoteRow(q) {
    return q ? `<tr><td>${esc(q.t)}</td><td>${cents(q.bid)}</td><td>${cents(q.ask)}</td><td>${cents(q.mid)}</td><td>${q.imputed ? "imputed" : ""}</td><td>${q.volume ?? ""}</td></tr>` : "";
  }
  const QHEAD = `<tr><th>candle end (UTC)</th><th>bid</th><th>ask</th><th>mid</th><th></th><th>volume</th></tr>`;
  const raw = (o, label = "raw API row") => (o ? `<details><summary>${label}</summary><pre>${esc(JSON.stringify(o, null, 2))}</pre></details>` : "");

  function openEvidence(E, m) {
    if (!m) return;
    const e = m.evd, a = m.a;
    let h = `<div class="src">${esc(m.id)} · ${esc(m.venue)}${m.indicative ? " · <b>indicative</b>" : ""}</div>`;
    if (a) {
      h += `<h4>Tradio's verdict <small>· from analysis.json</small></h4><table>
        <tr><th>verdict</th><td><span class="chip ${esc(a.status)}">${esc(STATUS_TEXT[a.status] || a.status)}</span></td></tr>
        <tr><th>trading activity</th><td>${esc(a.liquidity)}</td></tr>
        <tr><th>price before speech</th><td>${cents(a.pre_mid)}</td></tr>
        <tr><th>reaction detected</th><td>${a.alarm_t ? esc(a.alarm_t) + " (" + relMin(parseT(a.alarm_t) - E.t0) + ")" : "—"}</td></tr>
        <tr><th>confirming trade</th><td>${esc(a.confirmed_by_trade || "—")}</td></tr>
        <tr><th>move</th><td>${sgn(a.move_ticks, 1, true)} ticks (1 tick = ${cents(m.tick)})</td></tr>
        <tr><th>halfway / 90% of move</th><td>${a.t50_min ?? "—"} / ${a.t90_min ?? "—"} min after release</td></tr>
        <tr><th>reason</th><td>${esc(a.reason || "")}</td></tr></table>${raw(a, "full analysis.json entry")}`;
    }
    if (!e) {
      h += `<h4>Evidence</h4><p class="src">No evidence.json entry for this market.</p>`;
    } else {
      if (e.pre_event) h += `<h4>Last price before the text was released</h4><table>${QHEAD}${quoteRow(e.pre_event.quote)}</table>${raw(e.pre_event.raw_candle, "raw candle")}`;
      if (e.first_quote_change) {
        const f = e.first_quote_change;
        h += `<h4>First price change after release${f.minutes_after_t0 != null ? ` · +${f.minutes_after_t0} min` : ""}</h4><table>${QHEAD}${quoteRow(f.previous_quote)}${quoteRow(f.quote)}</table>${raw(f.raw_candle, "raw candle")}`;
      }
      if (e.first_trade_outside_spread) {
        const f = e.first_trade_outside_spread, t = f.trade || {};
        h += `<h4>First trade outside the pre-release spread${f.seconds_after_t0 != null ? ` · +${f.seconds_after_t0}s` : ""}</h4><table><tr><th>time (UTC)</th><th>price</th><th>size</th><th>taker</th></tr><tr><td>${esc(t.t)}</td><td>${cents(t.price)}</td><td>${t.size ?? ""}</td><td>${esc(t.taker_side)}</td></tr></table>${raw(f.raw_trade)}`;
      }
      if (Array.isArray(e.biggest_trades) && e.biggest_trades.length) {
        h += `<h4>Biggest trades in the first 30 minutes</h4><table><tr><th>time (UTC)</th><th>price</th><th>size</th><th>taker</th></tr>` +
          e.biggest_trades.map((b) => { const t = b.trade || b; return `<tr><td>${esc(t.t)}</td><td>${cents(t.price)}</td><td>${t.size ?? ""}</td><td>${esc(t.taker_side)}</td></tr>`; }).join("") + `</table>`;
      }
      if (e.end_of_window) h += `<h4>End of window</h4><table>${QHEAD}${quoteRow(e.end_of_window.quote)}</table>${raw(e.end_of_window.raw_candle, "raw candle")}`;
      if (e.source) {
        h += `<h4>Source</h4>` + Object.entries(e.source).map(([k, v]) => `<div class="src"><b>${esc(k)}:</b> ${esc(typeof v === "string" ? v : JSON.stringify(v))}</div>`).join("");
      }
    }
    showDrawer(displayLabel(m), h);
  }

  // ---------- model (tradio/learner.py) ----------
  const MODEL_NAMES = {
    base_rate: "Simple guessing (base rate)", playbook: "Playbook rules", logistic: "Logistic regression",
    boosting: "Boosted trees", mlp: "Neural network", mlp_bag: "Neural network ×10 (averaged)",
    mlp_tuned: "Neural network (self-tuning)", logistic_text: "Logistic + speech text (Gemini)",
    mlp_text: "Neural network + speech text (Gemini)", ensemble: "Ensemble (average of models)",
  };
  const FEATURE_TEXT = {
    uncertainty: "price near 50/50 before T0", policy_share: "speech mostly about the policy outlook",
    n_flags: "released at a data-release time or next to another speech", quote_changes: "quotes already moving before T0",
    kind_hike_small: "contract is “hike 25 bp”", kind_cut_small: "contract is “cut 25 bp”", kind_hold: "contract is “hold”",
    kind_cut_big: "contract is “cut >25 bp”", kind_hike_big: "contract is “hike >25 bp”", abs_z: "bigger stance surprise (|z|)",
    aligned_z: "surprise points this contract's way", spread_ticks: "wider bid–ask spread", log_trades: "more trades before T0",
    role_chair: "speaker is the Chair", role_vice_chair: "speaker is the Vice Chair",
    role_vice_chair_supervision: "speaker is the Vice Chair for Supervision", role_governor: "speaker is a governor",
    has_stance: "speech has a stance score", has_text: "speech text downloaded", after_hours: "released outside market hours",
    log_days_to_decision: "more days until the FOMC decision", pre_mid: "higher price before T0",
    overlap_speeches: "another Board speech within 90 min", is_testimony: "testimony to Congress (not a speech)", release_in_window: "jobs/CPI report lands inside the window",
    release_day: "jobs/CPI report day", has_embedding: "speech text embedded",
    novelty_vs_speaker: "speech unlike the speaker's earlier ones", novelty_vs_recent: "speech unlike recent Board speeches",
  };
  const pctText = (p) => (p == null ? "—" : p < 0.01 ? "<1%" : p > 0.99 ? ">99%" : Math.round(p * 100) + "%");

  async function loadModel() {
    if (!state.model) state.model = await fetchJson(DATA_ROOT + "model.json", true);
    return state.model;
  }

  // The champion's prediction for this market, made before the model saw this event.
  function modelPrediction(E, m) {
    const M = state.model;
    if (m.venue !== "kalshi" || !/^kalshi:KXFEDDECISION-/.test(m.id)) return null;
    // live (and archived live) sessions: odds written by tradio/live.py from the current model
    const lo = E.an && E.an.model;
    if (lo && lo.markets && m.id in lo.markets) {
      const p = lo.markets[m.id];
      return p == null ? { none: true } : { p, model: lo.model, trainedOn: lo.trained_on_events, base: null };
    }
    if (!M) return null;
    const id = E.tl.event_id;
    const ev = (M.predictions || []).find((e) => e.event === id);
    if (!ev) return null;
    const row = ev.rows.find((r) => r.market === m.id);
    if (!row) return { none: true };
    return { p: row[M.champion], base: row.base_rate, model: M.champion, trainedOn: ev.trained_on };
  }

  function modelLine(mp) {
    if (mp.none) return `<div class="model-line muted">Model: no prediction (not active before T0)</div>`;
    const w = Math.max(2, Math.round(mp.p * 100));
    return `<div class="model-line" title="${esc(MODEL_NAMES[mp.model] || mp.model)}, trained on ${mp.trainedOn} earlier speeches${mp.base != null ? `; simple guessing said ${pctText(mp.base)}` : ""}">
      <span>Model before T0</span><span class="mbar"><i style="width:${w}%"></i></span><b class="num">${pctText(mp.p)}</b><span class="muted">chance to react</span></div>`;
  }

  async function openModel() {
    const M = await loadModel();
    let body;
    if (!M) {
      body = `<p class="src note">No model yet. Run <span class="mono">python -m tradio.learner</span>.</p>`;
    } else {
      const sb = M.scoreboard || {}, models = sb.models || {};
      const names = Object.keys(models);
      const maxAbs = Math.max(0.05, ...names.map((n) => Math.abs(models[n].brier_skill || 0)));
      const bar = (v) => {
        const w = Math.round((Math.abs(v) / maxAbs) * 50);
        return `<span class="skill"><span class="axis"></span><i class="${v < 0 ? "neg" : "pos"}" style="${v < 0 ? `right:50%` : `left:50%`};width:${w}%"></i></span>`;
      };
      let h = `<p class="model-verdict">${esc(M.verdict)}</p>
        <p class="src note">Learned from <b>${M.n_events}</b> Fed Board speeches (Nov 2024 →), <b>${M.n_labeled}</b> contract windows where the market was active before T0, <b>${M.positives}</b> of which reacted. ${M.excluded_not_measurable} windows were too thin to judge and are left out.</p>
        <h4>Scoreboard · each speech predicted before the model saw it (${sb.n_events || 0} speeches)</h4>
        <table class="scoreboard"><tr><th>predictor</th><th>vs simple guessing</th><th class="barcol"></th><th>log loss</th><th>clearly better?</th></tr>` +
        names.sort((a, b) => models[a].log_loss - models[b].log_loss).map((n) => {
          const r = models[n], sk = r.brier_skill || 0;
          const ci = r.gain_vs_base_ci;
          const verdict = n === "base_rate" ? "—" : r.beats_base_rate ? "yes" : ci && ci[1] < 0 ? "no, worse" : "not yet";
          return `<tr class="${n === M.champion ? "champ" : ""}" title="${ci ? `log-loss gain vs guessing, ${Math.round((sb.ci_level || 0.95) * 1000) / 10}% interval: ${ci[0]} to ${ci[1]}` : ""}">
            <td>${esc(MODEL_NAMES[n] || n)}${n === M.champion ? ' <span class="chip reacted">best</span>' : ""}</td>
            <td class="num">${n === "base_rate" ? "baseline" : (sk >= 0 ? "" : "−") + Math.abs(Math.round(sk * 100)) + "% " + (sk >= 0 ? "better" : "worse")}</td>
            <td class="barcol">${n === "base_rate" ? "" : bar(sk)}</td><td class="num">${r.log_loss.toFixed(3)}</td><td>${verdict}</td></tr>`;
        }).join("") + `</table>
        <p class="src note">"Better" = lower Brier score (squared error of the probability). "Clearly better" needs the whole bootstrap interval over ${sb.n_clusters || "—"} independent speech windows above zero, corrected for picking the best of four. Hover a row for the interval.</p>`;
      const w = M.logistic_weights;
      if (w) {
        const top = Object.entries(w).filter(([, v]) => Math.abs(v) >= 0.15).slice(0, 8);
        h += `<h4>What raises or lowers the odds (logistic regression, all speeches)</h4><table class="weights">` +
          top.map(([f, v]) => `<tr><td>${esc(FEATURE_TEXT[f] || f)}</td><td class="num nowrap">${v > 0 ? "▲ raises" : "▼ lowers"}</td><td class="barcol">${bar(v / Math.max(...top.map(([, x]) => Math.abs(x))) * maxAbs)}</td></tr>`).join("") +
          `</table><p class="src note">Standardized weights; they show association, not cause. Jobs and CPI reports (8:30 ET) are tracked from the BLS calendar, so a move caused by the data isn't credited to the speech.</p>`;
      }
      const heroes = EVENTS.map((e) => e.id);
      const hp = (M.predictions || []).filter((e) => heroes.includes(e.event));
      if (hp.length) {
        h += `<h4>Our replay events: what the model said before T0</h4><table><tr><th>event</th><th>contract</th><th>predicted</th><th>happened</th></tr>` +
          hp.map((e) => e.rows.map((r) => `<tr><td>${esc(e.event)}</td><td>${esc(r.market.replace("kalshi:KXFEDDECISION-", ""))}</td><td class="num">${pctText(r[M.champion])}</td><td>${r.label ? "reacted" : "no reaction"}</td></tr>`).join("")).join("") +
          `</table><p class="src note">Each made from earlier speeches only. A low number that reacted is a genuine surprise, not a bug.</p>`;
      }
      h += `<p class="src note">${esc(M.caveat)} Built ${esc(M.built_at)} by <span class="mono">python -m tradio.learner</span>; every archived live speech retrains it.</p>`;
      body = h;
    }
    showDrawer("Model · predicting which markets react", body);
  }

  async function openPlaybook() {
    const pb = await fetchJson(DATA_ROOT + "playbook.json", true);
    let h;
    if (!pb) {
      h = `<p class="src">No playbook yet. Run <span class="mono">python tradio/playbook.py</span>.</p>`;
    } else {
      const r = pb.rules || {};
      const row = (name, k, fmt) => (r[k] ? `<tr><th>${name}</th><td><b>${esc(fmt ? fmt(r[k].value) : JSON.stringify(r[k].value))}</b><div class="src">${esc(r[k].why)}</div></td></tr>` : "");
      h = `<p class="src">${esc(pb.caveat)} Built ${esc(pb.built_at)}.</p><h4>Rules the desk follows <small>· <span class="mono">python live.py --playbook</span></small></h4><table>
        ${row("Trade only if |z| ≥", "min_abs_z", (v) => v)}
        ${row("Contract types", "trade_kinds", (v) => v.join(", ").replace(/_/g, " "))}
        ${row("Liquidity", "tradeable_liquidity", (v) => v.join(", "))}
        ${row("Planned exit after", "exit_after_min", (v) => v + " min")}
        ${row("Skip if already moved", "skip_if_alarmed", (v) => (v ? "yes" : "no"))}
        ${row("Direction right", "direction_hit_rate", (v) => (v == null ? "—" : Math.round(v * 100) + "%"))}
        ${row("Hold side follows the regime", "hold_side_by_regime", (v) => (v ? "yes" : "no"))}
      </table><h4>What it learned from</h4><table><tr><th>event</th><th>top z</th><th>direction</th><th>market leaning</th><th>reacted</th></tr>` +
        (pb.events || []).map((o) => `<tr><td>${esc(o.event)}</td><td>${sgn(o.top_z, 2)}</td><td>${esc(o.direction)}</td><td>${esc(o.regime || "—")}</td><td>${o.reacted ? "yes" : "no"}</td></tr>`).join("") + `</table>`;
      if (pb.backtests) {
        h += `<h4>Backtest, leave-one-out <small>· each event traded with rules learned from the others</small></h4><table><tr><th>event</th><th>trades</th><th>P&amp;L</th><th>capital</th></tr>` +
          pb.backtests.map((b) => `<tr><td>${esc(b.event)}</td><td>${esc(b.trades)}</td><td>${money(b.total_pnl)}</td><td>$${(b.capital || 0).toFixed(2)}</td></tr>`).join("") +
          `</table><p class="src">Real Kalshi 1-minute prices; entry 1 min after T0 at the ask, exit at the bid, Kalshi taker fees on both legs. Not trading advice.</p>`;
      }
    }
    showDrawer("Playbook · learned from past speeches", h);
  }

  // ---------- clock + controls ----------
  function currentRange() {
    if (state.view === "compare") return [-30 * MIN, 90 * MIN];
    const E = state.events[state.view];
    if (E && E.live) return [E.ws - E.t0, (E.liveNow || E.we) - E.t0];
    return E ? [E.ws - E.t0, E.we - E.t0] : [-60 * MIN, 90 * MIN];
  }

  function touch() {
    state.touched = true;
    const c = $("cta");
    if (c) c.hidden = true;
  }

  function setRel(rel, keepFollow) {
    const [a, b] = currentRange();
    state.rel = Math.max(a, Math.min(b, rel));
    if (state.view === "live" && !keepFollow) {
      state.follow = state.rel >= b - 1000;
      setPlaying(false);
    }
    if (state.rel >= b) setPlaying(false);
    draw(true);
  }

  function setPlaying(p) {
    const c = $("cta");
    if (state.view === "live") {
      // LIVE has no replay to play: the button pins the clock back to the newest data.
      state.playing = false;
      $("btnPlay").textContent = state.follow ? "● Live" : "Go live";
      $("btnPlay").classList.toggle("on", state.follow);
      if (c) c.hidden = true;
      return;
    }
    state.playing = p;
    $("btnPlay").textContent = p ? "❚❚ Pause" : "▶ Play";
    $("btnPlay").classList.toggle("on", p);
    if (c) c.hidden = state.touched || p;
  }

  function renderMarks() {
    const [a, b] = currentRange();
    const pct = (r) => ((r - a) / (b - a)) * 100;
    const E = state.view !== "compare" ? state.events[state.view] : null;
    // while LIVE the track ends at "now", so T0 and alarms can still lie ahead of it: leave those off
    const onTrack = (p) => p >= 0 && p <= 100;
    const t0On = onTrack(pct(0));
    let html = t0On ? `<span class="mark t0" style="left:${pct(0)}%" title="${E && E.overTime ? "The moment the speech starts" : "The moment the speech text is released"}">T0</span>` : "";
    if (E) {
      const alarms = E.markets.filter((m) => m.alarm && !m.indicative).sort((x, y) => x.alarm - y.alarm);
      // print a label only when it won't touch its neighbours (about 3.3px per character)
      const trackW = Math.max(200, $("scrub").clientWidth);
      const endText = E.live ? "now" : fmtEtShort(E.we) + " ET";
      const endLeft = 0.98 * trackW - endText.length * 3.3;
      let lastRight = t0On ? (pct(0) / 100) * trackW + 2 * 3.3 : -Infinity;
      const endFits = lastRight + 4 < endLeft; // T0 wins over the end label when they'd collide
      for (const m of alarms) {
        const r = m.alarm - E.t0, p = pct(r);
        if (!onTrack(p)) continue;
        const text = relMin(r);
        const center = (p / 100) * trackW, half = text.length * 3.3;
        const label = center - half > lastRight + 4 && (!endFits || center + half < endLeft - 4) ? text : "";
        if (label) lastRight = center + half;
        html += `<span class="mark alarm" style="left:${p}%" title="${esc(displayLabel(m))}: reaction detected ${relMin(r)}">${label}</span>`;
      }
      html += `<span class="mark" style="left:2%">${fmtEtShort(E.ws)} ET</span>${endFits ? `<span class="mark" style="left:98%">${endText}</span>` : ""}`;
    }
    $("marks").innerHTML = html;
    const s = $("scrub");
    s.min = a; s.max = b;
  }

  function setRelClock() {
    const el = $("clkRel");
    el.textContent = fmtRel(state.rel);
    el.classList.toggle("post", state.rel >= 0);
    const E = state.events[state.view];
    const spoken = !!(E && E.overTime);
    $("clkCap").textContent = state.rel < 0
      ? (spoken ? "until the speech starts" : "until the text is released")
      : (spoken ? "since the speech started" : "since the text was released");
  }

  // The chart card stays pinned next to the speech and market list only while it fits
  // between the top bar and the player dock; otherwise it scrolls with the page.
  function fitSticky() {
    const top = document.querySelector(".topbar"), dock = document.querySelector(".dock");
    if (top) document.documentElement.style.setProperty("--topbar-h", top.offsetHeight + "px");
    const card = $("marketCard");
    if (!card) return;
    // 16px gap under the top bar (the CSS `top` offset) plus a little clearance above the dock
    const room = window.innerHeight - (top ? top.offsetHeight : 0) - (dock ? dock.offsetHeight : 0) - 22;
    card.classList.toggle("nostick", card.offsetHeight > room);
  }

  function draw(force) {
    const t = performance.now();
    if (!force && t - state.lastDraw < 66) return; // ~15 fps is plenty for minute candles
    state.lastDraw = t;
    $("scrub").value = state.rel;

    if (state.view === "compare") {
      $("clkAbs").hidden = true; // two events, two wall clocks: only the T0-relative clock means anything here
      setRelClock();
      drawCompare(state.rel);
      return;
    }
    const E = state.events[state.view];
    if (!E) return;
    const now = E.t0 + state.rel;
    $("clkAbs").hidden = false;
    $("clkEt").textContent = fmtEt(now);
    $("clkUtc").textContent = fmtUtc(now);
    setRelClock();
    renderSpeech(E, now);
    const m = E.byId[state.focus[state.view]] || E.headline;
    if (m) {
      renderMarketHead(E, m, now);
      drawMainChart(E, m, now);
    }
    renderDesk(E, now);
    renderMarkets(E, now);
    const cta = $("cta");
    if (cta) cta.hidden = state.touched || state.playing || state.view === "live";
    if (force || t - state.lastFit > 500) { state.lastFit = t; fitSticky(); }
  }

  let lastTick = null;
  function tick(ts) {
    if (state.playing && lastTick != null) {
      const dt = ts - lastTick;
      const [, b] = currentRange();
      state.rel = Math.min(b, state.rel + dt * state.speed);
      if (state.rel >= b) setPlaying(false);
      draw(false);
    }
    lastTick = ts;
    requestAnimationFrame(tick);
  }

  function renderTabs() {
    const all = [LIVE, ...EVENTS, COMPARE];
    $("tabs").innerHTML = all.map((e) => `<button class="tab ${e.id === "compare" ? "compare" : ""} ${e.id === "live" ? "live" : ""} ${state.view === e.id ? "on" : ""}" data-id="${esc(e.id)}" ${state.view === e.id ? 'aria-current="page"' : ""}><b>${esc(e.short)}</b><small>${esc(e.sub)}</small></button>`).join("");
  }

  async function show(id) {
    closeDrawer();
    stopLive();
    state.view = id;
    $("liveControls").hidden = id !== "live";
    $("speedBox").hidden = id === "live";
    renderTabs();
    fitSticky();
    if (id === "live") {
      if (location.hash !== "#live") history.replaceState(null, "", "#live");
      return startLive();
    }
    setPlaying(false);
    try {
      if (id === "compare") {
        await Promise.all(COMPARE_PAIR.map((e) => loadEvent(e)));
        if (state.view !== id) return;
        renderCompareShell();
      } else {
        const E = await loadEvent(id);
        if (state.view !== id) return;
        const pref = (EVENTS.find((e) => e.id === id) || {}).focus;
        if (pref && E.byId[pref]) E.headline = E.byId[pref];
        if (!state.focus[id] && E.headline) state.focus[id] = E.headline.id;
        renderReplayShell(E);
      }
    } catch (err) {
      $("view").innerHTML = `<div class="error">Couldn't load data for ${esc(id)}: ${esc(err.message)}.<br>Serve the <span class="mono">web/</span> folder over HTTP (<span class="mono">python3 tradio/serve.py</span>, or <span class="mono">cd web && python3 -m http.server 8000</span>) and open <span class="mono">http://localhost:8000/tradio.html</span>; browsers block fetch() from file:// pages.</div>`;
      return;
    }
    if (location.hash.slice(1) !== id) history.replaceState(null, "", "#" + id);
    renderMarks();
    setRel(state.rel);
  }

  // ---------- live controls (only when served by tradio/serve.py) ----------
  const ctl = { ok: null, status: null, built: false, error: "", seq: 0 };

  async function api(path, body) {
    const r = await fetch(path, {
      method: body ? "POST" : "GET", cache: "no-store",
      headers: { "X-Tradio": "1", "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  async function refreshControls() {
    const box = $("liveControls");
    box.hidden = state.view !== "live";
    if (state.view !== "live") return;
    const seq = ctl.seq;
    let st = null;
    try { st = await api("/api/status"); } catch (e) { st = null; }
    if (seq !== ctl.seq) return;           // a Start/Stop happened meanwhile; its answer is newer
    ctl.status = st || ctl.status;
    ctl.ok = !!st && st.server === "tradio";
    renderControls();
  }

  function renderControls() {
    const box = $("liveControls");
    if (!ctl.ok) {
      ctl.built = false;
      box.innerHTML = `<span class="muted">Want Start/Stop buttons here? Run the site with <span class="mono">python3 tradio/serve.py</span> instead of <span class="mono">python3 -m http.server</span>.</span>`;
      return;
    }
    const st = ctl.status, k = st.keys;
    if (!ctl.built) {
      const opt = (v, t, dis) => `<option value="${esc(v)}" ${dis ? "disabled" : ""}>${esc(t)}</option>`;
      box.innerHTML = `
        <div class="ctl-group"><b>Practice</b>
          <select id="ctlEvent">${st.events.map((e) => opt(e, e)).join("")}</select>
          <select id="ctlSpeed">${st.speeds.map((v) => `<option value="${v}" ${v === 30 ? "selected" : ""}>${v}×</option>`).join("")}</select>
          <button class="btn" id="ctlPractice">▶ Start practice</button></div>
        <div class="ctl-group"><b>Real speech</b>
          <select id="ctlSpeaker">${st.speakers.map((v) => opt(v, v.replace(/_/g, " "))).join("")}</select>
          <select id="ctlMode">
            ${opt("dry", "Guide only (no orders)")}
            ${opt("demo", "Trade: Kalshi demo (fake money)" + (k.kalshi_demo ? "" : " · needs demo keys"), !k.kalshi_demo)}
            ${opt("live", "Trade: REAL MONEY" + (!st.allow_live ? " · off (TRADIO_ALLOW_LIVE=1)" : !k.kalshi_live ? " · needs Kalshi keys" : ""), !(st.allow_live && k.kalshi_live))}
          </select>
          <label><input type="checkbox" id="ctlTranscribe" checked> transcribe</label>
          <input id="ctlUrl" placeholder="stream (default: Fed live broadcast)" size="28">
          <input id="ctlConfirm" placeholder="type LIVE" size="9" hidden>
          <button class="btn" id="ctlLive">▶ Start live</button></div>
        <button class="btn stop" id="ctlStop">■ Stop</button>
        <div class="ctl-status" id="ctlStatus"></div>`;
      $("ctlMode").addEventListener("change", () => { $("ctlConfirm").hidden = $("ctlMode").value !== "live"; });
      $("ctlPractice").addEventListener("click", () => startSession({ kind: "practice", event: $("ctlEvent").value, speed: +$("ctlSpeed").value }));
      $("ctlLive").addEventListener("click", () => startSession({
        kind: "live", speaker: $("ctlSpeaker").value, mode: $("ctlMode").value, transcribe: $("ctlTranscribe").checked,
        url: $("ctlUrl").value.trim() || undefined, confirm: $("ctlConfirm").value.trim() }));
      $("ctlStop").addEventListener("click", async () => { ctl.seq++; try { ctl.status = await api("/api/stop", {}); ctl.error = ""; } catch (e) { ctl.error = e.message; } renderControls(); });
      ctl.built = true;
    }
    const procs = Object.entries(st.processes || {});
    const missing = [!k.gemini && "gemapi.txt", !k.elevenlabs && "elevenapi.txt", !k.kalshi_demo && "Kalshi demo keys"].filter(Boolean);
    $("ctlStatus").innerHTML =
      (ctl.error ? `<div class="err">${esc(ctl.error)}</div>` : "") +
      (procs.length ? procs.map(([n, p]) => `<details><summary><span class="chip ${p.running ? "reacted" : "watching"}">${p.running ? "running" : "stopped" + (p.exit_code ? ` (exit ${p.exit_code})` : "")}</span> <b>${esc(n)}</b> · ${esc(p.label)} <span class="muted mono">${esc((p.log.slice(-1)[0] || "").slice(0, 90))}</span></summary><pre>${esc(p.log.join("\n"))}</pre></details>`).join("")
        : `<span class="muted">Nothing running.${missing.length ? " Missing: " + missing.join(", ") + "." : ""}</span>`);
  }

  async function startSession(req) {
    ctl.seq++;
    ctl.error = "";
    try {
      ctl.status = await api("/api/start", req);
      state.liveShellKey = null;
      state.follow = true;
      state.focusPinned = {};
      setTimeout(pollLive, 1500);
    } catch (e) {
      ctl.error = e.message;
    }
    renderControls();
  }

  // ---------- live view ----------
  function stopLive() {
    if (state.liveTimer) clearInterval(state.liveTimer);
    state.liveTimer = null;
    state.liveShellKey = null;
  }

  function startLive() {
    state.follow = true;
    setPlaying(false);
    pollLive();
    state.liveTimer = setInterval(pollLive, LIVE_POLL_MS);
  }

  async function pollLive() {
    refreshControls();
    let E;
    try {
      E = await loadEvent("live", true);
    } catch (err) {
      if (state.view !== "live") return;
      state.events.live = null;
      state.liveShellKey = null;
      $("view").innerHTML = liveIdleHtml();
      $("marks").innerHTML = "";
      return;
    }
    if (state.view !== "live") return;
    const key = E.tl.t0 + "|" + E.markets.map((m) => m.id).join(",");
    // Follow the story (first alarm, then the biggest reaction) unless the viewer picked a market.
    const pinned = state.focusPinned.live && E.byId[state.focus.live];
    if (!pinned) state.focus.live = E.headline && E.headline.id;
    if (state.liveShellKey !== key) { renderReplayShell(E); state.liveShellKey = key; }
    renderLiveBanner(E);
    renderMarks();
    if (state.follow) state.rel = (E.liveNow || E.we) - E.t0;
    setRel(state.rel, true);
    setPlaying(false);
  }

  function liveIdleHtml() {
    return `<main class="stage"><div class="card idle">
      <h3>No live session running</h3>
      <p><b>Use the buttons above</b> (Practice replays a past speech; Real speech listens, scores and, if you pick it, trades). Or run the commands yourself:</p>
      <p>The LIVE tab shows a Fed speech as it happens: the transcript scored against the speaker's own history, next to Kalshi's live Fed-decision prices, with the same reaction checks as the replays. When it's over, it becomes one more History tab.</p>
      <ol>
        <li><b>Score the speech</b> (MarketPulse, dry, never trades):<br><span class="mono">python speechtxt.py --url https://www.federalreserve.gov/live-broadcast.htm</span><br><span class="mono">python live.py --speaker christopher_waller --no-prewarm --mode dry</span><br>or let it <b>trade on Kalshi's demo exchange</b> by the learned playbook:<br><span class="mono">python live.py --speaker christopher_waller --fast --playbook --mode demo --venues kalshi</span></li>
        <li><b>Stream the market tape</b> into this page:<br><span class="mono">python tradio/live.py --speaker christopher_waller --t0 now</span></li>
        <li><b>Afterwards, keep it</b> as a history event:<br><span class="mono">python tradio/live.py archive waller20261015 --title "Waller, 2026-10-15"</span></li>
      </ol>
      <p><b>No speech on right now?</b> Rehearse with a past event replayed as if live (offline, clearly labelled SIMULATED):<br><span class="mono">python tradio/live.py --simulate bowman20250623 --speed 30</span></p>
      <p class="muted">This page checks for <span class="mono">data/tradio/live/</span> every ${LIVE_POLL_MS / 1000} s and starts by itself.</p>
    </div></main>`;
  }

  async function loadArchivedEvents() {
    const extra = await fetchJson(DATA_ROOT + "events.json", true);
    for (const e of Array.isArray(extra) ? extra : []) {
      if (e && e.id && !EVENTS.some((x) => x.id === e.id)) EVENTS.push({ id: e.id, short: e.short || e.id, sub: e.sub || "recorded live", focus: e.focus });
    }
  }

  // ---------- page events ----------
  function onViewClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const E = state.events[state.view];
    switch (el.dataset.act) {
      case "focus":
      case "order":
        if (!E || !E.byId[el.dataset.id]) return;
        state.focus[state.view] = el.dataset.id;
        state.focusPinned[state.view] = true;
        draw(true);
        if (el.dataset.act === "focus") $("marketCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
        break;
      case "evidence":
        if (E) openEvidence(E, E.byId[el.dataset.id]);
        break;
      case "group": {
        if (!E) return;
        const g = GROUPS.find((x) => x.id === el.dataset.id);
        state.groupOpen[groupKey(g)] = !isGroupOpen(g);
        draw(true);
        break;
      }
      case "speech-toggle":
        state.speechOpen = !state.speechOpen;
        draw(true);
        break;
      case "paras-toggle":
        state.showAllParas = !state.showAllParas;
        draw(true);
        break;
      case "context-toggle":
        state.contextOpen = !state.contextOpen;
        draw(true);
        break;
      case "cta":
        touch();
        setPlaying(true);
        break;
    }
  }

  function toggleGuide() {
    const g = $("guide");
    if (g.open) g.close();
    else g.showModal();
    $("btnGuide").setAttribute("aria-expanded", String(g.open));
  }

  function init() {
    $("legendList").innerHTML = STATUS_ORDER.map((s) => `<dt><span class="chip ${s}">${esc(STATUS_TEXT[s])}</span></dt><dd>${esc(STATUS_HELP[s])}</dd>`).join("");
    $("speeds").innerHTML = SPEEDS.map((s) => `<button class="btn ${s === state.speed ? "on" : ""}" data-speed="${s}" title="Replay at ${s}× real time">${s}×</button>`).join(" ");
    $("speeds").addEventListener("click", (e) => {
      const b = e.target.closest("[data-speed]");
      if (!b) return;
      state.speed = +b.dataset.speed;
      for (const x of $("speeds").children) x.classList.toggle("on", x === b);
    });
    $("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) show(b.dataset.id); });
    $("view").addEventListener("click", onViewClick);
    $("view").addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target.closest("[role=button][data-act]")) {
        e.preventDefault();
        onViewClick(e);
      }
    });
    // <details> toggles don't bubble: remember whether the desk's skipped list is open
    $("view").addEventListener("toggle", (e) => {
      if (e.target.closest && e.target.closest("#desk")) state.deskSkipsOpen = e.target.open;
    }, true);
    $("btnPlay").addEventListener("click", () => {
      touch();
      if (state.view === "live") { state.follow = true; pollLive(); return; }
      const [, b] = currentRange();
      if (!state.playing && state.rel >= b) state.rel = START_REL;
      setPlaying(!state.playing);
    });
    $("btnBack").addEventListener("click", () => { touch(); setRel(state.rel - MIN); });
    $("btnFwd").addEventListener("click", () => { touch(); setRel(state.rel + MIN); });
    $("btnT0").addEventListener("click", () => { touch(); setPlaying(false); setRel(MIN); });
    $("scrub").addEventListener("input", (e) => { touch(); setRel(+e.target.value); });
    $("drawerClose").addEventListener("click", closeDrawer);
    $("btnPlaybook").addEventListener("click", openPlaybook);
    $("btnModel").addEventListener("click", openModel);
    $("btnGuide").addEventListener("click", toggleGuide);
    $("guideClose").addEventListener("click", () => $("guide").close());
    $("guide").addEventListener("close", () => $("btnGuide").setAttribute("aria-expanded", "false"));
    $("guide").addEventListener("click", (e) => { if (e.target === $("guide")) $("guide").close(); });
    document.addEventListener("keydown", (e) => {
      const t = e.target;
      if (t.tagName === "INPUT" && t.type !== "range") return;
      if (t.tagName === "SELECT" || t.tagName === "TEXTAREA") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if ($("guide").open) return; // the open guide handles its own keys (Esc closes it)
      const interactive = t.closest && t.closest("button, a, [role=button], summary");
      if (e.key === " ") { if (interactive) return; e.preventDefault(); $("btnPlay").click(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); touch(); setRel(state.rel - MIN); }
      else if (e.key === "ArrowRight") { e.preventDefault(); touch(); setRel(state.rel + MIN); }
      else if (e.key === "0") { $("btnT0").click(); }
      else if (e.key === "Escape") closeDrawer();
    });
    window.addEventListener("resize", () => {
      if (state.view && (state.view === "compare" || state.events[state.view])) renderMarks();
      draw(true);
      fitSticky();
    });
    window.addEventListener("hashchange", () => { const id = location.hash.slice(1); if (id && id !== state.view) show(id); });

    // recorded live sessions (data/tradio/events.json, written by `tradio/live.py archive`) add History tabs;
    // the model's predictions (data/tradio/model.json) are loaded once, before the first view
    Promise.all([loadArchivedEvents(), loadModel()]).finally(() => {
      const start = location.hash.slice(1);
      const valid = [LIVE, ...EVENTS, COMPARE].some((e) => e.id === start);
      show(valid ? start : EVENTS[0].id);
      requestAnimationFrame(tick);
    });
  }

  init();
})();
