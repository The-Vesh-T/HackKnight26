/* Tradio replay terminal.
 *
 * Reads the cached files written by roles 1-3 and replays them with a client-side clock:
 *   data/tradio/<event>/timeline.json   (role 1 + role 3's speech paragraphs)
 *   data/tradio/<event>/analysis.json   (role 2)
 *   data/tradio/<event>/evidence.json   (role 1, raw rows behind the numbers)
 *   data/tradio/model.json              (tradio/learner.py: out-of-sample predictions)
 *
 * Rules this page follows (CLAUDE.md): no network calls besides those local files, no
 * analytics in the browser (statuses, alarms, moves come from analysis.json), speech text
 * appears all at once at T0, Polymarket is labelled indicative and gets no timing markers.
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
  const COMPARE = { id: "compare", short: "Bowman vs Waller", sub: "same contracts, aligned at T0" };
  // LIVE reads data/tradio/live/, which tradio/live.py rewrites every few seconds during a speech.
  const LIVE = { id: "live", short: "● LIVE", sub: "Fed speech happening now" };
  const LIVE_POLL_MS = 3000;
  const STALE_MS = 90 * 1000;
  const COMPARE_PAIR = ["bowman20250623", "waller20250717"];
  const COMPARE_SUFFIXES = [["-C25", "July cut 25 bp"], ["-H0", "July hold"]];
  const SPEEDS = [1, 10, 30, 60];
  const ALARM_STATUSES = new Set(["reacted", "alarm", "candidate_delay", "candidate_inconsistency"]);
  const MIN = 60000;

  const STATUS_TEXT = {
    reacted: "Reacted",
    expected_no_reaction: "No reaction expected",
    cant_tell_illiquid: "Can't tell · illiquid",
    candidate_delay: "Possible delay",
    candidate_inconsistency: "Possible inconsistency",
    // live only, before the 90-minute window closes (see tradio/live.py provisional())
    alarm: "Alarm · moving",
    watching: "Watching",
  };
  const STATUS_ORDER = ["reacted", "alarm", "candidate_delay", "candidate_inconsistency", "expected_no_reaction", "watching", "cant_tell_illiquid"];

  const state = {
    view: null,          // event id or "compare"
    events: {},          // id -> prepared data
    rel: -10 * MIN,      // replay clock, ms relative to T0 (shared across events so switching keeps position)
    playing: false,
    speed: 30,
    focus: {},           // event id -> focused market id
    fired: new Set(),    // alarm pulses already shown
    lastDraw: 0,
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
  const relMin = (ms) => `T0${ms < 0 ? "−" : "+"}${Math.round(Math.abs(ms) / MIN)}m`;

  // Prices are stored in dollars (0.155); show cents.
  function cents(x) {
    if (x == null || Number.isNaN(x)) return "—";
    const c = x * 100;
    return (Math.abs(c - Math.round(c)) < 1e-6 ? c.toFixed(0) : c.toFixed(1)) + "¢";
  }
  const signed = (x, d = 1) => (x == null ? "—" : (x > 0 ? "+" : "") + (+x).toFixed(d).replace(/\.0+$/, ""));

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
      // Only surface an alarm when role 2's verdict backs it; a detector blip on an illiquid or
      // expected-no-reaction market is noise by their own definition.
      m.alarm = m.a && m.a.alarm_t && ALARM_STATUSES.has(m.a.status) ? parseT(m.a.alarm_t) : null;
      m.isKalshi = m.venue === "kalshi";
    }
    // Card order: by status, then Kalshi before Polymarket, then size of move.
    const rank = (m) => (m.status ? STATUS_ORDER.indexOf(m.status) : 9);
    markets.sort((a, b) => rank(a) - rank(b) || (b.isKalshi - a.isKalshi) || Math.abs(b.a?.move_ticks || 0) - Math.abs(a.a?.move_ticks || 0));

    // Headline market: biggest Kalshi reaction, else the biggest Kalshi move.
    const kal = markets.filter((m) => m.isKalshi && m.a);
    const headline =
      kal.filter((m) => m.status === "reacted").sort((a, b) => Math.abs(b.a.move_ticks) - Math.abs(a.a.move_ticks))[0] ||
      kal.filter((m) => m.status === "alarm").sort((a, b) => a.alarm - b.alarm)[0] ||
      kal.filter((m) => m.a.move_ticks).sort((a, b) => Math.abs(b.a.move_ticks) - Math.abs(a.a.move_ticks))[0] ||
      kal.slice().sort((a, b) => b.tr.length - a.tr.length)[0] ||   // nothing has moved: most-traded contract
      markets[0];

    // A prepared text is released whole at T0; a live transcript arrives passage by passage,
    // each with its own time `t`.
    const paragraphs = ((tl.speech && tl.speech.paragraphs) || []).map((p) => ({ ...p, _t: p.t ? parseT(p.t) : t0 }));
    const live = tl.live || null;
    const liveNow = live ? parseT(live.now) : null;
    // While live, leave room to the right of "now" so the line grows into the chart.
    const xe = live ? Math.max(we, t0 + 30 * MIN, (liveNow || we) + 10 * MIN) : we;
    return { tl, an, t0, ws, we, xe, markets, byId, headline, paragraphs, hasAnalysis: !!an, live, liveNow,
             overTime: !!(tl.speech && tl.speech.arrives_over_time) };
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

  const money = (x) => (x == null ? "—" : (x >= 0 ? "+$" : "−$") + Math.abs(x).toFixed(2));

  function renderDesk(E, now) {
    const el = $("desk");
    if (!el) return;
    const d = E.desk;
    if (!d) { el.innerHTML = ""; return; }
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
      rows += `<div class="order" data-id="${esc(t.mid)}">
        <span class="mono t">${fmtEtShort(t.t)}</span>
        <span>BUY <b>${esc(String(t.side).toUpperCase())}</b> ×${t.qty} ${esc(t.label)} @ <span class="num">${cents(t.cost)}</span></span>
        <span class="pnl">${pnlTxt}</span>
        ${t.sideFixed ? `<div class="fix">side corrected by the playbook: ${esc(t.sideFixed)}</div>` : ""}
      </div>`;
    }
    const sk = d.skipped.filter((x) => !x.t || x.t <= now);
    const done = shown.filter((t) => t.exitT && now >= t.exitT && t.pnlFinal != null);
    const total = done.length ? done.reduce((a, t) => a + t.pnlFinal, 0) : null;
    const started = now >= E.t0;
    el.innerHTML = `<div class="desk">
      <div class="row"><b>Desk</b> ${mode}<span class="px">${total != null ? `<span class="${total >= 0 ? "up" : "down"}">${money(total)}</span> <span class="muted">closed</span>` : ""}</span></div>
      ${rows || `<div class="muted">${started ? "No orders: the playbook hasn't found a trade." : "Waiting for the speech."}</div>`}
      ${sk.length && started ? `<details><summary>${sk.length} skipped by the playbook</summary>${sk.map((x) => `<div class="skip"><b>${esc(x.label)}</b>: ${esc(x.why)}</div>`).join("")}</details>` : ""}
      <div class="muted small">${esc(d.note || "")}</div>
    </div>`;
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
  // end of that window; everything else from T0. Before that the card just says "watching".
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
    const W = Math.max(320, wrap.clientWidth), H = Math.max(220, wrap.clientHeight);
    const L = 52, R = 14, T = 22, B = 26;
    const x = (t) => L + ((t - E.ws) / (E.xe - E.ws)) * (W - L - R);
    const y = yScale(m.lo, m.hi, m.tick, T, H - B);
    const until = Math.min(now, E.we);
    let g = "";

    // grid + y labels
    for (const v of yTicks(y)) {
      g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#1d2a33"/>`;
      g += `<text x="${L - 6}" y="${y(v) + 4}" fill="#8ea2ab" font-size="11" text-anchor="end" font-family="ui-monospace,Menlo,monospace">${cents(v)}</text>`;
    }
    // x labels every 15 min (ET)
    for (let t = Math.ceil(E.ws / (15 * MIN)) * 15 * MIN; t <= E.xe; t += 15 * MIN) {
      g += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" stroke="#16222a"/>`;
      g += `<text x="${x(t)}" y="${H - 8}" fill="#5d717b" font-size="11" text-anchor="middle" font-family="ui-monospace,Menlo,monospace">${fmtEtShort(t)}</text>`;
    }
    // pre-event noise band from analysis
    if (m.a && m.a.pre_band) {
      const [a, b] = m.a.pre_band;
      const yt = y(Math.max(a, b)), yb = y(Math.min(a, b));
      g += `<rect x="${L}" y="${yt - 1}" width="${W - L - R}" height="${Math.max(2, yb - yt + 2)}" fill="rgba(142,162,171,.10)"/>`;
      g += `<text x="${W - R - 4}" y="${yt - 4}" fill="#5d717b" font-size="10" text-anchor="end">pre-event noise band</text>`;
    }
    // T0
    g += `<line x1="${x(E.t0)}" x2="${x(E.t0)}" y1="${T - 8}" y2="${H - B}" stroke="#e3edf0" stroke-dasharray="3 3"/>`;
    g += `<text x="${x(E.t0) + 5}" y="${T - 8}" fill="#e3edf0" font-size="11">T0 · ${E.overTime ? "speech starts" : "text released"} ${fmtEtShort(E.t0)} ET</text>`;

    // bid-ask band + midpoint
    const color = m.isKalshi ? "#22d3ee" : "#f0b429";
    if (m.isKalshi) g += `<path d="${bandPath(m.q, x, y, until)}" fill="rgba(34,211,238,.13)"/>`;
    const mids = m.q.filter((q) => q.mid != null).map((q) => ({ t: q.t, v: q.mid }));
    g += `<path d="${stepPath(mids, x, y, until)}" fill="none" stroke="${color}" stroke-width="2" ${m.indicative ? 'stroke-dasharray="6 4"' : ""}/>`;

    // trades, sized by contracts
    const maxS = Math.max(1, ...m.tr.map((t) => t.s));
    for (const t of m.tr) {
      if (t.t > until) break;
      const r = 1.5 + 6 * Math.sqrt(t.s / maxS);
      g += `<circle cx="${x(t.t).toFixed(1)}" cy="${y(t.p).toFixed(1)}" r="${r.toFixed(1)}" fill="${t.side === "no" ? "rgba(239,91,91,.55)" : "rgba(132,240,79,.55)"}"/>`;
    }

    // markers from analysis.json, revealed only once the replay clock reaches them
    const markers = [];
    if (m.alarm && !m.indicative) markers.push([m.alarm, "#84f04f", `alarm ${relMin(m.alarm - E.t0)}`]);
    if (m.a && m.a.t50_min != null && !m.indicative) markers.push([E.t0 + m.a.t50_min * MIN, "#8ea2ab", "50% absorbed"]);
    if (m.a && m.a.t90_min != null && !m.indicative) markers.push([E.t0 + m.a.t90_min * MIN, "#8ea2ab", "90% absorbed"]);
    markers.forEach(([t, c, label], i) => {
      if (t > now) return;
      g += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" stroke="${c}" stroke-dasharray="${i ? "2 3" : "0"}" opacity=".85"/>`;
      g += `<text x="${x(t) + 4}" y="${T + 12 + i * 13}" fill="${c}" font-size="11">${label}</text>`;
    });

    // live transcript: a tick on the top edge for every passage that was a surprise for this speaker
    if (E.overTime) {
      for (const p of E.paragraphs) {
        if (p._t > now || !p.relevant || p.z == null || Math.abs(p.z) < 2) continue;
        const c = p.z < 0 ? "#4f9dff" : "#f0b429";
        g += `<path d="M${x(p._t) - 5},${T - 2}L${x(p._t) + 5},${T - 2}L${x(p._t)},${T + 7}Z" fill="${c}"><title>z ${signed(p.z, 1)}: ${esc((p.summary || p.text).slice(0, 80))}</title></path>`;
      }
    }

    // desk orders on this contract: entry (and planned exit) in YES-price terms
    for (const t of (E.desk ? E.desk.trades : [])) {
      if (t.mid !== m.id || t.t > now || t.cost == null) continue;
      const yp = t.side === "yes" ? t.cost : 1 - t.cost;
      const cx = x(t.t), cy = y(yp);
      g += `<path d="M${cx},${cy - 8}L${cx + 7},${cy}L${cx},${cy + 8}L${cx - 7},${cy}Z" fill="#ffffff" stroke="#0b1116" stroke-width="1.5"><title>BUY ${t.side.toUpperCase()} ×${t.qty} @ ${cents(t.cost)}</title></path>`;
      g += `<text x="${cx + 10}" y="${cy - 10}" fill="#ffffff" font-size="11" font-weight="600">BUY ${t.side.toUpperCase()} ×${t.qty}</text>`;
      if (t.exitT && now >= t.exitT && t.value != null) {
        const ex = x(t.exitT), ey = y(t.side === "yes" ? t.value : 1 - t.value);
        g += `<line x1="${cx}" y1="${cy}" x2="${ex}" y2="${ey}" stroke="#ffffff" stroke-dasharray="3 3" opacity=".7"/>`;
        g += `<rect x="${ex - 5}" y="${ey - 5}" width="10" height="10" fill="#ffffff"/>`;
        g += `<text x="${ex + 8}" y="${ey + 4}" fill="#ffffff" font-size="11" font-weight="600">exit ${money(t.pnlFinal)}</text>`;
      }
    }

    // now cursor
    if (now >= E.ws && now <= E.xe) g += `<line x1="${x(now)}" x2="${x(now)}" y1="${T}" y2="${H - B}" stroke="#22d3ee" opacity=".35"/>`;

    wrap.innerHTML = svgEl(W, H, g);
  }

  function sparkline(E, m, now, w = 260, h = 28) {
    const x = (t) => ((t - E.ws) / (E.xe - E.ws)) * w;
    const y = yScale(m.lo, m.hi, m.tick, 2, h - 2, 4);
    const mids = m.q.filter((q) => q.mid != null).map((q) => ({ t: q.t, v: q.mid }));
    const until = Math.min(now, E.we);
    const c = m.isKalshi ? "#22d3ee" : "#f0b429";
    let g = `<line x1="${x(E.t0)}" x2="${x(E.t0)}" y1="0" y2="${h}" stroke="#3a4b56" stroke-dasharray="2 2"/>`;
    g += `<path d="${stepPath(mids, x, y, until)}" fill="none" stroke="${c}" stroke-width="1.4" ${m.indicative ? 'stroke-dasharray="4 3"' : ""}/>`;
    if (m.alarm && m.alarm <= now && !m.indicative) g += `<circle cx="${x(m.alarm)}" cy="${h / 2}" r="3" fill="#84f04f"/>`;
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${g}</svg>`;
  }

  // ---------- replay view ----------
  function renderReplayShell(E) {
    $("view").innerHTML = `
      <main>
        <section class="pane" id="speechPane"></section>
        <section class="pane">
          <h2>Absorption · focused market</h2>
          <div class="chart-head" id="chartHead"></div>
          <div class="chart-wrap" id="chartWrap"></div>
          <div class="legend">
            <span><i style="background:#22d3ee"></i>Kalshi midpoint (1-min candle close)</span>
            <span><i style="background:rgba(34,211,238,.25)"></i>bid–ask</span>
            <span><i style="background:rgba(132,240,79,.6);border-radius:50%"></i>trade, taker yes</span>
            <span><i style="background:rgba(239,91,91,.6);border-radius:50%"></i>trade, taker no</span>
            <span><i style="background:#f0b429"></i>Polymarket (indicative, dashed)</span>
          </div>
          <div class="explain" id="explain"></div>
        </section>
        <section class="pane">
          <div id="desk"></div>
          <h2>Markets · ${E.markets.length} tracked</h2>
          <div class="cards" id="cards"></div>
        </section>
      </main>`;
    $("desk").addEventListener("click", (ev) => {
      const o = ev.target.closest(".order");
      if (!o) return;
      state.focus[state.view] = o.dataset.id;
      state.focusPinned = state.focusPinned || {};
      state.focusPinned[state.view] = true;
      draw(true);
    });
    $("cards").addEventListener("click", (ev) => {
      const card = ev.target.closest(".card");
      if (!card) return;
      const cur = state.events[state.view] || E;
      if (ev.target.closest(".evlink")) { openEvidence(cur, cur.byId[card.dataset.id]); return; }
      state.focus[state.view] = card.dataset.id;
      state.focusPinned = state.focusPinned || {};
      state.focusPinned[state.view] = true;
      draw(true);
    });
  }

  function renderSummary(E) {
    const counts = {};
    for (const m of E.markets) if (m.status) counts[m.status] = (counts[m.status] || 0) + 1;
    const h = E.headline;
    const a = h && h.a;
    let headline = "";
    if (a && a.final_status_pending) {
      headline = `<span class="headline">${esc(h.label)}${h.alarm ? `: alarm at <b class="num">${relMin(h.alarm - E.t0)}</b>` : ""} · <span class="chip ${a.status}">${STATUS_TEXT[a.status] || a.status}</span></span>`;
    } else if (a) {
      const when = h.alarm ? `, alarm at ${relMin(h.alarm - E.t0)}` : "";
      headline = `<span class="headline">${esc(h.label)}: <b class="num">${cents(a.pre_mid)}</b> before T0, move <b class="num">${signed(a.move_ticks)} ticks</b>${when} · <span class="chip ${a.status}">${STATUS_TEXT[a.status] || a.status}</span></span>`;
    }
    const parts = STATUS_ORDER.filter((s) => counts[s]).map((s) => `<span><b class="num">${counts[s]}</b> ${STATUS_TEXT[s].toLowerCase()}</span>`);
    if (E.live) {
      const sim = E.live.kind === "simulated";
      const age = Date.now() - parseT(E.live.updated_at);
      const stale = !sim && age > STALE_MS;
      const badge = sim
        ? `<span class="chip indic">SIMULATED LIVE · ${esc(E.live.source || "")}</span>`
        : stale ? `<span class="chip candidate_inconsistency">STALE · last update ${Math.round(age / 1000)}s ago, is tradio/live.py running?</span>`
                : `<span class="chip live-chip"><span class="dot"></span>LIVE</span>`;
      if (parts.length) parts.unshift(`<span style="color:var(--faint)">so far (final verdicts at T0+90m):</span>`);
      headline = badge + (headline ? " " + headline : "");
    }
    $("summary").innerHTML = `<b>${esc(E.tl.title)}</b>${headline}<span style="margin-left:auto"></span>${parts.join("")}` +
      (E.hasAnalysis ? "" : `<span class="chip cant_tell_illiquid">analysis.json missing</span>`);
  }

  function renderSpeech(E, now) {
    const pane = $("speechPane");
    const released = now >= E.t0;
    const src = E.tl.speech && E.tl.speech.source_url;
    const speaker = (E.tl.speaker || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    if (E.overTime) return renderLiveSpeech(E, now, speaker);
    let html = `<h2>Speech · ${esc(speaker)}</h2>
      <div class="speech-meta">Prepared text released whole at <b class="num">${fmtEt(E.t0)} ET</b> (${fmtUtc(E.t0)} UTC). ${src ? `<a href="${esc(src)}" target="_blank" rel="noopener">federalreserve.gov ↗</a>` : ""}</div>`;
    if (!released) {
      html += `<div class="embargo">Text not yet public<span class="big num">${fmtRel(now - E.t0)}</span>Markets are trading on expectations only.</div>`;
      pane.innerHTML = html;
      return;
    }
    html += `<div class="released">● Text released ${fmtRel(now - E.t0).replace("T+", "")} ago</div>`;
    const ps = E.paragraphs;
    if (!ps.length) {
      html += `<div class="pending-note">Paragraph scores aren't in <span class="mono">timeline.json</span> yet. Role 3's <span class="mono">surprise.py</span> fills <span class="mono">speech.paragraphs</span> (stance, z vs. the speaker's own baseline). Nothing is shown here until then, so no number on this screen is invented.</div>`;
      pane.innerHTML = html;
      return;
    }
    const rel = ps.filter((p) => p.relevant && p.z != null);
    const top = rel.slice().sort((a, b) => Math.abs(b.z) - Math.abs(a.z))[0];
    const tint = (z) => {
      if (z == null) return "var(--line-2)";
      const k = Math.min(1, Math.abs(z) / 3);
      return z < 0 ? `rgba(79,157,255,${0.25 + 0.75 * k})` : `rgba(240,180,41,${0.25 + 0.75 * k})`;
    };
    const one = (p, pinned) => {
      let text = esc(p.text);
      if (p.key_sentence) {
        const k = esc(p.key_sentence);
        text = text.includes(k) ? text.replace(k, `<span class="key">${k}</span>`) : text;
      }
      const z = p.z != null ? `z ${signed(p.z, 1)} · stance ${signed(p.stance, 2)}` : "not scored";
      return `<div class="para ${p.relevant ? "" : "irrelevant"} ${pinned ? "pinned" : ""}" style="border-left-color:${p.relevant ? tint(p.z) : "var(--line)"}"><span class="z">${z}</span>${text}</div>`;
    };
    if (top) html += `<div class="group-title">Biggest surprise vs. ${esc(speaker.split(" ").pop())}'s own history</div>` + one(top, true);
    html += `<div class="group-title">Full text · blue = dovish, amber = hawkish, grey = not about policy</div>`;
    html += ps.filter((p) => p !== top).map((p) => one(p, false)).join("");
    pane.innerHTML = html;
  }

  function paraHtml(p, pinned, withTime) {
    const tint = (z) => {
      if (z == null) return "var(--line-2)";
      const k = Math.min(1, Math.abs(z) / 3);
      return z < 0 ? `rgba(79,157,255,${0.25 + 0.75 * k})` : `rgba(240,180,41,${0.25 + 0.75 * k})`;
    };
    const z = p.z != null ? `z ${signed(p.z, 1)} · stance ${signed(p.stance, 2)}` : (p.role && p.role !== "speaker" ? esc(p.role) : "not scored");
    const when = withTime ? `<span class="t">${fmtEt(p._t)} ET</span> ` : "";
    return `<div class="para ${p.relevant ? "" : "irrelevant"} ${pinned ? "pinned" : ""}" style="border-left-color:${p.relevant ? tint(p.z) : "var(--line)"}"><span class="z">${z}</span>${when}${esc(p.text)}</div>`;
  }

  function renderLiveSpeech(E, now, speaker) {
    const sp = E.tl.speech || {};
    const heard = E.paragraphs.filter((p) => p._t <= now);
    let html = `<h2>Speech · ${esc(speaker || sp.speaker || "")} · live transcript</h2>
      <div class="speech-meta">Transcribed and scored as it's spoken by MarketPulse (<span class="mono">live.py</span>), z against ${esc((speaker || "the speaker").split(" ").pop())}'s own history${sp.baseline ? ` (n = ${sp.baseline.n})` : ""}. ${sp.status ? `Status: <b>${esc(sp.status)}</b>.` : ""}</div>`;
    if (!heard.length) {
      html += `<div class="pending-note">No speech passages yet. Start the live scorer on the laptop:<br><span class="mono">python live.py --speaker &lt;name&gt; --no-prewarm --mode dry</span><br>Passages appear here as they're scored, newest on top. Markets on the right update either way.</div>`;
      $("speechPane").innerHTML = html;
      return;
    }
    const surprises = heard.filter((p) => p.relevant && p.z != null && Math.abs(p.z) >= 2);
    const top = heard.filter((p) => p.relevant && p.z != null).sort((a, b) => Math.abs(b.z) - Math.abs(a.z))[0];
    html += `<div class="released">● ${heard.length} passages heard · ${surprises.length} surprise${surprises.length === 1 ? "" : "s"} (|z| ≥ 2)</div>`;
    if (top) html += `<div class="group-title">Biggest surprise so far</div>` + paraHtml(top, true, true);
    html += `<div class="group-title">Newest first · blue = dovish, amber = hawkish, grey = reporter / not scored</div>`;
    html += heard.slice().sort((a, b) => b._t - a._t).map((p) => paraHtml(p, false, true)).join("");
    $("speechPane").innerHTML = html;
  }

  function renderChartHead(E, m, now) {
    const q = quoteAt(m, now);
    const ind = m.indicative ? `<span class="indic-banner">INDICATIVE · NO TIMING CLAIMS</span>` : "";
    const ba = q && q.bid != null ? ` <span style="color:var(--muted);font-size:12px">${cents(q.bid)} / ${cents(q.ask)}</span>` : "";
    $("chartHead").innerHTML = `<span class="title">${esc(m.label)}</span><span class="venue" style="color:var(--muted)">${esc(m.venue)} · ${esc(m.exposure)} exposure</span>${ind}<span class="live">${q ? cents(q.mid) : "—"}${ba}</span>`;
    const a = m.a;
    // Same reveal rule as the cards: no verdict before T0, and a reaction only once its alarm time passes.
    const revealed = a && now >= verdictAt(E, m);
    if (!revealed) { $("explain").innerHTML = a ? `<b>Verdict:</b> <span class="chip watching">${now < E.t0 ? "before T0" : "watching…"}</span>` : ""; return; }
    const bits = [`<b>Verdict:</b> <span class="chip ${a.status}">${STATUS_TEXT[a.status] || a.status}</span>`];
    if (a.reason) bits.push(esc(a.reason));
    if (a.status === "cant_tell_illiquid") bits.push("Too little trading to tell a slow market from a quiet one, so Tradio doesn't call it slow.");
    if (m.indicative) bits.push("Polymarket price history changed depending on the query window, so it's shown for direction only.");
    $("explain").innerHTML = bits.join(" ");
  }

  function renderCards(E, now) {
    const focus = state.focus[state.view];
    let html = "";
    for (const m of E.markets) {
      const a = m.a || {};
      const q = quoteAt(m, now);
      let chip;
      if (!m.status) chip = `<span class="chip watching">no analysis</span>`;
      else if (now < E.t0) chip = `<span class="chip watching">before T0</span>`;
      else if (now < verdictAt(E, m)) chip = `<span class="chip watching">watching…</span>`;
      else chip = `<span class="chip ${m.status}">${STATUS_TEXT[m.status] || m.status}</span>`;
      const fired = m.alarm && now >= m.alarm && !state.fired.has(state.view + m.id);
      if (fired) state.fired.add(state.view + m.id);
      const facts = [];
      facts.push(`liq <b>${esc(a.liquidity || "—")}</b>`);
      if (a.pre_mid != null) facts.push(`pre <b>${cents(a.pre_mid)}</b>`);
      if (now >= verdictAt(E, m) && a.move_ticks != null) facts.push(`move <b>${signed(a.move_ticks)}t</b>`);
      if (m.alarm && !m.indicative && now >= m.alarm) facts.push(`alarm <b>${relMin(m.alarm - E.t0)}</b>`);
      if (a.t90_min != null && !m.indicative && now >= E.t0 + a.t90_min * MIN) facts.push(`t90 <b>${a.t90_min}m</b>`);
      const showReason = now >= verdictAt(E, m);
      const mp = modelPrediction(E, m);
      html += `<div class="card ${m.id === focus ? "focus" : ""} ${fired ? "fired" : ""}" data-id="${esc(m.id)}">
        <div class="row"><span class="lab">${esc(m.label)}</span><span class="venue">${esc(m.venue)}${m.exposure === "partial" ? " · partial" : ""}</span><span class="px">${q ? cents(q.mid) : "—"}</span></div>
        ${sparkline(E, m, now)}
        <div class="row">${chip}${m.indicative ? `<span class="chip indic">indicative</span>` : ""}<button class="evlink">evidence ›</button></div>
        <div class="facts">${facts.join("")}</div>
        ${mp ? modelLine(mp) : ""}
        ${showReason && a.reason ? `<div class="reason">${esc(a.reason)}</div>` : ""}
      </div>`;
    }
    $("cards").innerHTML = html;
  }

  // ---------- compare view ----------
  function renderCompareShell() {
    $("summary").innerHTML = `<b>Bowman vs Waller</b><span class="headline">Same Kalshi July 2025 contracts, both clocks aligned at the moment the text went public. Same cents-per-pixel scale on every chart.</span>`;
    $("view").innerHTML = `<div class="compare-grid" id="cmp"></div>`;
  }

  function drawCompare(now_rel) {
    const evs = COMPARE_PAIR.map((id) => state.events[id]).filter(Boolean);
    if (evs.length < 2) return;
    const rows = COMPARE_SUFFIXES.map(([suf, name]) => ({ name, ms: evs.map((E) => E.markets.find((m) => m.isKalshi && m.id.endsWith(suf))) }));
    // Common vertical span (in dollars) per row so equal moves look equal on both sides.
    for (const r of rows) r.span = Math.max(0.06, ...r.ms.filter(Boolean).map((m) => m.hi - m.lo));
    const W = 560, H = 190, L = 44, R = 10, T = 14, B = 20;
    let html = "";
    evs.forEach((E, i) => {
      const meta = EVENTS.find((e) => e.id === Object.keys(state.events).find((k) => state.events[k] === E));
      html += `<div class="cmp-col"><h3>${esc(E.tl.title)}</h3><div class="sub">${esc(meta ? meta.sub : "")} · text released ${fmtEtShort(E.t0)} ET</div>`;
      for (const r of rows) {
        const m = r.ms[i];
        if (!m) { html += `<div class="cmp-chart">missing ${esc(r.name)}</div>`; continue; }
        const now = E.t0 + now_rel;
        const ws = E.t0 - 30 * MIN, we = E.t0 + 90 * MIN;
        const x = (t) => L + ((t - ws) / (we - ws)) * (W - L - R);
        const c = (m.hi + m.lo) / 2;
        const y = yScale(c - r.span / 2, c + r.span / 2, m.tick, T, H - B, 2);
        let g = "";
        for (const v of yTicks(y, 4)) {
          g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#1d2a33"/><text x="${L - 5}" y="${y(v) + 4}" fill="#8ea2ab" font-size="10" text-anchor="end" font-family="ui-monospace,Menlo,monospace">${cents(v)}</text>`;
        }
        for (const mm of [-30, 0, 30, 60, 90]) {
          g += `<text x="${x(E.t0 + mm * MIN)}" y="${H - 5}" fill="#5d717b" font-size="10" text-anchor="middle" font-family="ui-monospace,Menlo,monospace">${mm === 0 ? "T0" : (mm > 0 ? "+" : "") + mm + "m"}</text>`;
        }
        g += `<line x1="${x(E.t0)}" x2="${x(E.t0)}" y1="${T}" y2="${H - B}" stroke="#e3edf0" stroke-dasharray="3 3"/>`;
        const until = Math.min(now, we);
        g += `<path d="${bandPath(m.q.filter((q) => q.t >= ws), x, y, until)}" fill="rgba(34,211,238,.13)"/>`;
        g += `<path d="${stepPath(m.q.filter((q) => q.t >= ws && q.mid != null).map((q) => ({ t: q.t, v: q.mid })), x, y, until)}" fill="none" stroke="#22d3ee" stroke-width="2"/>`;
        if (m.alarm && m.alarm <= now) g += `<line x1="${x(m.alarm)}" x2="${x(m.alarm)}" y1="${T}" y2="${H - B}" stroke="#84f04f"/><text x="${x(m.alarm) + 4}" y="${T + 10}" fill="#84f04f" font-size="10">alarm ${relMin(m.alarm - E.t0)}</text>`;
        const q = quoteAt(m, now);
        const shown = m.status && now >= verdictAt(E, m);
        const chip = shown ? `<span class="chip ${m.status}">${STATUS_TEXT[m.status]}</span>` : `<span class="chip watching">${now < E.t0 ? "before T0" : "watching…"}</span>`;
        const mv = shown && m.a ? ` · move <b class="num">${signed(m.a.move_ticks)}t</b>` : "";
        html += `<div class="cmp-chart"><div class="hd"><b>${esc(r.name)}</b>${chip}<span style="color:var(--muted);font-size:12px">pre ${cents(m.a?.pre_mid)}${mv}</span><span class="px">${q ? cents(q.mid) : "—"}</span></div>${svgEl(W, H, g).replace("<svg ", '<svg style="width:100%;height:190px" ')}</div>`;
      }
      html += `</div>`;
    });
    html += `<div class="cmp-note">Statuses, alarm times and moves come from role 2's <span class="mono">analysis.json</span>; prices are Kalshi 1-minute candles (time = candle end). Waller spoke at 6:30 p.m. ET, when these contracts traded thinly, so a flat line there is labelled "can't tell" rather than "slow".</div>`;
    $("cmp").innerHTML = html;
  }

  // ---------- evidence drawer ----------
  function quoteRow(q) {
    return q ? `<tr><td>${esc(q.t)}</td><td>${cents(q.bid)}</td><td>${cents(q.ask)}</td><td>${cents(q.mid)}</td><td>${q.imputed ? "imputed" : ""}</td><td>${q.volume ?? ""}</td></tr>` : "";
  }
  const QHEAD = `<tr><th>candle end (UTC)</th><th>bid</th><th>ask</th><th>mid</th><th></th><th>vol</th></tr>`;
  const raw = (o, label = "raw API row") => (o ? `<details><summary>${label}</summary><pre>${esc(JSON.stringify(o, null, 2))}</pre></details>` : "");

  function openEvidence(E, m) {
    if (!m) return;
    const e = m.evd, a = m.a;
    let h = `<div class="src">${esc(m.id)} · ${esc(m.venue)}${m.indicative ? " · <b>indicative</b>" : ""}</div>`;
    if (a) {
      h += `<h4>Role 2 verdict (analysis.json)</h4><table>
        <tr><th>status</th><td><span class="chip ${a.status}">${STATUS_TEXT[a.status] || a.status}</span></td></tr>
        <tr><th>liquidity</th><td>${esc(a.liquidity)}</td></tr>
        <tr><th>pre-event mid</th><td>${cents(a.pre_mid)}</td></tr>
        <tr><th>alarm</th><td>${a.alarm_t ? esc(a.alarm_t) + " (" + relMin(parseT(a.alarm_t) - E.t0) + ")" : "—"}</td></tr>
        <tr><th>confirming trade</th><td>${esc(a.confirmed_by_trade || "—")}</td></tr>
        <tr><th>move</th><td>${signed(a.move_ticks)} ticks</td></tr>
        <tr><th>t50 / t90</th><td>${a.t50_min ?? "—"} / ${a.t90_min ?? "—"} min</td></tr>
        <tr><th>reason</th><td>${esc(a.reason || "")}</td></tr></table>${raw(a, "full analysis.json entry")}`;
    }
    if (!e) {
      h += `<h4>Evidence</h4><p class="src">No evidence.json entry for this market.</p>`;
    } else {
      if (e.pre_event) h += `<h4>Last quote before T0</h4><table>${QHEAD}${quoteRow(e.pre_event.quote)}</table>${raw(e.pre_event.raw_candle, "raw candle")}`;
      if (e.first_quote_change) {
        const f = e.first_quote_change;
        h += `<h4>First quote change after T0${f.minutes_after_t0 != null ? ` · +${f.minutes_after_t0} min` : ""}</h4><table>${QHEAD}${quoteRow(f.previous_quote)}${quoteRow(f.quote)}</table>${raw(f.raw_candle, "raw candle")}`;
      }
      if (e.first_trade_outside_spread) {
        const f = e.first_trade_outside_spread, t = f.trade || {};
        h += `<h4>First trade outside the pre-T0 spread${f.seconds_after_t0 != null ? ` · +${f.seconds_after_t0}s` : ""}</h4><table><tr><th>time (UTC)</th><th>price</th><th>size</th><th>taker</th></tr><tr><td>${esc(t.t)}</td><td>${cents(t.price)}</td><td>${t.size ?? ""}</td><td>${esc(t.taker_side)}</td></tr></table>${raw(f.raw_trade)}`;
      }
      if (Array.isArray(e.biggest_trades) && e.biggest_trades.length) {
        h += `<h4>Biggest trades, T0 to T0+30 min</h4><table><tr><th>time (UTC)</th><th>price</th><th>size</th><th>taker</th></tr>` +
          e.biggest_trades.map((b) => { const t = b.trade || b; return `<tr><td>${esc(t.t)}</td><td>${cents(t.price)}</td><td>${t.size ?? ""}</td><td>${esc(t.taker_side)}</td></tr>`; }).join("") + `</table>`;
      }
      if (e.end_of_window) h += `<h4>End of window</h4><table>${QHEAD}${quoteRow(e.end_of_window.quote)}</table>${raw(e.end_of_window.raw_candle, "raw candle")}`;
      if (e.source) {
        h += `<h4>Source</h4>` + Object.entries(e.source).map(([k, v]) => `<div class="src"><b>${esc(k)}:</b> ${esc(typeof v === "string" ? v : JSON.stringify(v))}</div>`).join("");
      }
    }
    $("drawerTitle").textContent = m.label;
    $("drawerBody").innerHTML = h;
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
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
    overlap_speeches: "another Board speech within 90 min", release_in_window: "jobs/CPI report lands inside the window",
    release_day: "jobs/CPI report day", has_embedding: "speech text embedded",
    novelty_vs_speaker: "speech unlike the speaker's earlier ones", novelty_vs_recent: "speech unlike recent Board speeches",
  };
  const pct = (p) => (p == null ? "—" : p < 0.01 ? "<1%" : p > 0.99 ? ">99%" : Math.round(p * 100) + "%");

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
    return `<div class="model-line" title="${esc(MODEL_NAMES[mp.model] || mp.model)}, trained on ${mp.trainedOn} earlier speeches${mp.base != null ? `; simple guessing said ${pct(mp.base)}` : ""}">
      <span>Model before T0</span><span class="mbar"><i style="width:${w}%"></i></span><b class="num">${pct(mp.p)}</b><span class="muted">chance to react</span></div>`;
  }

  async function openModel() {
    const M = await loadModel();
    $("drawerTitle").textContent = "Model · predicting which markets react";
    if (!M) {
      $("drawerBody").innerHTML = `<p class="src note">No model yet. Run <span class="mono">python -m tradio.learner</span>.</p>`;
    } else {
      const sb = M.scoreboard || {}, models = sb.models || {};
      const names = Object.keys(models);
      const maxAbs = Math.max(0.05, ...names.map((n) => Math.abs(models[n].brier_skill || 0)));
      const bar = (v) => {
        const w = Math.round((Math.abs(v) / maxAbs) * 50);
        return `<span class="skill"><span class="axis"></span><i class="${v < 0 ? "neg" : "pos"}" style="${v < 0 ? `right:50%` : `left:50%`};width:${w}%"></i></span>`;
      };
      let h = `<p class="verdict">${esc(M.verdict)}</p>
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
          hp.map((e) => e.rows.map((r) => `<tr><td>${esc(e.event)}</td><td>${esc(r.market.replace("kalshi:KXFEDDECISION-", ""))}</td><td class="num">${pct(r[M.champion])}</td><td>${r.label ? "reacted" : "no reaction"}</td></tr>`).join("")).join("") +
          `</table><p class="src note">Each made from earlier speeches only. A low number that reacted is a genuine surprise, not a bug.</p>`;
      }
      h += `<p class="src note">${esc(M.caveat)} Built ${esc(M.built_at)} by <span class="mono">python -m tradio.learner</span>; every archived live speech retrains it.</p>`;
      $("drawerBody").innerHTML = h;
    }
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
  }

  async function openPlaybook() {
    const pb = await fetchJson(DATA_ROOT + "playbook.json", true);
    $("drawerTitle").textContent = "Playbook · learned from past speeches";
    if (!pb) {
      $("drawerBody").innerHTML = `<p class="src">No playbook yet. Run <span class="mono">python tradio/playbook.py</span>.</p>`;
    } else {
      const r = pb.rules, row = (name, k, fmt) => `<tr><th>${name}</th><td><b>${fmt ? fmt(r[k].value) : esc(JSON.stringify(r[k].value))}</b><div class="src">${esc(r[k].why)}</div></td></tr>`;
      let h = `<p class="src">${esc(pb.caveat)} Built ${esc(pb.built_at)}.</p><h4>Rules the desk follows (<span class="mono">python live.py --playbook</span>)</h4><table>
        ${row("Trade only if |z| ≥", "min_abs_z", (v) => v)}
        ${row("Contract types", "trade_kinds", (v) => v.join(", ").replace(/_/g, " "))}
        ${row("Liquidity", "tradeable_liquidity", (v) => v.join(", "))}
        ${row("Planned exit after", "exit_after_min", (v) => v + " min")}
        ${row("Skip if already moved", "skip_if_alarmed", (v) => (v ? "yes" : "no"))}
        ${row("Direction right", "direction_hit_rate", (v) => (v == null ? "—" : Math.round(v * 100) + "%"))}
        ${row("Hold side follows the regime", "hold_side_by_regime", (v) => (v ? "yes" : "no"))}
      </table><h4>What it learned from</h4><table><tr><th>event</th><th>top z</th><th>direction</th><th>market leaning</th><th>reacted</th></tr>` +
        pb.events.map((o) => `<tr><td>${esc(o.event)}</td><td>${signed(o.top_z, 2)}</td><td>${esc(o.direction)}</td><td>${esc(o.regime || "—")}</td><td>${o.reacted ? "yes" : "no"}</td></tr>`).join("") + `</table>`;
      if (pb.backtests) {
        h += `<h4>Backtest, leave-one-out (each event traded with rules learned from the others)</h4><table><tr><th>event</th><th>trades</th><th>P&amp;L</th><th>capital</th></tr>` +
          pb.backtests.map((b) => `<tr><td>${esc(b.event)}</td><td>${b.trades}</td><td>${money(b.total_pnl)}</td><td>$${(b.capital || 0).toFixed(2)}</td></tr>`).join("") +
          `</table><p class="src">Real Kalshi 1-minute prices; entry 1 min after T0 at the ask, exit at the bid, Kalshi taker fees on both legs. Not trading advice.</p>`;
      }
      $("drawerBody").innerHTML = h;
    }
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
  }

  // ---------- clock + controls ----------
  function currentRange() {
    if (state.view === "compare") return [-30 * MIN, 90 * MIN];
    const E = state.events[state.view];
    if (E && E.live) return [E.ws - E.t0, (E.liveNow || E.we) - E.t0];
    return E ? [E.ws - E.t0, E.we - E.t0] : [-60 * MIN, 90 * MIN];
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
    if (state.view === "live") {
      state.playing = false;
      $("btnPlay").textContent = state.follow ? "● Live" : "Go live";
      $("btnPlay").classList.toggle("on", state.follow);
      return;
    }
    state.playing = p;
    $("btnPlay").textContent = p ? "❚❚ Pause" : "▶ Play";
    $("btnPlay").classList.toggle("on", p);
  }

  function renderMarks() {
    const [a, b] = currentRange();
    const pct = (r) => ((r - a) / (b - a)) * 100;
    let html = `<span class="mark t0" style="left:${pct(0)}%">T0</span>`;
    if (state.view !== "compare") {
      const E = state.events[state.view];
      const alarms = E.markets.filter((m) => m.alarm && !m.indicative).sort((x, y) => x.alarm - y.alarm);
      let lastPct = pct(0);
      for (const m of alarms) {
        const r = m.alarm - E.t0, p = pct(r);
        const label = p - lastPct > 3 ? relMin(r).replace("T0", "") : "";
        if (label) lastPct = p;
        html += `<span class="mark alarm" style="left:${p}%" title="${esc(m.label)}: alarm ${relMin(r)}">${label}</span>`;
      }
      html += `<span class="mark" style="left:2%">${fmtEtShort(E.ws)} ET</span><span class="mark" style="left:98%">${E.live ? "now" : fmtEtShort(E.we) + " ET"}</span>`;
    }
    $("marks").innerHTML = html;
    const s = $("scrub");
    s.min = a; s.max = b;
  }

  function draw(force) {
    const t = performance.now();
    if (!force && t - state.lastDraw < 66) return; // ~15 fps is plenty for minute candles
    state.lastDraw = t;
    $("scrub").value = state.rel;

    if (state.view === "compare") {
      const E = state.events[COMPARE_PAIR[0]];
      // Two events, two wall clocks: only the T0-relative clock means anything here.
      $("clkEt").parentElement.hidden = true; $("clkUtc").parentElement.hidden = true;
      setRelClock();
      drawCompare(state.rel);
      return;
    }
    const E = state.events[state.view];
    if (!E) return;
    const now = E.t0 + state.rel;
    $("clkEt").parentElement.hidden = false; $("clkUtc").parentElement.hidden = false;
    $("clkEt").textContent = fmtEt(now);
    $("clkUtc").textContent = fmtUtc(now);
    setRelClock();
    const m = E.byId[state.focus[state.view]] || E.headline;
    renderSpeech(E, now);
    renderChartHead(E, m, now);
    drawMainChart(E, m, now);
    renderDesk(E, now);
    renderCards(E, now);
  }

  function setRelClock() {
    const el = $("clkRel");
    el.textContent = fmtRel(state.rel);
    el.classList.toggle("pre", state.rel < 0);
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
    $("tabs").innerHTML = all.map((e) => `<button class="tab ${e.id === "compare" ? "compare" : ""} ${e.id === "live" ? "live" : ""} ${state.view === e.id ? "on" : ""}" data-id="${e.id}"><b>${esc(e.short)}</b><small>${esc(e.sub)}</small></button>`).join("");
  }

  async function show(id) {
    closeDrawer();
    stopLive();
    state.view = id;
    $("liveControls").hidden = id !== "live";
    renderTabs();
    $("speeds").hidden = id === "live";
    if (id === "live") { if (location.hash !== "#live") history.replaceState(null, "", "#live"); return startLive(); }
    setPlaying(false);
    try {
      if (id === "compare") {
        await Promise.all(COMPARE_PAIR.map(loadEvent));
        renderCompareShell();
      } else {
        const E = await loadEvent(id);
        const pref = (EVENTS.find((e) => e.id === id) || {}).focus;
        if (pref && E.byId[pref]) E.headline = E.byId[pref];
        if (!state.focus[id]) state.focus[id] = E.headline.id;
        renderReplayShell(E);
        renderSummary(E);
      }
    } catch (err) {
      $("view").innerHTML = `<div class="error">Couldn't load data for ${esc(id)}: ${esc(err.message)}.<br>Serve the <span class="mono">web/</span> folder over HTTP (<span class="mono">cd web && python3 -m http.server 8000</span>) and open <span class="mono">http://localhost:8000/tradio.html</span>; browsers block fetch() from file:// pages.</div>`;
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
      $("summary").innerHTML = `<b>LIVE</b><span class="chip watching">nothing running</span>`;
      $("view").innerHTML = liveIdleHtml();
      $("marks").innerHTML = "";
      return;
    }
    if (state.view !== "live") return;
    const key = E.tl.t0 + "|" + E.markets.map((m) => m.id).join(",");
    // Follow the story (first alarm, then the biggest reaction) unless the viewer picked a market.
    const pinned = state.focusPinned && state.focusPinned.live && E.byId[state.focus.live];
    if (!pinned) state.focus.live = E.headline && E.headline.id;
    if (state.liveShellKey !== key) { renderReplayShell(E); state.liveShellKey = key; }
    renderSummary(E);
    renderMarks();
    if (state.follow) state.rel = (E.liveNow || E.we) - E.t0;
    setRel(state.rel, true);
    setPlaying(false);
  }

  function liveIdleHtml() {
    return `<div class="idle">
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
    </div>`;
  }

  async function loadArchivedEvents() {
    const extra = await fetchJson(DATA_ROOT + "events.json", true);
    for (const e of Array.isArray(extra) ? extra : []) {
      if (e && e.id && !EVENTS.some((x) => x.id === e.id)) EVENTS.push({ id: e.id, short: e.short || e.id, sub: e.sub || "recorded live", focus: e.focus });
    }
  }

  function init() {
    $("speeds").innerHTML = SPEEDS.map((s) => `<button class="btn ${s === state.speed ? "on" : ""}" data-speed="${s}">${s}×</button>`).join(" ");
    $("speeds").addEventListener("click", (e) => {
      const b = e.target.closest("[data-speed]");
      if (!b) return;
      state.speed = +b.dataset.speed;
      for (const x of $("speeds").children) x.classList.toggle("on", x === b);
    });
    $("tabs").addEventListener("click", (e) => { const b = e.target.closest(".tab"); if (b) show(b.dataset.id); });
    $("btnPlay").addEventListener("click", () => {
      if (state.view === "live") { state.follow = true; pollLive(); return; }
      const [, b] = currentRange();
      if (!state.playing && state.rel >= b) state.rel = -10 * MIN;
      setPlaying(!state.playing);
    });
    $("btnBack").addEventListener("click", () => setRel(state.rel - MIN));
    $("btnFwd").addEventListener("click", () => setRel(state.rel + MIN));
    $("btnT0").addEventListener("click", () => { setPlaying(false); setRel(MIN); });
    $("scrub").addEventListener("input", (e) => setRel(+e.target.value));
    $("drawerClose").addEventListener("click", closeDrawer);
    $("btnPlaybook").addEventListener("click", openPlaybook);
    $("btnModel").addEventListener("click", openModel);
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT" && e.target.type !== "range") return;
      if (e.key === " ") { e.preventDefault(); $("btnPlay").click(); }
      else if (e.key === "ArrowLeft") { e.preventDefault(); setRel(state.rel - MIN); }
      else if (e.key === "ArrowRight") { e.preventDefault(); setRel(state.rel + MIN); }
      else if (e.key === "0") $("btnT0").click();
      else if (e.key === "Escape") closeDrawer();
    });
    window.addEventListener("resize", () => draw(true));
    window.addEventListener("hashchange", () => { const id = location.hash.slice(1); if (id && id !== state.view) show(id); });

    Promise.all([loadArchivedEvents(), loadModel()]).finally(() => {
      const start = location.hash.slice(1);
      const valid = [LIVE, ...EVENTS, COMPARE].some((e) => e.id === start);
      show(valid ? start : EVENTS[0].id);
      requestAnimationFrame(tick);
    });
  }

  init();
})();
