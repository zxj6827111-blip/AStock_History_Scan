/* 模拟盘页 v1：只读 /api/paper/*（脱敏快照），复用 survivor.js 的模式——
   单一状态、请求序号防竞态、刷新失败保留上次数据并标 stale、401 停止轮询。
   本页只有 GET，没有任何下单/重试入口；真实调仓只在发布端引擎里。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const num = (v, d = 2) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)))
    ? "—" : Number(v).toLocaleString("zh-CN", {minimumFractionDigits: d, maximumFractionDigits: d});
  const pct = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)))
    ? "—" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
  const dateParts = (v) => String(v || "").match(
    /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/);
  const shortDT = (v) => {
    const m = dateParts(v);
    return m ? `${m[2]}-${m[3]}${m[4] ? ` ${m[4]}:${m[5]}` : ""}` : String(v || "—");
  };
  const clockTime = (v) => {
    const m = dateParts(v);
    return m && m[4] ? `${m[4]}:${m[5]}:${m[6] || "00"}` : "";
  };
  const positionQuoteTime = (p, d) => {
    if (!p.price_date) return {text: "—", title: "未找到该持仓的行情日期"};
    const sameDay = p.price_date === d.market_date;
    if (sameDay && d.market_as_of) {
      return {text: clockTime(d.market_as_of).slice(0, 5) || shortDT(d.market_as_of),
              title: `该持仓现价使用的行情时间：${d.market_as_of}`};
    }
    return {text: shortDT(p.price_date),
            title: `该持仓行情日期为 ${p.price_date}，不是当前行情日，请留意数据滞后`};
  };

  const S = {
    auto: localStorage.getItem("bazi_paper_auto") !== "0",
    lastGood: null,      // 上次成功数据（刷新失败时保留展示并标 stale）
    stopped: false,      // 401 后停止轮询
    seq: 0,              // 请求序号：慢响应不覆盖新响应
    caps: null,          // 本机人工交易能力
    data: null,          // 本次渲染数据
    manualSide: "buy",
    manualOpen: false,
    manualPending: false,
    manualPreview: null,
    focusCode: "",
    manualPrefill: null, // { side, code }
    manualPrefillApplied: false,
    planCache: new Map(),
    expandedPlanDate: null,
  };

  function showStatus(text, isErr) {
    const el = $("pp-status");
    if (!text) { el.className = "pp-status"; el.textContent = ""; return; }
    el.className = "pp-status show" + (isErr ? " err" : "");
    el.textContent = text;
  }

  async function fetchJson(url) {
    const r = await fetch(url, {headers: {Accept: "application/json"}});
    if (r.status === 401) {
      const err = new Error("登录已失效，请重新登录");
      err.status = 401;
      throw err;
    }
    let body = null;
    try { body = await r.json(); } catch (_) { /* 非 JSON（如 503 文本）按状态处理 */ }
    if (!r.ok) {
      const err = new Error((body && (body.message || body.error)) || `HTTP ${r.status}`);
      err.status = r.status;
      err.code = body && body.error;
      throw err;
    }
    return body;
  }

  async function postJson(url, payload) {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "Accept": "application/json",
                "X-Paper-Request": "manual"},
      body: JSON.stringify(payload),
    });
    if (r.status === 401) {
      S.stopped = true; S.auto = false; updateAutoBtn();
      const err = new Error("登录已失效，请重新登录");
      err.status = 401; throw err;
    }
    let body = null;
    try { body = await r.json(); } catch (_) { /* handled below */ }
    if (!r.ok || (body && body.ok === false)) {
      const err = new Error((body && (body.message || body.reject_reason))
        || `HTTP ${r.status}`);
      err.status = r.status; err.code = body && body.error;
      err.body = body; throw err;
    }
    return body;
  }

  function survivorBadgeFor(code) {
    const st = window.BaziNavShell && window.BaziNavShell.getCrossStatus();
    const row = st && st.survivor && st.survivor.byCode ? st.survivor.byCode[code] : null;
    if (!row) return "报告↗";
    if (row.group === "B") return row.no_chase ? "B8不追↗" : "B8↗";
    if (row.b_tier) return `${row.b_tier}↗`;
    return `${row.group === "A-other" ? "A" : row.group}↗`;
  }

  function stockCodeLink(code, name) {
    if (!code) return "—";
    const c = String(code);
    const n = String(name || "");
    const sv = survivorBadgeFor(c);
    return `<a class="pp-code-link" data-pp-stock="${esc(c)}" data-pp-name="${esc(n)}" ` +
      `href="/?id=${encodeURIComponent(c)}&tab=overview&from=paper" title="进入个股档案（保留返回模拟盘入口）">${esc(c)}</a>` +
      `<a class="pp-sv-link" href="/survivor?code=${encodeURIComponent(c)}" title="在生存者报告中定位 ${esc(c)}">${esc(sv)}</a>`;
  }

  function chip(el, text, cls) {
    el.textContent = text;
    el.className = "pp-chip" + (cls ? " " + cls : "");
  }

  function renderHeader(d) {
    const s = d.summary || {};
    const mode = s.mode || "—";
    const isLocal = s.execution_backend === "local";
    const backend = isLocal ? "本地模拟" : "雪球模拟";
    const modeText = mode === "live" ? `${backend} · 自动撮合`
      : mode === "dry_run" ? `${backend} · 演练（未下单）` : "模拟盘未启用";
    chip($("pp-mode"), modeText,
      mode === "dry_run" || isLocal ? "info" : mode === "live" ? "live" : "");
    const marketText = d.market_as_of ? `行情截至 ${shortDT(d.market_as_of)}`
      : d.market_date ? `行情日期 ${shortDT(d.market_date)}（档位未记）` : "行情时间 —";
    chip($("pp-market"), marketText, d.market_as_of ? "info" : "warn");
    $("pp-market").title = `行情截止：${d.market_as_of || "未记录具体档位"}\n`
      + `最近重估：${d.synced_at || "—"}\n快照生成：${d.generated_at_bjt || "—"}`;
    const ok = !!d.sync_ok && !d.stale;
    chip($("pp-sync"),
      ok ? "快照正常" : (d.sync_ok ? "数据可能过期" : "行情快照缺失"),
      ok ? "ok" : "warn");
    $("pp-sync").title = `最近重估：${d.synced_at || "—"}\n快照生成：${d.generated_at_bjt || "—"}`;
    const base = "按真实行情切片驱动的前向模拟：盘后信号定稿 → 次交易日开盘成交 → "
      + "每个 30 分钟档按最新价重估盈亏。非实盘，不构成投资建议。";
    $("pp-head-note").textContent = d.ledger_scope === "client"
      ? base + " 当前为本机独立账本：人工单只影响本机，自动计划从下一条 PM 报告开始；发布端快照仅作参考。"
      : base + " 当前为发布端账本。";
    const latestPlan = (d.plans_recent || [])[0];
    const svBtn = $("pp-goto-survivor");
    if (svBtn) {
      svBtn.textContent = latestPlan && latestPlan.segment_id
        ? `← 对照生存者报告（${latestPlan.segment_id} · ${latestPlan.regime || "—"}）`
        : "← 对照当期生存者报告";
      if (latestPlan && latestPlan.signal_date) {
        svBtn.title = `信号日 ${latestPlan.signal_date} · 点击前往生存者报告核查`;
      }
    }
    const hm = $("head-meta");
    if (hm) {
      const navTxt = s.nav != null ? `净值 ${num(s.nav, 4)}` : "—";
      hm.textContent = `${navTxt} · 持仓 ${(d.positions || []).length} 只 · 仓位 ${s.total_weight != null ? num(s.total_weight, 1) + "%" : "—"}`;
    }
  }

  function pnlClass(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
    return Number(v) > 0 ? "pp-up" : (Number(v) < 0 ? "pp-down" : "");
  }

  function computeMaxDrawdown(equityRows) {
    const rows = (equityRows || []).filter((x) => x.nav !== null && x.nav !== undefined && Number.isFinite(Number(x.nav)));
    if (rows.length < 2) return null;
    let peak = Number(rows[0].nav);
    let maxDd = 0;
    for (const r of rows) {
      const v = Number(r.nav);
      if (v > peak) peak = v;
      if (peak > 0) {
        const dd = (v / peak - 1.0) * 100.0;
        if (dd < maxDd) maxDd = dd;
      }
    }
    return maxDd;
  }

  function renderMetrics(d) {
    const s = d.summary || {};
    const money = (v) => (v === null || v === undefined) ? "—"
      : `${v > 0 ? "+" : ""}${num(v, 0)}`;
    const maxDd = computeMaxDrawdown(d.equity);
    const items = [
      ["总资产", s.total === null || s.total === undefined ? "—" : num(s.total, 0), ""],
      ["现金", s.cash === null || s.cash === undefined ? "—" : num(s.cash, 0), ""],
      ["总仓位", s.total_weight === null || s.total_weight === undefined ? "—" : `${num(s.total_weight, 1)}%`, ""],
      ["净值", s.nav === null || s.nav === undefined ? "—" : num(s.nav, 4), ""],
      ["最大回撤", maxDd === null ? "—" : `${maxDd.toFixed(2)}%`, maxDd !== null && maxDd < 0 ? "pp-down" : ""],
      ["今日盈亏", money(s.day_pnl), pnlClass(s.day_pnl)],
      ["浮动盈亏（毛）", money(s.unrealized_pnl), pnlClass(s.unrealized_pnl)],
      ["已实现盈亏（毛）", money(s.realized_pnl), pnlClass(s.realized_pnl)],
      ["累计净盈亏", money(s.cum_pnl), pnlClass(s.cum_pnl)],
      ["累计费用", s.total_fees === null || s.total_fees === undefined ? "—" : num(s.total_fees), ""],
    ];
    $("pp-metrics").innerHTML = items.map(([k, v, cls]) =>
      `<div class="pp-metric"><dt>${esc(k)}</dt><dd class="${cls}">${esc(v)}</dd></div>`).join("");
    const f = s.fee_breakdown || {};
    $("pp-metrics").title = `费用拆分：佣金 ${num(f.commission)} / 印花税 ${num(f.tax)} / 过户费 ${num(f.transfer_fee)}`
      + "；累计净盈亏 = 已实现毛盈亏 + 浮动毛盈亏 − 累计费用";
  }

  function renderEquity(d) {
    const rows = (d.equity || []).filter((x) => x.nav !== null && x.nav !== undefined);
    const box = $("pp-equity-box");
    if (rows.length < 1) {
      box.innerHTML = '<p class="pp-note">尚无净值快照（上线首个交易日收盘后生成）。</p>';
      $("pp-equity-note").textContent = "按每日收盘回读快照（净值 = 总资产 / 初始 100 万）";
      return;
    }
    const W = 720, H = 200, PAD = 42;
    const vals = rows.map((r) => Number(r.nav));
    let lo = Math.min(...vals, 1.0), hi = Math.max(...vals, 1.0);
    if (hi - lo < 1e-9) { hi += 0.01; lo -= 0.01; }
    const x = (i) => PAD + (i * (W - 2 * PAD)) / Math.max(1, rows.length - 1);
    const y = (v) => PAD + (H - 2 * PAD) * (1 - (v - lo) / (hi - lo));
    const pts = rows.map((r, i) => `${x(i).toFixed(1)},${y(Number(r.nav)).toFixed(1)}`);
    const first = vals[0];
    const last = vals[vals.length - 1];
    const up = last >= first;
    const stroke = up ? "var(--stock-up)" : "var(--stock-down)";
    const base1Y = y(1.0);
    const gridLines = [0, 0.25, 0.5, 0.75, 1].map((t) => {
      const gy = PAD + (H - 2 * PAD) * t;
      const gv = hi - (hi - lo) * t;
      return `<line x1="${PAD}" y1="${gy}" x2="${W - PAD}" y2="${gy}" stroke="var(--border-main)" stroke-width="1"/>`
        + `<text x="${PAD - 6}" y="${gy + 4}" text-anchor="end" font-size="10" fill="var(--text-sub)">${gv.toFixed(3)}</text>`;
    }).join("");
    const areaPts = `${x(0).toFixed(1)},${(H - PAD).toFixed(1)} ${pts.join(" ")} ${x(rows.length - 1).toFixed(1)},${(H - PAD).toFixed(1)}`;
    box.innerHTML =
      `<div class="pp-equity-wrap">`
      + `<svg class="pp-equity-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="净值曲线">`
      + `<defs><linearGradient id="pp-eq-grad" x1="0" y1="0" x2="0" y2="1">`
      + `<stop offset="0%" stop-color="${stroke}" stop-opacity="0.18"/><stop offset="100%" stop-color="${stroke}" stop-opacity="0"/></linearGradient></defs>`
      + gridLines
      + `<line x1="${PAD}" y1="${base1Y.toFixed(1)}" x2="${W - PAD}" y2="${base1Y.toFixed(1)}" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4,3"/>`
      + `<polygon points="${areaPts}" fill="url(#pp-eq-grad)"/>`
      + `<polyline points="${pts.join(" ")}" fill="none" stroke="${stroke}" stroke-width="2.2" stroke-linejoin="round"/>`
      + `<line id="pp-eq-hover-line" x1="0" y1="${PAD}" x2="0" y2="${H - PAD}" stroke="var(--accent-indigo)" stroke-width="1" stroke-dasharray="3,2" style="display:none"/>`
      + `<circle id="pp-eq-hover-dot" cx="0" cy="0" r="4" fill="var(--accent-indigo)" style="display:none"/>`
      + `<circle cx="${x(rows.length - 1)}" cy="${y(last)}" r="3.5" fill="${stroke}"/>`
      + `<text x="${W - PAD}" y="${PAD - 12}" text-anchor="end" font-size="11" fill="var(--text-sub)">${rows[0].date || rows[0].trade_date || ""} → ${rows[rows.length - 1].date || rows[rows.length - 1].trade_date || ""}</text>`
      + `<text x="${W - PAD}" y="${H - 8}" text-anchor="end" font-size="11" fill="${stroke}">最新净值 ${last.toFixed(4)}</text>`
      + `<rect id="pp-eq-hit" x="${PAD}" y="${PAD}" width="${W - 2 * PAD}" height="${H - 2 * PAD}" fill="transparent" style="cursor:crosshair"/>`
      + `</svg>`
      + `<div id="pp-eq-tip" class="pp-equity-tip hidden"></div>`
      + `</div>`;

    const maxDd = computeMaxDrawdown(rows);
    const upDays = rows.filter((r) => Number(r.day_pnl || 0) > 0).length;
    $("pp-equity-note").textContent =
      `共 ${rows.length} 个交易日 · 收红 ${upDays} 天`
      + ` · 首日 ${first.toFixed(4)} → 最新 ${last.toFixed(4)}（${pct((last / first - 1) * 100)}）`
      + (maxDd !== null ? ` · 区间最大回撤 ${maxDd.toFixed(2)}%` : "");

    const hit = document.getElementById("pp-eq-hit");
    const hLine = document.getElementById("pp-eq-hover-line");
    const hDot = document.getElementById("pp-eq-hover-dot");
    const tip = document.getElementById("pp-eq-tip");
    if (hit && tip) {
      const svg = hit.ownerSVGElement;
      const move = (ev) => {
        const rect = svg.getBoundingClientRect();
        if (!rect.width) return;
        const svgX = ((ev.clientX - rect.left) / rect.width) * W;
        const ratio = Math.max(0, Math.min(1, (svgX - PAD) / Math.max(1, W - 2 * PAD)));
        const idx = rows.length === 1 ? 0 : Math.round(ratio * (rows.length - 1));
        const r = rows[idx];
        const cx = x(idx), cy = y(Number(r.nav));
        hLine.setAttribute("x1", cx.toFixed(1));
        hLine.setAttribute("x2", cx.toFixed(1));
        hLine.style.display = "";
        hDot.setAttribute("cx", cx.toFixed(1));
        hDot.setAttribute("cy", cy.toFixed(1));
        hDot.style.display = "";
        const dt = r.date || r.trade_date || "—";
        const dp = r.day_pnl != null ? `${Number(r.day_pnl) > 0 ? "+" : ""}${num(r.day_pnl, 0)}` : "—";
        const cp = r.cum_pnl != null ? `${Number(r.cum_pnl) > 0 ? "+" : ""}${num(r.cum_pnl, 0)}` : "—";
        tip.innerHTML = `<b>${esc(dt)}</b> · 净值 <b>${Number(r.nav).toFixed(4)}</b><br>` +
          `总资产 ${num(r.total, 0)} · 当日 ${esc(dp)} · 累计 ${esc(cp)}`;
        tip.classList.remove("hidden");
        const px = ((cx / W) * rect.width);
        const py = ((cy / H) * rect.height);
        tip.style.left = `${Math.min(Math.max(8, px + 10), Math.max(8, rect.width - 210))}px`;
        tip.style.top = `${Math.max(6, py - 44)}px`;
      };
      hit.addEventListener("mousemove", move);
      hit.addEventListener("mouseleave", () => {
        hLine.style.display = "none";
        hDot.style.display = "none";
        tip.classList.add("hidden");
      });
    }
  }

  const STRATEGY_LABEL = {b8: "B8", s1c: "S1/C", d_v1: "D_V1", d_v2: "D_V2", adopted: "基线"};
  const PLAN_STATUS_LABEL = {planned: "待执行", dry_run: "演练（未下单）", confirmed: "已执行",
    missed: "已错过（不追单）", superseded: "已被新计划替换", rejected: "已拒绝",
    conflict: "计划冲突", needs_review: "待复核", unknown: "状态未知", submitting: "提交中"};

  function levelCell(price, level) {
    if (level === null || level === undefined) return "—";
    if (price === null || price === undefined || Number(price) <= 0) return num(level);
    const distance = (Number(level) / Number(price) - 1.0) * 100.0;
    return `${num(level)}<span class="pp-cell-sub">距 ${pct(distance)}</span>`;
  }

  function renderPositions(d) {
    const tbody = document.querySelector("#pp-positions tbody");
    const rows = d.positions || [];
    const latest = (d.plans_recent || [])[0] || {};
    const exitByCode = new Map((latest.exits || []).map((x) => [x.code, x]));
    const holdByCode = new Map((latest.holds || []).map((x) => [x.code, x]));
    const actionFor = (p) => {
      if (exitByCode.has(p.code)) return {text: "计划退出", title: exitByCode.get(p.code).reason || "已有持仓退出"};
      const h = holdByCode.get(p.code);
      if (h && h.weight_target !== null && h.weight_target !== undefined
          && Number(h.weight_target) > Number(h.weight || 0)) {
        return {text: "计划补仓", title: "仅允许段位内的已有仓补仓"};
      }
      if (h) return {text: "继续持有", title: "仅管理退出，不代表新增买入"};
      return {text: "—", title: "最新计划未指定动作"};
    };
    const marketText = d.market_as_of ? `行情截至 ${shortDT(d.market_as_of)}`
      : d.market_date ? `行情日期 ${shortDT(d.market_date)}（档位未记）` : "行情时间 —";
    $("pp-pos-note").textContent = rows.length
      ? `${marketText} · 共 ${rows.length} 只（30 分钟切片重估；可卖按 T+1；止损/目标盘后检查、次日执行）`
      : "当前无持仓（或引擎未启用）";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="pp-empty" colspan="17">—</td></tr>';
      return;
    }
    const pnlCell = (p) => p.pnl === null || p.pnl === undefined
      ? "—"
      : `${p.pnl > 0 ? "+" : ""}${num(p.pnl, 0)}（${pct(p.pnl_pct)}）`;
    tbody.innerHTML = rows.map((p) => `<tr data-code-row="${esc(p.code || "")}">
      <td data-label="代码">${stockCodeLink(p.code, p.name)}</td>
      <td data-label="名称">${esc(p.name || "—")}</td>
      <td data-label="策略">${esc(STRATEGY_LABEL[p.strategy] || p.strategy || "—")}</td>
      <td data-label="计划动作" title="${esc(actionFor(p).title)}">${esc(actionFor(p).text)}</td>
      <td data-label="持股">${num(p.qty, 0)}</td>
      <td data-label="可卖" title="今日买入 ${num(p.today_buy_qty, 0)} 股；当日买入不可卖">${num(p.sellable_qty, 0)}</td>
      <td data-label="权重">${num(p.weight, 1)}%</td>
      <td data-label="现价" title="行情日期 ${esc(p.price_date || d.market_date || "—")}">${p.price === null || p.price === undefined ? "—" : num(p.price)}</td>
      <td data-label="数据时间" title="${esc(positionQuoteTime(p, d).title)}">${esc(positionQuoteTime(p, d).text)}</td>
      <td data-label="今日" class="${pnlClass(p.day_pct)}">${p.day_pct === null || p.day_pct === undefined ? "—" : pct(p.day_pct)}</td>
      <td data-label="浮动盈亏" class="${pnlClass(p.pnl)}">${pnlCell(p)}</td>
      <td data-label="市值">${num(p.market_value, 0)}</td>
      <td data-label="成本（未含费）" title="成交均价，不含佣金、印花税和过户费">${p.avg_cost === null || p.avg_cost === undefined ? "—" : num(p.avg_cost)}</td>
      <td data-label="入场日">${esc(p.entry_date || "—")}</td>
      <td data-label="止损（盘后）" title="收盘检查；触发后生成下一交易日退出计划">${levelCell(p.price, p.stop)}</td>
      <td data-label="目标（盘后）" title="收盘检查；触发后生成下一交易日退出计划">${levelCell(p.price, p.target)}</td>
      <td data-label="操作">
        <button class="pp-btn pp-mini" type="button" data-manual-sell="partial" data-code="${esc(p.code)}" ${Number(p.sellable_qty || 0) <= 0 ? "disabled" : ""}>减仓</button>
        <button class="pp-btn pp-mini" type="button" data-manual-sell="all" data-code="${esc(p.code)}" ${Number(p.sellable_qty || 0) <= 0 ? "disabled" : ""}>清仓</button>
      </td>
    </tr>`).join("");
  }

  function renderPlanGroupsHtml(p, historical) {
    const noNew = ["S4", "S5", "S6"].includes(p.segment_id);
    const excluded = p.excluded || [];
    const observations = p.observations || excluded.filter((x) => x.signal_only);
    const blocked = p.blocked_candidates || excluded.filter((x) => x.blocked_reason && !x.signal_only);
    const itemKey = (x) => `${x.code || ""}|${x.strategy || ""}|${x.reason || ""}`;
    const obsKeys = new Set(observations.map(itemKey));
    const blockedKeys = new Set(blocked.map(itemKey));
    const regular = excluded.filter((x) => !obsKeys.has(itemKey(x)) && !blockedKeys.has(itemKey(x)));
    const groups = [[historical ? "当时买入明细（历史）" : "买入（真实订单）", p.buys || []],
      ["已有持仓退出（将卖出）", p.exits || []],
      [noNew ? "继续持有（不补仓）" : "持有 / 补仓", p.holds || []],
      ["D_V2观察（不下单）", observations],
      ["本段禁买 / 禁补仓", blocked], ["普通排除", regular]];
    const details = groups.filter(([, rows]) => rows.length).map(([label, rows]) => `
      <div class="pp-plan-group"><h3>${label}（${rows.length}）</h3>
        <div class="pp-table-wrap"><table class="pp-table"><thead><tr>
          <th>代码</th><th>名称</th><th>策略</th><th>目标仓位</th><th>原因</th>
        </tr></thead><tbody>${rows.map((r) => {
          const w = r.weight_target ?? r.weight;
          return `<tr data-code-row="${esc(r.code || "")}"><td data-label="代码">${stockCodeLink(r.code, r.name)}</td>
            <td data-label="名称">${esc(r.name || "—")}</td>
            <td data-label="策略">${esc(STRATEGY_LABEL[r.strategy] || r.strategy || "—")}</td>
            <td data-label="目标仓位">${w === null || w === undefined ? "—" : `${num(w, 2)}%`}</td>
            <td data-label="原因" class="pp-excl">${esc(r.reason || (r.weight_target !== null && r.weight_target !== undefined ? "收盘确认补仓" : "继续持有"))}</td></tr>`;
        }).join("")}</tbody></table></div></div>`).join("");
    return { details, observations, blocked, regular, noNew };
  }

  function renderPlan(d) {
    const box = $("pp-plan-body");
    const plans = d.plans_recent || [];
    if (!plans.length) {
      box.innerHTML = '<p class="pp-note">尚无计划（引擎未启用或报告未定稿）。</p>';
      $("pp-plan-note").textContent = "盘后定稿，次交易日执行";
      return;
    }
    const planned = plans.find((x) => x.status === "planned");
    const p = planned || plans[0];
    const historical = p.status !== "planned";
    const s = d.summary || {};
    const kindText = p.kind === "intraday_probe" ? "盘中试探" : "盘后计划";
    const statusText = PLAN_STATUS_LABEL[p.status] || p.status || "—";
    const { details, observations, blocked, regular, noNew } = renderPlanGroupsHtml(p, historical);
    const freshnessText = planned
      ? `当前待执行计划：${p.execute_trade_date || "—"}`
      : "当前无待执行计划；下一步自动买入/卖出为0。D_V2观察名单将在下一份PM计划生成后显示。";
    $("pp-plan-note").textContent =
      `${historical ? "历史计划" : "当前计划"} · ${kindText} · 执行日 ${p.execute_trade_date || "—"}（${p.segment_id || "—"}段 / ${p.regime || "—"}）`;
    const cap = p.total_weight_cap;
    const capText = noNew ? `${p.segment_id} 全策略禁止新开仓/加仓；已有持仓仅管理退出`
      : cap === null || cap === undefined ? "总仓上限：—"
      : `当前仓位 ${num(s.total_weight, 2)}% / 上限 ${num(cap, 2)}%（余量 ${num(Number(cap) - Number(s.total_weight || 0), 2)}%）`;
    const hasOrders = Number(p.n_buys || 0) + Number(p.n_exits || 0) > 0;
    const windowText = p.status === "planned" && hasOrders && s.execute_time
      ? ` · 预计执行窗口 ${s.execute_time}–${s.catchup_until || "—"}`
      : !hasOrders ? " · 无订单，仅记录" : "";
    const warnings = (p.warnings || []).length
      ? `<p class="pp-note" style="margin-top:8px"><b>计划提示：</b>${p.warnings.map(esc).join("；")}</p>` : "";
    const review = p.methodology_review;
    const reviewText = review ? (review.issue || review.reason || review.note || "已完成方法论复核") : "";
    const reviewHtml = reviewText
      ? `<div class="pp-alert"><b>方法论复核：</b>${esc(reviewText)}；现有持仓仅管理退出，不重写历史。</div>` : "";
    const policyHtml = p.policy_filter
      ? `<div class="pp-alert"><b>执行兜底：</b>已拦截旧计划中的非法买意，合法退出单独处理，计划进入待复核。</div>` : "";
    const freshnessHtml = `<div class="pp-alert"><b>${planned ? "执行状态" : "计划状态"}：</b>${esc(freshnessText)}</div>`;
    box.innerHTML = `
      ${reviewHtml}${policyHtml}${freshnessHtml}
      <div class="pp-kv">
        <div class="pp-plan-line">计划：<b>${esc(p.plan_id || "—")}</b></div>
        <div class="pp-plan-line">状态：<b>${esc(statusText)}</b>（信号日 ${esc(p.signal_date || "—")}）</div>
        <div class="pp-plan-line">买入 <b>${p.n_buys ?? 0}</b> / 卖出 <b>${p.n_exits ?? 0}</b> / 观察 <b>${p.n_observations ?? observations.length}</b> / 禁买 <b>${p.n_blocked ?? blocked.length}</b> / 其他排除 <b>${regular.length}</b></div>
        <div class="pp-plan-line">${esc(capText)}${esc(windowText)}</div>
      </div>
      ${warnings}
      <details class="pp-plan-details"><summary>展开买入 / 退出 / 持有 / 观察 / 禁买 / 排除明细</summary>
        ${details || '<p class="pp-note">当前快照没有计划明细。</p>'}
      </details>
      <p class="pp-note" style="margin-top:8px">成交与被拒结果见「调仓记录」；原始计划可走 <code>/api/paper/plans</code>。</p>`;
  }

  function renderTrades(d) {
    const tbody = document.querySelector("#pp-trades tbody");
    const rows = d.trades_recent || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="pp-empty" colspan="11">尚无调仓记录</td></tr>';
      return;
    }
    tbody.innerHTML = rows.slice(0, 40).map((t) => {
      const created = clockTime(t.created_at);
      const timeSub = [t.slot ? `档位 ${t.slot}` : "", created ? `入账 ${created}` : ""]
        .filter(Boolean).join(" · ");
      const feeCell = t.total_fee === null || t.total_fee === undefined ? "—"
        : `${num(t.total_fee)}<span class="pp-cell-sub">佣 ${num(t.fee)} · 印 ${num(t.tax)} · 过 ${num(t.transfer_fee)}</span>`;
      const refCell = t.ref_price === null || t.ref_price === undefined ? ""
        : `<span class="pp-cell-sub">参考 ${num(t.ref_price)} · 偏离 ${pct(t.ref_deviation_pct)}</span>`;
      const status = t.status === "filled" ? "已成交" : t.status === "rejected" ? "被拒" : (t.status || "—");
      const repair = t.is_repair
        ? `<span class="pp-cell-sub">补执行${t.repair_at ? ` · ${esc(shortDT(t.repair_at))}` : ""}</span>` : "";
      return `<tr data-code-row="${esc(t.code || "")}">
        <td data-label="执行日 / 入账">${esc(t.trade_date || "—")}${timeSub ? `<span class="pp-cell-sub">${esc(timeSub)}</span>` : ""}</td>
        <td data-label="方向">${t.kind === "buy" ? "买入" : t.kind === "sell" ? "卖出" : esc(t.kind || "—")}</td>
        <td data-label="来源">${t.source === "manual"
          ? `<span class="pp-order-manual">人工</span>${t.override_segment ? `<span class="pp-cell-sub">覆盖 ${esc(t.override_segment)}</span>` : ""}`
          : "自动计划"}</td>
        <td data-label="代码">${stockCodeLink(t.code, t.name)}</td>
        <td data-label="名称">${esc(t.name || "—")}</td>
        <td data-label="数量">${num(t.qty, 0)}</td>
        <td data-label="价格">${num(t.price)}</td>
        <td data-label="成交金额">${num(t.amount)}</td>
        <td data-label="费用明细" title="佣金 / 印花税 / 过户费">${feeCell}</td>
        <td data-label="原因 / 信号参考" class="pp-excl">${esc(t.reason || "—")}${refCell}</td>
        <td data-label="状态">${esc(status)}${repair}</td>
      </tr>`;
    }).join("");
  }

  function renderPlans(d) {
    const tbody = document.querySelector("#pp-plans tbody");
    const rows = d.plans_recent || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="pp-empty" colspan="8">—</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map((p, idx) => {
      const dt = p.execute_trade_date || "";
      const isOpen = S.expandedPlanDate && S.expandedPlanDate === dt;
      return `<tr class="pp-plan-row" data-plan-idx="${idx}" data-plan-date="${esc(dt)}" title="点击展开或收起该日计划明细">` +
        `<td data-label="执行日">${esc(dt || "—")}</td>` +
        `<td data-label="信号日">${esc(p.signal_date || "—")}</td>` +
        `<td data-label="状态">${esc(PLAN_STATUS_LABEL[p.status] || p.status || "—")}</td>` +
        `<td data-label="买入">${p.n_buys ?? 0}</td>` +
        `<td data-label="卖出">${p.n_exits ?? 0}</td>` +
        `<td data-label="观察">${p.n_observations ?? 0}</td>` +
        `<td data-label="排除">${p.n_excluded ?? 0}</td>` +
        `<td data-label="明细"><span class="pp-sv-link">${isOpen ? "收起 ▴" : "展开 ▾"}</span></td>` +
      `</tr>` +
      (isOpen ? `<tr class="pp-plan-expand" data-plan-detail-for="${esc(dt)}"><td colspan="8">读取明细中…</td></tr>` : "");
    }).join("");
    if (S.expandedPlanDate) {
      const hit = rows.find((x) => x.execute_trade_date === S.expandedPlanDate);
      if (hit) populatePlanDetailRow(hit);
    }
  }

  async function populatePlanDetailRow(summaryPlan) {
    const dt = summaryPlan.execute_trade_date || "";
    const cell = document.querySelector(`tr[data-plan-detail-for="${CSS.escape(dt)}"] td`);
    if (!cell) return;
    let detail = S.planCache.get(dt) || null;
    if (!detail && dt) {
      try {
        const out = await fetchJson(`/api/paper/plans?date=${encodeURIComponent(dt)}`);
        detail = out && out.detail ? out.detail : null;
        if (detail) S.planCache.set(dt, detail);
      } catch (_) {
        detail = null;
      }
    }
    const merged = Object.assign({}, summaryPlan, detail || {});
    const { details } = renderPlanGroupsHtml(merged, merged.status !== "planned");
    cell.innerHTML = `<div class="pp-note" style="margin-bottom:8px"><b>计划ID：</b>${esc(merged.plan_id || "—")} · ` +
      `<b>段位：</b>${esc(merged.segment_id || "—")} · <b>指数环境：</b>${esc(merged.regime || "—")}</div>` +
      (details || '<p class="pp-note">该日计划无明细行（或历史单日计划快照未归档）。</p>');
  }

  function manualSegment(d) {
    return (d.manual_summary && d.manual_summary.segment_id)
      || ((d.plans_recent || [])[0] || {}).segment_id || "";
  }

  function showManualError(text, fieldId) {
    const el = $("pp-manual-error");
    el.textContent = text || "";
    el.classList.toggle("pp-manual-hide", !text);
    if (text) {
      el.scrollIntoView({behavior: "smooth", block: "nearest"});
      if (fieldId && $(fieldId) && $(fieldId).focus) $(fieldId).focus();
    }
  }

  function localError(text, fieldId) {
    const err = new Error(text);
    err.field = fieldId;
    throw err;
  }

  function setManualTab(side) {
    S.manualSide = side;
    showManualError("");
    $("pp-form-buy").classList.toggle("pp-manual-hide", side !== "buy");
    $("pp-form-sell").classList.toggle("pp-manual-hide", side !== "sell");
    $("pp-manual-tab-buy").classList.toggle("primary", side === "buy");
    $("pp-manual-tab-sell").classList.toggle("primary", side === "sell");
  }

  function selectedPosition() {
    const code = $("pp-sell-code").value;
    return ((S.data && S.data.positions) || []).find((p) => p.code === code);
  }

  function updateSellHint() {
    const p = selectedPosition();
    if (!p) {
      $("pp-sell-hint").textContent = "请选择持仓";
      $("pp-sell-qty").value = "";
      return;
    }
    $("pp-sell-hint").textContent =
      `${p.name || p.code} · 持股 ${num(p.qty, 0)} · 可卖 ${num(p.sellable_qty, 0)}`
      + ` · 今日买入 ${num(p.today_buy_qty, 0)}（T+1） · 现价 ${num(p.price)}`;
    $("pp-sell-qty").max = String(Number(p.sellable_qty || 0));
  }

  function renderManual(d) {
    const card = $("pp-manual-card");
    const enabled = !!(S.caps && S.caps.enabled);
    card.classList.toggle("pp-manual-hide", !enabled);
    if (!enabled) return;
    $("pp-manual-mode").textContent = d.ledger_scope === "client" ? "本机人工单" : "人工单";
    if (S.manualOpen || S.manualPending) return;
    S.data = d;
    const seg = manualSegment(d);
    const quoteText = d.market_as_of ? `当前行情截至 ${shortDT(d.market_as_of)}。` : "";
    $("pp-manual-warning").textContent = (["S4", "S5", "S6"].includes(seg)
      ? `当前 ${seg}：自动策略禁止新开仓/加仓。人工建仓必须勾选覆盖并由你二次确认，记录为人工例外。`
      : "人工单按最新30分钟价立即成交；S4/S5/S6自动禁买不会放宽自动策略。")
      + ` ${quoteText}成交价留空时只接受当日30分钟行情；非交易时段可填写自定义价格（参考最近收盘价），入场日、成本与涨跌基准都按该收盘价。`;
    const overrideBox = $("pp-buy-override-box");
    overrideBox.classList.toggle("pp-manual-hide", !["S4", "S5", "S6"].includes(seg));
    const universe = d.manual_universe || [];
    const obs = ((d.plans_recent || [])[0] || {}).observations || [];
    const codeMap = new Map();
    for (const x of universe) codeMap.set(x.code, x);
    for (const x of obs) if (!codeMap.has(x.code)) codeMap.set(x.code, x);
    $("pp-manual-codes").innerHTML = [...codeMap.values()]
      .sort((a, b) => String(a.code).localeCompare(String(b.code)))
      .map((x) => `<option value="${esc(x.code)}">${esc(x.name || "")} ${esc(x.group || "")}</option>`)
      .join("");
    const positions = d.positions || [];
    const prev = $("pp-sell-code").value;
    $("pp-sell-code").innerHTML = positions.map((p) =>
      `<option value="${esc(p.code)}">${esc(p.code)} ${esc(p.name || "")} · 可卖 ${num(p.sellable_qty, 0)}</option>`).join("");
    if (prev && positions.some((p) => p.code === prev)) $("pp-sell-code").value = prev;
    updateSellHint();
    const orders = d.manual_orders_recent || [];
    $("pp-manual-orders").innerHTML = orders.length
      ? `<b>最近人工单：</b>` + orders.slice(0, 6).map((o) =>
        `<div>${esc(shortDT(o.created_at))} · ${o.side === "buy" ? "买入" : "卖出"} `
        + `${esc(o.code)} ${esc(o.name || "")} · ${o.status === "filled" ? `成交 ${num(o.fill_qty, 0)}股 @ ${num(o.price)}` : `被拒：${esc(o.reject_reason || "—")}`}`
        + `${o.price_source === "manual_price" ? " · 人工指定价" : ""}`
        + `${o.override ? " · 人工覆盖例外" : ""}</div>`).join("")
      : "最近没有人工订单。";
  }

  function syncBuyMode() {
    const qtyMode = $("pp-buy-mode").value === "qty";
    $("pp-buy-amount").closest(".pp-manual-field")
      .classList.toggle("pp-manual-hide", qtyMode);
    $("pp-buy-qty-box").classList.toggle("pp-manual-hide", !qtyMode);
  }

  function manualPayload() {
    const side = S.manualSide;
    const reasonEl = side === "buy" ? "pp-buy-reason" : "pp-sell-reason";
    const reason = $(reasonEl).value.trim();
    if (side === "buy") {
      const code = $("pp-buy-code").value.trim().toUpperCase();
      if (!code) localError("请先填写股票代码", "pp-buy-code");
      const mode = $("pp-buy-mode").value === "qty" ? "qty" : "amount";
      const stopText = $("pp-buy-stop").value.trim();
      const targetText = $("pp-buy-target").value.trim();
      const out = {side, code, reason,
                   stop_pct: stopText === "" ? null : Number(stopText),
                   target_pct: targetText === "" ? null : Number(targetText)};
      if (mode === "qty") {
        const qty = Number($("pp-buy-qty").value);
        if (!(qty > 0) || !Number.isInteger(qty)) {
          localError("请填写大于0的整数买入股数", "pp-buy-qty");
        }
        if (qty % 100 !== 0) localError("买入股数必须为100股整数倍", "pp-buy-qty");
        out.qty = qty;
      } else {
        const amount = Number($("pp-buy-amount").value);
        if (!(amount > 0)) localError("请填写大于0的买入金额", "pp-buy-amount");
        out.amount = amount;
      }
      const priceText = $("pp-buy-price").value.trim();
      if (priceText !== "") {
        const price = Number(priceText);
        if (!(price > 0)) localError("成交价必须大于0", "pp-buy-price");
        out.price = Math.round(price * 100) / 100;
      }
      if (!$("pp-buy-override-box").classList.contains("pp-manual-hide")) {
        if (!$("pp-buy-override").checked) {
          localError("当前段位禁止自动新买，请勾选“人工覆盖”", "pp-buy-override");
        }
        out.override = {ack: true, note: $("pp-buy-override-note").value.trim()};
      }
      return out;
    }
    const code = $("pp-sell-code").value;
    const qty = Number($("pp-sell-qty").value);
    if (!code) localError("请选择要减仓/清仓的持仓", "pp-sell-code");
    if (!(qty > 0)) localError("请填写大于0的卖出股数", "pp-sell-qty");
    return {side, code, qty, reason};
  }

  function renderPreviewDialog(out, payload) {
    S.manualPreview = {out, payload};
    const fees = out.fees || {};
    const rows = [
      ["方向", out.side === "buy" ? "人工建仓" : "人工减仓/清仓"],
      ["代码 / 名称", `${out.code} ${out.name || ""}`],
      ["计入日期", out.trade_date
        ? `${out.trade_date}${out.backdated ? "（按收盘价回填）" : ""}` : "—"],
      ["成交价 / 行情", `${num(out.price)} · ${out.price_label || ""} ${shortDT(out.price_time)}`],
      ["股数 / 金额", out.side === "buy"
        ? (out.amount_requested != null
          ? `按金额 ${num(out.amount_requested)} 元 → ${num(out.qty, 0)} 股 · 实际 ${num(out.amount)} 元`
          : `按股数 ${num(out.qty_requested, 0)} 股 · 实际 ${num(out.amount)} 元`)
        : `${num(out.qty, 0)} 股 · ${num(out.amount)} 元`],
      ["费用", `佣 ${num(fees.fee)} / 印 ${num(fees.tax)} / 过 ${num(fees.transfer_fee)}`],
      ["现金", `${num(out.cash_before)} → ${num(out.cash_after)}`],
      ["止损 / 目标", out.side === "buy"
        ? `${num(out.stop)} / ${num(out.target)}` : "沿用原持仓止损/目标"],
      ["策略口径", `${out.segment_id || "—"}段${out.override_required ? " · 人工覆盖自动禁买" : ""}`],
    ];
    $("pp-confirm-body").innerHTML = rows.map(([k, v]) =>
      `<div class="pp-dialog-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")
      + ((out.warnings || []).length
        ? `<div class="pp-note">${out.warnings.map(esc).join("；")}</div>` : "")
      + (out.override_required
        ? `<div class="pp-alert">本单覆盖 ${esc(out.segment_id)} 自动禁买，将记录为人工例外。</div>` : "");
    $("pp-confirm-error").textContent = "";
    $("pp-confirm-ack").checked = false;
    $("pp-confirm-submit").disabled = true;
    $("pp-confirm-submit").textContent = "确认成交";
    S.manualOpen = true;
    $("pp-manual-card").classList.remove("pp-manual-hide");
    $("pp-confirm-dialog").showModal();
  }

  async function previewManual() {
    if (S.manualPending) return;
    const button = $(S.manualSide === "buy" ? "pp-buy-preview" : "pp-sell-preview");
    try {
      showManualError("");
      const payload = manualPayload();
      S.manualPending = true;
      button.disabled = true;
      showStatus("正在生成人工订单预览…", false);
      const out = await postJson("/api/paper/manual/preview", payload);
      S.manualPending = false;
      button.disabled = false;
      showStatus("", false);
      showManualError("");
      renderPreviewDialog(out, payload);
    } catch (exc) {
      S.manualPending = false;
      button.disabled = false;
      showStatus(`预览失败：${exc.message}`, true);
      const body = exc.body || {};
      if (S.manualSide === "buy" && exc.code === "NO_CURRENT_QUOTE"
          && body.reference_close) {
        $("pp-buy-price").value = String(body.reference_close);
        showManualError(
          `无当日行情：已填入 ${body.reference_date || "最近交易日"} 收盘价 `
          + `${body.reference_close} 元，可直接再次预览（成交计入该交易日，成本按该收盘价）`,
          "pp-buy-price");
      } else {
        showManualError(`预览失败：${exc.message}`, exc.field);
      }
    }
  }

  async function confirmManual() {
    if (S.manualPending || !S.manualPreview) return;
    if (!$("pp-confirm-ack").checked) return;
    const {out, payload} = S.manualPreview;
    const btn = $("pp-confirm-submit");
    try {
      S.manualPending = true;
      btn.disabled = true;
      btn.textContent = "提交中…";
      const body = {...payload, preview_token: out.preview_token,
                    client_token: out.client_token, confirm: true};
      const res = await postJson("/api/paper/manual/order", body);
      S.manualPending = false;
      S.manualOpen = false;
      $("pp-confirm-dialog").close();
      showStatus(`人工单已成交：${res.order_id} · ${res.fill_qty || res.qty}股 @ ${res.price}`, false);
      S.manualPreview = null;
      await loadLatest(true);
    } catch (exc) {
      S.manualPending = false;
      btn.disabled = false;
      btn.textContent = "确认成交";
      $("pp-confirm-error").textContent = exc.message;
      if (exc.code === "PREVIEW_STALE") {
        S.manualPreview = null;
        $("pp-confirm-submit").disabled = true;
      }
    }
  }

  function fillSellFromPosition(code, mode) {
    setManualTab("sell");
    $("pp-sell-code").value = code;
    updateSellHint();
    const p = selectedPosition();
    const sellable = Number((p && p.sellable_qty) || 0);
    if (sellable > 0) {
      const qty = mode === "all" ? sellable
        : Math.max(100, Math.floor((sellable * 0.5) / 100) * 100);
      $("pp-sell-qty").value = String(Math.min(qty, sellable));
    }
    $("pp-manual-card").scrollIntoView({behavior: "smooth", block: "start"});
  }

  function applySellFraction(frac) {
    const p = selectedPosition();
    const sellable = Number((p && p.sellable_qty) || 0);
    if (sellable <= 0) return;
    const qty = Math.max(100, Math.floor((sellable * frac) / 100) * 100);
    $("pp-sell-qty").value = String(Math.min(qty, sellable));
  }

  function bindManualEvents() {
    $("pp-form-buy").addEventListener("submit", (e) => e.preventDefault());
    $("pp-form-sell").addEventListener("submit", (e) => e.preventDefault());
    $("pp-manual-tab-buy").addEventListener("click", () => setManualTab("buy"));
    $("pp-manual-tab-sell").addEventListener("click", () => setManualTab("sell"));
    $("pp-sell-code").addEventListener("change", updateSellHint);
    $("pp-buy-mode").addEventListener("change", syncBuyMode);
    $("pp-buy-preview").addEventListener("click", previewManual);
    $("pp-sell-preview").addEventListener("click", previewManual);
    document.querySelectorAll("[data-sell-frac]").forEach((b) =>
      b.addEventListener("click", () => applySellFraction(Number(b.dataset.sellFrac))));
    document.querySelectorAll("[data-sell-all]").forEach((b) =>
      b.addEventListener("click", () => {
        const p = selectedPosition();
        if (p) $("pp-sell-qty").value = String(Number(p.sellable_qty || 0));
      }));
    document.querySelector("#pp-positions tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-manual-sell]");
      if (btn) fillSellFromPosition(btn.dataset.code, btn.dataset.manualSell);
    });
    $("pp-confirm-ack").addEventListener("change", () => {
      $("pp-confirm-submit").disabled = !$("pp-confirm-ack").checked || !S.manualPreview;
    });
    $("pp-confirm-cancel").addEventListener("click", () => {
      if (S.manualPending) return;
      S.manualOpen = false;
      S.manualPreview = null;
      $("pp-confirm-dialog").close();
    });
    $("pp-confirm-submit").addEventListener("click", confirmManual);
    syncBuyMode();
  }

  async function loadCapabilities() {
    try {
      S.caps = await fetchJson("/api/paper/manual/capabilities");
    } catch (_) {
      S.caps = {enabled: false, reason: "本机未开放人工交易"};
    }
    if (S.lastGood) renderManual(S.lastGood);
  }

  function renderWarnings(d) {
    const el = $("pp-warnings");
    const ws = d.warnings || [];
    el.innerHTML = ws.length
      ? `<b>提示</b>：` + ws.map(esc).join("；")
      : "";
  }

  function applyFocusHighlight() {
    const code = S.focusCode;
    const bar = $("pp-focus-bar");
    document.querySelectorAll(".pp-row-focus").forEach((tr) => tr.classList.remove("pp-row-focus"));
    if (!code) {
      if (bar) bar.classList.add("hidden");
      return;
    }
    const rows = [...document.querySelectorAll(`tr[data-code-row="${CSS.escape(code)}"]`)];
    rows.forEach((tr) => tr.classList.add("pp-row-focus"));
    if (bar) {
      bar.classList.remove("hidden");
      $("pp-focus-text").textContent = rows.length
        ? `已定位标的 ${code}（当前在模拟盘 ${rows.length} 处记录中命中）`
        : `模拟盘当前持仓、最近计划与调仓记录中暂未找到 ${code}`;
    }
    if (rows[0]) rows[0].scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function applyManualPrefill(d) {
    if (S.manualPrefillApplied || !S.manualPrefill || !(S.caps && S.caps.enabled)) return;
    S.manualPrefillApplied = true;
    const { side, code } = S.manualPrefill;
    if (side === "sell") {
      fillSellFromPosition(code, "partial");
    } else {
      setManualTab("buy");
      if ($("pp-buy-code")) $("pp-buy-code").value = code;
      $("pp-manual-card").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function render(d) {
    renderHeader(d);
    renderMetrics(d);
    renderEquity(d);
    renderPositions(d);
    renderManual(d);
    renderPlan(d);
    renderTrades(d);
    renderPlans(d);
    renderWarnings(d);
    if (S.focusCode) applyFocusHighlight();
    applyManualPrefill(d);
  }

  async function loadLatest(manual) {
    if (S.stopped) return;
    const mySeq = ++S.seq;
    try {
      const d = await fetchJson("/api/paper/latest");
      if (mySeq !== S.seq) return;   // 已有更新的请求，丢弃本次
      S.lastGood = d;
      if (S.manualOpen || S.manualPending) return;  // 人工表单/弹窗期间不重绘
      render(d);
      showStatus("", false);
      $("pp-refresh").disabled = false;
    } catch (exc) {
      if (mySeq !== S.seq) return;
      if (exc.status === 401) {
        S.stopped = true;
        S.auto = false;
        updateAutoBtn();
        showStatus("登录已失效：请重新登录系统后回来查看（已停止自动刷新）", true);
        return;
      }
      if (S.lastGood) {
        render({...S.lastGood, stale: true, warnings:
          [...(S.lastGood.warnings || []), `刷新失败（保留上次数据）：${exc.message}`]});
        showStatus(`刷新失败（保留上次数据）：${exc.message}`, true);
      } else {
        showStatus(`加载失败：${exc.message}`, true);
      }
      if (manual) $("pp-refresh").disabled = false;
    }
  }

  function updateAutoBtn() {
    const b = $("pp-auto");
    b.classList.toggle("primary", S.auto);
    b.textContent = S.auto ? "自动刷新" : "已暂停";
  }

  function autoTick() {
    setInterval(() => {
      if (!S.auto || S.stopped) return;
      if (document.visibilityState !== "visible") return;  // 后台标签页不轮询
      if (S.manualOpen || S.manualPending) return;
      loadLatest(false);
    }, 60000);
  }

  if (window.BaziNavShell) {
    window.BaziNavShell.mountHeader({ headMetaDefault: "模拟盘账本" });
    window.BaziNavShell.fetchCrossStatus().then(() => {
      if (S.lastGood) render(S.lastGood);
    });
  } else {
    window.bindMainReturn();
  }
  (() => {
    const q = new URLSearchParams(location.search);
    const f = (q.get("focus") || "").trim().toUpperCase();
    if (f) S.focusCode = f;
    const m = (q.get("manual") || "").trim().toLowerCase();
    const mc = (q.get("code") || "").trim().toUpperCase();
    if ((m === "buy" || m === "sell") && mc) {
      S.manualPrefill = { side: m, code: mc };
    }
  })();
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a.pp-code-link[data-pp-stock]");
    if (a && window.BaziNavShell) {
      window.BaziNavShell.setLastStock({
        id: a.dataset.ppStock,
        code: a.dataset.ppStock,
        name: a.dataset.ppName || "",
        dataset: "ashare",
      });
      return;
    }
    const planRow = ev.target.closest("tr.pp-plan-row[data-plan-date]");
    if (planRow && !ev.target.closest("a")) {
      const dt = planRow.dataset.planDate || "";
      S.expandedPlanDate = (S.expandedPlanDate === dt) ? null : dt;
      if (S.lastGood) renderPlans(S.lastGood);
    }
  });
  if ($("pp-focus-clear")) {
    $("pp-focus-clear").addEventListener("click", () => {
      S.focusCode = "";
      applyFocusHighlight();
    });
  }
  bindManualEvents();
  $("pp-refresh").addEventListener("click", () => loadLatest(true));
  $("pp-auto").addEventListener("click", () => {
    S.auto = !S.auto;
    localStorage.setItem("bazi_paper_auto", S.auto ? "1" : "0");
    updateAutoBtn();
    if (S.auto) loadLatest(false);
  });

  updateAutoBtn();
  loadCapabilities().then(() => {
    if (S.lastGood) applyManualPrefill(S.lastGood);
  });
  loadLatest(false);
  autoTick();
})();
