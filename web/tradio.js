/* Tradio replay terminal.
 *
 * Reads the cached files written by roles 1-3 and replays them with a client-side clock:
 *   data/tradio/<event>/timeline.json   (role 1 + role 3's speech paragraphs)
 *   data/tradio/<event>/analysis.json   (role 2)
 *   data/tradio/<event>/evidence.json   (role 1, raw rows behind the numbers)
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
  const COMPARE_PAIR = ["bowman20250623", "waller20250717"];
  const COMPARE_SUFFIXES = [["-C25", "July cut 25 bp"], ["-H0", "July hold"]];
  const SPEEDS = [1, 10, 30, 60];
  const ALARM_STATUSES = new Set(["reacted", "candidate_delay", "candidate_inconsistency"]);
  const MIN = 60000;

  const STATUS_TEXT = {
    reacted: "Reacted",
    expected_no_reaction: "No reaction expected",
    cant_tell_illiquid: "Can't tell · illiquid",
    candidate_delay: "Possible delay",
    candidate_inconsistency: "Possible inconsistency",
  };
  const STATUS_ORDER = ["reacted", "candidate_delay", "candidate_inconsistency", "expected_no_reaction", "cant_tell_illiquid"];

  const state = {
    view: null,          // event id or "compare"
    events: {},          // id -> prepared data
    rel: -10 * MIN,      // replay clock, ms relative to T0 (shared across events so switching keeps position)
    playing: false,
    speed: 30,
    focus: {},           // event id -> focused market id
    fired: new Set(),    // alarm pulses already shown
    lastDraw: 0,
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

  async function loadEvent(id) {
    if (state.events[id]) return state.events[id];
    const base = DATA_ROOT + id + "/";
    const [tl, an, ev] = await Promise.all([
      fetchJson(base + "timeline.json"),
      fetchJson(base + "analysis.json", true),
      fetchJson(base + "evidence.json", true),
    ]);
    const prepared = prep(tl, an, ev);
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
      kal.slice().sort((a, b) => Math.abs(b.a.move_ticks || 0) - Math.abs(a.a.move_ticks || 0))[0] ||
      markets[0];

    const paragraphs = (tl.speech && tl.speech.paragraphs) || [];
    return { tl, an, t0, ws, we, markets, byId, headline, paragraphs, hasAnalysis: !!an };
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
    const x = (t) => L + ((t - E.ws) / (E.we - E.ws)) * (W - L - R);
    const y = yScale(m.lo, m.hi, m.tick, T, H - B);
    const until = Math.min(now, E.we);
    let g = "";

    // grid + y labels
    for (const v of yTicks(y)) {
      g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#1d2a33"/>`;
      g += `<text x="${L - 6}" y="${y(v) + 4}" fill="#8ea2ab" font-size="11" text-anchor="end" font-family="ui-monospace,Menlo,monospace">${cents(v)}</text>`;
    }
    // x labels every 15 min (ET)
    for (let t = Math.ceil(E.ws / (15 * MIN)) * 15 * MIN; t <= E.we; t += 15 * MIN) {
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
    g += `<text x="${x(E.t0) + 5}" y="${T - 8}" fill="#e3edf0" font-size="11">T0 · text released ${fmtEtShort(E.t0)} ET</text>`;

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

    // now cursor
    if (now >= E.ws && now <= E.we) g += `<line x1="${x(now)}" x2="${x(now)}" y1="${T}" y2="${H - B}" stroke="#22d3ee" opacity=".35"/>`;

    wrap.innerHTML = svgEl(W, H, g);
  }

  function sparkline(E, m, now, w = 260, h = 28) {
    const x = (t) => ((t - E.ws) / (E.we - E.ws)) * w;
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
          <h2>Markets · ${E.markets.length} tracked</h2>
          <div class="cards" id="cards"></div>
        </section>
      </main>`;
    $("cards").addEventListener("click", (ev) => {
      const card = ev.target.closest(".card");
      if (!card) return;
      if (ev.target.closest(".evlink")) { openEvidence(E, E.byId[card.dataset.id]); return; }
      state.focus[state.view] = card.dataset.id;
      draw(true);
    });
  }

  function renderSummary(E) {
    const counts = {};
    for (const m of E.markets) if (m.status) counts[m.status] = (counts[m.status] || 0) + 1;
    const h = E.headline;
    const a = h && h.a;
    let headline = "";
    if (a) {
      const when = h.alarm ? `, alarm at ${relMin(h.alarm - E.t0)}` : "";
      headline = `<span class="headline">${esc(h.label)}: <b class="num">${cents(a.pre_mid)}</b> before T0, move <b class="num">${signed(a.move_ticks)} ticks</b>${when} · <span class="chip ${a.status}">${STATUS_TEXT[a.status] || a.status}</span></span>`;
    }
    const parts = STATUS_ORDER.filter((s) => counts[s]).map((s) => `<span><b class="num">${counts[s]}</b> ${STATUS_TEXT[s].toLowerCase()}</span>`);
    $("summary").innerHTML = `<b>${esc(E.tl.title)}</b>${headline}<span style="margin-left:auto"></span>${parts.join("")}` +
      (E.hasAnalysis ? "" : `<span class="chip cant_tell_illiquid">analysis.json missing</span>`);
  }

  function renderSpeech(E, now) {
    const pane = $("speechPane");
    const released = now >= E.t0;
    const src = E.tl.speech && E.tl.speech.source_url;
    const speaker = (E.tl.speaker || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
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
      html += `<div class="card ${m.id === focus ? "focus" : ""} ${fired ? "fired" : ""}" data-id="${esc(m.id)}">
        <div class="row"><span class="lab">${esc(m.label)}</span><span class="venue">${esc(m.venue)}${m.exposure === "partial" ? " · partial" : ""}</span><span class="px">${q ? cents(q.mid) : "—"}</span></div>
        ${sparkline(E, m, now)}
        <div class="row">${chip}${m.indicative ? `<span class="chip indic">indicative</span>` : ""}<button class="evlink">evidence ›</button></div>
        <div class="facts">${facts.join("")}</div>
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
  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
  }

  // ---------- clock + controls ----------
  function currentRange() {
    if (state.view === "compare") return [-30 * MIN, 90 * MIN];
    const E = state.events[state.view];
    return E ? [E.ws - E.t0, E.we - E.t0] : [-60 * MIN, 90 * MIN];
  }

  function setRel(rel) {
    const [a, b] = currentRange();
    state.rel = Math.max(a, Math.min(b, rel));
    if (state.rel >= b) setPlaying(false);
    draw(true);
  }

  function setPlaying(p) {
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
      html += `<span class="mark" style="left:2%">${fmtEtShort(E.ws)} ET</span><span class="mark" style="left:98%">${fmtEtShort(E.we)} ET</span>`;
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
    const all = [...EVENTS, COMPARE];
    $("tabs").innerHTML = all.map((e) => `<button class="tab ${e.id === "compare" ? "compare" : ""} ${state.view === e.id ? "on" : ""}" data-id="${e.id}"><b>${esc(e.short)}</b><small>${esc(e.sub)}</small></button>`).join("");
  }

  async function show(id) {
    closeDrawer();
    state.view = id;
    renderTabs();
    try {
      if (id === "compare") {
        await Promise.all(COMPARE_PAIR.map(loadEvent));
        renderCompareShell();
      } else {
        const E = await loadEvent(id);
        const pref = EVENTS.find((e) => e.id === id).focus;
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
      const [, b] = currentRange();
      if (!state.playing && state.rel >= b) state.rel = -10 * MIN;
      setPlaying(!state.playing);
    });
    $("btnBack").addEventListener("click", () => setRel(state.rel - MIN));
    $("btnFwd").addEventListener("click", () => setRel(state.rel + MIN));
    $("btnT0").addEventListener("click", () => { setPlaying(false); setRel(MIN); });
    $("scrub").addEventListener("input", (e) => setRel(+e.target.value));
    $("drawerClose").addEventListener("click", closeDrawer);
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

    const start = location.hash.slice(1);
    const valid = [...EVENTS, COMPARE].some((e) => e.id === start);
    show(valid ? start : EVENTS[0].id);
    requestAnimationFrame(tick);
  }

  init();
})();
