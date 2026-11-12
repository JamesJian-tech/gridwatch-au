"use strict";

const REGION_NAMES = { NSW1: "新南威尔士", QLD1: "昆士兰", VIC1: "维多利亚", SA1: "南澳", TAS1: "塔斯马尼亚" };
// Colour follows the entity: each region keeps its slot everywhere on the page.
const REGION_SLOT = { NSW1: 1, QLD1: 2, VIC1: 3, SA1: 4, TAS1: 5 };

const state = { data: null, region: "VIC1", clip: true, charts: {} };
try {
  const saved = localStorage.getItem("gw-region");
  if (saved && REGION_NAMES[saved]) state.region = saved;
} catch (_) { /* storage unavailable: keep default */ }

const $ = (sel) => document.querySelector(sel);
const fmt = (v, d = 0) => v == null ? "–" : Number(v).toLocaleString("en-AU", { minimumFractionDigits: d, maximumFractionDigits: d });

function tokens() {
  const cs = getComputedStyle(document.documentElement);
  const v = (n) => cs.getPropertyValue(n).trim();
  return {
    surface: v("--surface"), ink: v("--ink"), ink2: v("--ink-2"), muted: v("--muted"),
    grid: v("--grid"), axis: v("--axis"),
    slot: (i) => v(`--s${i}`),
    seq: v("--seq").split(",").map((s) => s.trim()),
  };
}

function regionColor(t, reg) { return t.slot(REGION_SLOT[reg]); }

// ---------------------------------------------------------------- shared chart chrome

function base(t, extra = {}) {
  return {
    animation: false,
    textStyle: { fontFamily: "system-ui, -apple-system, 'Segoe UI', 'PingFang SC', sans-serif", color: t.ink2 },
    grid: { left: 8, right: 72, top: 36, bottom: 8, containLabel: true },
    legend: {
      type: "scroll", top: 0, left: 0, right: 0, icon: "roundRect", itemWidth: 12, itemHeight: 4,
      textStyle: { color: t.ink2 }, pageIconColor: t.ink2, pageIconInactiveColor: t.grid, pageTextStyle: { color: t.muted },
    },
    tooltip: {
      trigger: "axis",
      backgroundColor: t.surface, borderColor: t.grid, textStyle: { color: t.ink, fontSize: 12 },
      axisPointer: { type: "line", lineStyle: { color: t.muted, width: 1 } },
      confine: true,
    },
    ...extra,
  };
}

function catAxis(t, data, formatter) {
  return {
    type: "category", data, boundaryGap: false,
    axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false },
    axisLabel: { color: t.muted, hideOverlap: true, formatter },
  };
}

function valAxis(t, extra = {}) {
  return {
    type: "value",
    splitLine: { lineStyle: { color: t.grid, width: 1 } },
    axisLabel: { color: t.muted, formatter: (v) => fmt(v) },
    ...extra,
  };
}

function line(t, name, data, color, { endLabel = true, area = false, stack = null, z = 2 } = {}) {
  return {
    name, type: "line", data, stack, z,
    showSymbol: false, symbolSize: 8, connectNulls: false,
    lineStyle: { width: 2, color }, itemStyle: { color },
    areaStyle: area ? { color, opacity: 0.35 } : undefined,
    emphasis: { focus: "series" },
    endLabel: endLabel ? { show: true, formatter: name, color: t.ink2, fontSize: 12, distance: 6 } : undefined,
    labelLayout: endLabel ? { moveOverlap: "shiftY" } : undefined,
  };
}

const hhmm = (s) => {
  const [d, tm] = s.split(" ");
  return tm === "00:00" || tm === "00:05" ? `${d.slice(5)}\n${tm}` : tm;
};

function tooltipRows(params, unit, digits = 0) {
  const head = params.length ? params[0].axisValueLabel.replace("\n", " ") : "";
  const rows = params
    .filter((p) => p.value != null)
    .map((p) => `<div style="display:flex;justify-content:space-between;gap:16px">
        <span>${p.marker}${p.seriesName}</span><b>${fmt(p.value, digits)}${unit}</b></div>`);
  return `<div style="margin-bottom:4px">${head}</div>${rows.join("")}`;
}

function percentile(values, q) {
  const xs = values.filter((v) => v != null).sort((a, b) => a - b);
  if (!xs.length) return null;
  return xs[Math.min(xs.length - 1, Math.floor(q * xs.length))];
}

// ---------------------------------------------------------------- sections

function renderMeta(d) {
  $("#meta").textContent = d.empty
    ? "暂无数据"
    : `最新区间 ${d.last_interval}（NEM 时间） · 页面生成于 ${d.generated_at_utc} UTC`;
}

function renderLatest(t, d) {
  const tbody = $("#latest tbody");
  tbody.innerHTML = "";
  for (const reg of d.regions) {
    const v = d.latest[reg];
    if (!v) continue;
    const tr = document.createElement("tr");
    const neg = v.rrp < 0 ? '<span class="tag">负电价</span>' : "";
    tr.innerHTML = `
      <td><span class="dot" style="background:${regionColor(t, reg)}"></span>${REGION_NAMES[reg]} <span class="tag">${reg}</span></td>
      <td class="price">${fmt(v.rrp, 2)}${neg}</td>
      <td>${fmt(v.demand)}</td><td>${fmt(v.solar)}</td><td>${fmt(v.wind)}</td><td>${fmt(v.net)}</td>`;
    tbody.appendChild(tr);
  }
}

function renderPrice(t, d) {
  const r = d.recent;
  const all = d.regions.flatMap((reg) => r.series[reg].rrp);
  const max = Math.max(...all.filter((v) => v != null));
  const p99 = percentile(all, 0.99);
  const cap = Math.max(300, Math.ceil((p99 * 1.15) / 50) * 50);
  const clipped = state.clip && max > cap;
  $("#price-note").textContent = clipped
    ? `5 分钟区域参考价（RRP），$/MWh。纵轴截断在 $${fmt(cap)}，期间最高 $${fmt(max)}，悬停可看实际值。`
    : "5 分钟区域参考价（RRP），$/MWh";
  return base(t, {
    tooltip: { ...base(t).tooltip, formatter: (p) => tooltipRows(p, "", 2) },
    xAxis: catAxis(t, r.t, hhmm),
    yAxis: valAxis(t, clipped ? { max: cap } : {}),
    series: d.regions.map((reg) => line(t, REGION_NAMES[reg], r.series[reg].rrp, regionColor(t, reg))),
  });
}

function renderMix(t, d) {
  const s = d.recent.series[state.region];
  const solarC = t.slot(4), windC = t.slot(3);
  return base(t, {
    tooltip: { ...base(t).tooltip, formatter: (p) => tooltipRows(p, " MW") },
    xAxis: catAxis(t, d.recent.t, hhmm),
    yAxis: valAxis(t),
    series: [
      line(t, "风电", s.wind, windC, { endLabel: false, area: true, stack: "ren" }),
      line(t, "大型光伏", s.solar, solarC, { endLabel: false, area: true, stack: "ren" }),
      line(t, "需求", s.demand, t.ink, { endLabel: true, z: 3 }),
    ],
  });
}

function renderDaily(t, d, key, unit, digits) {
  const days = d.daily.days;
  return base(t, {
    tooltip: { ...base(t).tooltip, formatter: (p) => tooltipRows(p, unit, digits) },
    xAxis: catAxis(t, days, (s) => s.slice(5)),
    yAxis: valAxis(t, key === "neg_pct" ? { min: 0, max: (v) => Math.max(10, Math.ceil(v.max / 10) * 10) } : {}),
    series: d.regions.map((reg) => {
      const sr = line(t, REGION_NAMES[reg], d.daily.series[reg][key], regionColor(t, reg));
      if (days.length < 40) { sr.showSymbol = true; sr.symbolSize = 6; }
      return sr;
    }),
  });
}

function renderProfile(t, d) {
  const hours = d.profile.hours.map((h) => `${String(h).padStart(2, "0")}:00`);
  $("#profile-note").textContent = `最近 ${d.profile.days} 天，按区间开始时刻归入小时，$/MWh`;
  return base(t, {
    tooltip: { ...base(t).tooltip, formatter: (p) => tooltipRows(p, "", 2) },
    xAxis: { ...catAxis(t, hours), boundaryGap: true },
    yAxis: valAxis(t),
    series: d.regions.map((reg) => line(t, REGION_NAMES[reg], d.profile.series[reg].avg, regionColor(t, reg))),
  });
}

function renderHeat(t, d) {
  const h = d.heatmap;
  const rows = h.series[state.region];
  const cells = [];
  rows.forEach((row, mi) => row.forEach((v, hi) => cells.push([hi, mi, v])));
  return {
    animation: false,
    textStyle: { color: t.ink2 },
    grid: { left: 8, right: 16, top: 8, bottom: 56, containLabel: true },
    tooltip: {
      backgroundColor: t.surface, borderColor: t.grid, textStyle: { color: t.ink, fontSize: 12 },
      formatter: (p) => `${h.months[p.value[1]]} · ${String(p.value[0]).padStart(2, "0")}:00–${String(p.value[0] + 1).padStart(2, "0")}:00<br><b>${p.value[2] == null ? "无数据" : fmt(p.value[2], 1) + "%"}</b> 的区间为负电价`,
    },
    xAxis: { type: "category", data: h.hours.map((x) => String(x).padStart(2, "0")), splitArea: { show: false },
             axisLine: { lineStyle: { color: t.axis } }, axisTick: { show: false }, axisLabel: { color: t.muted } },
    yAxis: { type: "category", data: h.months, inverse: true, axisLine: { show: false }, axisTick: { show: false },
             axisLabel: { color: t.muted } },
    visualMap: {
      type: "continuous", min: 0, max: 100, calculable: false, orient: "horizontal", left: "center", bottom: 0,
      itemWidth: 12, itemHeight: 180, text: ["100%", "0%"], textStyle: { color: t.muted },
      inRange: { color: t.seq },
    },
    series: [{
      type: "heatmap", data: cells,
      itemStyle: { borderColor: t.surface, borderWidth: 2, borderRadius: 3 },
      emphasis: { itemStyle: { borderColor: t.ink, borderWidth: 1 } },
    }],
  };
}

function renderIc(t, d) {
  const ic = d.interconnectors;
  return base(t, {
    tooltip: { ...base(t).tooltip, formatter: (p) => tooltipRows(p, " MW") },
    grid: { ...base(t).grid, right: 96 },
    xAxis: catAxis(t, d.recent.t, hhmm),
    yAxis: valAxis(t),
    series: ic.ids.map((id, i) => line(t, id, ic.flow[id], t.slot((i % 8) + 1))),
  });
}

// ---------------------------------------------------------------- wiring

function chart(id) {
  if (!state.charts[id]) state.charts[id] = echarts.init(document.getElementById(id), null, { renderer: "svg" });
  return state.charts[id];
}

function renderAll() {
  const d = state.data;
  const t = tokens();
  renderMeta(d);
  if (d.empty) return;
  renderLatest(t, d);
  chart("c-price").setOption(renderPrice(t, d), true);
  chart("c-mix").setOption(renderMix(t, d), true);
  chart("c-daily").setOption(renderDaily(t, d, "avg", "", 2), true);
  chart("c-neg").setOption(renderDaily(t, d, "neg_pct", "%", 1), true);
  chart("c-profile").setOption(renderProfile(t, d), true);
  // one row per month: keep cells roughly square-ish instead of stretching one month tall
  document.getElementById("c-heat").style.height = `${Math.max(150, d.heatmap.months.length * 30 + 110)}px`;
  chart("c-heat").resize();
  chart("c-heat").setOption(renderHeat(t, d), true);
  chart("c-ic").setOption(renderIc(t, d), true);
  document.querySelectorAll(".seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.region === state.region)));
}

function buildSelectors() {
  document.querySelectorAll(".seg").forEach((seg) => {
    for (const reg of Object.keys(REGION_NAMES)) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.region = reg;
      b.textContent = REGION_NAMES[reg];
      b.addEventListener("click", () => {
        state.region = reg;
        try { localStorage.setItem("gw-region", reg); } catch (_) { /* ignore */ }
        renderAll();
      });
      seg.appendChild(b);
    }
  });
  $("#clip").addEventListener("change", (e) => { state.clip = e.target.checked; renderAll(); });
}

async function main() {
  buildSelectors();
  try {
    const res = await fetch("data/dashboard.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
  } catch (e) {
    $("#meta").textContent = "数据加载失败";
    const div = document.createElement("div");
    div.className = "error";
    div.textContent = `无法读取 data/dashboard.json（${e.message}）。数据由 GitHub Actions 每 30 分钟生成一次。`;
    document.querySelector("main").prepend(div);
    return;
  }
  renderAll();
  window.addEventListener("resize", () => Object.values(state.charts).forEach((c) => c.resize()));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderAll);
}

document.addEventListener("DOMContentLoaded", main);
