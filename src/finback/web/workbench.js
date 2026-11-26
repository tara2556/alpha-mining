"use strict";
/* global LightweightCharts */

const $ = (s) => document.querySelector(s);
const LW = LightweightCharts;
const state = {
  datasets: [], dataset: null, symbol: "", bars: [], events: null,
  alphas: [], functions: {}, variables: {}, current: null, ics: {},
  evalResult: null, backtest: null,
};

// ---------- helpers ----------
const pct = (v, d = 2) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(d)}%`);
const signedPct = (v, d = 2) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + pct(v, d));
const fix = (v, d = 3) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const money = (v) => Number(v).toLocaleString("zh-TW", { maximumFractionDigits: 0 });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

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
  try { return await fn(); } finally { button.disabled = false; }
}

// ---------- chart ----------
const chart = LW.createChart($("#chart"), {
  autoSize: true,
  localization: { locale: "zh-TW", dateFormat: "yyyy-MM-dd" },
  crosshair: { mode: LW.CrosshairMode.Normal },
  timeScale: { rightOffset: 6, minBarSpacing: 0.5 },
});
const candle = chart.addSeries(LW.CandlestickSeries, { priceLineVisible: false }, 0);
const volume = chart.addSeries(LW.HistogramSeries, {
  priceFormat: { type: "volume" }, priceScaleId: "vol", lastValueVisible: false, priceLineVisible: false,
}, 0);
volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
candle.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
const alphaLine = chart.addSeries(LW.LineSeries, { lineWidth: 2, title: "alpha", priceLineVisible: false }, 1);
const stratLine = chart.addSeries(LW.LineSeries, { lineWidth: 2, title: "策略", priceLineVisible: false }, 2);
const benchLine = chart.addSeries(LW.LineSeries, {
  lineWidth: 1, lineStyle: LW.LineStyle.Dashed, title: "買入持有", priceLineVisible: false,
}, 2);
const markers = LW.createSeriesMarkers(candle, []);
let thresholdLines = [];

function layoutPanes() {
  const panes = chart.panes();
  if (panes[0]) panes[0].setStretchFactor(3);
  if (panes[1]) panes[1].setStretchFactor(1);
  if (panes[2]) panes[2].setStretchFactor(1.2);
}

function applyTheme() {
  const up = css("--up"), down = css("--down");
  chart.applyOptions({
    layout: {
      background: { type: "solid", color: css("--bg") }, textColor: css("--muted"),
      fontFamily: getComputedStyle(document.body).fontFamily,
      panes: { separatorColor: css("--border"), enableResize: true },
    },
    grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderColor: css("--border") },
    timeScale: { borderColor: css("--border") },
  });
  candle.applyOptions({ upColor: up, downColor: down, borderVisible: false, wickUpColor: up, wickDownColor: down });
  alphaLine.applyOptions({ color: css("--series-1") });
  stratLine.applyOptions({ color: css("--series-1") });
  benchLine.applyOptions({ color: css("--bench") });
  renderVolume();
  renderMarkers();
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

function hexAlpha(color, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function renderVolume() {
  const up = hexAlpha(css("--up"), 0.35), down = hexAlpha(css("--down"), 0.35);
  volume.setData(state.bars.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? up : down })));
}

function renderMarkers() {
  const list = [];
  const ev = state.events;
  if (ev) {
    for (const d of ev.dividends) list.push({ time: d.ex_date, position: "aboveBar", shape: "circle", color: css("--bench"), text: `息 ${d.cash_per_share}` });
    for (const s of ev.splits) list.push({ time: s.date, position: "aboveBar", shape: "square", color: css("--bench"), text: `分割 1→${s.ratio}` });
  }
  const split = $("#split").value;
  if (split) {
    const first = state.bars.find((b) => b.time >= split);
    if (first) list.push({ time: first.time, position: "aboveBar", shape: "arrowDown", color: css("--accent"), text: "樣本外 →" });
  }
  const trades = state.backtest?.strategy.trades || [];
  const labelled = trades.length <= 40; // many trades: arrows only, details in the table
  for (const t of trades) {
    const buy = t.side === "buy";
    list.push({
      time: t.date, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown",
      color: buy ? css("--series-1") : css("--series-2"), text: labelled ? (buy ? "買" : "賣") : "",
    });
  }
  list.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  markers.setMarkers(list.filter((m) => state.bars.length && m.time >= state.bars[0].time));
}

function setThresholds(lines) {
  for (const l of thresholdLines) alphaLine.removePriceLine(l);
  thresholdLines = lines.map(([price, title, color]) => alphaLine.createPriceLine({
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
    parts.push(el("span", {}, `${k} `), el("span", { class: cls }, v.toFixed(2)));
  }
  parts.push(el("span", { class: cls }, signedPct(bar.close / prev - 1)));
  const a = param?.seriesData?.get(alphaLine) ?? state.alphaByTime?.get(bar.time);
  if (a && a.value !== undefined) parts.push(el("span", {}, `alpha ${a.value.toFixed(4)}`));
  legend.replaceChildren(...parts);
}
chart.subscribeCrosshairMove(updateLegend);

// ---------- data ----------
async function loadDatasets(selectId) {
  state.datasets = await api("/api/datasets");
  const select = $("#dataset");
  select.replaceChildren(...state.datasets.map((d) =>
    el("option", { value: d.id }, `${d.symbol}  ${d.first_date} ~ ${d.last_date}`)));
  if (!state.datasets.length) {
    $("#download-form").hidden = false;
    msg("#formula-msg", "還沒有資料，請先從 FinMind 下載。", "error");
    return;
  }
  select.value = selectId && state.datasets.some((d) => d.id === selectId) ? selectId : state.datasets[0].id;
  await loadBars();
}

async function loadBars() {
  const id = $("#dataset").value;
  const d = state.datasets.find((x) => x.id === id);
  const data = await api(`/api/bars?dataset=${encodeURIComponent(id)}`);
  state.dataset = id;
  state.symbol = data.symbol;
  state.bars = data.bars;
  state.events = data.events;
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
  if (state.current) evaluate();
}

function clearResults() {
  state.backtest = null;
  alphaLine.setData([]);
  stratLine.setData([]);
  benchLine.setData([]);
  setThresholds([]);
  $("#bt-result").hidden = true;
  renderMarkers();
}

$("#dataset").addEventListener("change", () => loadBars().catch((e) => msg("#formula-msg", e.message, "error")));
$("#split").addEventListener("change", () => { renderMarkers(); state.ics = {}; renderPool(); });

$("#open-download").addEventListener("click", () => {
  $("#download-form").hidden = false;
  $("#dl-end").value ||= new Date().toISOString().slice(0, 10);
  $("#dl-symbol").focus();
});
$("#dl-cancel").addEventListener("click", () => { $("#download-form").hidden = true; });
$("#download-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  await busy(button, async () => {
    $("#dl-status").textContent = "下載中…（每個資料集一次請求，可能需要一分鐘）";
    try {
      const out = await api("/api/data/download", {
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
    if (ic) head.append(el("span", { class: "ic", title: "5 日 IC：樣本內 / 樣本外" }, `${fix(ic.is, 2)} / ${fix(ic.oos, 2)}`));
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
      const out = await api("/api/alpha/evaluate", { ...range(), expr, horizons: [1, 5, 20] });
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
  const head = el("tr");
  head.append(el("th", {}, "預測期"));
  for (const label of ["IC", "Rank IC", "勝率", "樣本數"]) {
    head.append(el("th", {}, `${label} 內`), ...(oos ? [el("th", {}, `${label} 外`)] : []));
  }
  const rows = [head];
  for (const h of stats.horizons) {
    const a = is?.horizons?.[String(h)] || {}, b = oos?.horizons?.[String(h)] || {};
    const tr = el("tr");
    tr.append(el("td", {}, `${h} 日`));
    for (const [key, f] of [["ic", (v) => fix(v, 3)], ["rank_ic", (v) => fix(v, 3)], ["hit_rate", (v) => pct(v, 1)], ["n", (v) => v ?? "—"]]) {
      tr.append(el("td", {}, f(a[key])));
      if (oos) tr.append(el("td", {}, f(b[key])));
    }
    rows.push(tr);
  }
  table.replaceChildren(...rows);
  const h = stats.horizons.includes(5) ? 5 : stats.horizons[0];
  $("#q-title").textContent = `（alpha 由低到高分 ${stats.quantiles} 組，隔日開盤起 ${h} 日平均報酬；分組界線只用樣本內資料）`;
  const q = (s) => s?.horizons?.[String(h)]?.quantile_returns?.map((x) => x.mean_fwd);
  quantileChart($("#quantile-chart"), q(is), q(oos));
  $("#eval-note").textContent = stats.note || "";
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
  const W = container.clientWidth || 340, H = 150, m = { l: 44, r: 6, t: 8, b: 18 };
  const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (H - m.t - m.b);
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "各分組平均報酬長條圖" });
  const n = a.length, slot = (W - m.l - m.r) / n, bw = Math.max(4, (slot - 6) / groups.length - 2);
  const colors = [css("--series-1"), css("--series-2")];
  for (const v of [lo, 0, hi]) {
    const t = mk("text", { x: m.l - 4, y: y(v) + 3, "text-anchor": "end" });
    t.textContent = pct(v, 2);
    svg.append(t);
  }
  svg.append(mk("line", { class: "axis", x1: m.l, x2: W - m.r, y1: y(0), y2: y(0) }));
  for (let i = 0; i < n; i++) {
    groups.forEach((g, k) => {
      const v = g[i];
      if (v === null || v === undefined) return;
      const x = m.l + i * slot + 3 + k * (bw + 2);
      const top = Math.min(y(v), y(0)), h = Math.max(1, Math.abs(y(v) - y(0)));
      const rect = mk("rect", { x, y: top, width: bw, height: h, rx: 2, fill: colors[k] });
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
  container.append(key);
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
    const out = await api("/api/backtest", backtestBody(saveAs));
    state.backtest = out;
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
  const head1 = el("tr"), head2 = el("tr");
  head1.append(el("th", {}, ""));
  head2.append(el("th", {}, "指標"));
  for (const label of ["策略", "買入持有"]) {
    head1.append(el("th", { class: "grp", colspan: segs.length }, label));
    for (const [, s] of segs) head2.append(el("th", {}, segs.length > 1 ? `樣本${s}` : s));
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
      for (const [k] of segs) tr.append(el("td", {}, f(side.metrics[k]?.[key])));
    }
    return tr;
  });
  table.replaceChildren(head1, head2, ...rows);

  const trades = out.strategy.trades;
  $("#trade-count").textContent = `（${trades.length} 筆；價格為當時實際成交價，未做分割調整；點一列可跳到圖上）`;
  const head = el("tr");
  for (const h of ["日期", "買賣", "股數", "價格", "成本"]) head.append(el("th", {}, h));
  $("#trade-table").replaceChildren(head, ...trades.slice().reverse().map((t) => {
    const tr = el("tr");
    tr.append(el("td", {}, t.date), el("td", {}, t.side === "buy" ? "買" : "賣"),
      el("td", {}, Number(t.quantity).toLocaleString("zh-TW")), el("td", {}, Number(t.price).toFixed(2)),
      el("td", {}, money(Number(t.commission) + Number(t.tax))));
    tr.style.cursor = "pointer";
    tr.addEventListener("click", () => focusDate(t.date));
    return tr;
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
}
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
  } catch (e) {
    msg("#formula-msg", `資料載入失敗：${e.message}`, "error");
  }
})();
