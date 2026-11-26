"use strict";

const $ = (sel) => document.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_SERIES = 4; // fixed categorical slots; never cycle colors

const state = { report: null, ledger: 0, hover: null, charts: [] };

// ---------- formatting ----------
const moneyFmt = new Intl.NumberFormat("zh-TW", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const intFmt = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 0 });
const money = (v) => moneyFmt.format(Number(v));
const integer = (v) => intFmt.format(Number(v));
const pct = (v, digits = 2) => `${(Number(v) * 100).toFixed(digits)}%`;
const signedPct = (v) => (Number(v) > 0 ? "+" : "") + pct(v);
const num = (v, digits = 4) => Number(v).toFixed(digits);
// Axis percent: as few decimals as the tick needs (−2.5% stays −2.5%, 10% stays 10%).
const tickPct = (v) => `${+(v * 100).toFixed(2)}%`;

function seriesName(label) {
  const named = state.report?.run?.labels?.[label];
  if (named) return named;
  return /^\d+$/.test(label) ? `配置 ${label}%` : `帳本 ${label}`;
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function lineKey(color, dashed) {
  const s = svg("svg", { width: 16, height: 4, "aria-hidden": "true" });
  s.append(svg("line", {
    x1: 0, x2: 16, y1: 2, y2: 2, stroke: color, "stroke-width": 2.5,
    "stroke-linecap": "round", ...(dashed ? { "stroke-dasharray": "4 3" } : {}),
  }));
  return s;
}

// ---------- data loading ----------
async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function setStatus(text, isError = false) {
  const status = $("#status");
  status.hidden = !text;
  status.textContent = text || "";
  status.classList.toggle("error", isError);
}

async function init() {
  try {
    const reports = await getJSON("/api/reports");
    if (!reports.length) {
      setStatus("reports/ 內還沒有任何 ledger_*.csv。先在工作台執行回測並「另存報告」，或執行 python -m finback.backtest <設定檔>");
      return;
    }
    const select = $("#report-select");
    for (const r of reports) {
      select.append(el("option", { value: r.name }, r.synthetic ? `${r.name}（合成資料）` : r.name));
    }
    const fromHash = decodeURIComponent(location.hash.slice(1));
    select.value = reports.some((r) => r.name === fromHash) ? fromHash : reports[0].name;
    select.addEventListener("change", () => { location.hash = encodeURIComponent(select.value); });
    window.addEventListener("hashchange", () => {
      const name = decodeURIComponent(location.hash.slice(1));
      if (reports.some((r) => r.name === name)) { select.value = name; loadReport(name); }
    });
    loadReport(select.value);
  } catch (err) {
    setStatus(`無法讀取報告清單：${err.message}`, true);
  }
}

async function loadReport(name) {
  setStatus("載入中…");
  try {
    const report = await getJSON(`/api/reports/${encodeURIComponent(name)}`);
    state.report = report;
    state.ledger = 0;
    state.hover = null;
    setStatus("");
    $("#content").hidden = false;
    render();
  } catch (err) {
    $("#content").hidden = true;
    setStatus(`無法載入「${name}」：${err.message}`, true);
  }
}

// ---------- page ----------
function seriesList(report) {
  return report.ledgers.slice(0, MAX_SERIES).map((ledger, i) => ({
    name: seriesName(ledger.label),
    color: `var(--series-${i + 1})`,
    ledger,
  }));
}

function render() {
  const report = state.report;
  const series = seriesList(report);
  renderBanner(report);
  renderSummary(report, series);
  renderLegend(series);
  renderCharts(report, series);
  renderLedgerTabs(report);
  renderLedger();
  renderMeta(report);
}

function renderBanner(report) {
  const banner = $("#banner");
  const messages = [];
  if (report.run?.synthetic) messages.push("⚠ 合成教學資料：只用來檢查計算流程，沒有投資績效意義。");
  const failed = report.ledgers.filter((l) => !l.reconciliation.ok);
  if (failed.length) {
    messages.push(`✗ 帳本核對失敗：${failed.map((l) => seriesName(l.label)).join("、")}`
      + "（現金 + 股數 × 收盤價 ≠ 淨值），結果不可信。");
  }
  if (report.ledgers.length > MAX_SERIES) {
    messages.push(`圖表只畫前 ${MAX_SERIES} 本帳本；其餘請看表格。`);
  }
  banner.replaceChildren(...messages.map((m) => el("div", {}, m)));
  banner.hidden = !messages.length;
}

function renderSummary(report, series) {
  const m0 = report.ledgers[0].metrics;
  $("#period").textContent = `${m0.start_date} 至 ${m0.end_date}，共 ${m0.days} 個交易日`;
  const tbody = $("#summary-table tbody");
  tbody.replaceChildren();
  report.ledgers.forEach((ledger, i) => {
    const m = ledger.metrics;
    const tr = el("tr");
    const nameCell = el("td");
    if (series[i]) {
      const chip = el("span", { class: "chip" });
      chip.style.background = "var(--benchmark)";
      nameCell.append(chip);
    }
    nameCell.append(document.createTextNode(seriesName(ledger.label)));
    tr.append(nameCell);
    const cells = [
      [money(m.final_equity)],
      [money(m.net_external_flow)],
      [signedPct(m.total_return), Number(m.total_return) < 0 ? "neg" : ""],
      [pct(m.max_drawdown), Number(m.max_drawdown) < 0 ? "neg" : ""],
      [m.beta === null ? "未定義" : num(m.beta)],
      [`${pct(m.first_exposure, 1)} → ${pct(m.last_exposure, 1)}`],
      [m.trades === undefined ? "—" : integer(m.trades)],
      [money(m.fees_paid)],
      [money(m.dividends)],
    ];
    for (const [text, cls] of cells) tr.append(el("td", { class: `num ${cls || ""}` }, text));
    const rec = ledger.reconciliation;
    tr.append(el("td", { class: rec.ok ? "ok" : "bad", title: rec.rule },
      rec.ok ? "✓ 每日一致" : `✗ 最大差額 ${rec.max_abs_diff}`));
    tbody.append(tr);
  });
}

function renderLegend(series) {
  const legend = $("#legend");
  legend.replaceChildren();
  for (const s of series) {
    const item = el("span");
    item.append(lineKey(s.color, false), document.createTextNode(s.name));
    legend.append(item);
  }
  const bench = el("span");
  bench.append(lineKey("var(--benchmark)", true), document.createTextNode("標的含息報酬"));
  legend.append(bench);
}

function renderCharts(report, series) {
  for (const c of state.charts) c.destroy();
  const dates = report.ledgers[0].rows.map((r) => r.date);
  const pick = (field) => series.map((s) => ({
    name: s.name, color: s.color, values: s.ledger.rows.map((r) => Number(r[field])),
  }));
  const benchmark = {
    name: "標的含息報酬", color: "var(--benchmark)", dashed: true,
    values: report.benchmark.map(Number),
  };
  const fmtIndex = (v) => v.toFixed(4);
  state.charts = [
    lineChart($("#chart-wealth"), {
      dates, series: [...pick("wealth_index"), benchmark], baseline: 1,
      tick: (v) => v.toFixed(2), fmt: fmtIndex, title: "淨值指數",
    }),
    lineChart($("#chart-drawdown"), {
      dates, series: pick("drawdown"), baseline: 0, include: [0],
      tick: tickPct, fmt: (v) => pct(v), title: "回撤",
    }),
    lineChart($("#chart-exposure"), {
      dates, series: pick("gross_exposure"), baseline: 0, include: [0, 1], lanes: true,
      tick: tickPct, fmt: (v) => pct(v, 1), title: "曝險",
    }),
  ];
}

function renderLedgerTabs(report) {
  const tabs = $("#ledger-tabs");
  tabs.replaceChildren();
  report.ledgers.forEach((ledger, i) => {
    const b = el("button", { role: "tab", type: "button", "aria-selected": String(i === state.ledger) },
      seriesName(ledger.label));
    b.addEventListener("click", () => { state.ledger = i; renderLedgerTabs(report); renderLedger(); });
    tabs.append(b);
  });
}

function isEvent(r, tradeByDate) {
  return tradeByDate.has(r.date) || Number(r.fees) !== 0 || Number(r.dividend_cash) !== 0 || Number(r.external_flow) !== 0;
}

function renderLedger() {
  const ledger = state.report.ledgers[state.ledger];
  const eventsOnly = $("#events-only").checked;
  const tbody = $("#ledger-table tbody");
  const tradeByDate = new Map((ledger.trades || []).map((t) => [t.date, t]));
  const frag = document.createDocumentFragment();
  for (const r of ledger.rows) {
    const event = isEvent(r, tradeByDate);
    if (eventsOnly && !event) continue;
    const tr = el("tr", event ? { class: "event" } : {});
    tr.append(el("td", {}, r.date));
    const t = tradeByDate.get(r.date);
    tr.append(el("td", { class: t ? (t.side === "buy" ? "buy" : "sell") : "" },
      t ? `${t.side === "buy" ? "買" : "賣"} ${integer(t.quantity)} @ ${num(t.price, 2)}` : ""));
    const cells = [
      [num(r.close, 2)], [integer(r.quantity)], [money(r.cash)], [money(r.market_value)],
      [money(r.equity)], [Number(r.fees) ? money(r.fees) : "—"],
      [Number(r.dividend_cash) ? money(r.dividend_cash) : "—"],
      [Number(r.external_flow) ? money(r.external_flow) : "—"],
      [signedPct(r.daily_return), Number(r.daily_return) < 0 ? "neg" : ""],
      [pct(r.drawdown), Number(r.drawdown) < 0 ? "neg" : ""],
      [pct(r.gross_exposure, 1)],
    ];
    for (const [text, cls] of cells) tr.append(el("td", { class: `num ${cls || ""}` }, text));
    frag.append(tr);
  }
  tbody.replaceChildren(frag);
}
$("#events-only").addEventListener("change", () => state.report && renderLedger());

const META_LABELS = {
  synthetic: "資料類型",
  kind: "回測類型",
  config_file: "設定檔",
  config_sha256: "設定檔 SHA-256",
  lot_size: "交易單位（股）",
  costs: "成本模型",
  source: "輸入檔",
  source_sha256: "輸入檔 SHA-256",
  initial_cash: "初始資金",
  initial_weight: "初始配置",
  weights: "比較的初始配置",
  fee_rate: "手續費率",
  monthly_deposit: "每月入金",
  deposits_begin: "入金起始",
  reinvest_deposits: "入金再投入",
  execution: "成交假設",
  cash_flows: "現金流處理",
  limitations: "限制",
};

function metaValue(key, value) {
  if (key === "synthetic") return value ? "合成教學資料" : "真實行情";
  if (key === "kind") return value === "strategy_backtest" ? "策略回測（訊號收盤產生、隔日開盤成交）" : value;
  if (key === "costs") {
    return `手續費率 ${pct(value.commission_rate, 4)} × 折扣 ${value.commission_discount}，`
      + `最低 ${value.min_commission} 元；賣出交易稅 ${pct(value.sell_tax_rate, 2)}（皆無條件捨去至元）`;
  }
  if (key === "fee_rate") return `${value}（${pct(value, 3)}）`;
  if (key === "initial_cash" || key === "monthly_deposit") return money(value);
  if (key === "weights") return value.map((w) => pct(w, 0)).join("、");
  if (key === "initial_weight") return pct(value, 0);
  if (typeof value === "boolean") return value ? "是" : "否";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function renderMeta(report) {
  const dl = $("#run-meta");
  dl.replaceChildren();
  // hashes and paths read better in a monospace face
  const add = (k, v) => {
    const mono = /SHA-256|輸入檔|設定檔$/.test(k);
    dl.append(el("dt", {}, k), el("dd", mono ? { class: "mono" } : {}, v));
  };
  const run = report.run;
  if (!run) {
    add("run.json", "此報告沒有執行參數檔");
  } else {
    const prov = run.data_provenance;
    if (prov) {
      const p = prov.profile || {};
      add("資料提供者", [prov.provider, prov.symbol, p.name].filter(Boolean).join(" · "));
      if (p.start) add("設定期間", `${p.start} 至 ${p.end}（${p.price_basis === "unadjusted" ? "未調整價格" : p.price_basis}）`);
      if (prov.first_date) add("資料期間", `${prov.first_date} 至 ${prov.last_date}，${prov.rows} 個交易日（${prov.price_basis === "unadjusted" ? "未調整價格" : prov.price_basis}）`);
      const ca = prov.corporate_actions;
      if (ca) {
        add("除權息事件", `現金股利 ${ca.dividends.length} 次`
          + (ca.splits.length ? `；分割 ${ca.splits.map((x) => `${x.date} 1→${x.ratio}`).join("、")}` : "；無分割"));
      }
      if (prov.sources) add("原始來源", `${prov.sources.length} 個檔案，皆附 SHA-256；詳見原始 run.json`);
      if (p.dividends?.length) {
        add("股息事件", p.dividends.map((d) => `除息 ${d.ex_date}，每單位 ${d.amount} 元（發放 ${d.pay_date}）`).join("；"));
      }
    }
    for (const [key, value] of Object.entries(run)) {
      if (["data_provenance", "config", "labels"].includes(key)) continue;
      add(META_LABELS[key] || key, metaValue(key, value));
    }
  }
  $("#run-json").textContent = run ? JSON.stringify(run, null, 2) : "（無）";
  const md = $("#report-md");
  md.hidden = !report.report_md;
  md.querySelector("pre").textContent = report.report_md || "";
}

// ---------- line chart (SVG, index-spaced trading days) ----------
function niceTicks(min, max, count = 5) {
  const span = max - min || Math.abs(max) || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= raw);
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(+v.toFixed(12));
  return ticks;
}

// Calendar ticks sized to the span: months (< 8), quarters (< 18) or years.
function timeTicks(dates, plotWidth) {
  const months = (Number(dates.at(-1).slice(0, 4)) - Number(dates[0].slice(0, 4))) * 12
    + Number(dates.at(-1).slice(5, 7)) - Number(dates[0].slice(5, 7)) + 1;
  const tight = plotWidth < 420;
  const step = months > (tight ? 9 : 18) ? 12 : months > (tight ? 4 : 8) ? 3 : 1;
  const ticks = [];
  dates.forEach((d, i) => {
    const [y, m] = [d.slice(0, 4), Number(d.slice(5, 7))];
    const newMonth = i > 0 && d.slice(0, 7) !== dates[i - 1].slice(0, 7);
    if (!newMonth || (m - 1) % step !== 0) return;
    ticks.push({ i, text: step === 12 ? y : m === 1 ? `${y}/01` : `${m}月` });
  });
  return ticks;
}

function lineChart(container, opt) {
  let hoverIdx = null;
  let geom = null;
  const root = svg("svg", { role: "img", "aria-label": `${opt.title}折線圖；表格見下方每日帳本` });
  container.replaceChildren(root);

  function draw() {
    const W = container.clientWidth;
    const H = container.clientHeight;
    if (!W || !H) return;
    const directLabels = !opt.lanes && W >= 480;
    const m = opt.lanes ? { l: 104, r: 12, t: 6, b: 24 } : { l: 56, r: directLabels ? 100 : 12, t: 10, b: 24 };
    const n = opt.dates.length;
    const all = opt.series.flatMap((s) => s.values).concat(opt.include || [], [opt.baseline]);
    let lo = Math.min(...all), hi = Math.max(...all);
    const pad = (hi - lo || 1) * 0.06;
    lo -= (opt.include || []).includes(lo) ? 0 : pad;
    hi += pad;
    const ticks = niceTicks(lo, hi, H < 260 ? 4 : 5);
    lo = Math.min(lo, ticks[0]);
    hi = Math.max(hi, ticks[ticks.length - 1]);
    const pw = W - m.l - m.r, ph = H - m.t - m.b;
    const x = (i) => m.l + (n === 1 ? pw / 2 : (i / (n - 1)) * pw);
    const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * ph;
    const laneH = ph / opt.series.length;
    const ys = opt.lanes ? (j, v) => m.t + j * laneH + 3 + (1 - Math.max(0, Math.min(1, v))) * (laneH - 8) : null;
    geom = { x, y, ys, m, pw, ph, n };

    root.replaceChildren();
    root.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const grid = svg("g", { class: "grid" });
    const axis = svg("g", { class: "axis" });
    for (const t of opt.lanes ? [] : ticks) {
      grid.append(svg("line", { x1: m.l, x2: m.l + pw, y1: y(t), y2: y(t) }));
      const label = svg("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end" });
      label.textContent = opt.tick(t);
      axis.append(label);
    }
    // Ticks fall on calendar boundaries; the start date is left to the tooltip/summary.
    let lastX = -Infinity;
    for (const t of timeTicks(opt.dates, pw)) {
      const tx = x(t.i);
      if (tx - lastX < 44 || tx < m.l + 16 || tx > m.l + pw - 16) continue;
      lastX = tx;
      const label = svg("text", { x: tx, y: H - 6, "text-anchor": "middle" });
      label.textContent = t.text;
      axis.append(label);
    }
    root.append(grid, axis);
    if (opt.lanes) {
      opt.series.forEach((s, j) => {
        const base = ys(j, 0);
        root.append(svg("line", { class: "baseline", x1: m.l, x2: m.l + pw, y1: base, y2: base }));
        const d = s.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${ys(j, v).toFixed(1)}`).join("");
        root.append(svg("path", { class: "area", d: `M${x(0)},${base}L${d.slice(1)}L${x(n - 1)},${base}Z`, fill: s.color, "fill-opacity": .18, stroke: "none" }));
        root.append(svg("path", { class: "series", d, stroke: s.color, "stroke-width": 1.25, style: "stroke-width: 1.25" }));
        const label = svg("text", { class: "lane-label", x: m.l - 8, y: m.t + j * laneH + laneH / 2 + 4, "text-anchor": "end" });
        label.textContent = s.name;
        root.append(label);
      });
    } else {
      root.append(svg("line", { class: "baseline", x1: m.l, x2: m.l + pw, y1: y(opt.baseline), y2: y(opt.baseline) }));
      // Thin dashed benchmark goes last so a strategy that tracks it can't hide it.
      const order = [...opt.series].sort((a, b) => (a.dashed ? 1 : 0) - (b.dashed ? 1 : 0));
      for (const s of order) {
        const d = s.values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
        root.append(svg("path", {
          class: "series", d, stroke: s.color, ...(s.dashed ? { "stroke-dasharray": "5 4", "stroke-width": 1.5 } : {}),
        }));
      }
    }

    if (directLabels) {
      const ends = opt.series.map((s) => ({ s, y: y(s.values[n - 1]) })).sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 14);
      const overflow = ends.length ? ends[ends.length - 1].y - (m.t + ph) : 0;
      if (overflow > 0) for (const e of ends) e.y -= overflow;
      for (const e of ends) {
        root.append(svg("line", { x1: m.l + pw + 6, x2: m.l + pw + 14, y1: e.y, y2: e.y,
          stroke: e.s.color, "stroke-width": 2.5, "stroke-linecap": "round", ...(e.s.dashed ? { "stroke-dasharray": "3 2" } : {}) }));
        const t = svg("text", { class: "label", x: m.l + pw + 18, y: e.y + 4 });
        t.textContent = e.s.name;
        root.append(t);
      }
    }

    const cross = svg("line", { class: "cross", y1: m.t, y2: m.t + ph, visibility: "hidden" });
    const dots = opt.series.map((s) => svg("circle", { class: "dot", r: 4, fill: s.color, visibility: "hidden" }));
    const hit = svg("rect", {
      class: "hit", x: m.l, y: m.t, width: pw, height: ph, tabindex: 0,
      "aria-label": `${opt.title}：用左右方向鍵逐日檢視`,
    });
    root.append(cross, ...dots, hit);
    geom.cross = cross;
    geom.dots = dots;

    const idxAt = (evt) => {
      const rect = root.getBoundingClientRect();
      const px = evt.clientX - rect.left;
      return Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / pw) * (n - 1))));
    };
    hit.addEventListener("pointermove", (evt) => setHover(idxAt(evt), { chart: api, evt }));
    hit.addEventListener("pointerleave", () => setHover(null));
    hit.addEventListener("blur", () => setHover(null));
    hit.addEventListener("keydown", (evt) => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity }[evt.key];
      if (step === undefined) return;
      evt.preventDefault();
      const cur = state.hover ?? (step > 0 ? -1 : n);
      setHover(Math.max(0, Math.min(n - 1, cur + step)), { chart: api });
    });
    update(hoverIdx);
  }

  function update(idx) {
    hoverIdx = idx;
    if (!geom || !geom.cross) return;
    const show = idx !== null && idx !== undefined;
    geom.cross.setAttribute("visibility", show ? "visible" : "hidden");
    geom.dots.forEach((dot, k) => {
      dot.setAttribute("visibility", show ? "visible" : "hidden");
      if (show) {
        dot.setAttribute("cx", geom.x(idx));
        dot.setAttribute("cy", geom.ys ? geom.ys(k, opt.series[k].values[idx]) : geom.y(opt.series[k].values[idx]));
      }
    });
    if (show) {
      geom.cross.setAttribute("x1", geom.x(idx));
      geom.cross.setAttribute("x2", geom.x(idx));
    }
  }

  function showTooltip(idx, evt) {
    const tip = $("#tooltip");
    const rows = [el("div", { class: "t-date" }, `${opt.dates[idx]} · ${opt.title}`)];
    for (const s of opt.series) {
      const row = el("div", { class: "t-row" });
      row.append(lineKey(s.color, s.dashed), el("span", {}, s.name), el("b", {}, opt.fmt(s.values[idx])));
      rows.push(row);
    }
    tip.replaceChildren(...rows);
    tip.hidden = false;
    const rect = root.getBoundingClientRect();
    const ax = evt ? evt.clientX : rect.left + geom.x(idx);
    const ay = evt ? evt.clientY : rect.top + geom.m.t + 20;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = ax + 16;
    if (left + tw > window.innerWidth - 8) left = ax - tw - 16;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, Math.min(window.innerHeight - th - 8, ay - th / 2))}px`;
  }

  const observer = new ResizeObserver(() => requestAnimationFrame(draw));
  observer.observe(container);
  draw();
  const api = { update, showTooltip, destroy: () => observer.disconnect() };
  return api;
}

// One shared hover index keeps every chart's crosshair on the same trading day.
function setHover(idx, source) {
  state.hover = idx;
  for (const c of state.charts) c.update(idx);
  if (idx === null || !source) { $("#tooltip").hidden = true; return; }
  source.chart.showTooltip(idx, source.evt);
}

init();
