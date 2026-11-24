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

  const S = {
    auto: localStorage.getItem("bazi_paper_auto") !== "0",
    lastGood: null,      // 上次成功数据（刷新失败时保留展示并标 stale）
    stopped: false,      // 401 后停止轮询
    seq: 0,              // 请求序号：慢响应不覆盖新响应
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
    if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
    return body;
  }

  function chip(el, text, cls) {
    el.textContent = text;
    el.className = "pp-chip" + (cls ? " " + cls : "");
  }

  function renderHeader(d) {
    const mode = (d.summary && d.summary.mode) || "—";
    chip($("pp-mode"),
      mode === "live" ? "LIVE（自动调仓）" : mode === "dry_run" ? "DRY-RUN（不下单）" : "未启用",
      mode === "live" ? "live" : mode === "dry_run" ? "info" : "");
    const ok = !!d.sync_ok && !d.stale;
    chip($("pp-sync"),
      ok ? "行情同步正常" : (d.sync_ok ? "数据可能过期" : "行情快照缺失"),
      ok ? "ok" : "warn");
  }

  function pnlClass(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "";
    return Number(v) > 0 ? "pp-up" : (Number(v) < 0 ? "pp-down" : "");
  }

  function renderMetrics(d) {
    const s = d.summary || {};
    const money = (v) => (v === null || v === undefined) ? "—"
      : `${v > 0 ? "+" : ""}${num(v, 0)}`;
    const items = [
      ["总资产", s.total === null || s.total === undefined ? "—" : num(s.total, 0), ""],
      ["现金", s.cash === null || s.cash === undefined ? "—" : num(s.cash, 0), ""],
      ["总仓位", s.total_weight === null || s.total_weight === undefined ? "—" : `${num(s.total_weight, 1)}%`, ""],
      ["净值", s.nav === null || s.nav === undefined ? "—" : num(s.nav, 4), ""],
      ["浮动盈亏", money(s.unrealized_pnl), pnlClass(s.unrealized_pnl)],
      ["今日盈亏", money(s.day_pnl), pnlClass(s.day_pnl)],
      ["累计盈亏", money(s.cum_pnl), pnlClass(s.cum_pnl)],
    ];
    $("pp-metrics").innerHTML = items.map(([k, v, cls]) =>
      `<div class="pp-metric"><dt>${esc(k)}</dt><dd class="${cls}">${esc(v)}</dd></div>`).join("");
  }

  function renderEquity(d) {
    const rows = (d.equity || []).filter((x) => x.nav !== null && x.nav !== undefined);
    const box = $("pp-equity-box");
    if (rows.length < 1) {
      box.innerHTML = '<p class="pp-note">尚无净值快照（上线首个交易日收盘后生成）。</p>';
      $("pp-equity-note").textContent = "按每日收盘回读快照（净值 = 总资产 / 初始 100 万）";
      return;
    }
    const W = 720, H = 200, PAD = 34;
    const vals = rows.map((r) => Number(r.nav));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 1e-9) { hi += 0.01; lo -= 0.01; }
    const x = (i) => PAD + (i * (W - 2 * PAD)) / Math.max(1, rows.length - 1);
    const y = (v) => PAD + (H - 2 * PAD) * (1 - (v - lo) / (hi - lo));
    const pts = rows.map((r, i) => `${x(i).toFixed(1)},${y(Number(r.nav)).toFixed(1)}`);
    const first = vals[0];
    const last = vals[vals.length - 1];
    const up = last >= first;
    const stroke = up ? "var(--stock-up)" : "var(--stock-down)";
    const gridLines = [0, 0.25, 0.5, 0.75, 1].map((t) => {
      const gy = PAD + (H - 2 * PAD) * t;
      const gv = hi - (hi - lo) * t;
      return `<line x1="${PAD}" y1="${gy}" x2="${W - PAD}" y2="${gy}" stroke="var(--border-main)" stroke-width="1"/>`
        + `<text x="${PAD - 6}" y="${gy + 4}" text-anchor="end" font-size="10" fill="var(--text-sub)">${gv.toFixed(3)}</text>`;
    }).join("");
    box.innerHTML =
      `<svg class="pp-equity-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="净值曲线">`
      + gridLines
      + `<polyline points="${pts.join(" ")}" fill="none" stroke="${stroke}" stroke-width="2.2" stroke-linejoin="round"/>`
      + `<circle cx="${x(rows.length - 1)}" cy="${y(last)}" r="3.5" fill="${stroke}"/>`
      + `<text x="${W - PAD}" y="${PAD - 12}" text-anchor="end" font-size="11" fill="var(--text-sub)">${rows[0].date} → ${rows[rows.length - 1].date}</text>`
      + `<text x="${W - PAD}" y="${H - 8}" text-anchor="end" font-size="11" fill="${stroke}">最新净值 ${last.toFixed(4)}</text>`
      + `</svg>`;
    $("pp-equity-note").textContent =
      `共 ${rows.length} 个交易日；首日 ${first.toFixed(4)} → 最新 ${last.toFixed(4)}`
      + `（${pct((last / first - 1) * 100)}）`;
  }

  const STRATEGY_LABEL = {b8: "B8", s1c: "S1/C", d_v1: "D_V1", d_v2: "D_V2", adopted: "基线"};

  function renderPositions(d) {
    const tbody = document.querySelector("#pp-positions tbody");
    const rows = d.positions || [];
    $("pp-pos-note").textContent = rows.length
      ? `共 ${rows.length} 只（盈亏按最新一档切片重估，每 30 分钟更新）`
      : "当前无持仓（或引擎未启用）";
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="pp-empty" colspan="12">—</td></tr>';
      return;
    }
    const pnlCell = (p) => p.pnl === null || p.pnl === undefined
      ? "—"
      : `${p.pnl > 0 ? "+" : ""}${num(p.pnl, 0)}（${pct(p.pnl_pct)}）`;
    tbody.innerHTML = rows.map((p) => `<tr>
      <td data-label="代码">${esc(p.code || "—")}</td>
      <td data-label="名称">${esc(p.name || "—")}</td>
      <td data-label="策略">${esc(STRATEGY_LABEL[p.strategy] || p.strategy || "—")}</td>
      <td data-label="权重">${num(p.weight, 1)}%</td>
      <td data-label="现价">${p.price === null || p.price === undefined ? "—" : num(p.price)}</td>
      <td data-label="今日" class="${pnlClass(p.day_pct)}">${p.day_pct === null || p.day_pct === undefined ? "—" : pct(p.day_pct)}</td>
      <td data-label="浮动盈亏" class="${pnlClass(p.pnl)}">${pnlCell(p)}</td>
      <td data-label="市值">${num(p.market_value, 0)}</td>
      <td data-label="成本">${p.avg_cost === null || p.avg_cost === undefined ? "—" : num(p.avg_cost)}</td>
      <td data-label="入场日">${esc(p.entry_date || "—")}</td>
      <td data-label="止损">${p.stop === null || p.stop === undefined ? "—" : num(p.stop)}</td>
      <td data-label="目标">${p.target === null || p.target === undefined ? "—" : num(p.target)}</td>
    </tr>`).join("");
  }

  function renderPlan(d) {
    const box = $("pp-plan-body");
    const plans = d.plans_recent || [];
    if (!plans.length) {
      box.innerHTML = '<p class="pp-note">尚无计划（引擎未启用或报告未定稿）。</p>';
      $("pp-plan-note").textContent = "盘后定稿，次交易日执行";
      return;
    }
    const p = plans[0];
    $("pp-plan-note").textContent =
      `执行日 ${p.execute_trade_date || "—"}（${p.segment_id || "—"}段 / ${p.regime || "—"}）`;
    box.innerHTML = `
      <div class="pp-kv">
        <div class="pp-plan-line">计划：<b>${esc(p.plan_id || "—")}</b></div>
        <div class="pp-plan-line">状态：<b>${esc(p.status || "—")}</b>（信号日 ${esc(p.signal_date || "—")}）</div>
        <div class="pp-plan-line">买入 <b>${p.n_buys ?? 0}</b> 只 / 卖出 <b>${p.n_exits ?? 0}</b> 只 / 排除 <b>${p.n_excluded ?? 0}</b> 笔</div>
        <div class="pp-plan-line">总仓上限：${p.regime || "—"} 口径（见月度执行配置）</div>
      </div>
      <p class="pp-note" style="margin-top:8px">明细（买入/卖出/被拒原因）在「调仓记录」表；完整计划 JSON 走 <code>/api/paper/plans</code>。</p>`;
  }

  function renderTrades(d) {
    const tbody = document.querySelector("#pp-trades tbody");
    const rows = d.trades_recent || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="pp-empty" colspan="8">尚无调仓记录</td></tr>';
      return;
    }
    tbody.innerHTML = rows.slice(0, 40).map((t) => `<tr>
      <td data-label="执行日">${esc(t.trade_date || "—")}</td>
      <td data-label="方向">${t.kind === "buy" ? "买入" : t.kind === "sell" ? "卖出" : esc(t.kind || "—")}</td>
      <td data-label="代码">${esc(t.code || "—")}</td>
      <td data-label="名称">${esc(t.name || "—")}</td>
      <td data-label="数量">${t.qty === null || t.qty === undefined ? "—" : num(t.qty, 0)}</td>
      <td data-label="价格">${t.price === null || t.price === undefined ? "—" : num(t.price)}</td>
      <td data-label="原因" class="pp-excl">${esc(t.reason || "—")}</td>
      <td data-label="状态">${t.status === "filled" ? "已成交" : t.status === "rejected" ? "被拒" : esc(t.status || "—")}</td>
    </tr>`).join("");
  }

  function renderPlans(d) {
    const tbody = document.querySelector("#pp-plans tbody");
    const rows = d.plans_recent || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td class="pp-empty" colspan="6">—</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map((p) => `<tr>
      <td data-label="执行日">${esc(p.execute_trade_date || "—")}</td>
      <td data-label="信号日">${esc(p.signal_date || "—")}</td>
      <td data-label="状态">${esc(p.status || "—")}</td>
      <td data-label="买入">${p.n_buys ?? 0}</td>
      <td data-label="卖出">${p.n_exits ?? 0}</td>
      <td data-label="排除">${p.n_excluded ?? 0}</td>
    </tr>`).join("");
  }

  function renderWarnings(d) {
    const el = $("pp-warnings");
    const ws = d.warnings || [];
    el.innerHTML = ws.length
      ? `<b>提示</b>：` + ws.map(esc).join("；")
      : "";
  }

  function render(d) {
    renderHeader(d);
    renderMetrics(d);
    renderEquity(d);
    renderPositions(d);
    renderPlan(d);
    renderTrades(d);
    renderPlans(d);
    renderWarnings(d);
  }

  async function loadLatest(manual) {
    if (S.stopped) return;
    const mySeq = ++S.seq;
    try {
      const d = await fetchJson("/api/paper/latest");
      if (mySeq !== S.seq) return;   // 已有更新的请求，丢弃本次
      S.lastGood = d;
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
      loadLatest(false);
    }, 60000);
  }

  $("pp-refresh").addEventListener("click", () => loadLatest(true));
  $("pp-auto").addEventListener("click", () => {
    S.auto = !S.auto;
    localStorage.setItem("bazi_paper_auto", S.auto ? "1" : "0");
    updateAutoBtn();
    if (S.auto) loadLatest(false);
  });

  updateAutoBtn();
  loadLatest(false);
  autoTick();
})();
