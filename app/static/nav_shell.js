/* 全站统一导航壳、主题同步、全局 ⌘K 搜票与跨模块实况缓存（只读，不重算任何研究/账本指标）。 */
(function () {
  "use strict";

  const LAST_STOCK_KEY = "bazi_last_stock";
  const CROSS_CACHE_KEY = "bazi_cross_status_v1";
  const CROSS_TTL_MS = 45000;
  const DEFAULT_STOCK = {
    id: "600519.SH",
    code: "600519.SH",
    name: "贵州茅台",
    dataset: "ashare",
  };

  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

  /* ---------- 主题同步 ---------- */

  function readStoredTheme() {
    try {
      const p = new URLSearchParams(location.search).get("theme");
      if (p === "light" || p === "dark") return p === "light";
      return localStorage.getItem("bazi_theme") === "light";
    } catch (_) {
      return false;
    }
  }

  function applyTheme(isLight, opts = {}) {
    const light = !!isLight;
    document.documentElement.classList.toggle("light", light);
    document.documentElement.classList.toggle("dark", !light);
    if (document.body) {
      document.body.classList.toggle("theme-light", light);
    }
    const icon = document.getElementById("theme-icon");
    const btn = document.getElementById("theme-toggle");
    if (icon) icon.textContent = light ? "🌙" : "☀️";
    else if (btn && (btn.textContent.trim() === "☀️" || btn.textContent.trim() === "🌙")) {
      btn.textContent = light ? "🌙" : "☀️";
    }
    if (btn) {
      btn.title = light ? "当前浅色模式，点击切换到深色" : "当前深色模式，点击切换到浅色";
    }
    if (opts.persist) {
      try { localStorage.setItem("bazi_theme", light ? "light" : "dark"); } catch (_) {}
    }
    if (!opts.silent) {
      try {
        window.dispatchEvent(new CustomEvent("bazi-theme-change", { detail: { light } }));
      } catch (_) {}
    }
  }

  function bindThemeToggle(btn) {
    if (!btn || btn.dataset.navThemeBound === "1") return;
    btn.dataset.navThemeBound = "1";
    btn.addEventListener("click", () => {
      const nextLight = !document.documentElement.classList.contains("light");
      applyTheme(nextLight, { persist: true });
    });
  }

  // 脚本加载即应用主题，减少闪烁；DOMReady 后补一次 body class
  applyTheme(readStoredTheme(), { silent: true });
  document.addEventListener("DOMContentLoaded", () => {
    applyTheme(readStoredTheme(), { silent: true });
  });
  window.addEventListener("storage", (ev) => {
    if (ev.key === "bazi_theme") {
      applyTheme(ev.newValue === "light", { silent: true });
    }
  });

  /* ---------- 跨栏目最近标的记忆 ---------- */

  function getLastStock() {
    try {
      const raw = sessionStorage.getItem(LAST_STOCK_KEY);
      if (raw) {
        const obj = JSON.parse(raw);
        if (obj && (obj.id || obj.code)) {
          return {
            id: String(obj.id || obj.code),
            code: String(obj.code || obj.id),
            name: String(obj.name || ""),
            dataset: obj.dataset === "etf" ? "etf" : "ashare",
          };
        }
      }
    } catch (_) {}
    return { ...DEFAULT_STOCK };
  }

  function shortLabel(st) {
    if (!st) return "";
    const txt = (st.name || st.code || st.id || "").trim();
    if (!txt) return "";
    return txt.length > 6 ? txt.slice(0, 6) + "…" : txt;
  }

  function syncNavStockLinks() {
    const st = getLastStock();
    const isAshare = st.dataset !== "etf";
    const ashareCode = isAshare ? (st.id || st.code) : DEFAULT_STOCK.id;
    const lbl = shortLabel(st);
    document.querySelectorAll("[data-nav-stock-label]").forEach((el) => {
      el.textContent = lbl ? `· ${lbl}` : "";
    });
    const navEl = document.getElementById("global-nav");
    const active = navEl ? (navEl.dataset.active || "") : "";
    const fromSuffix = active ? `&from=${encodeURIComponent(active)}` : "";
    const stockLink = document.getElementById("nav-open-stock");
    if (stockLink) {
      stockLink.href = `/?id=${encodeURIComponent(st.id)}&dataset=${encodeURIComponent(st.dataset)}&tab=overview${fromSuffix}`;
      stockLink.title = `进入个股档案：${st.code} ${st.name || ""}`.trim();
    }
    const guaLink = document.getElementById("nav-open-gua");
    if (guaLink) {
      guaLink.href = `/gua?code=${encodeURIComponent(ashareCode)}${fromSuffix}`;
    }
    const n2Link = document.getElementById("nav-open-n2");
    if (n2Link) {
      n2Link.href = `/static/n2.html?code=${encodeURIComponent(ashareCode)}#stock`;
    }
  }

  function setLastStock(info) {
    if (!info || (!info.id && !info.code)) return;
    const next = {
      id: String(info.id || info.code),
      code: String(info.code || info.id),
      name: String(info.name || ""),
      dataset: info.dataset === "etf" ? "etf" : "ashare",
    };
    try {
      sessionStorage.setItem(LAST_STOCK_KEY, JSON.stringify(next));
    } catch (_) {}
    syncNavStockLinks();
  }

  /* ---------- 跨模块实况索引（生存者报告 + 模拟盘） ---------- */

  let _crossStatus = null;
  let _crossPromise = null;

  function buildCrossStatus(svRep, ppData) {
    const out = {
      ts: Date.now(),
      survivor: {
        available: false, asof_date: "", asof_time: "", session: "",
        month_id: "", pillar: "", pool: 0, execB8: 0, noChaseB8: 0,
        bWatch: 0, holdings: 0, byCode: {},
      },
      paper: {
        available: false, mode: "", backend: "", nav: null,
        total_weight: null, day_pnl: null, cum_pnl: null,
        posCount: 0, byCode: {}, plannedBuys: {}, plannedExits: {},
      },
    };
    if (svRep && Array.isArray(svRep.rows)) {
      const sv = out.survivor;
      sv.available = true;
      sv.asof_date = svRep.asof_date || "";
      sv.asof_time = svRep.asof_time || "";
      sv.session = svRep.session || "";
      sv.month_id = svRep.month_id || "";
      sv.pillar = svRep.pillar || "";
      sv.pool = svRep.rows.length;
      const holdingMap = {};
      (svRep.s1_holdings || []).forEach((h) => {
        if (h && h.code) holdingMap[h.code] = h;
      });
      sv.holdings = Object.keys(holdingMap).length;
      svRep.rows.forEach((r) => {
        if (!r || !r.code) return;
        if (r.group === "B") {
          if (r.no_chase) sv.noChaseB8 += 1;
          else sv.execB8 += 1;
        }
        if (r.b_tier) sv.bWatch += 1;
        sv.byCode[r.code] = {
          code: r.code,
          name: r.name || "",
          group: r.group || "",
          b_tier: r.b_tier || "",
          no_chase: !!r.no_chase,
          d_v1_state: r.d_v1_state || "",
          d_v2_signal: !!r.d_v2_signal,
          mtd_pct: r.mtd_pct,
          mtm_pct: r.mtm_pct,
          cross: r.cross || "",
          s1_holding: holdingMap[r.code] || null,
        };
      });
    }
    if (ppData && ppData.summary) {
      const pp = out.paper;
      const s = ppData.summary || {};
      pp.available = true;
      pp.mode = s.mode || "";
      pp.backend = s.execution_backend || "";
      pp.nav = s.nav;
      pp.total_weight = s.total_weight;
      pp.day_pnl = s.day_pnl;
      pp.cum_pnl = s.cum_pnl;
      const posList = ppData.positions || [];
      pp.posCount = posList.length;
      posList.forEach((p) => {
        if (!p || !p.code) return;
        pp.byCode[p.code] = {
          code: p.code,
          name: p.name || "",
          strategy: p.strategy || "",
          weight: p.weight,
          qty: p.qty,
          sellable_qty: p.sellable_qty,
          pnl_pct: p.pnl_pct,
          day_pct: p.day_pct,
          price: p.price,
        };
      });
      const planned = (ppData.plans_recent || []).find((x) => x.status === "planned");
      if (planned) {
        (planned.buys || []).forEach((b) => { if (b && b.code) pp.plannedBuys[b.code] = b; });
        (planned.exits || []).forEach((e) => { if (e && e.code) pp.plannedExits[e.code] = e; });
      }
    }
    return out;
  }

  function getCrossStatus() {
    if (_crossStatus) return _crossStatus;
    try {
      const raw = sessionStorage.getItem(CROSS_CACHE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.ts && (Date.now() - parsed.ts < CROSS_TTL_MS)) {
          _crossStatus = parsed;
          return _crossStatus;
        }
      }
    } catch (_) {}
    return null;
  }

  function updateNavBadges(st) {
    st = st || getCrossStatus();
    if (!st) return;
    const sv = st.survivor || {};
    const pp = st.paper || {};
    document.querySelectorAll('[data-nav-badge="survivor"]').forEach((el) => {
      if (!sv.available) { el.classList.add("hidden"); return; }
      const txt = sv.execB8 > 0 ? `B8·${sv.execB8}` : (sv.bWatch > 0 ? `观·${sv.bWatch}` : `${sv.pool}池`);
      el.textContent = txt;
      el.classList.toggle("is-hot", sv.execB8 > 0);
      el.classList.remove("hidden");
      el.title = `生存者最新（${sv.asof_date}）：可执行B8 ${sv.execB8} / 不追 ${sv.noChaseB8} / B观察带 ${sv.bWatch} / 全池 ${sv.pool}`;
    });
    document.querySelectorAll('[data-nav-badge="paper"]').forEach((el) => {
      if (!pp.available) { el.classList.add("hidden"); return; }
      const txt = pp.posCount > 0 ? `持${pp.posCount}` : (pp.mode === "dry_run" ? "演练" : "空仓");
      el.textContent = txt;
      el.classList.toggle("is-hot", pp.posCount > 0);
      el.classList.remove("hidden");
      const navStr = pp.nav != null ? Number(pp.nav).toFixed(4) : "—";
      el.title = `模拟盘：净值 ${navStr} · 持仓 ${pp.posCount} 只 · 总仓位 ${pp.total_weight ?? 0}%`;
    });
  }

  async function safeJson(url) {
    try {
      const r = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
      if (!r.ok) return null;
      return await r.json();
    } catch (_) {
      return null;
    }
  }

  async function fetchCrossStatus(opts = {}) {
    if (!opts.force) {
      const cached = getCrossStatus();
      if (cached) {
        updateNavBadges(cached);
        return cached;
      }
      if (_crossPromise) return _crossPromise;
    }
    _crossPromise = (async () => {
      const [svRep, ppData] = await Promise.all([
        safeJson("/api/survivor/latest"),
        safeJson("/api/paper/latest"),
      ]);
      const st = buildCrossStatus(svRep, ppData);
      _crossStatus = st;
      try { sessionStorage.setItem(CROSS_CACHE_KEY, JSON.stringify(st)); } catch (_) {}
      updateNavBadges(st);
      _crossPromise = null;
      return st;
    })();
    return _crossPromise;
  }

  function renderStockBadges(code, opts = {}) {
    const st = getCrossStatus();
    if (!st || !code) return "";
    const out = [];
    const svRow = (st.survivor && st.survivor.byCode) ? st.survivor.byCode[code] : null;
    const ppPos = (st.paper && st.paper.byCode) ? st.paper.byCode[code] : null;
    const ppBuy = (st.paper && st.paper.plannedBuys) ? st.paper.plannedBuys[code] : null;
    const ppExit = (st.paper && st.paper.plannedExits) ? st.paper.plannedExits[code] : null;

    if (svRow && opts.includeSurvivor !== false) {
      let lbl = "生存者池";
      let cls = "xm-sv-d";
      if (svRow.group === "B") {
        lbl = svRow.no_chase ? "B8·不追" : "生存者·B8";
        cls = svRow.no_chase ? "xm-sv-watch" : "xm-sv-b";
      } else if (svRow.b_tier) {
        lbl = `观察·${svRow.b_tier}`;
        cls = "xm-sv-watch";
      } else if (svRow.group === "C" || svRow.group === "A-other") {
        lbl = svRow.group === "C" ? "生存者·C" : "生存者·A";
        cls = "xm-sv-a";
      } else if (svRow.d_v2_signal) {
        lbl = "D_V2";
      }
      out.push(
        `<a class="xm-badge ${cls}" href="/survivor?code=${encodeURIComponent(code)}" ` +
        `onclick="event.stopPropagation()" title="在生存者报告中查看 ${esc(code)}（当前 ${esc(svRow.group)} 组，${esc(svRow.cross || "—")}）">${esc(lbl)}</a>`
      );
    }
    if (opts.includePaper !== false) {
      if (ppPos) {
        const pnl = ppPos.pnl_pct != null ? ` ${Number(ppPos.pnl_pct) > 0 ? "+" : ""}${Number(ppPos.pnl_pct).toFixed(1)}%` : "";
        out.push(
          `<a class="xm-badge xm-pp-pos" href="/paper?focus=${encodeURIComponent(code)}" ` +
          `onclick="event.stopPropagation()" title="模拟盘当前持仓 ${ppPos.weight ?? 0}%${pnl}">模拟持${ppPos.weight != null ? Number(ppPos.weight).toFixed(0) + "%" : ""}</a>`
        );
      } else if (ppBuy) {
        out.push(
          `<a class="xm-badge xm-pp-buy" href="/paper?focus=${encodeURIComponent(code)}" ` +
          `onclick="event.stopPropagation()" title="模拟盘明日计划待买入">计划买</a>`
        );
      }
      if (ppExit) {
        out.push(
          `<a class="xm-badge xm-pp-exit" href="/paper?focus=${encodeURIComponent(code)}" ` +
          `onclick="event.stopPropagation()" title="模拟盘计划退出：${esc(ppExit.reason || "")}">计划卖</a>`
        );
      }
    }
    return out.join("");
  }

  /* ---------- 通用键盘搜票联想器 ---------- */

  function attachStockSuggest(inputEl, arg2, arg3) {
    if (!inputEl || inputEl.dataset.suggestAttached === "1") return;
    inputEl.dataset.suggestAttached = "1";

    let onPick = arg2;
    let opts = arg3 || {};
    if (arg2 && typeof arg2 === "object" && arg2.nodeType === 1) {
      opts = Object.assign({}, arg3, { dropdownEl: arg2 });
      onPick = opts.onPick;
    }
    const getDs = () => (typeof opts.dataset === "function" ? opts.dataset() : (opts.dataset || "ashare"));

    let drop = opts.dropdownEl;
    if (!drop) {
      const parent = inputEl.parentElement;
      if (parent && getComputedStyle(parent).position === "static") {
        parent.style.position = "relative";
      }
      drop = document.createElement("div");
      drop.className = "suggest hidden";
      (parent || document.body).appendChild(drop);
    }

    let items = [];
    let activeIdx = -1;
    let timer = null;
    let reqSeq = 0;

    function close() {
      drop.classList.add("hidden");
      drop.innerHTML = "";
      items = [];
      activeIdx = -1;
    }

    function highlight(idx) {
      const nodes = drop.querySelectorAll(".sg[data-idx]");
      if (!nodes.length) { activeIdx = -1; return; }
      activeIdx = (idx + nodes.length) % nodes.length;
      nodes.forEach((n, i) => {
        const on = i === activeIdx;
        n.classList.toggle("sg-active", on);
        if (on && n.scrollIntoView) n.scrollIntoView({ block: "nearest" });
      });
    }

    function pick(idx) {
      const row = items[idx];
      if (!row) return;
      close();
      setLastStock({
        id: row.identity_code,
        code: row.security_code,
        name: row.name,
        dataset: getDs(),
      });
      if (typeof onPick === "function") onPick(row);
    }

    async function search(q) {
      const my = ++reqSeq;
      try {
        const ds = getDs();
        const res = await fetch(`/api/search?dataset=${encodeURIComponent(ds)}&q=${encodeURIComponent(q)}&limit=${opts.limit || 20}`, {
          cache: "no-store", headers: { Accept: "application/json" },
        });
        if (!res.ok || my !== reqSeq) return;
        const rows = await res.json();
        if (my !== reqSeq) return;
        items = Array.isArray(rows) ? rows : [];
        activeIdx = -1;
        drop.innerHTML = "";
        if (!items.length) {
          drop.innerHTML = '<div class="sg">无匹配证券</div>';
          drop.classList.remove("hidden");
          return;
        }
        items.forEach((r, i) => {
          const div = document.createElement("div");
          div.className = "sg" + (r.has_natal_profile === 1 ? "" : " nop");
          div.dataset.idx = String(i);
          const typeTxt = r.security_type === "INDEX" ? "指数" : (r.security_type === "ETF" ? "ETF" : "股票");
          const left = `<span><b>${esc(r.security_code)}</b> ${esc(r.name || "")}</span>`;
          const profBtn = opts.showProfileBtn
            ? `<button type="button" class="ns-prof-go" data-prof="${esc(r.identity_code)}" title="直达个股档案">档案↗</button>`
            : "";
          const right = `<span style="display:inline-flex;align-items:center;gap:6px"><i>${typeTxt}${r.has_natal_profile === 1 ? "" : " · 无命盘"}</i>${profBtn}</span>`;
          div.innerHTML = left + right;
          div.addEventListener("mousedown", (ev) => {
            const pBtn = ev.target.closest("[data-prof]");
            if (pBtn) {
              ev.preventDefault();
              ev.stopPropagation();
              close();
              setLastStock({ id: r.identity_code, code: r.security_code, name: r.name, dataset: getDs() });
              const from = opts.fromPage ? `&from=${encodeURIComponent(opts.fromPage)}` : "";
              location.href = `/?id=${encodeURIComponent(r.identity_code)}&tab=overview${from}`;
              return;
            }
            ev.preventDefault();
            pick(i);
          });
          drop.appendChild(div);
        });
        drop.classList.remove("hidden");
      } catch (_) {}
    }

    inputEl.addEventListener("input", () => {
      clearTimeout(timer);
      const q = inputEl.value.trim();
      if (!q) { close(); return; }
      timer = setTimeout(() => search(q), 180);
    });

    inputEl.addEventListener("keydown", (ev) => {
      if (drop.classList.contains("hidden") || !items.length) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        highlight(activeIdx < 0 ? 0 : activeIdx + 1);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        highlight(activeIdx <= 0 ? items.length - 1 : activeIdx - 1);
      } else if (ev.key === "Enter" && activeIdx >= 0) {
        ev.preventDefault();
        pick(activeIdx);
      } else if (ev.key === "Escape") {
        close();
      }
    });

    document.addEventListener("click", (ev) => {
      if (ev.target !== inputEl && !drop.contains(ev.target)) close();
    });

    return { close };
  }

  /* ---------- 独立页统一全局顶栏挂载 ---------- */

  const PAGE_META = {
    survivor: { label: "生存者报告", desc: "100%节气月实时追踪" },
    paper: { label: "模拟盘", desc: "前向模拟账本" },
    n2: { label: "流日关系", desc: "全市场日级影子库" },
    gua: { label: "流月卦", desc: "节气K线定卦（文化展示）" },
  };

  function markReturnFrom(page) {
    try {
      sessionStorage.setItem("bazi_main_return", "1");
      if (page) sessionStorage.setItem("bazi_return_source", page);
    } catch (_) {}
  }

  function mountHeader(opts = {}) {
    const host = document.getElementById("global-nav");
    if (!host) return;
    const active = host.dataset.active || opts.active || "";
    const meta = PAGE_META[active] || { label: "八字流月", desc: "" };
    const st = getLastStock();
    const isAshare = st.dataset !== "etf";
    const ashareCode = isAshare ? (st.id || st.code) : DEFAULT_STOCK.id;
    const fromSuffix = active ? `&from=${encodeURIComponent(active)}` : "";

    host.className = "theme-header relative z-50 backdrop-blur-md border-b px-3 lg:px-5 py-1.5 transition-all";
    host.innerHTML = `
      <div class="max-w-[1780px] mx-auto flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <div class="flex items-center gap-2 shrink-0">
          <a href="/#board" class="flex items-center gap-2 no-underline text-inherit" title="回到流月机会表首页">
            <div class="w-7 h-7 rounded-lg bg-gradient-to-br from-red-600 to-amber-600 flex items-center justify-center font-bold text-white shadow-md text-xs">八</div>
            <h1 class="text-sm font-semibold tracking-wide whitespace-nowrap m-0">八字流月 · 历史规律</h1>
          </a>
          <span class="rv-nav-mode-tag">${esc(meta.label)}</span>
        </div>

        <nav class="rv-nav" id="ns-nav" aria-label="主导航">
          <a class="rv-nav-btn" href="/#board">流月机会表</a>
          <a class="rv-nav-btn" href="/?seg=1">100%段</a>
          <span class="rv-nav-sep" aria-hidden="true"></span>
          <span class="rv-nav-pair">
            <a class="rv-nav-btn${active === "survivor" ? " on" : ""}" href="/survivor">生存者报告<span class="rv-nav-pill hidden" data-nav-badge="survivor"></span></a>
            <a class="rv-nav-btn${active === "paper" ? " on" : ""}" href="/paper">模拟盘<span class="rv-nav-pill hidden" data-nav-badge="paper"></span></a>
          </span>
          <span class="rv-nav-sep" aria-hidden="true"></span>
          <a class="rv-nav-btn${active === "n2" ? " on" : ""}" id="nav-open-n2" href="/static/n2.html?code=${encodeURIComponent(ashareCode)}#stock">流日关系</a>
          <a class="rv-nav-btn${active === "gua" ? " on" : ""}" id="nav-open-gua" href="/gua?code=${encodeURIComponent(ashareCode)}${fromSuffix}">流月卦</a>
          <span class="rv-nav-sep" aria-hidden="true"></span>
          <a class="rv-nav-btn" id="nav-open-stock" href="/?id=${encodeURIComponent(st.id)}&dataset=${encodeURIComponent(st.dataset)}&tab=overview${fromSuffix}">个股档案<span class="rv-nav-sub" data-nav-stock-label></span></a>
        </nav>

        <div class="searchwrap relative flex-1 min-w-[160px] max-w-[300px]" id="ns-searchwrap">
          <input id="ns-q" type="search" placeholder="快搜代码或名称，进个股档案" autocomplete="off"
                 class="w-full theme-input border rounded-lg pl-8 pr-11 py-1 text-xs focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition-all">
          <svg class="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-2 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
          <span class="absolute right-2 top-1.5 text-[10px] text-slate-400 border border-slate-600 px-1 rounded pointer-events-none">⌘K</span>
          <div id="ns-suggest" class="suggest hidden"></div>
        </div>

        <div class="flex items-center gap-2 ml-auto min-w-0">
          <span class="rv-note truncate max-w-[340px]" id="head-meta">${esc(opts.headMetaDefault || "")}</span>
          <button id="theme-toggle" type="button" class="rv-btn rv-btn-sm" title="切换浅色/深色">☀️</button>
          <a class="rv-btn rv-btn-sm" id="back-main" href="/" style="display:inline-flex;align-items:center;text-decoration:none;color:inherit">← 返回系统</a>
        </div>
      </div>
    `;

    syncNavStockLinks();
    applyTheme(readStoredTheme(), { silent: true });
    bindThemeToggle(document.getElementById("theme-toggle"));

    const stockLink = document.getElementById("nav-open-stock");
    if (stockLink) stockLink.addEventListener("click", () => markReturnFrom(active));

    const nsQ = document.getElementById("ns-q");
    const nsSug = document.getElementById("ns-suggest");
    if (nsQ && nsSug) {
      const inPagePick = (active === "n2" || active === "gua") && typeof opts.onSelectStock === "function";
      nsQ.placeholder = inPagePick ? "搜代码/名称，在本页切换或进档案" : "快搜代码/名称，进个股档案";
      attachStockSuggest(nsQ, (row) => {
        if (inPagePick) {
          nsQ.value = row.security_code;
          opts.onSelectStock(row);
        } else {
          markReturnFrom(active);
          location.href = `/?id=${encodeURIComponent(row.identity_code)}&tab=overview${fromSuffix}`;
        }
      }, { dropdownEl: nsSug, showProfileBtn: inPagePick, fromPage: active });

      document.addEventListener("keydown", (ev) => {
        if ((ev.metaKey || ev.ctrlKey) && (ev.key === "k" || ev.key === "K")) {
          ev.preventDefault();
          nsQ.focus();
          nsQ.select();
        }
      });
    }

    if (typeof window.bindMainReturn === "function") {
      window.bindMainReturn();
    }
    fetchCrossStatus();
  }

  window.BaziNavShell = {
    applyTheme,
    bindThemeToggle,
    getLastStock,
    setLastStock,
    syncNavStockLinks,
    attachStockSuggest,
    fetchCrossStatus,
    getCrossStatus,
    updateNavBadges,
    renderStockBadges,
    mountHeader,
  };
})();
