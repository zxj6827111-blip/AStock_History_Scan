/* 生存者报告页 v2：只读 /api/survivor/*，把报告呈现成“决策摘要 → 持仓风险 →
   B 组机会 → 变化 → 统一名单”。本文件不重新计算任何研究指标；金叉/死叉、
   分组、买卖参考全部以报告 JSON 为准。 */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  const AUTO_MS = 60000;
  /* PM = 晚间全量切片（当日已收盘、数据补全），与生成器 Markdown 的“日报”口径一致。 */
  const SESSION_LABEL = { INTRADAY: "盘中", AM: "午盘", PM: "日终", EOD: "日终" };
  const SESSION_KIND = { INTRADAY: "半小时报告", AM: "午盘报告", PM: "日报", EOD: "日报" };

  /* 单一状态：正在看的报告、档位列表、自动刷新偏好和请求序号。
     手动查看历史档位时 S.mode=manual，自动刷新只作用于 latest。 */
  const S = {
    mode: "latest",          // latest | manual
    report: null,
    slots: [],
    slotValue: "",
    date: "",
    auto: true,
    loading: false,
    stale: false,
    seq: 0,
    lastGood: null,          // { report, slots, mode }
    list: "watch",
    query: "",
  };

  /* ---------- 基础格式化 ---------- */

  const isNil = (v) => v === null || v === undefined || v === "";

  function pct(v, nd = 2) {
    if (isNil(v)) return "—";
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return (n > 0 ? "+" : "") + n.toFixed(nd) + "%";
  }

  function num(v, nd = 2) {
    if (isNil(v)) return "—";
    const n = Number(v);
    return Number.isFinite(n) ? n.toFixed(nd) : "—";
  }

  function changeHtml(v, nd = 2) {
    if (isNil(v)) return '<span class="sv-flat">—</span>';
    const n = Number(v);
    if (!Number.isFinite(n)) return '<span class="sv-flat">—</span>';
    const cls = n > 0 ? "sv-up" : (n < 0 ? "sv-down" : "sv-flat");
    const arrow = n > 0 ? "▲" : (n < 0 ? "▼" : "•");
    return `<span class="${cls}">${arrow} ${pct(n, nd)}</span>`;
  }

  function stockLink(code) {
    return `/?id=${encodeURIComponent(String(code || ""))}&tab=stock&from=survivor`;
  }

  function stockCell(r) {
    return `<td class="sv-cell-main" data-label="股票">` +
      `<a class="sv-code" href="${stockLink(r.code)}">${esc(r.code)}</a>` +
      `<span class="sv-name">${esc(r.name)}</span></td>`;
  }

  function groupBadge(g) {
    const label = g === "A-other" ? "A" : (g === "B" ? "B8" : (g || "—"));
    return `<span class="sv-badge g-${esc(g || "")}">${esc(label)}</span>`;
  }

  /* 金叉/死叉只读报告的 cross 字段；DIF/DEA 作为辅助小字，不在前端重新判定。 */
  function crossCell(r) {
    const cross = r.cross || "—";
    const cls = cross === "金叉" ? " up" : (cross === "死叉" ? " down" : "");
    return `<td data-label="信号"><span class="sv-cross${cls}">${esc(cross)}</span>` +
      `<span class="sv-mini">DIF ${num(r.dif, 3)} / DEA ${num(r.dea, 3)}</span></td>`;
  }

  function priceChangeCell(r) {
    return `<td class="sv-num" data-label="现价 / 今日">` +
      `<span class="sv-strong">${num(r.close)}</span>` +
      `<span class="sv-mini">${changeHtml(r.today_pct)}</span></td>`;
  }

  function momentumCell(r) {
    return `<td class="sv-num" data-label="MTD / MTM">` +
      `<span class="sv-strong">${changeHtml(r.mtd_pct)}</span>` +
      `<span class="sv-mini">${changeHtml(r.mtm_pct, 1)}</span></td>`;
  }

  function noteCell(text) {
    return `<td class="sv-note-cell" data-label="备注">${esc(text || "—")}</td>`;
  }

  function setChip(el, text, variant) {
    if (!el) return;
    el.textContent = text;
    el.className = `sv-chip ${variant || ""}`.trim();
  }

  function setEmpty(table, emptyEl, isEmpty, message) {
    const wrap = table && table.closest(".sv-tablewrap");
    if (wrap) wrap.classList.toggle("hidden", isEmpty);
    if (!emptyEl) return;
    emptyEl.classList.toggle("hidden", !isEmpty);
    if (isEmpty) emptyEl.textContent = message;
  }

  /* ---------- 状态提示 ---------- */

  function showStatus(message, kind) {
    const el = $("status");
    el.textContent = message || "";
    el.className = `sv-status ${kind || ""}`.trim();
    el.classList.toggle("show", !!message);
  }

  async function fetchJson(url) {
    const res = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
    let body = null;
    try { body = await res.json(); } catch (_e) { body = null; }
    if (!res.ok) {
      const msg = (body && body.error) || `HTTP ${res.status}`;
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function rememberGood(report, slots, mode) {
    S.report = report;
    S.slots = slots || [];
    S.mode = mode;
    if (mode === "manual") {
      S.slotValue = S.slotValue || (S.slots.length ? slotValue(S.slots[S.slots.length - 1]) : "");
    } else {
      S.slotValue = reportSlotValue(report)
        || (S.slots.length ? slotValue(S.slots[S.slots.length - 1]) : "");
    }
    S.stale = false;
    S.lastGood = { report, slots: S.slots, mode, slotValue: S.slotValue };
  }

  function handleLoadError(err, label) {
    const msg = err && err.status === 401
      ? `${label}：登录已失效，请刷新页面重新登录`
      : `${label}：${err && err.message ? err.message : "请求失败"}`;
    if (err && err.status === 401) {
      S.auto = false; // 会话已失效时停止 60s 轮询，避免持续制造 401
      localStorage.setItem("bazi_survivor_auto", "0");
    }
    if (S.lastGood) {
      S.report = S.lastGood.report;
      S.slots = S.lastGood.slots;
      S.mode = S.lastGood.mode;
      S.slotValue = S.lastGood.slotValue || "";
      S.stale = true;
      syncControls();
      const asof = reportAsofText(S.report);
      showStatus(`${msg}；当前保留上次成功报告（${asof}）。`, "err");
    } else {
      S.report = null;
      S.stale = false;
      clearRender();
      syncControls();
      showStatus(msg, "err");
    }
  }

  function reportAsofText(rep) {
    if (!rep) return "—";
    const sess = SESSION_LABEL[rep.session] || rep.session || "";
    const hhmm = effectiveHhmm(rep);
    return `${rep.asof_date || "—"} ${sess}${hhmm ? " " + hhmm : ""}`;
  }

  function slotValue(slot) {
    return `${slot.hhmm}_${slot.session}`;
  }

  function slotLabel(slot, markLatest) {
    const sess = SESSION_LABEL[slot.session] || slot.session;
    const hhmm = `${String(slot.hhmm).slice(0, 2)}:${String(slot.hhmm).slice(2)}`;
    return `${sess} ${hhmm}${markLatest ? "（最新）" : ""}`;
  }

  function reportSlotValue(rep) {
    if (!rep || !rep.asof_time || !rep.session) return "";
    return `${String(rep.asof_time).replace(":", "")}_${rep.session}`;
  }

  /* PM/EOD 报告的 asof_time 可能为空；展示时用当前档位键补齐 15:00，
     保证“数据截止”和档位下拉说的是同一份文件。 */
  function effectiveHhmm(rep) {
    if (rep && rep.asof_time) return rep.asof_time;
    const active = S.slotValue
      || (S.slots.length ? slotValue(S.slots[S.slots.length - 1]) : "");
    const hhmm = String(active).split("_")[0] || "";
    return hhmm.length === 4 ? `${hhmm.slice(0, 2)}:${hhmm.slice(2)}` : "";
  }

  /* ---------- 控件同步：日期、档位、下载地址、自动刷新 ---------- */

  function syncControls() {
    const rep = S.report;
    const dateInput = $("date-sel");
    const slotSel = $("slot-sel");
    const latestBtn = $("latest-btn");
    const autoBtn = $("auto-btn");

    dateInput.value = rep ? rep.asof_date : (S.date || "");

    if (!S.slots.length) {
      slotSel.innerHTML = '<option value="">无报告</option>';
      slotSel.value = "";
      slotSel.disabled = true;
    } else {
      slotSel.disabled = false;
      const active = S.mode === "manual"
        ? S.slotValue
        : (reportSlotValue(rep) || slotValue(S.slots[S.slots.length - 1]));
      slotSel.innerHTML = S.slots.map((s) =>
        `<option value="${esc(slotValue(s))}">${esc(slotLabel(s, S.mode === "latest" && slotValue(s) === active))}</option>`
      ).join("");
      if ([...slotSel.options].some((o) => o.value === active)) {
        slotSel.value = active;
      } else {
        slotSel.value = slotSel.options.length ? slotSel.options[slotSel.options.length - 1].value : "";
        if (rep) showStatus("当前报告档位不在档位列表中，请检查报告文件。", "err");
      }
    }

    /* 已在最新且自动刷新开启时不需要“回到最新”；自动刷新关闭或数据滞后时
       保留可用，作为手动刷新入口。 */
    latestBtn.disabled = S.mode === "latest" && !!rep && S.auto && !S.stale;
    if (S.mode === "manual") {
      autoBtn.disabled = true;
      autoBtn.textContent = "自动刷新已暂停";
      autoBtn.setAttribute("aria-pressed", "false");
    } else {
      autoBtn.disabled = false;
      autoBtn.textContent = S.auto ? "自动刷新 60s" : "自动刷新关";
      autoBtn.setAttribute("aria-pressed", S.auto ? "true" : "false");
    }

    /* 下载文件名必须与实际档位文件一致：EOD 报告的 asof_time 可能为空，
       因此从当前档位键取 hhmm，而不是直接用 rep.asof_time。 */
    let mdName = "latest.md";
    if (S.mode === "manual" && rep) {
      const activeSlot = S.slotValue || (S.slots.length ? slotValue(S.slots[S.slots.length - 1]) : "");
      const [hhmm, session] = activeSlot.split("_");
      if (hhmm && session) mdName = `survivor_${rep.asof_date}_${hhmm}_${session}.md`;
    }
    $("md-link").href = `/api/survivor/export?name=${encodeURIComponent(mdName)}`;

    const modeChip = $("mode-chip");
    if (!rep) setChip(modeChip, "不可用", "risk");
    else if (S.mode === "manual") setChip(modeChip, "手动档位", "info");
    else setChip(modeChip, "最新报告", "live");

    $("stale-chip").classList.toggle("hidden", !S.stale);

    const note = S.mode === "manual"
      ? "已暂停自动刷新：正在查看手动档位；点击“回到最新”恢复最新报告并继续自动刷新。"
      : (S.auto
        ? "自动刷新开启：每 60 秒只更新最新报告；页面在后台时跳过刷新。"
        : "自动刷新已关闭；可点击按钮或“回到最新”手动更新。");
    $("control-note").textContent = note;
  }

  /* ---------- 数据加载 ---------- */

  async function loadLatest(opts = {}) {
    if (S.loading && !opts.force) return; // 手动“回到最新”允许打断在途请求
    const seq = ++S.seq;
    S.loading = true;
    if (!opts.silent) showStatus("正在读取最新报告…");
    try {
      const rep = await fetchJson("/api/survivor/latest");
      if (seq !== S.seq) return;
      rememberGood(rep, rep.slots_today || [], "latest");
      render(rep);
      if (!opts.silent) showStatus("已更新到最新报告。", "ok");
    } catch (e) {
      if (seq !== S.seq) return;
      handleLoadError(e, "最新报告加载失败");
    } finally {
      if (seq === S.seq) S.loading = false;
    }
  }

  async function loadSlot(date, hhmm, session, opts = {}) {
    if (S.loading && !opts.force) return; // 用户切换档位时允许打断在途请求
    const seq = ++S.seq;
    S.loading = true;
    const readable = `${date} ${SESSION_LABEL[session] || session} ${String(hhmm).slice(0, 2)}:${String(hhmm).slice(2)}`;
    showStatus(`正在读取 ${readable} 报告…`);
    try {
      const url = `/api/survivor/slot?date=${encodeURIComponent(date)}` +
        `&hhmm=${encodeURIComponent(hhmm)}&session=${encodeURIComponent(session)}`;
      const rep = await fetchJson(url);
      if (seq !== S.seq) return;
      S.slotValue = `${hhmm}_${session}`;
      rememberGood(rep, S.slots, "manual");
      render(rep);
      showStatus(`已切换到 ${readable} 报告。`, "ok");
    } catch (e) {
      if (seq !== S.seq) return;
      handleLoadError(e, `${readable} 报告加载失败`);
    } finally {
      if (seq === S.seq) S.loading = false;
    }
  }

  async function onDateChange() {
    const input = $("date-sel");
    const date = input.value;
    if (!date) {
      input.value = S.report ? S.report.asof_date : (S.date || "");
      return;
    }
    if (S.report && date === S.report.asof_date) return;

    const seq = ++S.seq;
    S.loading = true; // 查询日期档位期间阻止自动刷新抢跑
    const slotSel = $("slot-sel");
    slotSel.disabled = true;
    slotSel.innerHTML = '<option value="">读取档位…</option>';
    showStatus(`正在查询 ${date} 的报告档位…`);
    try {
      const out = await fetchJson(`/api/survivor/slots?date=${encodeURIComponent(date)}`);
      if (seq !== S.seq) return;
      const slots = out.slots || [];
      if (!slots.length) {
        input.value = S.report ? S.report.asof_date : (S.date || "");
        S.loading = false;
        syncControls();
        showStatus(`${date} 没有生存者报告；已保留当前显示内容。`, "err");
        return;
      }
      S.slots = slots;
      const last = slots[slots.length - 1];
      await loadSlot(date, last.hhmm, last.session, { force: true });
    } catch (e) {
      if (seq !== S.seq) return;
      input.value = S.report ? S.report.asof_date : (S.date || "");
      S.loading = false;
      syncControls();
      showStatus(`查询 ${date} 档位失败：${e.message}`, "err");
    }
  }

  function onSlotChange() {
    const value = $("slot-sel").value;
    if (!value) return;
    const date = $("date-sel").value || (S.report ? S.report.asof_date : "");
    const [hhmm, session] = value.split("_");
    if (!date || !hhmm || !session) return;
    loadSlot(date, hhmm, session, { force: true });
  }

  function autoTick() {
    if (!S.auto || S.mode !== "latest" || S.loading || document.hidden) return;
    loadLatest({ silent: true });
  }

  /* ---------- 飞书卡片深链：/survivor?date=..&hhmm=..&session=.. ----------
     卡片按钮要精确回到生成时的那一档，而不是最新档；参数严格校验，
     非法或档位不存在时明确提示并回退最新报告。 */

  const DEEP_LINK_RE = {
    date: /^\d{4}-\d{2}-\d{2}$/,
    hhmm: /^\d{4}$/,
    session: /^[A-Z]+$/,
  };

  function parseDeepLink() {
    const q = new URLSearchParams(location.search);
    const date = q.get("date") || "";
    const hhmm = q.get("hhmm") || "";
    const session = (q.get("session") || "").toUpperCase();
    if (!DEEP_LINK_RE.date.test(date) || !DEEP_LINK_RE.hhmm.test(hhmm)
        || !DEEP_LINK_RE.session.test(session)) return null;
    return { date, hhmm, session };
  }

  async function openDeepLink(deep) {
    const readable = `${deep.date} ${SESSION_LABEL[deep.session] || deep.session} `
      + `${deep.hhmm.slice(0, 2)}:${deep.hhmm.slice(2)}`;
    try {
      const out = await fetchJson(`/api/survivor/slots?date=${encodeURIComponent(deep.date)}`);
      const slots = out.slots || [];
      const hit = slots.find((s) => s.hhmm === deep.hhmm && s.session === deep.session);
      if (!hit) {
        await loadLatest({ silent: true });
        showStatus(`链接指定的档位（${readable}）不存在，已回退到最新报告。`, "err");
        return;
      }
      S.slots = slots;
      await loadSlot(deep.date, deep.hhmm, deep.session, { force: true });
    } catch (e) {
      await loadLatest({ silent: true });
      showStatus(`指定档位打开失败：${e && e.message ? e.message : e}；回退到最新报告。`, "err");
    }
  }

  /* ---------- 渲染 ---------- */

  function render(rep) {
    renderHeader(rep);
    renderDecision(rep);
    renderIndex(rep);
    renderAlert(rep);
    renderHoldings(rep);
    renderB(rep);
    renderDReversal(rep);
    renderDailySummary(rep);
    renderDiff(rep);
    renderList(rep);
    renderSchedule(rep);
    renderResearch(rep);
    syncControls();
  }

  function renderHeader(rep) {
    const c = rep.counts || {};
    const sess = SESSION_LABEL[rep.session] || rep.session || "—";
    const kind = SESSION_KIND[rep.session] || "报告";
    const hhmm = effectiveHhmm(rep);
    $("report-subtitle").textContent =
      `${rep.title || "生存者报告"} · ${rep.pillar || "—"}（${rep.month_id || "—"}） · ${kind}`;
    $("fact-asof").textContent = `${rep.asof_date || "—"} ${sess}${hhmm ? " " + hhmm : ""}`;
    $("fact-generated").textContent = rep.generated_at_bjt || "—";
    $("fact-segment").textContent = rep.current_segment
      ? `${rep.current_segment.id}（${rep.current_segment.start} ~ ${rep.current_segment.end}）`
      : "—";
    $("fact-pool").textContent = `${c.pool ?? "—"} 只`;

    const rows = rep.rows || [];
    $("head-meta").textContent =
      `${rep.asof_date || ""}${hhmm ? " " + hhmm : ""} · B8 ${c.B ?? 0}` +
      ` · B观察带 ${c.B_watch ?? 0} · 观察 ${(c.C ?? 0) + (c["A-other"] ?? 0)} · 全池 ${rows.length}`;
  }

  function renderDecision(rep) {
    const seg = rep.current_segment || {};
    const c = rep.counts || {};
    const rows = rep.rows || [];
    const holdings = rep.s1_holdings || [];
    const triggered = holdings.filter((h) => h.stop_triggered);

    $("seg-name").textContent = seg.id ? `当前段 ${seg.id}` : "当前段 —";
    $("seg-window").textContent = seg.start ? `${seg.start} ~ ${seg.end}` : "—";

    const cond = $("seg-condition");
    if (seg.conditions_ok === true) setChip(cond, "段条件成立", "ok");
    else if (seg.conditions_ok === false) setChip(cond, "段条件未成立", "warn");
    else setChip(cond, "条件状态未标注", "");

    const parts = String(seg.action || "—").split(/[；;]/).map((s) => s.trim()).filter(Boolean);
    $("seg-action").innerHTML = (parts.length ? parts : ["—"])
      .map((p) => `<li>${esc(p)}</li>`).join("");

    $("m-b").innerHTML = `${c.B ?? 0}<small>只</small>`;
    $("m-watch").innerHTML = `${(c.C ?? 0) + (c["A-other"] ?? 0)}<small>只</small>`;
    $("m-stop").innerHTML = `${triggered.length}<small>只</small>`;
    $("m-stop-card").classList.toggle("risk", triggered.length > 0);
    $("m-stop-detail").textContent = holdings.length
      ? `共 ${holdings.length} 只持仓，${triggered.length} 只触发止损`
      : "当前无 S1 持仓";
    $("m-pool").innerHTML = `${rows.length}<small>只</small>`;
    $("m-pool-detail").textContent = c.pool == null || Number(c.pool) === rows.length
      ? "100% 节气月样本池"
      : `配置池 ${c.pool} 只，成功计算 ${rows.length} 只`;
  }

  function renderIndex(rep) {
    const idx = rep.index_filter || {};
    const text = idx.text || "—";
    const regime = idx.regime || "—";
    const label = regime === "Bear" ? "弱势（Bear）" : (regime === "Bull" ? "强势（Bull）" : regime);
    setChip($("index-chip"), label, regime === "Bear" ? "warn" : (regime === "Bull" ? "ok" : ""));
    $("index-stale").classList.toggle("hidden", !/未更新|滞后/.test(text));
    $("index-text").textContent = text;
  }

  function missingText(m) {
    if (m === null || m === undefined) return "";
    if (typeof m === "string") return m;
    return [m.code, m.name, m.reason].filter(Boolean).join(" ");
  }

  function renderAlert(rep) {
    const box = $("alert-box");
    const holdings = rep.s1_holdings || [];
    const triggered = holdings.filter((h) => h.stop_triggered);
    const missing = rep.missing || [];
    const lines = [];

    triggered.forEach((h) => {
      lines.push(`<div><b>${esc(h.code)} ${esc(h.name)}</b>：现价 ${num(h.close)}，${esc(h.stop_detail || "已触发止损")}；按规则退出，不补仓。</div>`);
    });
    if (missing.length) {
      const codes = missing.map((m) => esc(missingText(m))).join("、");
      lines.push(`<div><b>数据缺口 ${missing.length} 只</b>：${codes}</div>`);
    }

    if (!lines.length) {
      box.classList.add("hidden");
      return;
    }
    box.classList.remove("hidden");
    box.classList.toggle("risk", triggered.length > 0);
    $("alert-title").textContent = triggered.length ? "需要立即处理" : "数据质量提醒";
    $("alert-body").innerHTML = lines.join("");
  }

  function holdingRow(h) {
    const status = h.stop_triggered
      ? '<span class="sv-chip risk">已触发止损</span>'
      : '<span class="sv-chip ok">持有</span>';
    const processing = h.stop_triggered
      ? `<span class="sv-chip risk">按规则退出</span>` +
        `<span class="sv-mini">${esc(h.stop_detail || "不补仓")}</span>`
      : `<span>${esc(h.add_condition || "持有")}</span>`;
    return `<tr>${stockCell(h)}<td data-label="状态">${status}</td>` +
      `<td class="sv-num" data-label="入场"><span class="sv-strong">${num(h.entry_price)}</span>` +
      `<span class="sv-mini">${esc(h.entry_date || "—")}</span></td>` +
      `<td class="sv-num" data-label="现价 / 浮盈亏"><span class="sv-strong">${num(h.close)}</span>` +
      `<span class="sv-mini">${changeHtml(h.pnl_pct)}</span></td>` +
      `<td class="sv-num" data-label="止损-5%">${num(h.stop_loss)}</td>` +
      `<td class="sv-num" data-label="结构止损">${num(h.break_level)}</td>` +
      `<td data-label="处理">${processing}</td></tr>`;
  }

  function renderHoldings(rep) {
    const holdings = (rep.s1_holdings || []).slice()
      .sort((a, b) => Number(b.stop_triggered || 0) - Number(a.stop_triggered || 0));
    const table = $("holdings-table");
    $("holdings-body").innerHTML = holdings.map(holdingRow).join("");
    setEmpty(table, $("holdings-empty"), !holdings.length,
      S.report ? "当前没有 S1 持仓" : "报告暂不可用");

    const triggered = holdings.filter((h) => h.stop_triggered);
    setChip($("holding-chip"),
      holdings.length ? `${holdings.length} 只持仓${triggered.length ? ` · ${triggered.length} 触发` : ""}` : "无持仓",
      triggered.length ? "risk" : "");
    $("holdings-note").textContent = triggered.length
      ? triggered.map((h) => `已触发止损：${h.code}（现价 ${num(h.close)}，${h.stop_detail}）；按规则退出，不补仓。`).join(" ")
      : (holdings.length ? "当前没有触发止损的持仓。" : "");
  }

  function bRow(r) {
    return `<tr>${stockCell(r)}${priceChangeCell(r)}${momentumCell(r)}${crossCell(r)}` +
      `<td class="sv-num" data-label="买入参考">${num(r.buy_ref)}</td>` +
      `<td class="sv-num" data-label="止损-5%">${num(r.stop_loss)}</td>` +
      `<td class="sv-num" data-label="结构位（破月初价）">${num(r.break_level)}</td>` +
      `<td class="sv-num" data-label="目标价"><span class="sv-strong">${num(r.target_3)}</span>` +
      `<span class="sv-mini">+5% ${num(r.target_5)}</span></td>${noteCell(r.note)}</tr>`;
  }

  function renderB(rep) {
    const rows = (rep.rows || []).filter((r) => r.group === "B");
    $("b-body").innerHTML = rows.map(bRow).join("");
    setEmpty($("b-table"), $("b-empty"), !rows.length,
      S.report ? "当前没有可执行的 B8 股票" : "报告暂不可用");
    const nc = rows.filter((r) => r.no_chase).length;
    setChip($("b-chip"), nc ? `B8 ${rows.length} 只 · ${nc} 不追` : `B8 ${rows.length} 只`,
      rows.length ? "info" : "");
    $("modeb-note").textContent = nc
      ? `仅 B8 执行 · ${nc} 只 MTM>15% 不追`
      : "仅 B8 执行 · B7/B5/B3 只观察";
    $("b-rule").textContent = (rep.current_segment || {}).b_rule || "";
  }

  function dV1Row(r) {
    const bought = r.d_v1_state === "buy_today";
    const status = bought ? "今日开盘已触发" : "今日首次跌破";
    const buy = bought ? num(r.d_v1_buy_ref) : "明日开盘";
    const pnl = bought ? changeHtml(r.d_v1_pnl) : "—";
    const stop = r.d_v1_stop != null ? num(r.d_v1_stop) : "—";
    const target = r.d_v1_target != null ? num(r.d_v1_target) : "—";
    const action = bought ? "已买（半仓；ST 再半）" : "明日开盘买（半仓；ST 再半）";
    return `<tr>${stockCell(r)}<td>${esc(status)}</td>` +
      `<td>${esc(r.d_v1_trigger_date || "—")}</td>` +
      `<td class="sv-num">${buy}</td><td class="sv-num">${num(r.close)}</td>` +
      `<td class="sv-num">${pnl}</td><td class="sv-num">${stop}</td>` +
      `<td class="sv-num">${target}</td><td>${esc(action)}</td></tr>`;
  }

  function dV2Row(r) {
    return `<tr>${stockCell(r)}<td>${groupBadge(r.group)}</td>` +
      `<td class="sv-num">${changeHtml(r.mtd_pct)}</td>` +
      `<td class="sv-num"><span class="sv-strong">${changeHtml(r.mtm_pct, 1)}</span>` +
      `<span class="sv-mini">昨 ${changeHtml(r.mtm_prev, 1)}</span></td>` +
      `<td>MTM 上拐，次日观察/小仓</td></tr>`;
  }

  function renderDReversal(rep) {
    const card = $("d-reversal-card");
    const has = rep && (rep.d_v1 !== undefined || rep.d_v2 !== undefined);
    card.classList.toggle("hidden", !has);
    if (!has) return;
    const v1 = rep.d_v1 || [];
    const v2 = rep.d_v2 || [];
    setChip($("d-reversal-chip"), `V1 ${v1.length} · V2 ${v2.length}`,
      (v1.length || v2.length) ? "info" : "");
    const rc = rep.research_constants || {};
    $("d-reversal-note").textContent =
      [rc.d_v1_stats, rc.d_v2_stats].filter(Boolean).join(" ");
    $("d-v1-body").innerHTML = v1.map(dV1Row).join("");
    setEmpty($("d-v1-table"), $("d-v1-empty"), !v1.length, "今日无 V1 触发");
    $("d-v2-body").innerHTML = v2.map(dV2Row).join("");
    setEmpty($("d-v2-table"), $("d-v2-empty"), !v2.length, "今日无 V2 信号");
  }

  /* ---------- 日报全天汇总（仅 PM/EOD 且带 daily_summary；INTRADAY/AM 隐藏） ---------- */

  const COUNT_KEYS = ["A", "B", "C", "A-other", "D"];
  const DAILY_STATUS = {
    ok: { label: "覆盖完整", variant: "ok" },
    partial: { label: "部分汇总", variant: "warn" },
    snapshot_only: { label: "仅收盘快照", variant: "warn" },
  };

  function fmtDelta(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "";
    return n === 0 ? "±0" : (n > 0 ? "+" : "−") + Math.abs(n);
  }

  function dailyGroupLabel(g) {
    return g === "A-other" ? "A" : (g || "—");
  }

  function dailyReadHtml(read) {
    if (/失效|未守住/.test(read || ""))
      return `<span class="sv-chip risk">${esc(read)}</span>`;
    if (/确认$/.test(read || ""))
      return `<span class="sv-chip ok">${esc(read)}</span>`;
    return esc(read || "—");
  }

  function renderDailySummary(rep) {
    const card = $("daily-card");
    const diffSection = $("diff-section");
    const ds = (rep.session === "PM" || rep.session === "EOD")
      && rep.daily_summary
      && rep.daily_summary.schema === "survivor_daily_summary_v1"
      ? rep.daily_summary : null;
    if (!ds) {
      card.classList.add("hidden");
      diffSection.classList.remove("hidden");
      return;
    }
    card.classList.remove("hidden");
    /* 日报的「与上一份对比」已被全天时间线覆盖，隐藏易误导的快照对比卡 */
    diffSection.classList.add("hidden");

    const st = DAILY_STATUS[ds.status] || { label: ds.status || "—", variant: "" };
    setChip($("daily-chip"), st.label, st.variant);
    const cov = ds.coverage || {};
    const covParts = [];
    if (cov.intraday_count) covParts.push(`盘中/午盘 ${cov.intraday_count} 档`);
    if (cov.folded_same_time && cov.folded_same_time.length)
      covParts.push(`折叠同刻 ${cov.folded_same_time.length} 档`);
    covParts.push("收盘 1 档");
    $("daily-coverage-sub").textContent = `${ds.asof_date || "—"}：${covParts.join(" + ")}`;

    const cc = ds.close_counts || {};
    const prevDay = ds.previous_day || {};
    const pd = prevDay.count_delta;
    const prevLabel = prevDay.date ? `较 ${prevDay.date} 日终` : "无日环比";
    const metrics = [
      { k: "A 生存者", v: cc.A, d: pd ? `${prevLabel} ${fmtDelta(pd.A)}` : prevLabel },
      { k: "B 强确认", v: cc.B, d: pd ? `${prevLabel} ${fmtDelta(pd.B)}` : "" },
      { k: "C 早段弹簧", v: cc.C, d: pd ? `${prevLabel} ${fmtDelta(pd.C)}` : "" },
      { k: "A-其他", v: cc["A-other"], d: pd ? `${prevLabel} ${fmtDelta(pd["A-other"])}` : "" },
      { k: "D 非生存者", v: cc.D, d: pd ? `${prevLabel} ${fmtDelta(pd.D)}` : "" },
    ];
    $("daily-metrics").innerHTML = metrics.map((m) =>
      `<div class="sv-metric"><span class="k">${esc(m.k)}</span>` +
      `<span class="v">${isNil(m.v) ? "—" : m.v}<small>只</small></span>` +
      `<span class="d">${esc(m.d || "")}</span></div>`).join("");

    const tcs = prevDay.tier_changes || [];
    $("daily-tier").textContent = tcs.length
      ? "B 档位变化：" + tcs.map((c) =>
        `${c.name}${c.from || "—"}→${c.to || "—"}`).join("；")
      : "";
    $("daily-tier").classList.toggle("hidden", !tcs.length);

    const tl = ds.timeline || [];
    $("daily-timeline-body").innerHTML = tl.map((p, i) => {
      const c = p.counts || {};
      let chg = i === 0 ? "基准" : "";
      if (i > 0) {
        const prevC = tl[i - 1].counts || {};
        const parts = COUNT_KEYS
          .map((k) => ({ k, d: (Number(c[k] ?? 0) || 0) - (Number(prevC[k] ?? 0) || 0) }))
          .filter((x) => x.d)
          .map((x) => `${x.k} ${fmtDelta(x.d)}`);
        const n = p.change_count || 0;
        chg = parts.length ? parts.join("、") : "无变化";
        if (n) chg += `（${n} 处变动${parts.length ? "" : "，净零"}）`;
      }
      const kind = p.is_close ? "日终" : (p.session === "AM" ? "午盘" : "盘中");
      return `<tr${p.is_close ? ' class="sv-daily-close"' : ""}>` +
        `<td data-label="时点">${esc(p.time || "—")}</td>` +
        `<td data-label="档位">${esc(kind)}</td>` +
        COUNT_KEYS.map((k) =>
          `<td class="sv-num" data-label="${k}">${isNil(c[k]) ? "—" : c[k]}</td>`).join("") +
        `<td class="sv-daily-chg" data-label="较上档">${esc(chg)}</td></tr>`;
    }).join("");

    const paths = ds.stock_paths || [];
    $("daily-paths-body").innerHTML = paths.map((p) =>
      `<tr>${stockCell(p)}` +
      `<td data-label="昨日日终">${esc(dailyGroupLabel(p.prev_day_group))}</td>` +
      `<td class="sv-daily-path" data-label="日内轨迹">${esc(p.path || "—")}</td>` +
      `<td data-label="收盘">${groupBadge(p.close_group)}</td>` +
      `<td data-label="解读">${dailyReadHtml(p.read)}</td></tr>`).join("");
    setEmpty($("daily-paths-table"), $("daily-paths-empty"), !paths.length,
      ds.status === "snapshot_only" ? "仅收盘快照，无法还原全天过程" : "全天分组无变化");

    const br = ds.b_review || [];
    $("daily-b-body").innerHTML = br.map((b) =>
      `<tr>${stockCell(b)}` +
      `<td data-label="昨日日终">${esc(dailyGroupLabel(b.prev_day_group))}</td>` +
      `<td data-label="首次观测到 B">${esc(b.first_b_time || "—")}</td>` +
      `<td class="sv-num" data-label="在B档数">${b.b_slot_count ?? "—"}/${b.total_slots ?? "—"}</td>` +
      `<td data-label="状态">${esc(b.status || "—")}</td></tr>`).join("");
    $("daily-b-fold").classList.toggle("hidden", !br.length);

    const fails = [...(ds.b_failures || [])];
    (ds.survivor_failures || []).forEach((f) => {
      if (!fails.some((x) => x.code === f.code)) fails.push(f);
    });
    $("daily-failures").classList.toggle("hidden", !fails.length);
    $("daily-failures-body").innerHTML = fails.map((f) =>
      `<div><b>${esc(f.code)} ${esc(f.name)}</b>：${esc(f.path || "—")}，` +
      `收盘 ${esc(dailyGroupLabel(f.close_group))}（${esc(f.read)}）</div>`).join("");

    const ns = ds.next_stage || {};
    const nseg = ns.segment || {};
    const typeLabel = ns.type === "continue" ? "本阶段继续"
      : (ns.type === "advance" ? "进入下一阶段" : "段表外");
    setChip($("daily-next-chip"), typeLabel, ns.type === "advance" ? "info" : "");
    const nrange = nseg.start ? `（${nseg.start} ~ ${nseg.end}）` : "";
    $("daily-next-text").innerHTML =
      `<b>${esc(ns.title || "—")}${esc(nrange)}</b> — ${esc(ns.action || "—")}` +
      (ns.b_rule ? `<div class="sv-mini">${esc(ns.b_rule)}</div>` : "");

    const warns = ds.input_warnings || [];
    $("daily-warn-fold").classList.toggle("hidden", !warns.length);
    $("daily-warn-count").textContent = `${warns.length} 条`;
    $("daily-warnings").innerHTML = warns.map((w) => `<li>${esc(w)}</li>`).join("");
  }

  function clearDailySummary() {
    $("daily-card").classList.add("hidden");
    $("daily-chip").textContent = "—";
    $("daily-chip").className = "sv-chip";
    $("daily-coverage-sub").textContent = "—";
    $("daily-metrics").innerHTML = "";
    $("daily-tier").textContent = "";
    $("daily-tier").classList.add("hidden");
    $("daily-timeline-body").innerHTML = "";
    $("daily-paths-body").innerHTML = "";
    $("daily-paths-empty").classList.add("hidden");
    $("daily-b-body").innerHTML = "";
    $("daily-b-fold").classList.add("hidden");
    $("daily-failures").classList.add("hidden");
    $("daily-failures-body").innerHTML = "";
    $("daily-warn-fold").classList.add("hidden");
    $("daily-warnings").innerHTML = "";
    setChip($("daily-next-chip"), "—", "");
    $("daily-next-text").textContent = "—";
    $("diff-section").classList.remove("hidden");
  }

  function renderDiff(rep) {
    const dp = rep.diff_prev || {};
    const lines = dp.lines || [];
    const noChange = lines.length === 0
      || (lines.length === 1 && /分组无变化/.test(lines[0]));
    $("diff-prev").textContent = `上一份：${dp.prev_asof || "—"}`;
    $("diff-list").innerHTML = noChange ? "" : lines.map((l) => `<li>${esc(l)}</li>`).join("");
    $("diff-empty").classList.toggle("hidden", !noChange);
    setChip($("diff-chip"), noChange ? "分组无变化" : (dp.summary || `${lines.length} 处变动`),
      noChange ? "" : "info");
  }

  function listRows(rep) {
    const rows = rep.rows || [];
    let out;
    if (S.list === "watch") out = rows.filter((r) => r.group === "C" || r.group === "A-other");
    else if (S.list === "nearD") out = rows.filter((r) => r.group === "D").slice(0, 8);
    else out = rows;
    const q = S.query.trim().toLowerCase();
    if (!q) return out;
    return out.filter((r) =>
      String(r.code || "").toLowerCase().includes(q) || String(r.name || "").toLowerCase().includes(q));
  }

  function listRow(r) {
    let advice;
    if (r.b_tier) {
      advice = `<span class="sv-badge">${esc(r.b_tier)}</span> ` +
        `<span>${esc(r.b_tier_action || "观察")}</span>` +
        `<span class="sv-mini">只观察，不参与半仓</span>`;
    } else if (r.group === "A-other") {
      advice = `<span>${esc(r.advice || "观察")}</span><span class="sv-mini">MTM>8 且金叉→升 B8</span>`;
    } else {
      advice = `<span>${esc(r.advice || "—")}</span>`;
    }
    return `<tr>${stockCell(r)}<td data-label="组">${groupBadge(r.group)}</td>` +
      `${priceChangeCell(r)}${momentumCell(r)}${crossCell(r)}` +
      `<td data-label="建议 / 升级条件">${advice}</td>` +
      `<td class="sv-num" data-label="防守位"><span class="sv-strong">${num(r.break_level)}</span>` +
      `<span class="sv-mini">收盘跌破此位</span></td></tr>`;
  }

  function renderList(rep) {
    const rows = listRows(rep);
    $("list-body").innerHTML = rows.map(listRow).join("");
    setEmpty($("list-table"), $("list-empty"), !rows.length,
      S.query ? "没有匹配搜索的股票" : "当前分组没有股票");

    const allInGroup = S.list === "watch"
      ? (rep.rows || []).filter((r) => r.group === "C" || r.group === "A-other").length
      : (S.list === "nearD"
        ? Math.min(8, (rep.rows || []).filter((r) => r.group === "D").length)
        : (rep.rows || []).length);
    setChip($("list-count"), S.query ? `${rows.length}/${allInGroup} 只` : `${rows.length} 只`,
      rows.length ? "" : "");

    $("list-note").textContent = S.list === "watch"
      ? "观察 = C 组 + A-其他（含 B7/B5/B3 观察带）；B7/B5/B3 只观察，MTM>8 且金叉才升入可执行 B8。"
      : (S.list === "nearD"
        ? (rep.d_note || "非生存者近端：不买，只跟踪最接近起点的股票。")
        : "完整 100% 池按报告原顺序展示；可用搜索快速定位代码或名称。");

    const active = $(`tab-${S.list}`);
    $("list-panel").setAttribute("aria-labelledby", active ? active.id : "tab-watch");
  }

  function renderSchedule(rep) {
    const sell = rep.sell_schedule || [];
    $("schedule-list").innerHTML = sell.map((s) =>
      `<li><span class="when">${esc(s.when)}</span><span class="what">${esc(s.action)}</span></li>`).join("");
  }

  function renderResearch(rep) {
    const rc = rep.research_constants || {};
    const labels = {
      s2_stats: "S2 统计", s3_stats: "S3 统计",
      b8_stats: "B8 统计", t3_hold: "T3 持有",
      mtm_extreme: "MTM 极值", tier_note: "B 分层",
      d_v1_stats: "D 反转 V1", d_v2_stats: "D 反转 V2",
      d_s3_rebound: "S3 反抽", feishu_brief: "飞书口径",
      mode_b: "B 模式回测", s6_high: "S6 高点统计",
    };
    $("research-notes").innerHTML = Object.entries(rc).map(([k, v]) =>
      `<p><b>${esc(labels[k] || k)}：</b>${esc(v)}</p>`).join("");
    $("risk-note").innerHTML = `<b>风险约束：</b>${esc(rep.risk_note || "—")}`;
    $("data-note").innerHTML = `<b>数据来源：</b>${esc(rep.data_source_note || "—")}`;
    $("footer-note").innerHTML = `<b>口径说明：</b>${esc(rep.footer || "—")}`;
    $("staging-note").textContent = rep.staging_note || "只读分析页面；不修改主库。";

    const warns = rep.bridge_warnings || [];
    $("bridge-count").textContent = `${warns.length} 条`;
    $("bridge-list").innerHTML = warns.map((w) => `<li>${esc(w)}</li>`).join("");
    $("bridge-fold").classList.toggle("hidden", !warns.length);
  }

  function clearRender() {
    clearDailySummary();
    $("report-subtitle").textContent = "暂无可用报告";
    ["fact-asof", "fact-generated", "fact-segment", "fact-pool"].forEach((id) => { $(id).textContent = "—"; });
    $("head-meta").textContent = "报告暂不可用";
    setChip($("seg-condition"), "—", "");
    $("seg-name").textContent = "当前段 —";
    $("seg-window").textContent = "—";
    $("seg-action").innerHTML = "<li>—</li>";
    ["m-b", "m-watch", "m-stop", "m-pool"].forEach((id) => { $(id).innerHTML = "—<small>只</small>"; });
    $("m-stop-card").classList.remove("risk");
    $("m-stop-detail").textContent = "—";
    $("m-pool-detail").textContent = "—";
    setChip($("index-chip"), "—", "");
    $("index-stale").classList.add("hidden");
    $("index-text").textContent = "—";
    $("alert-box").classList.add("hidden");
    $("holdings-body").innerHTML = "";
    setEmpty($("holdings-table"), $("holdings-empty"), true, "报告暂不可用");
    setChip($("holding-chip"), "—", "");
    $("holdings-note").textContent = "";
    $("b-body").innerHTML = "";
    setEmpty($("b-table"), $("b-empty"), true, "报告暂不可用");
    setChip($("b-chip"), "—", "");
    $("modeb-note").textContent = "可操作，含买卖参考";
    $("b-rule").textContent = "";
    $("d-reversal-card").classList.add("hidden");
    $("d-v1-body").innerHTML = "";
    $("d-v2-body").innerHTML = "";
    setChip($("d-reversal-chip"), "—", "");
    $("d-reversal-note").textContent = "";
    $("diff-prev").textContent = "上一份：—";
    $("diff-list").innerHTML = "";
    $("diff-empty").classList.remove("hidden");
    setChip($("diff-chip"), "—", "");
    $("list-body").innerHTML = "";
    setEmpty($("list-table"), $("list-empty"), true, "报告暂不可用");
    setChip($("list-count"), "—", "");
    $("list-note").textContent = "—";
    $("schedule-list").innerHTML = "";
    $("research-notes").innerHTML = "";
    $("risk-note").textContent = "—";
    $("data-note").textContent = "—";
    $("footer-note").textContent = "—";
    $("staging-note").textContent = "—";
    $("bridge-count").textContent = "—";
    $("bridge-list").innerHTML = "";
    $("bridge-fold").classList.add("hidden");
  }

  /* ---------- 主题与事件 ---------- */

  function applyTheme(light) {
    document.documentElement.classList.toggle("light", light);
    document.documentElement.classList.toggle("dark", !light);
    document.body.classList.toggle("theme-light", light);
    const btn = $("theme-toggle");
    if (btn) btn.textContent = light ? "🌙" : "☀️";
  }

  function bind() {
    $("theme-toggle").addEventListener("click", () => {
      const light = !document.documentElement.classList.contains("light");
      localStorage.setItem("bazi_theme", light ? "light" : "dark");
      applyTheme(light);
    });

    $("latest-btn").addEventListener("click", () => loadLatest({ force: true }));
    $("auto-btn").addEventListener("click", () => {
      S.auto = !S.auto;
      localStorage.setItem("bazi_survivor_auto", S.auto ? "1" : "0");
      syncControls();
    });
    $("date-sel").addEventListener("change", onDateChange);
    $("slot-sel").addEventListener("change", onSlotChange);

    document.querySelectorAll(".sv-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        S.list = btn.dataset.list;
        document.querySelectorAll(".sv-tab").forEach((b) =>
          b.setAttribute("aria-selected", b === btn ? "true" : "false"));
        if (S.report) renderList(S.report);
        else syncControls();
      });
    });

    $("stock-search").addEventListener("input", (ev) => {
      S.query = ev.target.value;
      if (S.report) renderList(S.report);
    });
  }

  async function init() {
    const storedTheme = localStorage.getItem("bazi_theme");
    applyTheme(storedTheme === "light");
    S.auto = localStorage.getItem("bazi_survivor_auto") !== "0";
    bind();
    const deep = parseDeepLink();
    if (deep) await openDeepLink(deep);
    else await loadLatest();
    window.setInterval(autoTick, AUTO_MS);
  }

  init().catch((e) => {
    showStatus(`页面初始化失败：${e && e.message ? e.message : e}`, "err");
  });
})();
