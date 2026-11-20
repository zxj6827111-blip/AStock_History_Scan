/* 生存者报告页：只读 /api/survivor/*，渲染 latest.json（或指定档位）。 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function pct(v, nd = 2) {
  if (v === null || v === undefined) return "—";
  return (v > 0 ? "+" : "") + Number(v).toFixed(nd) + "%";
}
function num(v, nd = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(nd);
}
function updown(v, nd = 2) {
  if (v === null || v === undefined) return "—";
  const cls = v > 0 ? "sv-up" : (v < 0 ? "sv-down" : "");
  return `<span class="${cls}">${pct(v, nd)}</span>`;
}
function badge(g) {
  const label = g === "A-other" ? "A" : g;
  return `<span class="sv-badge g-${esc(g)}">${esc(label)}</span>`;
}
function codeCell(r) {
  return `<td><span class="sv-code">${esc(r.code)}</span><span class="sv-name">${esc(r.name)}</span></td>`;
}
function difDea(r) {
  if (r.dif === null || r.dif === undefined) return "—";
  return `${num(r.dif, 3)}/${num(r.dea, 3)} ${r.dif > r.dea ? "金叉" : "死叉"}`;
}

function table(id, headers, rowsHtml) {
  const t = $(id);
  if (!rowsHtml) {
    t.innerHTML = "";
    t.insertAdjacentHTML("afterbegin",
      `<thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>` +
      `<tbody><tr><td colspan="${headers.length}" style="color:var(--text-sub)">当前为空</td></tr></tbody>`);
    return;
  }
  t.innerHTML = `<thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rowsHtml}</tbody>`;
}

function render(rep) {
  const c = rep.counts || {};
  const sess = rep.session || "EOD";
  const sessLabel = { INTRADAY: "盘中", AM: "午盘", EOD: "日终" }[sess] || sess;
  const kind = { INTRADAY: "半小时报告", AM: "午盘报告", EOD: "日报" }[sess] || "报告";

  $("head-kvs").innerHTML = [
    ["报告", `${esc(rep.title || "")}${kind} · ${esc(rep.pillar)}（${esc(rep.month_id)}）`],
    ["数据到", `${esc(rep.asof_date)} ${sessLabel}${rep.asof_time ? " " + esc(rep.asof_time) : ""}`],
    ["生成时间", esc(rep.generated_at_bjt || "")],
    ["当前段", rep.current_segment ? `S 段 ${esc(rep.current_segment.id)}（${esc(rep.current_segment.start)}~${esc(rep.current_segment.end)}）` : "—"],
    ["100% 池", `${c.pool ?? "—"} 只`],
    ["A 生存者", `${c.A ?? "—"} 只（B ${c.B ?? 0} / C ${c.C ?? 0} / A-other ${c["A-other"] ?? 0}）`],
    ["D 非生存者", `${c.D ?? "—"} 只`],
    ["指数过滤", esc((rep.index_filter || {}).regime || "—")],
  ].map(([k, v]) => `<div class="sv-kv"><span class="k">${k}</span><span class="v" title="${esc(v)}">${v}</span></div>`).join("");

  $("seg-banner").innerHTML = `<b>当前段动作：</b>${esc((rep.current_segment || {}).action || "—")}`;
  $("idx-banner").innerHTML = `<b>指数过滤：</b>${esc((rep.index_filter || {}).text || "—")}`;
  $("modeb-note").textContent = (rep.research_constants || {}).mode_b || "";
  $("b-rule").textContent = (rep.current_segment || {}).b_rule || "";

  const rows = rep.rows || [];
  const bs = rows.filter((r) => r.group === "B");
  const watch = rows.filter((r) => r.group !== "D");
  const ao = rows.filter((r) => r.group === "A-other");
  const cs = rows.filter((r) => r.group === "C");
  const ds = rows.filter((r) => r.group === "D").slice(0, 8);

  table("tb-b",
    ["代码", "组", "月初开", "现价", "今日", "MTD", "MTM", "DIF/DEA", "买入参考", "止损-5%", "破月初价", "目标+3%", "目标+5%", "备注"],
    bs.map((r) => `<tr>${codeCell(r)}<td>${badge(r.group)}</td><td>${num(r.month_open)}</td><td><b>${num(r.close)}</b></td><td>${updown(r.today_pct)}</td><td>${updown(r.mtd_pct)}</td><td>${updown(r.mtm_pct, 1)}</td><td>${difDea(r)}</td><td>${num(r.buy_ref)}</td><td>${num(r.stop_loss)}</td><td>${num(r.break_level)}</td><td>${num(r.target_3)}</td><td>${num(r.target_5)}</td><td class="sv-note">${esc(r.note || "")}</td></tr>`).join(""));

  const watchRows = [...bs, ...cs, ...ao];
  table("tb-watch",
    ["代码", "组", "月初开", "最新价", "今日", "MTD", "MTM", "DIF/DEA", "建议"],
    watchRows.map((r) => `<tr>${codeCell(r)}<td>${badge(r.group)}</td><td>${num(r.month_open)}</td><td><b>${num(r.close)}</b></td><td>${updown(r.today_pct)}</td><td>${updown(r.mtd_pct)}</td><td>${updown(r.mtm_pct, 1)}</td><td>${difDea(r)}</td><td>${esc(r.advice)}</td></tr>`).join(""));

  const s1 = rep.s1_holdings || [];
  table("tb-s1",
    ["代码", "组", "入场日", "入场价", "现价", "浮盈亏", "止损-5%", "结构止损", "状态"],
    s1.map((h) => `<tr><td><span class="sv-code">${esc(h.code)}</span><span class="sv-name">${esc(h.name)}</span></td><td>${badge(h.group)}</td><td>${esc(h.entry_date)}</td><td>${num(h.entry_price)}</td><td><b>${num(h.close)}</b></td><td>${updown(h.pnl_pct)}</td><td>${num(h.stop_loss)}</td><td>${num(h.break_level)}</td><td>${h.stop_triggered ? `<span class="sv-up">已触发止损</span>` : "持有"}</td></tr>`).join(""));
  $("s1-notes").textContent = s1.filter((h) => h.stop_triggered)
    .map((h) => `已触发止损：${h.code}（现价 ${num(h.close)}，${h.stop_detail}）；按规则退出，不补仓。`).join(" ");

  const dp = rep.diff_prev || {};
  $("diff-box").innerHTML = `上一份：${esc(dp.prev_asof || "—")}<br>` +
    (dp.lines || []).map((l) => `· ${esc(l)}`).join("<br>");

  table("tb-ao",
    ["代码", "组", "月初开", "现价", "MTD", "MTM", "DIF/DEA", "升级条件", "防守位"],
    ao.map((r) => `<tr>${codeCell(r)}<td>${badge(r.group)}</td><td>${num(r.month_open)}</td><td>${num(r.close)}</td><td>${updown(r.mtd_pct)}</td><td>${updown(r.mtm_pct, 1)}</td><td>${difDea(r)}</td><td>MTM>8 且金叉→升 B</td><td>收盘破 ${num(r.break_level)}</td></tr>`).join(""));

  $("d-note").textContent = rep.d_note || "";
  table("tb-d",
    ["代码", "组", "MTD", "MTM"],
    ds.map((r) => `<tr>${codeCell(r)}<td>${badge(r.group)}</td><td>${updown(r.mtd_pct)}</td><td>${updown(r.mtm_pct, 1)}</td></tr>`).join(""));

  $("pool-n").textContent = rows.length;
  table("tb-pool",
    ["代码", "月初开", "最新价", "今日", "MTD", "MTM", "DIF/DEA", "组", "建议"],
    rows.map((r) => `<tr>${codeCell(r)}<td>${num(r.month_open)}</td><td>${num(r.close)}</td><td>${updown(r.today_pct)}</td><td>${updown(r.mtd_pct)}</td><td>${updown(r.mtm_pct, 1)}</td><td>${difDea(r)}</td><td>${badge(r.group)}</td><td>${esc(r.advice)}</td></tr>`).join(""));

  const sell = rep.sell_schedule || [];
  $("foot-note").innerHTML =
    `<b>卖出时间表：</b>${sell.map((s) => `${esc(s.when)}：${esc(s.action)}`).join("；")}<br>` +
    `${esc(rep.risk_note || "")}<br>${esc(rep.staging_note || "")}<br>${esc(rep.data_source_note || "")}<br><i>*${esc(rep.footer || "")}*</i>`;

  if (rep.missing && rep.missing.length) {
    $("fresh-note").textContent = `⚠ ${rep.missing.length} 只数据缺口：` +
      rep.missing.map((m) => m.code).join("、");
  } else {
    $("fresh-note").textContent = "";
  }
}

async function fetchJson(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

function fillDateSelect(rep) {
  const sel = $("date-sel");
  const slotsToday = rep.slots_today || [];
  const dates = [...new Set(slotsToday.map((s) => s.date))];
  // 日期下拉先给当前日（历史日由档位接口按需补；首版只列当日）
  if (!dates.length) dates.push(rep.asof_date || "");
  sel.innerHTML = dates.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
}

function fillSlotSelect(rep, active) {
  const sel = $("slot-sel");
  const slots = rep.slots_today || [];
  sel.innerHTML = slots.map((s) => {
    const label = { INTRADAY: "盘中", AM: "午盘", EOD: "日终" }[s.session] || s.session;
    const hh = s.hhmm.slice(0, 2) + ":" + s.hhmm.slice(2);
    return `<option value="${esc(s.hhmm)}_${esc(s.session)}">${label} ${hh}</option>`;
  }).join("");
  if (active) sel.value = active;
  else sel.value = $("slot-sel").options.length ? $("slot-sel").options[0].value : "";
}

async function loadLatest() {
  try {
    const rep = await fetchJson("/api/survivor/latest");
    render(rep);
    fillDateSelect(rep);
    fillSlotSelect(rep);
    $("latest-btn").classList.remove("on");
    $("md-link").href = "/api/survivor/export?name=latest.md";
  } catch (e) {
    $("fresh-note").textContent = "加载失败：" + e.message;
  }
}

async function loadSlot(date, hhmm, session) {
  try {
    const rep = await fetchJson(
      `/api/survivor/slot?date=${encodeURIComponent(date)}&hhmm=${encodeURIComponent(hhmm)}&session=${encodeURIComponent(session)}`);
    render(rep);
    $("latest-btn").classList.add("on");
    $("md-link").href = `/api/survivor/export?name=survivor_${date}_${hhmm}_${session}.md`;
  } catch (e) {
    $("fresh-note").textContent = "加载失败：" + e.message;
  }
}

let autoTimer = null;
function setAuto(on) {
  const btn = $("auto-btn");
  if (on && !autoTimer) {
    btn.classList.add("on");
    btn.textContent = "自动刷新 60s";
    autoTimer = setInterval(loadLatest, 60000);
  } else if (!on && autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
    btn.classList.remove("on");
    btn.textContent = "自动刷新关";
  }
}

$("auto-btn").addEventListener("click", () => setAuto(!autoTimer));
$("latest-btn").addEventListener("click", loadLatest);
$("slot-sel").addEventListener("change", () => {
  const v = $("slot-sel").value;
  if (!v) return loadLatest();
  const [hhmm, session] = v.split("_");
  const date = $("date-sel").value;
  loadSlot(date, hhmm, session);
});
$("date-sel").addEventListener("change", async () => {
  const date = $("date-sel").value;
  if (!date) return;
  try {
    const out = await fetchJson(`/api/survivor/slots?date=${encodeURIComponent(date)}`);
    const rep = { slots_today: out.slots };
    fillSlotSelect({ slots_today: out.slots }, null);
    if (out.slots.length) {
      const s = out.slots[out.slots.length - 1];
      loadSlot(s.date, s.hhmm, s.session);
    }
  } catch (e) {
    $("fresh-note").textContent = "档位加载失败：" + e.message;
  }
});

loadLatest().then(() => setAuto(true));
