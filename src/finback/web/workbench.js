"use strict";
/* global LightweightCharts */

const $ = (s) => document.querySelector(s);
const LW = LightweightCharts;
const state = {
  datasets: [], dataset: null, symbol: "", bars: [], events: null,
  alphas: [], functions: {}, variables: {}, current: null, ics: {},
  evalResult: null, backtest: null, position: [], positionByTime: new Map(), eventByTime: new Map(),
  markersZoomOnly: false, zoomedIn: false,
};

// ---------- helpers ----------
const pct = (v, d = 2) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
const signedPct = (v, d = 2) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + pct(v, d));
const fix = (v, d = 3) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const money = (v) => Number(v).toLocaleString("zh-TW", { maximumFractionDigits: 0 });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtPx = (v, d) => Number(v).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d });
const signedFix = (v, d) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d));
const isDark = () => getComputedStyle(document.documentElement).colorScheme === "dark";

// A clickable table row that is also reachable and operable from the keyboard.
function clickableRow(tr, handler) {
  tr.classList.add("clickable");
  tr.tabIndex = 0;
  tr.addEventListener("click", handler);
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handler(); } });
  return tr;
}

function el(tag, attrs = {}, text) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
}

async function api(path, body, method) {
  const opts = body === undefined ? { method: method || "GET" }
    : { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function msg(target, text, kind = "") {
  const node = $(target);
  node.textContent = text;
  node.className = `msg ${kind}`;
}

async function busy(button, fn) {
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  try { return await fn(); } finally { button.disabled = false; button.removeAttribute("aria-busy"); }
}

// ---------- chart ----------
const chart = LW.createChart($("#chart"), {
  autoSize: true,
  localization: { locale: "zh-TW", dateFormat: "yyyy-MM-dd" },
  crosshair: { mode: LW.CrosshairMode.Normal },
  timeScale: { rightOffset: 6, minBarSpacing: 0.5 },
});
const posBand = chart.addSeries(LW.HistogramSeries, {
  priceScaleId: "pos", lastValueVisible: false, priceLineVisible: false, base: 0,
  autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }),
}, 0);
posBand.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
const candle = chart.addSeries(LW.CandlestickSeries, { priceLineVisible: false }, 0);
const volume = chart.addSeries(LW.HistogramSeries, {
  priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false,
}, 0);
volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
candle.priceScale().applyOptions({ scaleMargins: { top: 0.12, bottom: 0.22 } });
const alphaLine = chart.addSeries(LW.LineSeries, {
  lineWidth: 2, title: "alpha", priceLineVisible: false, priceFormat: { type: "price", precision: 3, minMove: 0.001 },
}, 1);
alphaLine.priceScale().applyOptions({ scaleMargins: { top: 0.15, bottom: 0.1 } });
const stratLine = chart.addSeries(LW.LineSeries, {
  lineWidth: 2, title: "策略", priceLineVisible: false, priceFormat: { type: "price", precision: 2, minMove: 0.01 },
}, 2);
const benchLine = chart.addSeries(LW.LineSeries, {
  lineWidth: 1, lineStyle: LW.LineStyle.Dashed, title: "買入持有", priceLineVisible: false,
  priceFormat: { type: "price", precision: 2, minMove: 0.01 },
}, 2);
const markers = LW.createSeriesMarkers(candle, []);
let thresholdLines = [];

// Empty panes collapse to a sliver; each visible lower pane gets a title at its top-left.
function layoutPanes() {
  const panes = chart.panes();
  const hasAlpha = alphaLine.data().length > 0, hasEq = stratLine.data().length > 0;
  panes[0]?.setStretchFactor(3);
  panes[1]?.setStretchFactor(hasAlpha ? 1 : 0.001);
  panes[2]?.setStretchFactor(hasEq ? 1.2 : 0.001);
  schedulePaneLabels();
}

// Pane heights update on the chart's next render, so measure after it (and again later to be safe).
function schedulePaneLabels() {
  requestAnimationFrame(() => requestAnimationFrame(() => { positionPaneLabels(); positionSplitLine(); }));
  setTimeout(positionPaneLabels, 120);
  setTimeout(() => { positionPaneLabels(); positionSplitLine(); }, 400);
}

function positionPaneLabels() {
  // Measure each pane's real DOM position instead of summing heights, which can lag a render.
  const box = $("#chart"), base = box.getBoundingClientRect().top;
  const has = [true, alphaLine.data().length > 0, stratLine.data().length > 0];
  chart.panes().forEach((p, i) => {
    const label = $(`#pane-label-${i}`);
    if (!label) return;
    const el = p.getHTMLElement();
    const top = el ? el.getBoundingClientRect().top - base : 0;
    label.hidden = p.getHeight() < 40 || !has[i];
    label.style.top = `${box.offsetTop + top + 6}px`;
  });
}
new ResizeObserver(schedulePaneLabels).observe($("#chart"));
$("#chart").addEventListener("pointerup", () => requestAnimationFrame(positionPaneLabels));

function applyTheme() {
  const up = css("--up"), down = css("--down");
  const crossLine = { color: css("--crosshair"), labelBackgroundColor: css("--crosshair-label"), style: LW.LineStyle.Dashed };
  chart.applyOptions({
    layout: {
      background: { type: "solid", color: css("--bg") }, textColor: css("--muted"), fontSize: 11,
      fontFamily: getComputedStyle(document.body).fontFamily,
      panes: { separatorColor: css("--border"), separatorHoverColor: css("--panel-2"), enableResize: true },
    },
    crosshair: { mode: LW.CrosshairMode.Normal, vertLine: crossLine, horzLine: crossLine },
    grid: { vertLines: { visible: false }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderColor: css("--border") },
    timeScale: { borderColor: css("--border") },
  });
  candle.applyOptions({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
  alphaLine.applyOptions({ color: css("--series-1") });
  stratLine.applyOptions({ color: css("--series-1") });
  benchLine.applyOptions({ color: css("--bench") });
  renderVolume();
  renderPosBand();
  renderMarkers();
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

function hexAlpha(color, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function renderVolume() {
  const a = isDark() ? 0.45 : 0.35;
  const up = hexAlpha(css("--up"), a), down = hexAlpha(css("--down"), a);
  volume.setData(state.bars.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? up : down })));
}

function renderPosBand() {
  state.positionByTime = new Map(state.position.map((p) => [p.time, p.side]));
  posBand.setData(state.bars.map(({ time }) => {
    const side = state.positionByTime.get(time);
    return side ? { time, value: 1, color: hexAlpha(css(side === "put" ? "--series-2" : "--series-1"), isDark() ? 0.12 : 0.08) } : { time };
  }));
}

const visibleBars = () => {
  const r = chart.timeScale().getVisibleLogicalRange();
  return r ? r.to - r.from : Infinity;
};

function positionSplitLine() {
  const line = $("#split-line"), split = $("#split").value, box = $("#chart");
  const first = split && state.bars.find((b) => b.time >= split);
  const x = first ? chart.timeScale().timeToCoordinate(first.time) : null;
  const plotW = box.clientWidth - chart.priceScale("right").width();
  if (x === null || x < 48 || x > plotW - 4) { line.hidden = true; return; }
  line.hidden = false;
  line.style.left = `${box.offsetLeft + Math.round(x)}px`;
  line.style.top = `${box.offsetTop}px`;
  line.style.height = `${box.clientHeight - chart.timeScale().height()}px`;
}

function renderMarkers() {
  const list = [];
  const ev = state.events;
  const trades = !state.markersZoomOnly || visibleBars() <= 250 ? state.tradeMarkers || [] : [];
  const quiet = trades.length > 0;
  if (ev) {
    for (const d of ev.dividends) list.push({ time: d.ex_date, position: "aboveBar", shape: "circle", color: css("--bench"), size: 0.6, text: quiet ? "" : "息" });
    for (const s of ev.splits) list.push({ time: s.date, position: "aboveBar", shape: "square", color: css("--bench"), size: 0.6, text: quiet ? "" : "分割" });
  }
  list.push(...trades);
  list.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  markers.setMarkers(list.filter((m) => state.bars.length && m.time >= state.bars[0].time));
  positionSplitLine();
}
chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
  const zoomedIn = visibleBars() <= 250;
  if (zoomedIn !== state.zoomedIn) {
    state.zoomedIn = zoomedIn;
    renderMarkers();
  }
  positionSplitLine();
});

function setThresholds(lines) {
  for (const l of thresholdLines) alphaLine.removePriceLine(l);
  const merged = [];
  for (const [price, title, color] of lines) {
    if (!Number.isFinite(price)) continue;
    const m = merged.find((x) => Math.abs(x[0] - price) < 1e-12);
    if (m) { m[1] += "/" + title; m[2] = css("--muted"); }
    else merged.push([price, title, color]);
  }
  thresholdLines = merged.map(([price, title, color]) => alphaLine.createPriceLine({
    price, title, color, lineWidth: 1, lineStyle: LW.LineStyle.Dotted, axisLabelVisible: true,
  }));
}

// Legend: OHLC + alpha + equity at the crosshair (or the last bar).
const legend = $("#legend");
function updateLegend(param) {
  const bar = param?.seriesData?.get(candle) || state.bars.at(-1);
  if (!bar) { legend.replaceChildren(); return; }
  const i = state.barIndex.get(bar.time);
  const prev = i > 0 ? state.bars[i - 1].close : bar.open;
  const cls = bar.close >= prev ? "up" : "down";
  const parts = [el("span", { class: "sym" }, `${state.symbol} 日`)];
  for (const [k, v] of [["開", bar.open], ["高", bar.high], ["低", bar.low], ["收", bar.close]]) {
    const item = el("span");
    item.append(el("span", { class: "k" }, k), el("span", { class: cls }, fmtPx(v, 2)));
    parts.push(item);
  }
  parts.push(el("span", { class: cls }, signedPct(bar.close / prev - 1)));
  const a = param?.seriesData?.get(alphaLine) ?? state.alphaByTime?.get(bar.time);
  if (a && a.value !== undefined) parts.push(el("span", {}, `alpha ${a.value.toFixed(4)}`));
  const side = state.positionByTime.get(bar.time);
  if (side) parts.push(el("span", { class: "k" }, side === "long" ? "持倉中" : side === "call" ? "持有買權" : "持有賣權"));
  if (state.eventByTime.has(bar.time)) parts.push(el("span", { class: "k" }, state.eventByTime.get(bar.time)));
  legend.replaceChildren(...parts);
}
chart.subscribeCrosshairMove(updateLegend);

// ---------- data ----------
async function loadDatasets(selectId) {
  state.datasets = await api("/api/datasets");
  const select = $("#dataset");
  const yrs = (d) => d.first_date.slice(0, 4) === d.last_date.slice(0, 4)
    ? d.first_date.slice(0, 4) : `${d.first_date.slice(0, 4)}–${d.last_date.slice(0, 4)}`;
  select.replaceChildren(...state.datasets.map((d) =>
    el("option", { value: d.id, title: `${d.first_date} ~ ${d.last_date}` }, d.kind === "options"
      ? `台指選擇權 TXO · ${yrs(d)}` : `${d.symbol} · ${yrs(d)}`)));
  if (!state.datasets.length) {
    $("#download-form").hidden = false;
    msg("#formula-msg", "還沒有資料，請先從 FinMind 下載。", "error");
    return;
  }
  select.value = selectId && state.datasets.some((d) => d.id === selectId) ? selectId : state.datasets[0].id;
  await loadBars();
}

async function loadBars() {
  $(".chart-area").setAttribute("aria-busy", "true");
  try {
    await loadBarsInner();
  } finally {
    $(".chart-area").removeAttribute("aria-busy");
  }
}

async function loadBarsInner() {
  const id = $("#dataset").value;
  const d = state.datasets.find((x) => x.id === id);
  state.isOptions = d?.kind === "options";
  document.querySelector('[data-tab="options"]').hidden = !state.isOptions;
  document.querySelector('[data-tab="strategy"]').hidden = state.isOptions;
  if (state.isOptions && !$("#tab-strategy").hidden) switchTab("options");
  if (!state.isOptions && !$("#tab-options").hidden) switchTab("strategy");
  state.dataset = id; // set before awaiting so in-flight results for the old dataset are dropped
  const data = await api(`/api/bars?dataset=${encodeURIComponent(id)}`);
  const lo = Math.min(...data.bars.map((b) => b.low));
  const fmt = (d) => (p) => (p < lo * 0.98 ? "" : fmtPx(p, d));
  candle.applyOptions({ priceFormat: state.isOptions
    ? { type: "custom", minMove: 1, formatter: fmt(0) }
    : { type: "custom", minMove: 0.01, formatter: fmt(2) } });
  state.symbol = data.symbol;
  state.bars = data.bars;
  state.events = data.events;
  state.eventByTime = new Map();
  const event = (time, text) => state.eventByTime.set(time, [state.eventByTime.get(time), text].filter(Boolean).join("、"));
  for (const d of data.events?.dividends || []) event(d.ex_date, `除息 ${d.cash_per_share}`);
  for (const s of data.events?.splits || []) event(s.date, `分割 1→${s.ratio}`);
  state.barIndex = new Map(data.bars.map((b, i) => [b.time, i]));
  for (const input of ["#start", "#end", "#split"]) {
    $(input).min = d.first_date;
    $(input).max = d.last_date;
  }
  $("#start").value = d.first_date;
  $("#end").value = d.last_date;
  // Default: last ~30% of the data is held out as out-of-sample.
  $("#split").value = data.bars[Math.floor(data.bars.length * 0.7)].time;
  candle.setData(data.bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
  renderVolume();
  clearResults();
  chart.timeScale().fitContent();
  updateLegend();
  state.ics = {};
  renderPool();
  layoutPanes();
  if (state.current) evaluate();
}

function clearResults() {
  state.backtest = null;
  state.tradeMarkers = [];
  state.markersZoomOnly = false;
  state.position = [];
  renderPosBand();
  if ($("#op-result")) $("#op-result").hidden = true;
  alphaLine.setData([]);
  stratLine.setData([]);
  benchLine.setData([]);
  setThresholds([]);
  $("#bt-result").hidden = true;
  renderMarkers();
  layoutPanes();
}

$("#dataset").addEventListener("change", () => loadBars().catch((e) => msg("#formula-msg", e.message, "error")));
$("#split").addEventListener("change", () => { renderMarkers(); state.ics = {}; renderPool(); });

$("#open-download").addEventListener("click", () => {
  $("#download-form").hidden = false;
  $("#dl-end").value ||= new Date().toISOString().slice(0, 10);
  $("#dl-symbol").focus();
});
$("#dl-cancel").addEventListener("click", () => { $("#download-form").hidden = true; });
$("#dl-kind").addEventListener("change", () => {
  $("#dl-symbol-field").hidden = $("#dl-kind").value === "options";
  if ($("#dl-kind").value === "options") $("#dl-status").textContent = "每個月一次請求（約 10 MB），一年約需 1 分鐘；已下載的月份會略過";
});
$("#download-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  await busy(button, async () => {
    $("#dl-status").textContent = "下載中…（每個資料集一次請求，可能需要一分鐘）";
    try {
      const out = $("#dl-kind").value === "options"
        ? await api("/api/options/download", { start: $("#dl-start").value, end: $("#dl-end").value })
        : await api("/api/data/download", {
          symbol: $("#dl-symbol").value.trim(), start: $("#dl-start").value, end: $("#dl-end").value,
        });
      $("#dl-status").textContent = "完成";
      $("#download-form").hidden = true;
      await loadDatasets(out.id);
    } catch (e) {
      $("#dl-status").textContent = `失敗：${e.message}`;
    }
  });
});

function range() {
  return { dataset: state.dataset, start: $("#start").value || null, end: $("#end").value || null,
    split_date: $("#split").value || null };
}

// ---------- alpha pool ----------
async function loadAlphas() {
  const data = await api("/api/alphas");
  state.alphas = data.alphas;
  state.functions = data.functions;
  state.variables = data.variables;
  renderPool();
  renderReference();
}

function renderPool() {
  const list = $("#pool-list");
  list.replaceChildren(...state.alphas.map((a) => {
    const li = el("li", { tabindex: 0, role: "button", "aria-current": String(state.current === a.name) });
    const head = el("div", { class: "name" });
    head.append(el("span", {}, a.name));
    const ic = state.ics[a.name];
    if (ic) head.append(el("span", { class: "ic", title: "5 日 IC：樣本內 / 樣本外" }, `${signedFix(ic.is, 2)} / ${signedFix(ic.oos, 2)}`));
    li.append(head);
    if (a.description) li.append(el("div", { class: "desc" }, a.description));
    li.append(el("code", {}, a.expr));
    const open = () => selectAlpha(a.name);
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return li;
  }));
}

function renderReference() {
  const dl = $("#ref-list");
  dl.replaceChildren();
  for (const [group, items] of [["變數", state.variables], ["函數", state.functions]]) {
    dl.append(el("dt", { class: "muted" }, `— ${group} —`));
    for (const [name, help] of Object.entries(items)) dl.append(el("dt", {}, name), el("dd", {}, help));
  }
}

function selectAlpha(name) {
  const a = state.alphas.find((x) => x.name === name);
  state.current = name;
  $("#alpha-name").value = a?.name || "";
  $("#alpha-expr").value = a?.expr || "";
  $("#alpha-desc").value = a?.description || "";
  renderPool();
  switchTab("formula");
  evaluate();
}

$("#new-alpha").addEventListener("click", () => {
  state.current = null;
  $("#alpha-name").value = "";
  $("#alpha-expr").value = "";
  $("#alpha-desc").value = "";
  $("#eval-result").hidden = true;
  renderPool();
  switchTab("formula");
  $("#alpha-name").focus();
});

$("#alpha-expr").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); evaluate(); }
});
$("#evaluate").addEventListener("click", () => evaluate());

async function evaluate() {
  const expr = $("#alpha-expr").value.trim();
  if (!expr || !state.dataset) return;
  await busy($("#evaluate"), async () => {
    try {
      const requested = state.dataset;
      const out = await api("/api/alpha/evaluate", { ...range(), expr, horizons: [1, 5, 20] });
      if (state.dataset !== requested) return; // dataset switched while waiting: stale result
      state.evalResult = out;
      state.alphaByTime = new Map(out.series.filter((p) => p.value !== undefined).map((p) => [p.time, p]));
      alphaLine.setData(out.series);
      layoutPanes();
      setThresholds([[0, "0", css("--bench")]]);
      renderEval(out.stats);
      msg("#formula-msg", `已評估（涵蓋率 ${pct(out.stats.coverage, 0)}）`, "ok");
    } catch (e) {
      msg("#formula-msg", e.message, "error");
    }
  });
}

function seg(stats, name) { return stats[name] || null; }

function renderEval(stats) {
  $("#eval-result").hidden = false;
  const table = $("#ic-table");
  const is = seg(stats, "in_sample"), oos = seg(stats, "out_of_sample");
  const focus = (h) => (h === 5 ? " focus" : "");
  const head1 = el("tr"), head2 = el("tr", { class: "sub" });
  head1.append(el("th", {}, ""));
  head2.append(el("th", {}, "指標"));
  for (const h of stats.horizons) {
    head1.append(el("th", { class: `grp gs${focus(h)}`, colspan: oos ? 2 : 1 }, `${h} 日`));
    head2.append(el("th", { class: `gs${focus(h)}` }, oos ? "內" : "全期"));
    if (oos) head2.append(el("th", { class: focus(h).trim() }, "外"));
  }
  const rows = [head1, head2];
  for (const [label, key, f] of [["IC", "ic", (v) => fix(v, 3)], ["Rank IC", "rank_ic", (v) => fix(v, 3)],
    ["勝率", "hit_rate", (v) => pct(v, 1)], ["樣本數", "n", (v) => v ?? "—"]]) {
    const tr = el("tr");
    tr.append(el("td", {}, label));
    for (const h of stats.horizons) {
      tr.append(el("td", { class: `gs${focus(h)}` }, f(is?.horizons?.[String(h)]?.[key])));
      if (oos) tr.append(el("td", { class: focus(h).trim() }, f(oos?.horizons?.[String(h)]?.[key])));
    }
    rows.push(tr);
  }
  table.replaceChildren(...rows);
  const h = stats.horizons.includes(5) ? 5 : stats.horizons[0];
  $("#q-title").textContent = `alpha 由低到高分 ${stats.quantiles} 組，隔日開盤起 ${h} 日平均報酬；分組界線只用樣本內資料。`;
  const q = (s) => s?.horizons?.[String(h)]?.quantile_returns?.map((x) => x.mean_fwd);
  quantileChart($("#quantile-chart"), q(is), q(oos));
  $("#eval-note").textContent = (stats.note || "").replace(/\s*\([^()]*[A-Za-z]{4,}[^()]*\)\s*$/, "").replace(/。(?=.)/g, "。\n");
  if (state.current && is) {
    state.ics[state.current] = { is: is.horizons?.["5"]?.ic, oos: oos?.horizons?.["5"]?.ic };
    renderPool();
  }
}

function quantileChart(container, a, b) {
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  container.replaceChildren();
  if (!a) return;
  const groups = [a, b].filter(Boolean);
  const vals = groups.flat().filter((v) => v !== null);
  if (!vals.length) return;
  const W = container.clientWidth || 340, H = 160, m = { l: 48, r: 8, t: 10, b: 22 };
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "各分組平均報酬長條圖" });
  const n = a.length, slot = (W - m.l - m.r) / n;
  const bw = Math.min(28, Math.max(4, (slot - 6) / groups.length - 2));
  const colors = [css("--series-1"), css("--series-2")];
  const ticks = [...new Set([lo, lo / 2, 0, hi / 2, hi].filter((v) => v !== 0 || true))]
    .filter((v, i, arr) => arr.findIndex((u) => Math.abs(u - v) < 1e-12) === i);
  for (const v of ticks) {
    svg.append(mk("line", { class: "grid", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    const t = mk("text", { x: m.l - 4, y: y(v) + 3, "text-anchor": "end" });
    t.textContent = pct(v, 2);
    svg.append(t);
  }
  svg.append(mk("line", { class: "axis", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }));
  for (let i = 0; i < n; i++) {
    groups.forEach((g, k) => {
      const v = g[i];
      if (v === null || v === undefined) return;
      const groupW = groups.length * bw + (groups.length - 1) * 2;
      const x = m.l + i * slot + (slot - groupW) / 2 + k * (bw + 2);
      const top = Math.min(y(v), y(0)), h = Math.max(1, Math.abs(y(v) - y(0)));
      const rect = mk("rect", { x, y: top, width: bw, height: h, rx: 1, fill: colors[k] });
      const tip = mk("title", {});
      tip.textContent = `第 ${i + 1} 組（${k ? "樣本外" : "樣本內"}）：${signedPct(v, 3)}`;
      rect.append(tip);
      svg.append(rect);
    });
    const t = mk("text", { x: m.l + i * slot + slot / 2, y: H - 4, "text-anchor": "middle" });
    t.textContent = `Q${i + 1}`;
    svg.append(t);
  }
  container.append(svg);
  const key = el("div", { class: "qkey" });
  ["樣本內", "樣本外"].slice(0, groups.length).forEach((label, k) => {
    const s = el("span");
    const i = el("i");
    i.style.background = colors[k];
    s.append(i, document.createTextNode(label));
    key.append(s);
  });
  container.prepend(key);
}

$("#save-alpha").addEventListener("click", () => busy($("#save-alpha"), async () => {
  const name = $("#alpha-name").value.trim();
  try {
    await api(`/api/alphas/${encodeURIComponent(name)}`,
      { expr: $("#alpha-expr").value.trim(), description: $("#alpha-desc").value.trim() }, "PUT");
    state.current = name;
    await loadAlphas();
    msg("#formula-msg", `已存入 alpha 池：${name}`, "ok");
  } catch (e) {
    msg("#formula-msg", e.message, "error");
  }
}));

// Two-step delete instead of confirm(): first click arms, second click deletes.
let deleteArmed = null;
$("#delete-alpha").addEventListener("click", () => busy($("#delete-alpha"), async () => {
  const name = $("#alpha-name").value.trim();
  if (!state.alphas.some((a) => a.name === name)) { msg("#formula-msg", "池中沒有這個名稱", "error"); return; }
  if (deleteArmed !== name) {
    deleteArmed = name;
    $("#delete-alpha").textContent = "確定刪除？";
    setTimeout(() => { deleteArmed = null; $("#delete-alpha").textContent = "刪除"; }, 4000);
    return;
  }
  deleteArmed = null;
  $("#delete-alpha").textContent = "刪除";
  try {
    await api(`/api/alphas/${encodeURIComponent(name)}`, {}, "DELETE");
    state.current = null;
    delete state.ics[name];
    await loadAlphas();
    msg("#formula-msg", `已刪除 ${name}`, "ok");
  } catch (e) {
    msg("#formula-msg", e.message, "error");
  }
}));

$("#eval-all").addEventListener("click", () => busy($("#eval-all"), async () => {
  for (const a of state.alphas) {
    try {
      const out = await api("/api/alpha/evaluate", { ...range(), expr: a.expr, horizons: [5] });
      state.ics[a.name] = { is: seg(out.stats, "in_sample")?.horizons?.["5"]?.ic,
        oos: seg(out.stats, "out_of_sample")?.horizons?.["5"]?.ic };
    } catch (e) {
      state.ics[a.name] = { is: null, oos: null };
    }
    renderPool();
  }
}));

// ---------- strategy ----------
const PARAMS = {
  alpha: [["entry", "進場門檻（alpha >）", 0, 0.01], ["exit", "出場門檻（alpha <）", 0, 0.01], ["weight", "持股比例", 1, 0.05]],
  sma_cross: [["fast", "快線天數", 20, 1], ["slow", "慢線天數", 60, 1], ["weight", "持股比例", 1, 0.05]],
  momentum: [["lookback", "回看天數", 120, 1], ["weight", "持股比例", 1, 0.05]],
  fixed_weight: [["weight", "目標比例", 0.5, 0.05]],
  buy_and_hold: [["weight", "持股比例", 1, 0.05]],
};

function renderParams() {
  const type = $("#st-type").value;
  const box = $("#st-params");
  box.replaceChildren(...PARAMS[type].map(([key, label, value, step]) => {
    const f = el("label", { class: "field" });
    f.append(el("span", {}, label), el("input", { type: "number", "data-key": key, value, step }));
    return f;
  }));
  if (type === "fixed_weight") {
    const f = el("label", { class: "field" });
    const s = el("select", { "data-key": "period" });
    s.append(el("option", { value: "month" }, "每月"), el("option", { value: "quarter" }, "每季"));
    f.append(el("span", {}, "再平衡頻率"), s);
    box.append(f);
  }
}
$("#st-type").addEventListener("change", renderParams);

function strategySpec() {
  const type = $("#st-type").value;
  const spec = { type };
  for (const input of $("#st-params").querySelectorAll("[data-key]")) {
    const key = input.dataset.key;
    spec[key] = input.tagName === "SELECT" ? input.value
      : ["fast", "slow", "lookback"].includes(key) ? parseInt(input.value, 10) : input.value;
  }
  if (type === "alpha") {
    spec.expr = $("#alpha-expr").value.trim();
    if (!spec.expr) throw new Error("請先在「公式」頁輸入或選擇一個 alpha");
    spec.name = state.current ? `alpha ${state.current}` : "alpha 公式";
  }
  return spec;
}

function backtestBody(saveAs) {
  return {
    ...range(), strategy: strategySpec(), initial_cash: $("#cash").value, lot_size: parseInt($("#lot").value, 10),
    costs: { commission_rate: $("#c-rate").value, commission_discount: $("#c-disc").value,
      min_commission: $("#c-min").value, sell_tax_rate: $("#c-tax").value },
    ...(saveAs ? { save_as: saveAs } : {}),
  };
}

async function runBacktest(saveAs) {
  try {
    const requested = state.dataset;
    const out = await api("/api/backtest", backtestBody(saveAs));
    if (state.dataset !== requested) return;
    state.backtest = out;
    const trades = out.strategy.trades.slice().sort((a, b) => a.date.localeCompare(b.date));
    state.position = [];
    let qty = 0, next = 0;
    for (const bar of state.bars) {
      while (next < trades.length && trades[next].date <= bar.time) {
        const t = trades[next++];
        qty += (t.side === "buy" ? 1 : -1) * Number(t.quantity);
      }
      if (qty > 0) state.position.push({ time: bar.time, side: "long" });
    }
    renderPosBand();
    state.markersZoomOnly = trades.length > 40;
    const many = state.markersZoomOnly;
    state.tradeMarkers = out.strategy.trades.map((t) => {
      const buy = t.side === "buy";
      const color = buy ? css("--series-1") : css("--series-2");
      return many
        ? { time: t.date, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", size: 0.8, color, text: "" }
        : { time: t.date, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", size: 0.8,
          color, text: buy ? "買" : "賣" };
    });
    stratLine.setData(out.strategy.wealth);
    benchLine.setData(out.benchmark.wealth);
    if (out.alpha) {
      state.alphaByTime = new Map(out.alpha.filter((p) => p.value !== undefined).map((p) => [p.time, p]));
      alphaLine.setData(out.alpha);
      const spec = backtestBody().strategy;
      setThresholds([[Number(spec.entry), "進場", css("--series-1")], [Number(spec.exit), "出場", css("--series-2")]]);
    }
    layoutPanes();
    renderMarkers();
    renderBacktest(out);
    const note = out.strategy.reconciled ? "帳本每日核對一致" : "⚠ 帳本核對失敗，結果不可信";
    msg("#strategy-msg", saveAs ? `已存成報告「${out.saved}」；${note}` : note,
      out.strategy.reconciled ? "ok" : "error");
  } catch (e) {
    msg("#strategy-msg", e.message, "error");
  }
}
$("#run").addEventListener("click", () => busy($("#run"), () => runBacktest()));
$("#save-report").addEventListener("click", () => busy($("#save-report"), () => {
  const name = $("#save-name").value.trim();
  if (!name) { msg("#strategy-msg", "請輸入報告名稱（英文、數字、- 或 _）", "error"); return null; }
  return runBacktest(name);
}));

function renderBacktest(out) {
  $("#bt-result").hidden = false;
  const segs = out.split_date ? [["in_sample", "內"], ["out_of_sample", "外"]] : [["full", "全期"]];
  const table = $("#bt-table");
  const head1 = el("tr"), head2 = el("tr", { class: "sub" });
  head1.append(el("th", {}, ""));
  head2.append(el("th", {}, "指標"));
  for (const label of ["策略", "買入持有"]) {
    head1.append(el("th", { class: "grp gs", colspan: segs.length }, label));
    segs.forEach(([, s], i) => head2.append(el("th", i === 0 ? { class: "gs" } : {}, segs.length > 1 ? `樣本${s}` : s)));
  }
  const rows = [
    ["總報酬", "total_return", (v) => signedPct(v)],
    ["年化報酬", "cagr", (v) => signedPct(v)],
    ["年化波動", "volatility", (v) => pct(v)],
    ["Sharpe", "sharpe", (v) => fix(v, 2)],
    ["最大回撤", "max_drawdown", (v) => pct(v)],
    ["平均曝險", "exposure", (v) => pct(v, 0)],
    ["交易次數", "trades", (v) => v ?? "—"],
    ["交易成本", "costs", (v) => (v === undefined || v === null ? "—" : money(v))],
  ].map(([label, key, f]) => {
    const tr = el("tr");
    tr.append(el("td", {}, label));
    for (const side of [out.strategy, out.benchmark]) {
      segs.forEach(([k], i) => tr.append(el("td", i === 0 ? { class: "gs" } : {}, f(side.metrics[k]?.[key]))));
    }
    return tr;
  });
  table.replaceChildren(head1, head2, ...rows);

  const trades = out.strategy.trades;
  $("#trade-count").textContent = `${trades.length} 筆`;
  const head = el("tr");
  for (const h of ["日期", "買賣", "股數", "價格", "成本"]) head.append(el("th", {}, h));
  $("#trade-table").replaceChildren(head, ...trades.slice().reverse().map((t) => {
    const tr = el("tr");
    tr.append(el("td", {}, t.date), el("td", { class: "txt" }, t.side === "buy" ? "買" : "賣"),
      el("td", {}, Number(t.quantity).toLocaleString("zh-TW")), el("td", {}, Number(t.price).toFixed(2)),
      el("td", {}, money(Number(t.commission) + Number(t.tax))));
    return clickableRow(tr, () => focusDate(t.date));
  }));
}

function focusDate(time) {
  const i = state.barIndex.get(time);
  if (i === undefined) return;
  chart.timeScale().setVisibleLogicalRange({ from: i - 60, to: i + 60 });
}

// ---------- tabs ----------
function switchTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  $("#tab-formula").hidden = name !== "formula";
  $("#tab-strategy").hidden = name !== "strategy";
  $("#tab-options").hidden = name !== "options";
  if (name === "options" && !$("#chain-date").value && state.bars.length) loadChain(state.bars.at(-1).time);
}

// ---------- options ----------
function optionParams() {
  const out = {};
  for (const input of document.querySelectorAll("[data-op]")) {
    out[input.dataset.op] = input.type === "checkbox" ? input.checked : input.value;
  }
  return out;
}

function metricsTable(table, out, leftLabel, rightLabel) {
  const segs = out.split_date ? [["in_sample", "樣本內"], ["out_of_sample", "樣本外"]] : [["full", "全期"]];
  const head1 = el("tr"), head2 = el("tr", { class: "sub" });
  head1.append(el("th", {}, ""));
  head2.append(el("th", {}, "指標"));
  for (const label of [leftLabel, rightLabel]) {
    head1.append(el("th", { class: "grp gs", colspan: segs.length }, label));
    segs.forEach(([, s], i) => head2.append(el("th", i === 0 ? { class: "gs" } : {}, s)));
  }
  const rows = [
    ["總報酬", "total_return", (v) => signedPct(v)], ["年化報酬", "cagr", (v) => signedPct(v)],
    ["年化波動", "volatility", (v) => pct(v)], ["Sharpe", "sharpe", (v) => fix(v, 2)],
    ["最大回撤", "max_drawdown", (v) => pct(v)], ["平均曝險", "exposure", (v) => pct(v, 0)],
  ].map(([label, key, f]) => {
    const tr = el("tr");
    tr.append(el("td", {}, label));
    for (const side of [out.strategy, out.benchmark]) {
      segs.forEach(([k], i) => tr.append(el("td", i === 0 ? { class: "gs" } : {}, f(side.metrics[k]?.[key]))));
    }
    return tr;
  });
  table.replaceChildren(head1, head2, ...rows);
}

const ACTION = { open: "買進", close: "賣出", expire: "到期結算", missed: "未成交" };

async function runOptions(saveAs) {
  const expr = $("#alpha-expr").value.trim();
  if (!expr) { msg("#options-msg", "請先在「公式」頁輸入或選擇一個 alpha", "error"); return; }
  try {
    const out = await api("/api/options/backtest", {
      start: $("#start").value || null, end: $("#end").value || null, split_date: $("#split").value || null,
      expr, params: optionParams(), ...(saveAs ? { save_as: saveAs } : {}),
    });
    if (!state.isOptions) return; // switched away from the options dataset while waiting
    state.backtest = out;
    stratLine.setData(out.strategy.wealth);
    benchLine.setData(out.benchmark.wealth);
    state.alphaByTime = new Map(out.alpha.filter((p) => p.value !== undefined).map((p) => [p.time, p]));
    alphaLine.setData(out.alpha);
    const p = optionParams();
    setThresholds([[Number(p.long_entry), "多", css("--series-1")], [Number(p.short_entry), "空", css("--series-2")]]);
    const trades = out.trades.slice().sort((a, b) => a.date.localeCompare(b.date));
    state.position = [];
    let side = null, next = 0;
    for (const bar of state.bars) {
      while (next < trades.length && trades[next].date <= bar.time) {
        const t = trades[next++];
        if (t.action === "open") side = t.call_put;
        else if (t.action === "close" || t.action === "expire") side = null;
      }
      if (side) state.position.push({ time: bar.time, side });
    }
    renderPosBand();
    state.markersZoomOnly = false;
    state.tradeMarkers = out.trades.filter((t) => t.action !== "missed").map((t) => {
      if (t.action === "open") {
        const call = t.call_put === "call";
        return { time: t.date, position: call ? "belowBar" : "aboveBar", shape: call ? "arrowUp" : "arrowDown", size: 0.8,
          color: call ? css("--series-1") : css("--series-2"), text: `${call ? "C" : "P"}${Number(t.strike)}` };
      }
      return { time: t.date, position: "aboveBar", shape: "circle", size: 0.5, color: css("--bench"), text: t.action === "expire" ? "結算" : "" };
    });
    layoutPanes();
    renderMarkers();
    metricsTable($("#op-table"), out, "選擇權策略", "加權指數");
    const s = out.summary;
    const figures = [
      ["進場", `${s.entries} 次（${s.calls}C／${s.puts}P）`],
      ["到期結算", `${s.expired} 次（歸零 ${s.expired_worthless}）`],
      ["未成交", `${s.missed} 次`], ["交易成本", money(s.costs)],
      ["權利金支出", money(s.premium_paid)], ["權利金收回", money(s.premium_received)],
    ];
    $("#op-summary").replaceChildren(...figures.map(([label, value]) => {
      const pair = el("div");
      pair.append(el("dt", {}, label), el("dd", { title: value }, value));
      return pair;
    }));
    $("#op-result").hidden = false;
    $("#op-trade-count").textContent = `${out.trades.length} 筆`;
    const head = el("tr");
    for (const h of ["日期", "動作", "契約", "口數", "價格"]) head.append(el("th", {}, h));
    $("#op-trades").replaceChildren(head, ...out.trades.slice().reverse().map((t) => {
      const tr = el("tr", { title: t.reason || "" });
      const action = ACTION[t.action] || t.action;
      const actionCell = el("td", { class: "txt wrap" }, action);
      if (t.reason && t.reason !== action) actionCell.append(el("span", { class: "why" }, t.reason));
      tr.append(el("td", {}, t.date), actionCell,
        el("td", { class: "txt mono" }, `${t.contract} ${t.call_put === "call" ? "C" : "P"}${Number(t.strike).toFixed(0)}`),
        el("td", {}, String(t.contracts)), el("td", {}, Number(t.price).toFixed(1)));
      return clickableRow(tr, () => { focusDate(t.date); loadChain(t.date, t.contract); });
    }));
    msg("#options-msg", (s.reconciled ? "帳本每日核對一致" : "⚠ 帳本核對失敗，結果不可信") + (out.saved ? `；已存成報告「${out.saved}」` : ""),
      s.reconciled ? "ok" : "error");
  } catch (e) {
    msg("#options-msg", e.message, "error");
  }
}
$("#op-run").addEventListener("click", () => busy($("#op-run"), () => runOptions()));
$("#op-save").addEventListener("click", () => busy($("#op-save"), () => {
  const name = $("#op-save-name").value.trim();
  if (!name) { msg("#options-msg", "請輸入報告名稱（英文、數字、- 或 _）", "error"); return null; }
  return runOptions(name);
}));

async function loadChain(day, contract) {
  if (!state.isOptions || !day) return;
  try {
    const q = new URLSearchParams({ date: day, ...(contract ? { contract } : {}) });
    const out = await api(`/api/options/chain?${q}`);
    $("#chain-date").value = out.date;
    $("#chain-contract").replaceChildren(...out.contracts.map((c) =>
      el("option", { value: c.code }, `${c.code}（到期 ${c.expiry}）`)));
    $("#chain-contract").value = out.contract;
    $("#chain-info").className = "note";
    $("#chain-info").textContent = out.spot ? `加權指數收盤 ${out.spot.toLocaleString("zh-TW")}；價格為日盤收盤，量為日盤成交口數；淺底為價內` : "";
    const group = el("tr");
    group.append(el("th", { class: "grp", colspan: 2 }, "買權 Call"), el("th", { class: "mid grp gs" }, ""),
      el("th", { class: "grp gs", colspan: 2 }, "賣權 Put"));
    const head = el("tr", { class: "sub" });
    for (const [h, cls] of [["量", ""], ["收盤", ""], ["履約價", "mid gs"], ["收盤", "gs"], ["量", ""]]) head.append(el("th", { class: cls }, h));
    const atm = out.spot === null ? null
      : out.rows.reduce((best, r) => (best === null || Math.abs(r.strike - out.spot) < Math.abs(best - out.spot) ? r.strike : best), null);
    const cell = (x, k) => (x && x[k] ? (k === "volume" ? x[k].toLocaleString("zh-TW") : x[k].toFixed(1)) : "—");
    $("#chain-table").replaceChildren(group, head, ...out.rows.map((r) => {
      const tr = el("tr", r.strike === atm ? { class: "atm" } : {});
      const callItm = out.spot !== null && r.strike < out.spot ? "itm" : "";
      const putItm = out.spot !== null && r.strike > out.spot ? "itm" : "";
      tr.append(el("td", { class: callItm }, cell(r.call, "volume")), el("td", { class: callItm }, cell(r.call, "close")),
        el("td", { class: "mid gs strike" }, r.strike.toFixed(0)),
        el("td", { class: `gs ${putItm}`.trim() }, cell(r.put, "close")), el("td", { class: putItm }, cell(r.put, "volume")));
      return tr;
    }));
    const atmRow = $("#chain-table tr.atm");
    if (atmRow) {
      // scroll only the chain's own container, never the panel or the page
      const wrap = atmRow.closest(".table-wrap");
      wrap.scrollTop = atmRow.offsetTop - wrap.clientHeight / 2;
    }
  } catch (e) {
    $("#chain-info").className = "note msg error";
    $("#chain-info").textContent = e.message;
    $("#chain-table").replaceChildren();
  }
}
$("#chain-date").addEventListener("change", () => loadChain($("#chain-date").value));
$("#chain-contract").addEventListener("change", () => loadChain($("#chain-date").value, $("#chain-contract").value));
chart.subscribeClick((param) => {
  if (state.isOptions && param.time) {
    const t = typeof param.time === "string" ? param.time
      : `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
    if (!$("#tab-options").hidden) loadChain(t);
  }
});
for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => switchTab(b.dataset.tab));

// ---------- start ----------
(async function init() {
  renderParams();
  applyTheme();
  try {
    await loadAlphas();
  } catch (e) {
    msg("#formula-msg", `alpha 池載入失敗：${e.message}`, "error");
  }
  try {
    await loadDatasets();
    if (state.alphas.length) selectAlpha(state.alphas[0].name);
    // #run: run the default backtest right away (handy for a quick look or a screenshot).
    if (location.hash === "#run") {
      switchTab("strategy");
      await runBacktest();
    }
    // #options: open the options dataset and run its default backtest
    if (location.hash === "#options" && state.datasets.some((d) => d.kind === "options")) {
      $("#dataset").value = state.datasets.find((d) => d.kind === "options").id;
      await loadBars();
      const a = state.alphas.find((x) => x.name === "trend_20_60");
      if (a) selectAlpha(a.name);
      switchTab("options");
      await runOptions();
    }
  } catch (e) {
    msg("#formula-msg", `資料載入失敗：${e.message}`, "error");
  }
})();
