/* 本地页面逻辑：所有数据都来自 /api/*，即本机 SQLite 只读查询。 */
const S = { id: null, stock: null, cal: null, opp: null, oppExport: null,
            oppMsort: [], oppPage: 1, oppFilterSig: null,
            branchPolicy: 'ALL', fromBoard: false,
            segMode: false, segCtx: null,
            relKind: 'OFFICIAL', relType: null, catalog: [], dataset: 'ashare',
            epoch: 0, meta: null, tgMode: 'ALL', tgBranch: '', tgRel: '' };
window.S = S;
// 旧地址 /seg → /?seg=1：启动流程会立刻重写地址栏，必须在模块加载时就记下来
const BOOT_SEG = (() => { try { return new URLSearchParams(location.search).get('seg') === '1'; } catch (e) { return false; } })();
const BRANCH_TERM = { 子: '大雪', 丑: '小寒', 寅: '立春', 卯: '惊蛰', 辰: '清明', 巳: '立夏',
                      午: '芒种', 未: '小暑', 申: '立秋', 酉: '白露', 戌: '寒露', 亥: '立冬' };
const $ = (s) => document.querySelector(s);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c;
                           if (h !== undefined) e.innerHTML = h; return e; };
const esc = (x) => (x === null || x === undefined ? '' : String(x))
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const typeLabel = (t) => (S.meta && S.meta.type_labels && S.meta.type_labels[t])
  || (t === 'INDEX' ? '指数' : t === 'ETF' ? 'ETF' : '股票');
const dash = (x) => (x === null || x === undefined || x === '') ? '<span class="dim">—</span>' : esc(x);
const efPair = (label, value) => '<span class="ef"><i>' + esc(label) + '</i><b>' +
  dash(value) + '</b></span>';

async function get(path, params) {
  const merged = Object.assign({ dataset: S.dataset }, params || {});
  const qs = Object.entries(merged).filter(([, v]) => v !== '' && v !== undefined)
    .map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
  const r = await fetch(path + (qs ? '?' + qs : ''));
  if (r.status === 401) {
    window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname + window.location.search);
    return new Promise(() => {});
  }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

/* 并发请求的返回顺序不定：后到的旧渲染必须放弃写 DOM，否则同一个容器里会重复出表。 */
const SEQ = {};
const seqStart = (k) => (SEQ[k] = (SEQ[k] || 0) + 1);
const seqStale = (k, my) => SEQ[k] !== my;

const pct = (x) => (x === null || x === undefined) ? '—' : (x * 100).toFixed(1) + '%';
const ret = (x) => (x === null || x === undefined) ? '—'
  : (x > 0 ? '+' : '') + (x * 100).toFixed(2) + '%';
const yiText = (x) => (x === null || x === undefined) ? '—'
  : (Math.abs(x) >= 100 ? Number(x).toFixed(0) : Number(x).toFixed(2)) + ' 亿';
const dirCls = (d) => { const s = String(d || '').toUpperCase(); return s === 'UP' ? 'up' : s === 'DOWN' ? 'down' : s === 'FLAT' ? 'flat' : 'na'; };
const dirBadge = (d) => { const s = String(d || '').toUpperCase(); return s === 'UP' ? 'badge-up' : s === 'DOWN' ? 'badge-down' : 'badge-flat'; };
const clsOf = (x) => (x === null || x === undefined) ? 'dim' : x > 0 ? 'up' : 'down';
const dirTxt = (d) => { const s = String(d || '').toUpperCase(); return s === 'UP' ? '涨' : s === 'DOWN' ? '跌' : s === 'FLAT' ? '平' : '无数据'; };

const LEFT_ALIGN_KEYS = new Set([
  'main_relations', 'official_types', 'candidate_types',
  'candidate_kinds', 'position_breakdown', 'source_breakdown', 'positions_json',
  'origins_json', 'events', 'path', 'item', 'rule_version', 'hidden_rule_version',
  'name', 'security_name', 'positions'
]);

function sampleGradeTxt(g) {
  const m = {
    RELATIVELY_MORE: '≥6次 (充裕)',
    FEW: '4–5次 (偏少)',
    VERY_FEW: '2–3次 (极少)',
    SINGLE_OBSERVATION: '1次 (孤本)',
    NO_HISTORY: '0次 (无历史)',
  };
  return m[g] || g || '—';
}
function sampleGradeIsHigh(g) {
  return g === 'RELATIVELY_MORE' || g === '≥6' || g === '6+';
}

/* ---------------- 全局即时悬浮提示 (Instant Tooltip) ----------------
   消除原生 title 的 800-1000ms 迟钝延迟，提供 80ms 极速响应、相邻表头 0ms 丝滑切换、
   结构化分段排版（指标名/正文/排序指引）与智能防溢出定位。 */
const AppTip = (() => {
  let tipEl = null;
  let arrowEl = null;
  let bodyEl = null;
  let showTimer = null;
  let hideTimer = null;
  let warmTimer = null;
  let isWarm = false;
  let currentTarget = null;

  function ensureEl() {
    if (tipEl) return tipEl;
    tipEl = document.createElement('div');
    tipEl.id = 'app-tooltip';
    tipEl.className = 'app-tooltip';
    tipEl.setAttribute('role', 'tooltip');
    tipEl.setAttribute('aria-hidden', 'true');

    arrowEl = document.createElement('div');
    arrowEl.className = 'app-tooltip-arrow';
    tipEl.appendChild(arrowEl);

    bodyEl = document.createElement('div');
    bodyEl.className = 'app-tooltip-inner';
    tipEl.appendChild(bodyEl);

    document.body.appendChild(tipEl);
    return tipEl;
  }

  function parseContent(rawText, targetEl) {
    if (!rawText) return '';
    const sortPhrase = '点击按此列加入多列排序；再次点击切换方向。标签按点击顺序优先。';
    const hasSortTip = rawText.includes(sortPhrase) || (targetEl && targetEl.classList && targetEl.classList.contains('opp-sortable'));
    let cleanText = rawText.replace(sortPhrase, '').trim();

    let title = '';
    let category = '字段说明';

    // 尝试从目标 TH 提取列名
    if (targetEl && targetEl.tagName === 'TH') {
      const clone = targetEl.cloneNode(true);
      clone.querySelectorAll('.opp-sort-head, sup, .tip-badge').forEach((n) => n.remove());
      const thName = (clone.textContent || '').trim();
      if (thName && thName.length <= 15) {
        title = thName;
      }
    }

    const lines = cleanText.split('\n').map((l) => l.trim()).filter(Boolean);

    // 如果首行符合标题特征（较短且非完整句子）
    if (lines.length > 1 && lines[0].length <= 18 && !/[。；;!！]$/.test(lines[0])) {
      title = lines[0];
      lines.shift();
    }

    if (title.includes('占比') || title.includes('涨幅') || title.includes('一致性') || title.includes('稳健') || title.includes('超额')) {
      category = '指标释义';
    } else if (title.includes('八字') || title.includes('五行') || title.includes('柱')) {
      category = '命理参数';
    } else if (targetEl && targetEl.id === 'boardtitle') {
      title = '流月机会看板';
      category = '核心口径与风险提示';
    }

    let html = '';
    if (title) {
      html += '<div class="app-tip-head">' +
        '<div class="app-tip-title">' +
        '<svg class="w-3.5 h-3.5 text-indigo-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>' +
        '<span>' + esc(title) + '</span>' +
        '</div>' +
        '<span class="app-tip-badge">' + esc(category) + '</span>' +
        '</div>';
    }

    if (lines.length) {
      html += '<div class="app-tip-paras">';
      lines.forEach((ln) => {
        html += '<p>' + esc(ln) + '</p>';
      });
      html += '</div>';
    }

    if (hasSortTip) {
      html += '<div class="app-tip-foot">' +
        '<span class="app-tip-dot"></span>' +
        '<span>点击此列加入多列排序（再次点击切换升/降序）</span>' +
        '</div>';
    }

    return html;
  }

  function position(target) {
    const rect = target.getBoundingClientRect();
    const tip = ensureEl();

    tip.style.visibility = 'hidden';
    tip.style.display = 'block';

    const tipRect = tip.getBoundingClientRect();
    const tipW = tipRect.width;
    const tipH = tipRect.height;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let left = rect.left + rect.width / 2 - tipW / 2;
    const minLeft = 12;
    const maxLeft = vw - tipW - 12;
    if (left < minLeft) left = minLeft;
    if (left > maxLeft) left = maxLeft;

    let top = rect.bottom + 8;
    let place = 'bottom';
    if (top + tipH > vh - 10 && rect.top - tipH - 8 > 10) {
      top = rect.top - tipH - 8;
      place = 'top';
    }

    tip.setAttribute('data-placement', place);
    tip.style.left = Math.round(left) + 'px';
    tip.style.top = Math.round(top) + 'px';

    const targetCenter = rect.left + rect.width / 2;
    let arrowLeft = targetCenter - left;
    const minArrow = 14;
    const maxArrow = tipW - 14;
    if (arrowLeft < minArrow) arrowLeft = minArrow;
    if (arrowLeft > maxArrow) arrowLeft = maxArrow;
    if (arrowEl) {
      arrowEl.style.left = Math.round(arrowLeft) + 'px';
    }

    tip.style.visibility = 'visible';
  }

  function show(target, text) {
    clearTimeout(hideTimer);
    clearTimeout(warmTimer);
    currentTarget = target;

    const tip = ensureEl();
    if (bodyEl) bodyEl.innerHTML = parseContent(text, target);

    position(target);
    tip.classList.add('visible');
    isWarm = true;
  }

  function hide() {
    clearTimeout(showTimer);
    currentTarget = null;
    if (!tipEl) return;
    tipEl.classList.remove('visible');
    hideTimer = setTimeout(() => {
      if (!currentTarget && tipEl) {
        tipEl.style.display = 'none';
      }
    }, 150);
    warmTimer = setTimeout(() => {
      isWarm = false;
    }, 300);
  }

  function init() {
    document.addEventListener('mouseover', (e) => {
      const target = e.target.closest('[data-tip], th.cursor-help, th.opp-sortable, #boardtitle, [title]');
      if (!target) return;

      if (target.hasAttribute('title')) {
        const raw = target.getAttribute('title');
        if (raw && raw.trim()) {
          target.setAttribute('data-tip', raw);
        }
        target.removeAttribute('title');
      }

      const text = target.getAttribute('data-tip');
      if (!text || !text.trim()) return;

      if (target === currentTarget) return;

      clearTimeout(showTimer);
      const delay = isWarm ? 0 : 80;
      if (delay === 0) {
        show(target, text);
      } else {
        showTimer = setTimeout(() => {
          show(target, text);
        }, delay);
      }
    }, true);

    document.addEventListener('mouseout', (e) => {
      if (!currentTarget) return;
      if (e.relatedTarget && currentTarget.contains(e.relatedTarget)) return;
      hide();
    }, true);

    window.addEventListener('scroll', () => { if (currentTarget) hide(); }, { passive: true });
    document.addEventListener('mousedown', () => { if (currentTarget) hide(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  }

  return { init, show, hide };
})();

function table(cols, rows, onRow, opts) {
  const t = el('table', (opts && opts.cls ? opts.cls + ' ' : '') + 'w-full text-xs');
  if (opts && opts.widths) {
    const cg = el('colgroup');
    opts.widths.forEach((w) => { const c = el('col'); c.style.width = w; cg.appendChild(c); });
    t.appendChild(cg);
  }
  const thead = el('thead', 'theme-card-sub uppercase text-[11px] font-semibold border-b border-slate-700/60');
  const head = el('tr');
  const colAligns = cols.map((c) => {
    if (c[3] && typeof c[3] === 'string' && (c[3] === 'left' || c[3] === 'center' || c[3] === 'right')) {
      return c[3];
    }
    return LEFT_ALIGN_KEYS.has(c[0]) ? 'left' : 'center';
  });
  cols.forEach((c, idx) => {
    const isFirst = idx === 0;
    const isLast = idx === cols.length - 1;
    const padCls = isFirst ? 'pl-4 pr-2' : (isLast ? 'pl-2 pr-4' : 'px-2');
    const th = el('th', 'py-2.5 whitespace-nowrap text-' + colAligns[idx] + ' ' + padCls, esc(c[1]));
    if (c[4]) {
      th.dataset.tip = c[4];
      th.classList.add('cursor-help');
    }
    head.appendChild(th);
  });
  thead.appendChild(head);
  t.appendChild(thead);
  const tb = el('tbody', 'divide-y divide-slate-700/40');
  rows.forEach((r, i) => {
    const trCls = 'hover:bg-indigo-500/5 transition' + (onRow ? ' cursor-pointer' : '') +
      (opts && opts.rowClass ? ' ' + opts.rowClass(r, i) : '');
    const tr = el('tr', trCls);
    cols.forEach((c, idx) => {
      const isFirst = idx === 0;
      const isLast = idx === cols.length - 1;
      const padCls = isFirst ? 'pl-4 pr-2' : (isLast ? 'pl-2 pr-4' : 'px-2');
      const wrapCls = c[0] === 'relation_types' ? '' : 'whitespace-nowrap ';
      const v = typeof c[2] === 'function' ? c[2](r) : r[c[0]];
      const extraCls = (typeof c[3] === 'function') ? (c[3](r) + ' ') : '';
      const td = el('td', extraCls + 'py-2 text-' + colAligns[idx] + ' ' + padCls + ' ' + wrapCls, v === null || v === undefined ? '—' : v);
      tr.appendChild(td);
    });
    if (opts && opts.rowData) Object.assign(tr.dataset, opts.rowData(r));
    if (onRow) { tr.classList.add('clickable');
                 tr.onclick = () => onRow(r, tr); }
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  return t;
}

function statTable(pairs) {
  const d = el('div', 'kv');
  pairs.forEach(([k, v]) => d.appendChild(el('div', 'kv-i',
    '<span>' + esc(k) + '</span><b>' + (v === null || v === undefined || v === '' ? '—' : v) + '</b>')));
  return d;
}

/* ---------------- header: search + stock card ---------------- */
function syncDayunLink() {
  const href = '/dayun?dataset=' + encodeURIComponent(S.dataset) +
    (S.id ? '&id=' + encodeURIComponent(S.id) : '') +
    '&tab=' + encodeURIComponent(currentTab()) +
    (S.fromBoard ? '&from=board' : '');
  ['#dayun-link', '#dayun-tab'].forEach((sel) => {
    const a = $(sel);
    if (a) a.href = href;
  });
}

/* ---------------- 视图状态：URL + history.state + sessionStorage ----------------
   URL 只放 dataset/id/tab/#board/from=board，便于刷新与浏览器前进后退；
   所有筛选控件（个股页签 + 机会表）进入 history.state 和当前标签页的 sessionStorage，
   后退时按历史条目恢复，刷新时按会话快照恢复。 */
/* 个股档案子页（改版 v2）。旧页签名继续认：stock→总览，branch/pillar→按流月看（对应粒度），meta→档案与来源，
   这样旧书签、大运页的返回链接、机会表抽屉里的「进入个股」都不用改。 */
const SUB_TABS = ['overview', 'flow', 'relation', 'tengod', 'dayun', 'lowpoint', 'archive'];
const TAB_ALIAS = { stock: 'overview', branch: 'flow', pillar: 'flow', meta: 'archive' };
const TAB_PANES = { overview: ['tab-overview'], relation: ['tab-relation'], tengod: ['tab-tengod'],
                    dayun: ['tab-dayun'], lowpoint: ['tab-lowpoint'], archive: ['tab-stock', 'tab-meta'] };
const MAIN_TABS = new Set(SUB_TABS.concat(Object.keys(TAB_ALIAS)));
const FILTER_IDS = [
  'b-pillar', 'b-stem-el', 'b-branch-el', 'b-sort', 'b-min', 'b-exclude-single',
  'p-search', 'p-stem-el', 'p-branch-el', 'p-dir', 'p-rate', 'p-sort', 'p-min', 'p-tg',
  'f-clash', 'f-harmony', 'f-both', 'f-punish', 'f-harm', 'f-broken',
  'o-month', 'o-min', 'o-grade', 'o-dir', 'o-stab', 'o-cons', 'o-rate', 'o-both-months',
  'o-next-rate', 'o-sort', 'o-cap', 'o-cap-tier', 'o-es', 'o-clash', 'o-harmony', 'o-both',
  'o-punish', 'o-harm', 'o-broken', 'o-rel', 'o-delist', 'o-idx', 'o-search'
];
const NAV = { restoring: false, ready: false, timer: null, seq: 0, pendingDs: null };

/* 地址栏里「按流月看」写成 branch / pillar，刷新和前进后退能回到同一个粒度 */
function currentTab() {
  const b = document.querySelector('.tabs button.on');
  const t = b && SUB_TABS.includes(b.dataset.tab) ? b.dataset.tab : 'overview';
  return t === 'flow' ? (S.flowGrain === 'branch' ? 'branch' : 'pillar') : t;
}

function filterStoreKey(ds) { return 'bazi_ui_filters_v1_' + (ds || 'ashare'); }

function snapshotFilters() {
  const out = { relKind: S.relKind, tgMode: S.tgMode, tgBranch: S.tgBranch, tgRel: S.tgRel,
                oMsort: oppMsortClean(S.oppMsort), oppPage: S.oppPage || 1,
                pillarLayer: S.pillarLayer || 'month',
                pillarView: S.pillarView || 'stock',
                flowGrain: S.flowGrain === 'branch' ? 'branch' : 'pillar',
                oppLayer: S.oppLayer || 'month',
                oppWindowNo: S.oppWindowNo || '' };
  FILTER_IDS.forEach((id) => {
    const e = document.getElementById(id);
    if (!e) return;
    out[id] = e.type === 'checkbox' ? (e.checked ? 1 : 0) : e.value;
  });
  return out;
}

function saveFiltersNow() {
  const f = snapshotFilters();
  try { sessionStorage.setItem(filterStoreKey(S.dataset), JSON.stringify(f)); } catch (e) {}
  if (history.state && history.state.appNav) {
    try { history.replaceState(Object.assign({}, history.state, { filters: f }), '', location.href); } catch (e) {}
  }
}

const scheduleFiltersSave = () => {
  clearTimeout(NAV.timer);
  NAV.timer = setTimeout(saveFiltersNow, 250);
};

function restoreFilters(f) {
  if (!f || typeof f !== 'object') return;
  FILTER_IDS.forEach((id) => {
    const e = document.getElementById(id);
    if (!e || f[id] === undefined || f[id] === null) return;
    if (e.type === 'checkbox') e.checked = !!f[id];
    else e.value = String(f[id]);
  });
  S.relKind = f.relKind === 'CANDIDATE' ? 'CANDIDATE' : 'OFFICIAL';
  syncSegUi('#r-kind', S.relKind);
  S.tgMode = f.tgMode === 'EXCLUDE_SINGLE' ? 'EXCLUDE_SINGLE' : 'ALL';
  S.tgBranch = f.tgBranch || '';
  S.tgRel = f.tgRel || '';
  S.oppMsort = oppMsortClean(f.oMsort);
  S.oppPage = Math.max(1, parseInt(f.oppPage, 10) || 1);
  syncSegUi('#tg-mode', S.tgMode);
  S.branchPolicy = $('#b-exclude-single').checked ? 'EXCLUDE_SINGLE' : 'ALL';
  if (f.pillarLayer) {
    S.pillarLayer = f.pillarLayer;
    const pSeg = $('#p-layer-seg');
    if (pSeg) {
      pSeg.querySelectorAll('button').forEach((b) => {
        const on = b.dataset.v === S.pillarLayer;
        b.classList.toggle('on', on);
        b.classList.toggle('bg-indigo-600', on);
        b.classList.toggle('text-white', on);
        b.classList.toggle('shadow-sm', on);
        b.classList.toggle('text-slate-400', !on);
      });
      const notes = {
        month: '【月层】60 柱整月规律；本票视图取冻结 stats_pillar60（全部正式月）。',
        xun: '【旬层】60 柱 × 上/中/下旬 = 180 窗；本票视图取 xw_xun_rules（只收完整流月）。',
        week: '【段层】60 柱 × 6 个节气段 = 360 窗；本票视图取 xw_week_rules（只收完整流月）。'
      };
      if ($('#p-layer-note')) $('#p-layer-note').textContent = notes[S.pillarLayer] || '';
    }
  }
  S.pillarView = f.pillarView === 'pool' ? 'pool' : 'stock';
  syncSegUi('#p-view-seg', S.pillarView);
  S.flowGrain = f.flowGrain === 'branch' ? 'branch' : 'pillar';
  if (f.oppLayer) {
    S.oppLayer = f.oppLayer;
    S.oppWindowNo = f.oppWindowNo || '';
    const oSeg = $('#o-layer-seg');
    if (oSeg) {
      oSeg.querySelectorAll('button').forEach((b) => {
        const on = b.dataset.v === S.oppLayer;
        b.classList.toggle('on', on);
        b.classList.toggle('bg-indigo-600', on);
        b.classList.toggle('text-white', on);
        b.classList.toggle('shadow-sm', on);
        b.classList.toggle('text-slate-400', !on);
      });
      const xPills = $('#o-xun-pills');
      const wPills = $('#o-week-pills');
      if (xPills) {
        xPills.classList.toggle('hidden', S.oppLayer !== 'xun');
        xPills.classList.toggle('flex', S.oppLayer === 'xun');
        xPills.querySelectorAll('button').forEach((b) => {
          const on = (b.dataset.w || '') === S.oppWindowNo;
          b.classList.toggle('on', on);
          b.classList.toggle('bg-indigo-600', on);
          b.classList.toggle('text-white', on);
          b.classList.toggle('shadow-sm', on);
          b.classList.toggle('text-slate-400', !on);
        });
      }
      if (wPills) {
        wPills.classList.toggle('hidden', S.oppLayer !== 'week');
        wPills.classList.toggle('flex', S.oppLayer === 'week');
        wPills.querySelectorAll('button').forEach((b) => {
          const on = (b.dataset.w || '') === S.oppWindowNo;
          b.classList.toggle('on', on);
          b.classList.toggle('bg-indigo-600', on);
          b.classList.toggle('text-white', on);
          b.classList.toggle('shadow-sm', on);
          b.classList.toggle('text-slate-400', !on);
        });
      }
      const notes = {
        month: '【月层】整月历史规律；展示所选流月的全市场机会标的。',
        xun: '【旬层】所选流月 × 上/中/下旬 3 个十日窗口机会排行；可按窗位聚焦查看。',
        week: '【段层】所选流月 × 6 个节气段窗口机会排行；可按段位聚焦查看。'
      };
      if ($('#o-layer-note')) $('#o-layer-note').textContent = notes[S.oppLayer] || '';
      const bothWrap = $('#o-both-months') ? $('#o-both-months').closest('label') : null;
      if (S.oppLayer !== 'month') {
        if (bothWrap) bothWrap.classList.add('hidden');
        if ($('#o-stab')) $('#o-stab').classList.add('hidden');
        if ($('#o-cons')) $('#o-cons').classList.add('hidden');
      } else {
        if (bothWrap) bothWrap.classList.remove('hidden');
        if ($('#o-stab')) $('#o-stab').classList.remove('hidden');
        if ($('#o-cons')) $('#o-cons').classList.remove('hidden');
      }
    }
  }
  syncPillarDirUi();
  syncDirUi();
  syncNextRateUi();
  syncOppSortUi();
  // 恢复的筛选就是当前筛选：首次渲染不能再把页码重置回第 1 页
  S.oppFilterSig = JSON.stringify(oppFilter());
}

function savedFilters(ds) {
  try {
    const raw = sessionStorage.getItem(filterStoreKey(ds));
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}

function syncNextRateUi() {
  const both = $('#o-both-months'), next = $('#o-next-rate');
  if (both && next) next.classList.toggle('hidden', !both.checked);
}

function syncReturnBar() {
  const bar = $('#return-board-bar');
  if (!bar) return;
  bar.classList.toggle('hidden', !S.fromBoard || document.body.classList.contains('board'));
}

function resetBoardFilters() {
  const set = (id, v) => {
    const e = document.getElementById(id);
    if (!e) return;
    if (e.type === 'checkbox') e.checked = !!v; else e.value = v;
  };
  set('o-month', '0'); set('o-min', '3'); set('o-grade', ''); set('o-dir', 'up');
  set('o-stab', ''); set('o-cons', ''); set('o-rate', 'ge80'); set('o-both-months', false);
  set('o-next-rate', 'inherit'); set('o-sort', OPP_DEFAULT_SORT); set('o-cap', '100'); set('o-cap-tier', '');
  set('o-es', false); set('o-clash', false); set('o-harmony', false); set('o-both', false);
  set('o-punish', false); set('o-harm', false); set('o-broken', false);
  set('o-rel', ''); set('o-delist', false); set('o-idx', false); set('o-search', '');
  S.oppPage = 1;
  syncNextRateUi();
}

function readUrlState() {
  const p = new URLSearchParams(location.search);
  return {
    dataset: p.get('dataset') === 'etf' ? 'etf' : 'ashare',
    id: p.get('id') || '',
    tab: MAIN_TABS.has(p.get('tab')) ? p.get('tab') : 'overview',
    // 首页是流月机会表：地址栏没指定标的 / 页签时默认进机会表
    board: location.hash === '#board' || (!p.get('id') && !p.get('tab') && p.get('from') !== 'board'),
    fromBoard: p.get('from') === 'board',
  };
}

function navUrl(ns) {
  const p = new URLSearchParams();
  p.set('dataset', ns.dataset || S.dataset || 'ashare');
  if (ns.id) p.set('id', ns.id);
  const tab = MAIN_TABS.has(ns.tab) ? ns.tab : 'overview';
  if (tab !== 'overview') p.set('tab', tab);
  if (ns.fromBoard) p.set('from', 'board');
  return location.pathname + '?' + p.toString() + (ns.board ? '#board' : '');
}

function navState(partial) {
  return Object.assign({
    dataset: S.dataset,
    id: S.id || '',
    tab: currentTab(),
    board: document.body.classList.contains('board'),
    fromBoard: !!S.fromBoard,
  }, partial || {});
}

function applyUrl(ns, mode) {
  if (NAV.restoring) return;
  const url = navUrl(ns);
  const cur = location.pathname + location.search + location.hash;
  const state = { appNav: true, filters: snapshotFilters() };
  if (mode === 'push' && url !== cur) history.pushState(state, '', url);
  else history.replaceState(state, '', url);
}

const pushView = (partial) => applyUrl(navState(partial), 'push');
const replaceView = (partial) => applyUrl(navState(partial), 'replace');

// 窗口层已统一改称「段」；规则库里存的 week_name 仍是「上半周/下半周」，只在显示时映射
const asSegment = (s) => String(s == null ? '' : s).replace(/周/g, '段');

// 抽屉里「极值时序」的 ✅/⚠️ 与月内低点页同一套展示门槛
const GATE_TIP_UI = '同向占比 ≥60% 标 ✅，不到标 ⚠️；60% 是本页展示门槛，不是统计显著性。';

function arrowStats(hist, isUp) {
  const rows = (hist || []).filter((h) => ((h.dir === 'UP') === isUp) && h.n > 0 && h.lk >= 1 && h.hk >= 1);
  if (!rows.length) return null;
  const N = rows.length;
  const pc = (a) => Math.round((a / N) * 100);
  const edge1 = (h) => Math.ceil(h.n / 3);
  const edge2 = (h) => Math.ceil((h.n * 2) / 3);
  const lowFirst = rows.filter((h) => h.lk <= edge1(h)).length;
  const highLast = rows.filter((h) => h.hk > edge2(h)).length;
  const ordered = rows.filter((h) => (isUp ? h.lk < h.hk : h.hk < h.lk)).length;
  const meanLow = rows.reduce((a, h) => a + h.lk, 0) / N;
  const meanHigh = rows.reduce((a, h) => a + h.hk, 0) / N;
  const span = meanHigh - meanLow;
  const edges = rows.map(edge1);
  const eMin = Math.min.apply(null, edges), eMax = Math.max.apply(null, edges);
  const rangeTxt = eMin === eMax ? ('第 1~' + eMax + ' 日') : ('第 1~' + eMin + ' 日，长窗到第 ' + eMax + ' 日');
  return { N, pc, lowFirst, highLast, ordered, meanLow, meanHigh, span, rangeTxt };
}

function windowPatternCard(d, hist, isUp, layer) {
  const c = arrowStats(hist, isUp);
  if (!c) return '';
  const { N, pc, lowFirst, highLast, ordered, meanLow, meanHigh, span, rangeTxt } = c;
  const icon = (ok) => '<span title="' + GATE_TIP_UI + '">' + (ok ? '✅' : '⚠️') + '</span>';
  const head = (isUp ? '涨' : '跌') + (layer === 'month' ? '月' : '窗') + '极值时序';
  const one = N === 1;
  const body = one
    ? '<div>• 只有 1 次，谈不上分布。</div>'
    : '<div>• 最低点：' + icon(pc(lowFirst) >= 60) + ' <b>' + pc(lowFirst) + '%</b> 落在前 1/3（' + rangeTxt + '），平均第 <b>' + meanLow.toFixed(1) + '</b> 日</div>'
      + '<div>• 最高点：' + icon(pc(highLast) >= 60) + ' <b>' + pc(highLast) + '%</b> 落在末 1/3，平均第 <b>' + meanHigh.toFixed(1) + '</b> 日</div>'
      + '<div>• 单边性：' + (isUp ? '先低后高' : '先高后低') + '占 <b>' + pc(ordered) + '%</b>（' + ordered + '/' + N + '），'
      + (span >= 0 ? '平均跨度 <b>' + span.toFixed(1) + '</b> 日' : '高点平均比低点早 <b>' + Math.abs(span).toFixed(1) + '</b> 日') + '</div>';
  return '<div class="rounded-xl theme-card border border-slate-700/60 p-3 mt-2">'
    + '<div class="flex items-center justify-between gap-2 mb-1">'
    + '<div class="text-xs font-bold text-slate-200">' + head + ' · <span class="text-indigo-400 font-mono">' + esc(d.win_name || '') + '</span></div>'
    + '<span class="text-[10px] text-slate-500">' + N + ' 次 · 按日线重算</span></div>'
    + '<div class="text-[11px] text-slate-300 space-y-1 leading-relaxed">' + body + '</div>'
    + (one ? '' : '<div class="text-[10px] text-slate-500 pt-1 mt-1 border-t border-slate-800/60">✅ / ⚠️：占比 ≥60% 标 ✅，不到标 ⚠️（本页门槛，非显著性）。</div>')
    + '</div>';
}

function activateTab(name, opts = {}) {
  if (!NAV.ready && !opts.force) return;
  if (name === 'branch' || name === 'pillar') S.flowGrain = name;
  name = TAB_ALIAS[name] || name;
  if (!SUB_TABS.includes(name)) name = 'overview';
  const b = document.querySelector('.tabs button[data-tab="' + name + '"]');
  if (!b) return;
  document.querySelectorAll('.tabs button').forEach((x) => x.classList.remove('on'));
  document.querySelectorAll('.tab').forEach((x) => x.classList.remove('on'));
  b.classList.add('on');
  const panes = name === 'flow'
    ? ['tab-flowbar', S.flowGrain === 'branch' ? 'tab-branch' : 'tab-pillar'] : TAB_PANES[name];
  panes.forEach((id) => { const pane = $('#' + id); if (pane) pane.classList.add('on'); });
  syncFlowGrainUi();
  if (name === 'archive') renderMeta();
  if (name === 'tengod') renderTenGodStats();
  if (name === 'overview') renderOverview();
  if (name === 'dayun') syncDayunFrame();
  if (name === 'lowpoint' && window.onV5TabActive) window.onV5TabActive(S.id);
  syncDayunLink();
  if (!opts.noUrl) pushView({ tab: currentTab() });
}

/* 按流月看：粒度开关（12 地支 = 原页面二，60 柱 = 原页面三） */
function syncFlowGrainUi() {
  const grain = S.flowGrain === 'branch' ? 'branch' : 'pillar';
  syncSegUi('#flow-grain-seg', grain);
  const note = $('#flow-grain-note');
  if (note) {
    note.textContent = grain === 'branch'
      ? '12 地支：只看流月地支，每年都出现一次，样本更多；只有整月层。'
      : '60 柱：干支完整的流月柱，每 60 个月（约 5 年）出现一次；可切整月 / 旬 / 段层和全市场池。';
  }
}

async function applyHistoryState(st, histState) {
  const my = ++NAV.seq;
  NAV.restoring = true;
  try {
    const f = (histState && histState.filters) || savedFilters(st.dataset);
    if (st.dataset !== S.dataset) {
      await switchDataset(st.dataset, true, {
        fromHistory: true,
        id: st.id || null,
        tab: st.tab,
        board: st.board,
        fromBoard: st.fromBoard,
        filters: f,
      });
      return;
    }
    S.fromBoard = st.fromBoard;
    const before = JSON.stringify(snapshotFilters());
    restoreFilters(f);
    if (S.cal) {
      if (!mpSelected()) $('#o-month').value = '0';
      mpSync();
    }
    const filtersChanged = JSON.stringify(snapshotFilters()) !== before;
    let reloaded = false;
    if (st.id && st.id !== S.id) {
      await loadStock(st.id, { syncQ: true });
      if (my !== NAV.seq) return;
      reloaded = true;
    } else if (!st.id) {
      const def = (S.meta && S.meta.default_identity) || '000001.SH';
      if (def !== S.id) {
        await loadStock(def, { syncQ: true });
        if (my !== NAV.seq) return;
        reloaded = true;
      }
    }
    if (!reloaded && filtersChanged) {
      await Promise.all([renderBranch(), renderPillar(), renderRelation()]);
    }
    if (st.id && S.stock && S.stock.security && S.stock.security.security_code) {
      $('#q').value = S.stock.security.security_code;
    }
    activateTab(st.tab, { noUrl: true });
    setBoardMode(st.board, { syncUrl: false, render: false });
    if (st.board) await renderOpportunity();
    syncReturnBar();
  } finally {
    if (my === NAV.seq) NAV.restoring = false;
  }
}

let reqSeq = 0;
async function loadStock(id, opts = {}) {
  const my = ++reqSeq;
  const ep = S.epoch;
  const j = await get('/api/stock', { id });
  if (my !== reqSeq || ep !== S.epoch) return;   // a newer selection/dataset superseded this
  S.id = id; S.stock = j; S.selBranch = null;
  syncDayunLink();
  // 当前/下一流月的十神必须绑定该身份的日主，因此随身份重新取一次冻结历法
  const cal = await get('/api/calendar_now', { id });
  if (my !== reqSeq || ep !== S.epoch) return;
  S.cal = cal;
  renderStockHead();
  renderStockCard();
  renderTenGodNow();
  updateSingleControl();
  await Promise.all([renderStock(), renderBranch(), renderPillar(), renderRelation()]);
  const activeTab = document.querySelector('.tabs button.on');
  if (activeTab && activeTab.dataset.tab === 'tengod') await renderTenGodStats();
  if (activeTab && activeTab.dataset.tab === 'overview') await renderOverview();
  if (activeTab && activeTab.dataset.tab === 'dayun') syncDayunFrame();
  const code = (j.security && j.security.security_code) || '';
  // 机会表里搜索框保持空白，免得看起来像选中了某只票
  if (code && (opts.syncQ || opts.navigate) && !document.body.classList.contains('board')) $('#q').value = code;
  if (opts.navigate) {
    S.fromBoard = !!opts.fromBoard;
    syncReturnBar();
    pushView({ id, tab: currentTab(), fromBoard: S.fromBoard });
  }
}

const stemEl = (s) => ({ '甲':'木', '乙':'木', '丙':'火', '丁':'火', '戊':'土', '己':'土', '庚':'金', '辛':'金', '壬':'水', '癸':'水' }[s] || '—');
const branchEl = (b) => ({ '寅':'木', '卯':'木', '巳':'火', '午':'火', '申':'金', '酉':'金', '亥':'水', '子':'水', '辰':'土', '戌':'土', '丑':'土', '未':'土' }[b] || '—');
const elCls = (el) => {
  if (!el) return '';
  if (el.includes('木')) return 'el-wood';
  if (el.includes('火')) return 'el-fire';
  if (el.includes('土')) return 'el-earth';
  if (el.includes('金')) return 'el-metal';
  if (el.includes('水')) return 'el-water';
  return '';
};

function renderStockCard() {
  const box = $('#stockcard'); box.innerHTML = '';
  if (!S.stock) return;
  box.setAttribute('data-symbol', S.id || '');
  box.dataset.symbol = S.id || '';
  if (window.onV5StockChange) window.onV5StockChange(S.id);
  const s = S.stock.security, p = S.stock.profile || {};
  const card = el('div', 'relative overflow-hidden rounded-xl theme-card border p-3.5 sm:p-4 shadow-md');
  const hasProf = p.profile_present === 1;
  const initialChar = (s.name || s.security_code || '票')[0];

  const pillarsHtml = hasProf
    ? `
      <!-- 年柱 -->
      <div class="w-20 sm:w-22 md:w-24 p-2 sm:p-2.5 rounded-xl theme-card-sub border text-center hover:border-indigo-500 transition-all shrink-0">
        <span class="text-[11px] text-slate-400 block mb-1">年柱 · 根基</span>
        <div class="text-base sm:text-lg font-bold tracking-wider flex justify-center gap-1">
          <span class="${elCls(p.year_stem_element)} px-1 rounded">${p.natal_year_pillar ? p.natal_year_pillar[0] : '—'}</span>
          <span class="${elCls(p.year_branch_element)} px-1 rounded">${p.natal_year_pillar ? p.natal_year_pillar[1] : '—'}</span>
        </div>
        <span class="text-[10px] text-slate-400 block mt-1">${esc(p.year_stem_element || '—')}/${esc(p.year_branch_element || '—')}</span>
      </div>

      <!-- 月柱 -->
      <div class="w-20 sm:w-22 md:w-24 p-2 sm:p-2.5 rounded-xl theme-card-sub border text-center hover:border-indigo-500 transition-all shrink-0">
        <span class="text-[11px] text-slate-400 block mb-1">月柱 · 提纲</span>
        <div class="text-base sm:text-lg font-bold tracking-wider flex justify-center gap-1">
          <span class="${elCls(p.month_stem_element)} px-1 rounded">${p.natal_month_pillar ? p.natal_month_pillar[0] : '—'}</span>
          <span class="${elCls(p.month_branch_element)} px-1 rounded">${p.natal_month_pillar ? p.natal_month_pillar[1] : '—'}</span>
        </div>
        <span class="text-[10px] text-slate-400 block mt-1">${esc(p.month_stem_element || '—')}/${esc(p.month_branch_element || '—')}</span>
      </div>

      <!-- 日柱 (日主加冕核心) -->
      <div class="w-22 sm:w-24 md:w-26 p-2 sm:p-2.5 rounded-xl bg-gradient-to-b from-amber-500/10 to-transparent border-2 border-amber-500/60 text-center relative shadow-lg shrink-0 mt-1">
        <div class="absolute -top-3 left-1/2 -translate-x-1/2 px-2.5 py-0.5 rounded-full bg-gradient-to-r from-red-600 to-amber-600 text-white text-[9px] font-bold uppercase tracking-wider shadow whitespace-nowrap z-10">
          日主
        </div>
        <span class="text-[11px] text-amber-500 font-semibold block mb-1 mt-0.5">日柱 · 主体</span>
        <div class="text-base sm:text-lg font-bold tracking-wider flex justify-center items-center gap-1">
          <span class="${elCls(p.day_stem_element)} px-1.5 py-0.5 rounded shadow-sm">${p.natal_day_pillar ? p.natal_day_pillar[0] : '—'}</span>
          <span class="${elCls(p.day_branch_element)} px-1.5 py-0.5 rounded shadow-sm">${p.natal_day_pillar ? p.natal_day_pillar[1] : '—'}</span>
        </div>
        <span class="text-[10px] text-amber-500/90 block mt-1">${esc(p.day_stem_element || '—')}/${esc(p.day_branch_element || '—')}</span>
      </div>

      <!-- 时柱 (档案记载时柱 / 四柱一行严禁折行) -->
      ${p.archive_hour_pillar ? `
      <div class="w-20 sm:w-22 md:w-24 p-2 sm:p-2.5 rounded-xl theme-card-sub border text-center hover:border-indigo-500 transition-all shrink-0">
        <span class="text-[11px] text-slate-400 block mb-1">时柱 · 归宿</span>
        <div class="text-base sm:text-lg font-bold tracking-wider flex justify-center gap-1">
          <span class="${elCls(stemEl(p.archive_hour_pillar[0]))} px-1 rounded">${p.archive_hour_pillar[0]}</span>
          <span class="${elCls(branchEl(p.archive_hour_pillar[1]))} px-1 rounded">${p.archive_hour_pillar[1]}</span>
        </div>
        <span class="text-[10px] text-slate-400 block mt-1">${stemEl(p.archive_hour_pillar[0])}/${branchEl(p.archive_hour_pillar[1])}</span>
      </div>
      ` : `
      <div class="w-20 sm:w-22 md:w-24 p-2 sm:p-2.5 rounded-xl theme-card-sub border border-dashed border-slate-700/80 text-center opacity-70 shrink-0">
        <span class="text-[11px] text-slate-500 block mb-1">时柱 · 归宿</span>
        <div class="text-base sm:text-lg font-bold tracking-wider flex justify-center gap-1 text-slate-500">
          <span>—</span>
          <span>—</span>
        </div>
        <span class="text-[10px] text-slate-500 block mt-1">缺档案</span>
      </div>
      `}
    `
    : `<div class="p-4 rounded-xl theme-card-sub border text-amber-400 text-xs">
        冻结档案缺该身份三柱，冲合关系不可计算；客观行情统计仍照常展示。
      </div>`;

  let etfStrip = '';
  if (S.dataset === 'etf') {
    const fm = S.stock.fund_meta || {};
    const polTxt = fm.polarity
      ? fm.polarity + (fm.gender ? ' → ' + fm.gender : '')
      : '空（未赋值）';
    const firstTxt = (fm.first_bar_yinyang || '—') + '；' + polTxt;
    etfStrip = `
      <div class="mt-3 pt-3 border-t border-slate-700/60 text-xs text-slate-400 flex flex-wrap gap-x-4 gap-y-1">
        <span>基金类型：<b class="text-slate-200">${esc(fm.fund_type || '—')}</b></span>
        <span>行业主题：<b class="text-slate-200">${esc(fm.industry_theme || '—')}</b></span>
        <span>跟踪指数：<b class="text-slate-200">${esc(fm.tracking_index || '—')}</b></span>
        <span>管理人：<b class="text-slate-200">${esc(fm.manager || '—')}</b></span>
        <span>首根K线：<b class="text-slate-200">${esc(fm.first_bar_date || '—')}</b></span>
        <span>首日K线：<b class="text-slate-200">${esc(firstTxt)}</b></span>
        <span>tushare 上市日：<b class="text-slate-200">${esc(fm.tushare_list_date || '—')}</b></span>
      </div>
    `;
  }

  card.innerHTML = `
    <div class="flex flex-col xl:flex-row xl:items-center justify-between gap-4 relative z-10">
      <!-- 左侧：基本面与历史构成 -->
      <div class="flex items-start gap-4 min-w-0">
        <div class="w-12 h-12 rounded-xl bg-gradient-to-br from-red-600 to-amber-600 flex items-center justify-center text-xl font-bold text-white shadow-md flex-shrink-0">
          ${esc(initialChar)}
        </div>
        <div>
          <div class="flex items-center gap-2.5 flex-wrap">
            <h2 class="text-xl font-bold tracking-tight">${esc(s.name || s.security_code)}</h2>
            <span class="tabular-num text-xs font-mono px-2 py-0.5 rounded border border-slate-600 font-semibold">${esc(s.security_code)}</span>
            <span class="text-xs px-2 py-0.5 rounded-full bg-emerald-500/10 border border-emerald-500/30 text-emerald-500 font-medium">${esc(s.exchange || '')}</span>
            <span class="text-xs px-2 py-0.5 rounded-full bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 font-medium">${esc(typeLabel(s.security_type))}</span>
            ${s.formal_month_count === 0 ? '<span class="text-xs px-2 py-0.5 rounded-full bg-amber-500/10 border border-amber-500/30 text-amber-500">暂无正式月</span>' : ''}
            ${hasProf ? '' : '<span class="text-xs px-2 py-0.5 rounded-full bg-red-500/10 border border-red-500/30 text-red-400">无命盘</span>'}
          </div>
          <div class="text-xs text-slate-400 mt-2 flex flex-wrap items-center gap-y-1 gap-x-4">
            <span>上市首日：<b class="text-slate-200">${esc(s.listed_date || '—')}</b></span>
            <span>退市：<b class="text-slate-400">${esc(s.delisted_date || '—（存续正常）')}</b></span>
            <span>正式历史月：<b class="text-amber-500 tabular-num font-bold">${s.formal_month_count}</b></span>
            <span>无争议月：<b class="c-up tabular-num font-bold">${S.stock.month_totals.undisputed}</b></span>
            <span>交节争议月：<b class="c-down tabular-num font-bold">${S.stock.month_totals.disputed}</b></span>
            <span>缺档案月：<b class="text-slate-400 tabular-num">${S.stock.month_totals.no_profile}</b></span>
            ${S.stock.cap ? `
              <span title="按 ${esc(S.stock.cap.month_id)} 月末快照，不复权收盘 × 自由流通股本">自由流通市值：<b class="text-slate-200 tabular-num font-bold">${yiText(S.stock.cap.free_mv_yi)}</b></span>
              <span title="阈值 100/300/500/1000 亿，下界含、上界不含">市值档：<span class="px-1.5 py-0.5 rounded text-[10px] bg-slate-500/10 border border-slate-500/30 text-slate-300 font-medium">${esc(S.stock.cap.band)}</span></span>
              <span title="20 个市场交易日日均成交额，停牌日按 0">20日均额：<b class="text-slate-200 tabular-num">${yiText(S.stock.cap.avg_amt_20d_yi)}</b></span>
            ` : ''}
          </div>
        </div>
      </div>

      <!-- 中间：四柱立式牌匾（严格一行 flex-nowrap shrink-0，预留顶部内边距防日元胶囊截断） -->
      <div class="flex items-center gap-2 sm:gap-2.5 flex-nowrap shrink-0 overflow-x-auto pt-4 pb-2 px-1">
        ${pillarsHtml}
      </div>

      <!-- 右侧：档案与时柱说明 -->
      <div class="border-l border-slate-700/60 pl-4 text-xs text-slate-400 space-y-1 max-w-xs shrink-0">
        <div class="flex items-center gap-1.5 text-amber-500 font-semibold">
          <span>📜 冻结档案状态</span>
        </div>
        <div class="text-[11px] text-slate-400 leading-relaxed">
          • 档案记载时柱：<b class="text-slate-200">${esc(p.archive_hour_pillar || '无记载')}</b>（本轮不参与判定）<br>
          • 阴阳属性：<b class="text-slate-200">${p.archive_polarity ? esc(p.archive_polarity) + '/' + esc(p.archive_gender || '—') : '—'}</b>（按档案记账）<br>
          • 来源：<span class="text-slate-200">完整八字_20261016</span>
        </div>
      </div>
    </div>
    ${etfStrip}
  `;
  box.appendChild(card);
}

/* 紧凑头部：名称、四柱、日主、关键计数、当前流月十神。完整档案卡（五行、冻结档案状态、20 日均额、
   ETF 基金信息）在「档案与来源」里，这里不重复。 */
function renderStockHead() {
  const box = $('#stockhead');
  if (!box) return;
  if (!S.stock) { box.innerHTML = ''; return; }
  const s = S.stock.security, p = S.stock.profile || {}, mt = S.stock.month_totals || {};
  const hasProf = p.profile_present === 1;
  const num = (x) => (x === null || x === undefined ? '—' : x);
  const gz = (label, pil, cls, stemE, branchE) => (!pil ? '' :
    '<div class="rv-gz ' + cls + '"><b><span class="' + elCls(stemE) + '">' + esc(pil[0]) + '</span>' +
    '<span class="' + elCls(branchE) + '">' + esc(pil[1]) + '</span></b><span>' + label + '</span></div>');
  const pillars = hasProf ? [
    gz('年', p.natal_year_pillar, '', p.year_stem_element, p.year_branch_element),
    gz('月', p.natal_month_pillar, '', p.month_stem_element, p.month_branch_element),
    gz('日 · 日主' + esc((p.day_master_stem || '') + (p.day_master_element || '')), p.natal_day_pillar, 'dm',
       p.day_stem_element, p.day_branch_element),
    p.archive_hour_pillar ? gz('时 · 不参与', p.archive_hour_pillar, 'off',
       stemEl(p.archive_hour_pillar[0]), branchEl(p.archive_hour_pillar[1])) : '',
  ].join('') : '<span class="rv-tag warn">无命盘：冲合关系不可计算</span>';
  const cur = S.cal && S.cal.current;
  const win = S.cal && S.cal.current_window;
  const curTg = cur && cur.ten_god && cur.ten_god.stem && cur.ten_god.stem.ten_god;
  const nowChip = !cur ? '' : '<span class="rv-sh-chip">本月 <b>' + esc(cur.month_pillar) + '</b> · ' +
    esc(cur.month_id) + '（' + esc(cur.start_term) + '月）' +
    (curTg && S.cal.ten_god_status === 'OK' ? ' · 天干十神 <b>' + esc(curTg) + '</b>' : '') +
    (win ? ' · ' + esc(win.xun_name) + ' · ' + esc(asSegment(win.week_name)) : '') +
    (cur.days_until_end !== undefined && cur.days_until_end !== null ? ' · 距交节 ' + cur.days_until_end + ' 天' : '') +
    '</span>';
  const cap = S.stock.cap;
  box.innerHTML =
    '<div class="rv-sh-id">' +
      '<div class="rv-sh-name"><h2>' + esc(s.name || s.security_code) + '</h2>' +
        '<span class="rv-sh-code">' + esc(s.security_code) + '</span>' +
        '<span class="rv-tag">' + esc(s.exchange || '') + '</span>' +
        '<span class="rv-tag">' + esc(S.dataset === 'etf' ? 'ETF' : typeLabel(s.security_type)) + '</span>' +
        (cap ? '<span class="rv-tag" title="按 ' + esc(cap.month_id) + ' 月末快照的自由流通市值">' +
          esc(cap.band) + ' · ' + yiText(cap.free_mv_yi) + '</span>' : '') +
        (s.delisted_date ? '<span class="rv-tag warn">已退市 ' + esc(s.delisted_date) + '</span>' : '') +
        (s.formal_month_count === 0 ? '<span class="rv-tag warn">暂无正式月</span>' : '') +
      '</div>' +
      '<div class="rv-sh-meta">上市 <b>' + esc(s.listed_date || '—') + '</b> · 正式历史月 <b>' +
        num(s.formal_month_count) + '</b>（无争议 ' + num(mt.undisputed) + ' · 交节争议 ' + num(mt.disputed) +
        ' · 缺档案 ' + num(mt.no_profile) + '）</div>' +
    '</div>' +
    '<div class="rv-sh-pillars" aria-label="本命四柱">' + pillars + '</div>' +
    '<div class="rv-sh-now">' + nowChip +
      '<button type="button" class="rv-link" id="sh-goto-board">在机会表里看本月全市场 →</button></div>';
  const go = $('#sh-goto-board');
  if (go) go.onclick = () => setBoardMode(true, { push: true });
}

/* 总览：只汇总已有接口的数据（60 柱统计、十神统计、月内低点分组），不新增任何计算 */
async function renderOverview() {
  const box = $('#overviewbody');
  if (!box) return;
  if (!S.stock || !S.id || !S.cal) { box.innerHTML = ''; return; }
  const my = seqStart('overview');
  const ep = S.epoch;
  const id = S.id;
  const [p60, tgs, v5] = await Promise.all([
    get('/api/pillar60', { id }).catch(() => null),
    get('/api/ten_god_stats', { id, sample_mode: 'ALL' }).catch(() => null),
    fetch('/api/v5/groups?id=' + encodeURIComponent(id) + '&dimension=P60')
      .then((r) => (r.ok ? r.json() : null)).catch(() => null),
  ]);
  if (seqStale('overview', my) || ep !== S.epoch || id !== S.id) return;

  const byPillar = {};
  (Array.isArray(p60) ? p60 : []).forEach((r) => { byPillar[r.flow_pillar] = r; });
  const months = (S.cal.months || []).filter((m) => m.offset >= 0 && m.offset <= 5)
    .sort((a, b) => a.offset - b.offset);
  const curM = months.find((m) => m.offset === 0) || S.cal.current;
  const nextM = months.find((m) => m.offset === 1);
  const tgOf = (m) => (m && m.ten_god && m.ten_god.stem && m.ten_god.stem.ten_god) ||
    (m && byPillar[m.month_pillar] && byPillar[m.month_pillar].stem_ten_god) || '';
  const curTg = tgOf(curM), nextTg = tgOf(nextM);
  const cls = (x) => (x > 0 ? 'c-up' : x < 0 ? 'c-down' : '');
  const rels = (r) => { try { return JSON.parse(r.relation_types || '[]'); } catch (e) { return []; } };
  const udf = (r) => '<span class="c-up">' + r.up_n_ALL + '</span> / <span class="c-down">' + r.down_n_ALL + '</span> / ' + r.flat_n_ALL;

  // 本柱历史
  const pr = curM && byPillar[curM.month_pillar];
  const kv = (label, val) => '<div><i>' + label + '</i><b>' + val + '</b></div>';
  const curCard = '<div class="rv-ov-card">' +
    '<div class="rv-ov-head"><h3>本柱历史 · ' + esc(curM ? curM.month_pillar : '—') + '</h3>' +
      (pr ? '<button type="button" class="rv-link" data-locate="' + esc(pr.flow_pillar) + '">看历次明细 →</button>' : '') + '</div>' +
    (!pr ? '<p class="rv-note">该柱在本身份正式历史中没有出现过，不推断。</p>' :
      '<span class="rv-note">出现 ' + pr.occurrence_count + ' 次：' + esc((pr.occurrence_years || '').split(';').join(' / ')) +
        (pr.next_occurrence ? ' · 下次 ' + esc(pr.next_occurrence.month_id) : '') + '</span>' +
      '<div class="rv-kv">' +
        kv('涨 / 跌 / 平', udf(pr)) + kv('上涨占比', pct(pr.up_rate)) +
        kv('平均涨跌', '<span class="' + cls(pr.mean_body_return_ALL) + '">' + ret(pr.mean_body_return_ALL) + '</span>') +
        kv('中位涨跌', '<span class="' + cls(pr.median_body_return_ALL) + '">' + ret(pr.median_body_return_ALL) + '</span>') +
        kv('最差一次', '<span class="' + cls(pr.min_body_return_ALL) + '">' + ret(pr.min_body_return_ALL) + '</span>') +
        kv('最好一次', '<span class="' + cls(pr.max_body_return_ALL) + '">' + ret(pr.max_body_return_ALL) + '</span>') +
        kv('同期超额', '<span class="' + cls(pr.excess_mean) + '">' + ret(pr.excess_mean) + '</span>') +
        kv('样本门槛', esc(markerTxt(pr.sample_marker_ALL))) +
      '</div>' +
      '<div class="rv-chipset"><span class="rv-note">命中关系</span>' +
        (rels(pr).map((x) => '<span class="rel-chip">' + esc(x) + '</span>').join('') || '<span class="rv-note">（无）</span>') +
      '</div>') +
    '</div>';

  // 100% 极值流月（来自月内低点研究的分组；门槛同第八页）
  const v5rows = v5 && !v5.error && Array.isArray(v5.rows) ? v5.rows : null;
  const ext = (list, chipCls, word) => list.length
    ? list.map((r) => '<span class="' + chipCls + '">' + esc(r.group_key) + ' · ' + r.n_windows + ' 次' + word + '</span>').join('')
    : '<span class="rv-note">无</span>';
  const extCard = '<div class="rv-ov-card">' +
    '<div class="rv-ov-head"><h3>100% 极值流月</h3><button type="button" class="rv-link" data-goto="lowpoint">去月内低点 →</button></div>' +
    (!v5rows ? '<p class="rv-note">该标的暂无月内低点研究数据。</p>' :
      '<span class="rv-note">历史全涨</span><div class="rv-chipset">' + ext(v5rows.filter((r) => r.up_rate === 1), 'rv-up-chip', '全涨') + '</div>' +
      '<span class="rv-note">历史全跌</span><div class="rv-chipset">' + ext(v5rows.filter((r) => r.up_rate === 0), 'rv-dn-chip', '全跌') + '</div>' +
      '<p class="rv-note">样本只有几次；V5 结论是没有可交易的月内低点信号，这里只作历史描述。</p>') +
    '</div>';

  // 十神 10 类（流月天干十神，含单交易日月）
  const groups = tgs && Array.isArray(tgs.groups) ? tgs.groups : [];
  const totalN = groups.reduce((a, g) => a + (g.n || 0), 0);
  const tgCard = '<div class="rv-ov-card span2">' +
    '<div class="rv-ov-head"><h3>按流月天干十神 · 10 类</h3><span class="rv-note">含单交易日月 · 10 类合计 ' + totalN +
      ' 个有效月</span><button type="button" class="rv-link" data-goto="tengod">按十神看明细 →</button></div>' +
    (!groups.length ? '<p class="rv-note">该身份缺本命档案（或日主非法），按规则不计算十神。</p>' :
      '<table class="rv-tbl"><thead><tr><th>十神</th><th class="r">有效月</th><th class="r">涨</th><th class="r">跌</th>' +
      '<th>上涨占比</th><th class="r">平均涨跌</th></tr></thead><tbody>' +
      groups.map((g) => {
        const tag = g.ten_god === curTg ? '本月' : g.ten_god === nextTg ? '下月' : '';
        return '<tr class="' + (tag ? 'hl' : '') + '"><td><b>' + esc(g.ten_god) + '</b>' +
          (tag ? ' <span class="rv-tag">' + tag + '</span>' : '') + '</td>' +
          '<td class="r">' + g.n + '</td><td class="r c-up">' + g.up + '</td><td class="r c-down">' + g.down + '</td>' +
          '<td><span style="display:flex;align-items:center;gap:8px"><span class="rv-bar"><span style="width:' +
            ((g.up_rate || 0) * 100).toFixed(1) + '%"></span></span><span class="r" style="width:52px">' + pct(g.up_rate) + '</span></span></td>' +
          '<td class="r ' + cls(g.mean_return) + '">' + ret(g.mean_return) + '</td></tr>';
      }).join('') + '</tbody></table>') +
    '</div>';

  // 接下来 6 个流月：每格是该柱在这只票身上的历史表现
  const n6 = months.map((m) => {
    const r = byPillar[m.month_pillar];
    return '<button type="button" class="rv-n6' + (m.offset === 0 ? ' cur' : '') + '" data-locate="' + esc(m.month_pillar) + '">' +
      '<span class="m">' + esc(m.label) + ' · ' + esc(m.month_id) + '</span>' +
      '<span><span class="p">' + esc(m.month_pillar) + '</span><span class="tg">' + esc(tgOf(m)) + '</span></span>' +
      '<span class="m">' + esc(m.start_term) + '月 · ' + esc(date10(m.start_at).slice(5)) + ' 起</span>' +
      (r ? '<span>' + r.occurrence_count + ' 次 · ' + udf(r) + ' · ' + pct(r.up_rate) + '</span>' +
           '<span class="' + cls(r.mean_body_return_ALL) + '">平均 ' + ret(r.mean_body_return_ALL) + '</span>'
         : '<span class="rv-note">该柱无历史，不推断</span>') +
      '</button>';
  }).join('');
  const n6Card = '<div class="rv-ov-card span3">' +
    '<div class="rv-ov-head"><h3>接下来 6 个流月</h3><span class="rv-note">每格是该柱在这只' +
      (S.dataset === 'etf' ? ' ETF ' : '票') + '身上的历史表现，只看过去，不含未来行情；点一格看历次明细</span></div>' +
    '<div class="rv-next6">' + n6 + '</div></div>';

  box.innerHTML = '<div class="rv-ov">' +
    '<div class="rv-ov-col">' + curCard + extCard + '</div>' + tgCard + n6Card + '</div>';
  box.onclick = (e) => {
    const loc = e.target.closest('[data-locate]');
    if (loc) {
      $('#p-search').value = loc.dataset.locate;
      activateTab('pillar');
      renderPillar();
      saveFiltersNow();
      return;
    }
    const go = e.target.closest('[data-goto]');
    if (go) activateTab(go.dataset.goto);
  };
}

/* 大运子页：嵌入原独立页（embed=1 时它自己的顶栏和搜索不显示），跟随当前标的与主题；高度随内容自适应 */
function syncDayunTheme() {
  const f = $('#dayun-frame');
  try {
    const d = f && f.contentDocument;
    if (!d || !d.documentElement) return;
    const light = document.documentElement.classList.contains('light');
    d.documentElement.classList.toggle('light', light);
    d.documentElement.classList.toggle('dark', !light);
    if (d.body) d.body.classList.toggle('theme-light', light);
  } catch (e) { /* 跨域时拿不到，按它自己的主题显示 */ }
}

function syncDayunFrame() {
  const f = $('#dayun-frame');
  if (!f || !S.id) return;
  const src = '/dayun?dataset=' + encodeURIComponent(S.dataset) + '&id=' + encodeURIComponent(S.id) + '&embed=1';
  if (f.dataset.src === src) return;
  f.dataset.src = src;
  f.onload = () => {
    syncDayunTheme();
    try {
      const d = f.contentDocument;
      const fit = () => { f.style.height = Math.max(d.documentElement.scrollHeight, 400) + 'px'; };
      fit();
      if (f.rvResize) f.rvResize.disconnect();
      f.rvResize = new ResizeObserver(fit);
      f.rvResize.observe(d.body);
    } catch (e) { /* 拿不到内容高度就用默认高度 */ }
  };
  f.src = src;
}

/* 当前流月 / 下一流月十神：只展示命理标签与冻结历法日期，绝不显示未来行情。 */
function tgHiddenLine(tg) {
  const items = (tg && tg.hidden_stems) || [];
  if (!items.length) return '<span class="text-slate-500">—</span>';
  return items.map((h) => '<span class="whitespace-nowrap">' + esc(h.stem) + '·' +
    esc(h.ten_god) + '</span>').join('<span class="text-slate-600"> / </span>');
}

function renderTenGodNow() {
  const box = $('#tengodnow');
  if (!box) return;
  box.innerHTML = '';
  if (!S.stock || !S.cal || !S.cal.current) return;
  if (S.cal.ten_god_status !== 'OK') {
    box.appendChild(el('div',
      'rounded-xl border border-amber-500/30 bg-amber-500/10 text-amber-500 text-xs p-3',
      '暂无十神数据：该身份缺本命档案（或日主非法），按规则不猜测十神。'));
    return;
  }
  const cur = S.cal.current;
  const next = (S.cal.months || []).find((m) => m.offset === 1);
  const dm = S.cal.day_master || '—';
  const dmEl = stemEl(dm);
  const dmYinYang = ({ '甲':'阳','丙':'阳','戊':'阳','庚':'阳','壬':'阳','乙':'阴','丁':'阴','己':'阴','辛':'阴','癸':'阴' }[dm] || '');
  const dmDesc = dmYinYang ? `${dmEl} · ${dmYinYang}` : dmEl;

  const card = (title, m, isNext) => {
    if (!m) return '<div class="flex-1 min-w-[320px] p-4 rounded-xl theme-card-sub border text-slate-500 flex items-center justify-center">无历法数据</div>';
    const tg = m.ten_god || {};
    const stemGod = (tg.stem && tg.stem.ten_god) || '—';
    const pillar = m.month_pillar || '—';
    const pStem = pillar[0] || '—';
    const pBranch = pillar[1] || '—';
    const term = m.start_term || BRANCH_TERM[pBranch] || '节气';
    const startDate = (m.start_at || '').slice(0, 10);
    const endDate = (m.end_at || '').slice(0, 10);

    // 藏干芯片：一行紧凑排布三个标签，消除换行与空洞
    const hiddenChips = (tg.hidden_stems || []).map((h, idx, arr) => {
      const roleName = h.order === 1 ? '本气' : (arr.length === 3 && idx === 1 ? '中气' : '余气');
      const isPrimary = h.order === 1;
      const roleBadge = isPrimary
        ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 font-semibold'
        : 'bg-slate-800 text-slate-400 border-slate-700/80';
      const elColor = elCls(h.element || stemEl(h.stem));
      return `
        <div class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-slate-900/90 border border-slate-700/80 text-xs shadow-sm hover:border-slate-500 transition-colors whitespace-nowrap shrink-0">
          <span class="text-[9px] px-1 py-0.2 rounded border ${roleBadge}">${roleName}</span>
          <span class="${elColor} font-bold px-0.5 rounded text-xs font-mono">${esc(h.stem)}</span>
          <span class="text-slate-100 font-semibold text-xs">${esc(h.ten_god)}</span>
        </div>
      `;
    }).join('');

    const factTag = m.fact_status === 'NO_HISTORICAL_FACT'
      ? '<span class="px-1.5 py-0.5 rounded bg-slate-800 border border-slate-700 text-slate-400 text-[10px] whitespace-nowrap shrink-0">无完整整月事实</span>'
      : '<span class="px-1.5 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-[10px] whitespace-nowrap shrink-0">有历史事实</span>';

    const rightBadge = isNext
      ? '<span class="px-2 py-0.5 rounded-full bg-amber-500/10 border border-amber-500/30 text-amber-400 text-[10px] font-medium whitespace-nowrap shrink-0">未来历法 · 无未来行情</span>'
      : (m.days_until_end !== undefined ? `<span class="px-2 py-0.5 rounded-full bg-indigo-500/15 border border-indigo-500/30 text-indigo-300 text-[10px] font-mono whitespace-nowrap shrink-0">交节剩余 <b class="text-amber-400 font-bold">${m.days_until_end}</b> 天</span>` : '');

    const win = (!isNext && (m.window || S.cal?.current_window)) ? (m.window || S.cal?.current_window) : null;
    const winBadge = win ? `
      <div class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-lg bg-indigo-500/20 border border-indigo-400 text-xs text-indigo-200 font-semibold shadow-sm whitespace-nowrap shrink-0">
        <span class="text-amber-300">📍</span>
        <span>当下：</span>
        <span class="text-white font-bold">${esc(win.xun_name)}</span>
        <span class="text-indigo-400 font-mono">/</span>
        <span class="text-white font-bold">${esc(asSegment(win.week_name))}</span>
        <span class="text-indigo-300/90 font-mono text-[10px]">(${win.week_left_str || ''})</span>
      </div>
    ` : '';

    const weeksBar = (win && Array.isArray(win.weeks)) ? `
      <!-- 六段进程微条 (方案二) -->
      <div class="px-3 py-1 bg-slate-950/70 border-b border-slate-800/80 flex items-center gap-2 overflow-x-auto">
        <span class="text-[11px] text-slate-400 shrink-0 font-medium">六段进程:</span>
        <div class="grid grid-cols-6 gap-1 flex-1 min-w-[320px]">
          ${win.weeks.map(w => {
            if (w.is_current) {
              return `
                <div class="text-center py-0.5 px-1 rounded bg-indigo-600 text-white font-bold text-[10px] current-window-glow flex items-center justify-center gap-1 whitespace-nowrap shadow-sm" title="${asSegment(w.week_name)} (当下进行中 · ${w.day_from}–${w.day_to != null ? w.day_to + '日' : '下节'} · ${win.week_left_str || ''})">
                  <span class="pulse-dot"></span>
                  <span>${esc(asSegment(w.week_name))}当下</span>
                </div>
              `;
            } else if (w.is_passed) {
              return `
                <div class="text-center py-0.5 px-1 rounded bg-slate-800/60 text-slate-500 text-[10px] whitespace-nowrap" title="${asSegment(w.week_name)} (已过 · ${w.day_from}–${w.day_to}日)">
                  ${esc(asSegment(w.week_name))}已过
                </div>
              `;
            } else {
              return `
                <div class="text-center py-0.5 px-1 rounded bg-slate-900/60 border border-slate-800 text-slate-400 text-[10px] whitespace-nowrap" title="${asSegment(w.week_name)} (未到 · ${w.day_from}–${w.day_to != null ? w.day_to + '日' : '下节'})">
                  ${esc(asSegment(w.week_name))}未到
                </div>
              `;
            }
          }).join('')}
        </div>
      </div>
    ` : '';

    return `
      <div class="flex-1 rounded-xl theme-card-sub border border-slate-700/70 shadow-sm flex flex-col justify-start overflow-hidden">
        
        <!-- 顶栏：严格单行并排显示（左侧状态+节气，右侧窗位+日期+倒计时） -->
        <div class="px-3 py-1.5 bg-slate-900/70 border-b border-slate-700/60 flex items-center justify-between gap-1.5 whitespace-nowrap overflow-hidden">
          <div class="flex items-center gap-1.5 shrink-0">
            <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-bold ${isNext ? 'bg-amber-500/15 text-amber-300 border border-amber-500/35' : 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/35'}">
              <span class="w-1.5 h-1.5 rounded-full ${isNext ? 'bg-amber-400' : 'bg-emerald-400'}"></span>
              ${title}
            </span>
            <span class="px-1.5 py-0.5 rounded text-xs bg-slate-800 border border-slate-700 text-slate-200 font-mono font-semibold">${esc(m.month_id || '')}</span>
            <span class="px-1.5 py-0.5 rounded text-xs bg-amber-500/10 border border-amber-500/30 text-amber-300 font-medium">
              ${esc(pillar)} · ${esc(term)}月
            </span>
            ${factTag}
          </div>
          <div class="flex items-center gap-1.5 text-xs shrink-0">
            ${winBadge}
            <div class="flex items-center gap-1 font-mono text-[11px] text-slate-300 bg-slate-800/90 border border-slate-700/70 px-2 py-0.5 rounded shadow-inner whitespace-nowrap">
              <span class="text-indigo-400">📅</span>
              <span>${esc(startDate)}</span>
              <span class="text-slate-500 font-bold">～</span>
              <span>${esc(endDate)}</span>
            </div>
            ${rightBadge}
          </div>
        </div>

        ${weeksBar}

        <!-- 卡片主体：左区干支天干十神 + 右区地支藏干矩阵 -->
        <div class="p-2.5 flex-1 flex flex-col sm:flex-row items-stretch gap-2.5">
          
          <!-- 左块：干支立牌（宽度 w-20，字居中且绝不溢出框）与天干十神 -->
          <div class="flex items-center gap-2.5 p-2 rounded-xl bg-slate-900/50 border border-slate-700/60 shrink-0">
            <div class="w-20 py-2 rounded-xl bg-slate-900 border border-slate-700/90 text-center shadow-md shrink-0">
              <span class="text-[10px] text-slate-400 block mb-0.5 font-medium">流月干支</span>
              <div class="text-base font-bold font-mono tracking-wider flex justify-center items-center gap-1">
                <span class="${elCls(stemEl(pStem))} px-1.5 py-0.5 rounded shadow-sm">${pStem}</span>
                <span class="${elCls(branchEl(pBranch))} px-1.5 py-0.5 rounded shadow-sm">${pBranch}</span>
              </div>
              <span class="text-[10px] text-slate-400 block mt-0.5 font-mono">${stemEl(pStem)}/${branchEl(pBranch)}</span>
            </div>
            <div class="space-y-1 min-w-[105px]">
              <div class="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg text-xs font-bold bg-indigo-500/20 border border-indigo-500/40 text-indigo-200 shadow-sm">
                <span class="text-indigo-300/80 font-normal text-[11px]">天干十神</span>
                <b class="text-white text-xs">${esc(stemGod)}</b>
              </div>
              <div class="text-[11px] text-slate-400">
                <span>干属性: <b class="text-slate-200 font-mono">${stemEl(pStem)}·${esc(tg.stem?.yinyang || '—')}</b></span>
              </div>
            </div>
          </div>

          <!-- 右块：地支藏干十神矩阵 (顶格左对齐排列，一行三个标签，绝不空上面一行) -->
          <div class="flex-1 p-2 rounded-xl bg-slate-900/50 border border-slate-700/60 flex flex-col justify-start gap-1.5 min-w-0">
            <div class="flex items-center justify-between text-xs text-slate-400 border-b border-slate-800 pb-1">
              <div class="flex items-center gap-1 font-medium">
                <span class="text-indigo-400">❖</span>
                <span>地支【<b class="text-slate-100 font-bold">${pBranch}</b>】藏干十神</span>
              </div>
              <div class="text-[11px] text-slate-400 font-mono">
                基准日主: <b class="text-amber-400 font-bold">${esc(dm)}</b>
              </div>
            </div>
            <div class="flex items-center gap-1.5 flex-nowrap pt-0.5 justify-start overflow-x-auto">
              ${hiddenChips || '<span class="text-slate-500 text-xs">—</span>'}
            </div>
          </div>

        </div>

      </div>
    `;
  };

  box.appendChild(el('div',
    'rounded-xl theme-card border border-slate-700/80 p-3 space-y-2 text-xs shadow-sm',
    `<!-- 顶部统领栏：明确日元基准与计算口径 -->
    <div class="flex flex-wrap items-center justify-between gap-3 px-1 pb-2 border-b border-slate-700/50">
      <div class="flex items-center gap-3 flex-wrap">
        <div class="flex items-center gap-2 font-bold text-sm text-slate-100 tracking-wide">
          <span class="text-amber-400">📜</span>
          <span>流月十神推演</span>
        </div>
        <div class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-lg bg-amber-500/10 border border-amber-500/30 text-xs">
          <span class="text-slate-400">推演基准 · 日主</span>
          <b class="text-amber-300 font-mono text-sm font-bold">${esc(dm)}</b>
          <span class="text-amber-400/80 text-[11px] font-mono">(${esc(dmDesc)})</span>
        </div>
        <span class="text-[11px] text-slate-400 px-2 py-0.5 rounded bg-slate-800/80 border border-slate-700/60 whitespace-nowrap">
          客观测日计算 · 冻结节气对齐 · 不含未来行情
        </span>
      </div>
      <div class="text-xs text-slate-400 flex items-center gap-2.5">
        <span>当前节气月：<b class="text-slate-200 font-mono">${esc(cur.month_pillar)} · ${esc(cur.month_id)}（${esc(cur.start_term)}月）</b></span>
        <span class="text-slate-600">|</span>
        <span>当月在行剩余：<b class="text-amber-400 font-bold font-mono">${cur.days_until_end}</b> 天</span>
      </div>
    </div>

    <!-- 当前流月与下一流月双卡横向并列 -->
    <div class="grid grid-cols-1 xl:grid-cols-2 gap-3.5 items-stretch">
      ${card('当前流月', cur, false)}
      ${card('下一流月', next, true)}
    </div>`
  ));
}

/* ---------------- page 1 ---------------- */
async function renderStock() {
  const b = $('#stockbody'); b.innerHTML = '';
  if (!S.stock) return;
  const s = S.stock.security, p = S.stock.profile || {};
  const rows = [
    ['正式身份ID', esc(s.identity_code)], ['证券代码', esc(s.security_code)],
    ['名称', esc(s.name)], ['交易所', esc(s.exchange)],
    ['类型', S.dataset === 'etf' ? 'ETF' : esc(s.security_type)],
    ['上市日期', esc(s.listed_date || '—')], ['退市日期', esc(s.delisted_date || '未退市/无记录')],
    ['对比基准', esc(s.benchmark_code || '未指派（本身即基准）')],
    ['批次处理状态', esc(s.processing_status)],
    ['首个/末个正式月', s.first_month_id
      ? esc(s.first_month_id + ' ~ ' + (s.last_month_id || '')) : '—'],
    ['是否缺档案', p.profile_present === 1 ? '有命盘' : '缺档案'],
    ['交节争议', esc(p.boundary_conflict || 'NOT_APPLICABLE') +
      '（该身份争议月 ' + s.disputed_month_count + ' 个）'],
    ['命盘来源', esc(p.profile_source_note ||
      (S.dataset === 'etf' ? '库内首根K线日期' : '—'))],
    ['命盘版本', esc(p.profile_version || '—')],
    ['命盘哈希 profile_sha256', '<span class="mono">' + esc(p.profile_sha256 || '—') + '</span>'],
    ['命盘图哈希 chart_sha256', '<span class="mono">' + esc(p.chart_sha256 || '—') + '</span>'],
    ['本命时柱', '档案记载 ' + esc(p.archive_hour_pillar || '—') +
      '，本轮不参与任何冲合判定'],
    ['阴阳/性别字段', '档案记 ' + esc(p.archive_polarity || '—') + '/' +
      esc(p.archive_gender || '—') + '，仅按档案记账，不参与关系成立判定'],
  ];
  if (S.dataset === 'etf') {
    const fm = S.stock.fund_meta || {};
    rows[5] = ['库内首根K线', esc(s.listed_date || '—')];
    rows[6] = ['退市日期', '源只含存续 ETF'];
    rows.splice(7, 0,
      ['基金类型（一级分类）', dash(fm.fund_type || fm.category_l1)],
      ['行业/主题', dash(fm.industry_theme)],
      ['跟踪指数', dash(fm.tracking_index)],
      ['管理人', dash(fm.manager)],
      ['tushare 上市日', dash(fm.tushare_list_date)],
      ['首日K线阴阳', dash(fm.first_bar_yinyang)]);
    const i = rows.findIndex(([k]) => k === '阴阳/性别字段');
    const polArchive = p.archive_polarity
      ? esc(p.archive_polarity) + '/' + esc(p.archive_gender || '—')
      : '空（平盘首日，未赋值）';
    rows[i] = ['阴阳/性别字段', '档案记 ' + polArchive +
      '；阳=男，阴=女；平盘为空；仅按档案记账，不参与关系成立判定'];
    rows.push(['阴阳来源 / 规则', esc(p.polarity_source || '—') + '；' +
      esc(p.polarity_rule || '—')]);
    rows.push(['阴阳是否参与关系', 'polarity_participates=' +
      (p.polarity_participates === null || p.polarity_participates === undefined
        ? '—' : p.polarity_participates) + '（0=不参与）']);
  }
  b.appendChild(statTable(rows));
  if (S.dataset === 'etf' && s.formal_month_count === 0) {
    b.appendChild(el('p', 'noprof',
      '该 ETF 的库内首根 K 线晚于 as_of 2026-10-14，暂无 FINAL 月；页面二/三/四会显示零样本。'));
  }
  b.appendChild(el('p', 'hint', '说明：日主为<b>天干</b>；地支冲合针对本命年支、月支、日支分别判定，' +
    '页面把每一次命中都标出实际柱位，不使用「日主冲月支」这类不精确表述。'));
  const mt = S.stock.month_totals;
  b.appendChild(el('h3', null, '该身份正式历史月份构成'));
  b.appendChild(statTable([['FINAL 月份', mt.final], ['无争议可计算', mt.undisputed],
    ['交节争议', mt.disputed], ['缺档案阻塞', mt.no_profile]]));
}

/* ---------------- page 2: 12 branches ---------------- */
const RATE_TXT = { '': '不限', ge50: '≥50%', ge60: '≥60%', ge70: '≥70%',
                   ge80: '≥80%', ge90: '≥90%', eq100: '=100%' };

function syncPillarDirUi() {
  const down = $('#p-dir').value === 'down';
  const T = down ? '下跌' : '上涨';
  $('#p-dir').classList.toggle('c-down', down);
  $('#p-dir').classList.toggle('c-up', !down);
  [...$('#p-rate').options].forEach((o) => {
    o.text = T + '占比 ' + (RATE_TXT[o.value] || '');
  });
  const sortTexts = {
    pillar: '按六十甲子顺序',
    branch: '按月支分组（子丑寅卯…）',
    up_rate: '按历史' + T + '占比',
    n: '按出现次数',
    mean: down ? '按平均涨跌（跌得最多在前）' : '按平均涨跌',
  };
  [...$('#p-sort').options].forEach((o) => { if (sortTexts[o.value]) o.text = sortTexts[o.value]; });
}

function branchFilter() {
  return { pillar: $('#b-pillar').value.trim(), stem_element: $('#b-stem-el').value,
           branch_element: $('#b-branch-el').value, sort: $('#b-sort').value,
           min_n: $('#b-min').value };
}

function branchFilterBits(f) {
  const bits = [];
  if (f.pillar) bits.push('流月柱含「' + esc(f.pillar) + '」');
  if (f.stem_element) bits.push('天干五行 ' + esc(f.stem_element));
  if (f.branch_element) bits.push('地支五行 ' + esc(f.branch_element));
  if (f.min_n) bits.push('出现次数 ≥' + esc(f.min_n));
  return bits;
}

function updateSingleControl() {
  const n = (S.stock && S.stock.month_totals && S.stock.month_totals.single_day) || 0;
  $('#b-single-n').textContent = n;
  $('#b-single-wrap').classList.toggle('hidden', n === 0);
  if (!n) { $('#b-exclude-single').checked = false; S.branchPolicy = 'ALL'; }
  const tgEsBtn = document.querySelector('#tg-mode button[data-v="EXCLUDE_SINGLE"]');
  if (tgEsBtn) {
    tgEsBtn.title = n === 0
      ? '单交易日月＝节气月内仅有 1 个交易日（多见于停牌/月末交节）。当前标的历史单交易日月为 0，两种口径结果完全一致。'
      : ('当前标的历史有 ' + n + ' 个单交易日月，点击将予以排除。');
  }
}

function policyLabel() {
  const n = (S.stock && S.stock.month_totals && S.stock.month_totals.single_day) || 0;
  return S.branchPolicy === 'ALL' ? '含单交易日月'
    : '不含单交易日月（已排除 ' + n + ' 个）';
}

async function renderBranch() {
  const box = $('#branchtable'); box.innerHTML = ''; $('#branchdetail').innerHTML = '';
  if (!S.id) return;
  const my = seqStart('branch');
  const ep = S.epoch;
  const f = branchFilter();
  const rows = await get('/api/branch12', Object.assign({ id: S.id,
                                policy: S.branchPolicy }, f));
  if (seqStale('branch', my) || ep !== S.epoch) return;
  const bits = branchFilterBits(f);
  const recomputed = !!(f.pillar || f.stem_element || f.branch_element);
  const shown = bits.concat(S.branchPolicy === 'ALL' ? [] : ['不含单交易日月']);
  let note = shown.length ? '已设：' + shown.join(' · ')
    : '未筛选：12支固定全列，零样本地支比例为 NULL';
  const branchNote = (S.meta && S.meta.branch_stats_note) ||
    '汇总取冻结 stats_branch12 的';
  note += ' · ' + (recomputed
    ? '汇总为筛选月份即时重算（口径同' + (S.dataset === 'etf' ? ' A股' : '') + '冻结表）'
    : branchNote + ' ' + S.branchPolicy + ' 口径');
  if (shown.length) note += ' · 12 支中有样本 ' + rows.filter((r) => r.n > 0).length + ' 支';
  $('#b-filter-note').innerHTML = note;
  if (!rows.length) {
    box.appendChild(el('p', 'noprof', '筛选后没有地支达到该出现次数门槛。'));
    return;
  }
  const cur = rows.find((r) => r.branch === S.selBranch);
  const keep = (cur && (cur.n > 0 || !rows.some((r) => r.n > 0)))
    ? S.selBranch : (rows.find((r) => r.n > 0) || rows[0]).branch;
  S.selBranch = keep;
  box.appendChild(table([
    ['branch', '地支', (r) => '<div class="font-bold text-sm flex items-center justify-center gap-1.5 whitespace-nowrap"><span class="' + elCls(r.branch_element) + ' px-1.5 rounded">' + r.branch + '</span><span class="text-[10px] text-slate-400">' + (BRANCH_TERM[r.branch] || '') + '</span></div>'],
    ['branch_element', '五行', (r) => '<span class="text-amber-500 whitespace-nowrap">' + esc(r.branch_element) + '</span>'],
    ['n', '出现次数', (r) => '<span class="tabular-num font-medium whitespace-nowrap">' + r.n + '</span>'],
    ['up_n', '上涨', (r) => '<span class="tabular-num c-up font-bold whitespace-nowrap">' + r.up_n + '</span>'],
    ['down_n', '下跌', (r) => '<span class="tabular-num c-down font-bold whitespace-nowrap">' + r.down_n + '</span>'],
    ['flat_n', '平盘', (r) => '<span class="tabular-num text-slate-400 whitespace-nowrap">' + r.flat_n + '</span>'],
    ['up_frequency', '上涨占比', (r) => {
      if (r.up_frequency === null || r.up_frequency === undefined) return '<span class="dim">—</span>';
      const p = pct(r.up_frequency);
      const pctNum = Math.min(Math.max(r.up_frequency * 100, 0), 100).toFixed(1);
      return '<div class="flex items-center justify-center gap-2 whitespace-nowrap"><span class="tabular-num font-bold c-up w-10 text-right">' + p + '</span><div class="w-16 theme-card-sub rounded-full h-1.5 overflow-hidden border border-slate-700/40"><div class="bg-red-500 h-1.5 rounded-full" style="width: ' + pctNum + '%"></div></div></div>';
    }],
    ['down_frequency', '下跌占比', (r) => {
      if (r.down_frequency === null || r.down_frequency === undefined) return '<span class="dim">—</span>';
      const p = pct(r.down_frequency);
      const pctNum = Math.min(Math.max(r.down_frequency * 100, 0), 100).toFixed(1);
      return '<div class="flex items-center justify-center gap-2 whitespace-nowrap"><span class="tabular-num font-bold c-down w-10 text-right">' + p + '</span><div class="w-16 theme-card-sub rounded-full h-1.5 overflow-hidden border border-slate-700/40"><div class="bg-emerald-500 h-1.5 rounded-full" style="width: ' + pctNum + '%"></div></div></div>';
    }],
    ['mean_return', '平均涨跌', (r) => '<span class="tabular-num font-bold ' + (r.mean_return > 0 ? 'c-up' : r.mean_return < 0 ? 'c-down' : 'c-flat') + ' whitespace-nowrap">' + ret(r.mean_return) + '</span>'],
    ['median_return', '中位涨跌幅', (r) => '<span class="tabular-num whitespace-nowrap">' + ret(r.median_return) + '</span>'],
    ['valid_sample_n', '有效样本', (r) => '<span class="tabular-num whitespace-nowrap">' + r.valid_sample_n + '</span>'],
    ['single_day_n', '单日', (r) => '<span class="tabular-num whitespace-nowrap">' + r.single_day_n + '</span>'],
    ['excess_mean', '同期超额', (r) => '<span class="tabular-num font-bold ' + (r.excess_mean > 0 ? 'c-up' : r.excess_mean < 0 ? 'c-down' : 'c-flat') + ' whitespace-nowrap">' + ret(r.excess_mean) + '</span>'],
    ['sample_grade', '样本等级', (r) => '<span class="px-2 py-0.5 rounded text-[10px] ' + (sampleGradeIsHigh(r.sample_grade) ? 'badge-up' : 'badge-flat') + ' font-bold inline-block text-center whitespace-nowrap">' + sampleGradeTxt(r.sample_grade) + '</span>'],
    ['main_relations', '主要命中关系', (r) => r.main_relations ? '<span class="px-2 py-0.5 rounded text-[11px] badge-up font-medium whitespace-nowrap">' + esc(r.main_relations) + '</span>' : '<span class="dim">（无）</span>'],
  ], rows, (r, tr) => {
    document.querySelectorAll('#branchtable tbody tr.sel').forEach((x) => x.classList.remove('sel'));
    tr.classList.add('sel'); S.selBranch = r.branch; showBranchDetail(r.branch);
  }));
  box.querySelectorAll('tbody tr').forEach((tr, i) => {
    if (rows[i] && rows[i].branch === keep) tr.classList.add('sel');
  });
  await showBranchDetail(keep);
}

async function showBranchDetail(branch) {
  const box = $('#branchdetail'); box.innerHTML = '';
  const my = seqStart('branchdetail');
  const ep = S.epoch;
  const f = branchFilter();
  const rows = await get('/api/branch_detail', { id: S.id, branch, policy: S.branchPolicy,
                                                 pillar: f.pillar, stem_element: f.stem_element,
                                                 branch_element: f.branch_element });
  if (seqStale('branchdetail', my) || ep !== S.epoch) return;
  const bits = branchFilterBits(f);
  const term = rows[0] ? rows[0].start_term : (BRANCH_TERM[branch] || '');
  box.appendChild(el('h3', null, branch + '（' + esc(term) + ' 起月）历次出现 · 口径 ' +
    policyLabel() + (bits.length ? ' · ' + bits.join(' · ') : '') +
    ' · 共 ' + rows.length + ' 个月'));
  if (!rows.length) {
    box.appendChild(el('p', 'noprof', bits.length
      ? '该地支在本身份正式历史中无满足筛选条件的月份。' : '该地支在本身份正式历史中无样本。'));
    return;
  }
  box.appendChild(table([
    ['month_id', '月ID', (r) => '<span class="mono">' + esc(r.month_id) + '</span>'],
    ['flow_pillar', '流月柱', (r) => '<span class="font-bold font-mono text-sm c-up whitespace-nowrap">' + esc(r.flow_pillar) + '</span>'],
    ['stem_ten_god', '流月干十神', (r) => r.ten_god_status === 'OK'
      ? '<span class="px-1.5 py-0.2 rounded text-[10px] bg-indigo-500/10 border border-indigo-500/30 text-indigo-400">' + esc(r.stem_ten_god) + '</span>'
      : '<span class="text-amber-500 text-[10px]">无档案</span>'],
    ['hidden_stem_summary', '地支藏干十神', (r) => r.hidden_stem_summary
      ? '<span class="text-[10px] text-slate-400 whitespace-nowrap">' + esc(r.hidden_stem_summary) + '</span>'
      : '<span class="text-slate-600">—</span>'],
    ['start_term', '节气'],
    ['first_date', '区间', (r) => esc(r.first_date + ' ~ ' + r.last_date)],
    ['trading_days', '交易日'],
    ['direction', '涨跌', (r) => '<span class="px-2 py-0.5 rounded text-[10px] font-bold ' + dirBadge(r.direction) + '">' + dirTxt(r.direction) + '</span>'],
    ['body_return', '涨跌幅', (r) => '<span class="tabular-num font-bold ' + (r.body_return > 0 ? 'c-up' : r.body_return < 0 ? 'c-down' : 'c-flat') + '">' + ret(r.body_return) + '</span>'],
    ['official_types', '正式关系', (r) => chipText(r.official_types)],
    ['candidate_types', '候选结构', (r) => chipText(r.candidate_types, 'cand')],
    ['excess_return', '超额', (r) => ret(r.excess_return)],
    ['research_sample_status', '样本状态', (r) => sampleTag(r.research_sample_status)],
  ], rows, (r) => showMonth(r.month_id), { cls: 'dense' }));
}

function chipText(txt, cls) {
  if (!txt) return '<span class="dim">（无）</span>';
  return txt.split(' ').filter(Boolean)
    .map((x) => '<span class="chip ' + (cls || '') + '">' + esc(x) + '</span>').join('');
}
function sampleTag(v) {
  const m = { ELIGIBLE_UNDISPUTED: ['无争议', 'ok'],
              EXCLUDED_BOUNDARY_DISPUTED: ['交节争议', 'warn'],
              EXCLUDED_NO_PROFILE: ['缺档案', 'warn'] };
  const x = m[v] || [v || '—', ''];
  return '<span class="tag ' + x[1] + '">' + esc(x[0]) + '</span>';
}

const date10 = (s) => (s || '').slice(0, 10);

/* 下次出现直接取服务端从冻结 calendar_months 查到的行，页面不自己推历法。 */
function nextCell(r) {
  const nx = r.next_occurrence;
  if (!nx) return '<span class="dim">超出冻结历法</span>';
  const when = (nx.days_until === null || nx.days_until === undefined) ? ''
    : nx.days_until <= 0 ? '今天起' : nx.days_until + ' 天后';
  return (r.current_occurrence ? '<span class="tag ok">当前</span>' : '') +
    '<div class="mono">' + esc(date10(nx.start_at)) + '</div>' +
    '<div class="small">' + esc(nx.start_term) + ' 起' + (when ? ' · ' + when : '') + '</div>';
}

/* ---------------- page 3: 60 pillars ---------------- */
/* 三项筛选按「单支票」定义，池层套不进去；置灰而不是静默空转。 */
const PILLAR_REL_KEYS = [['#f-clash', 'clash'], ['#f-harmony', 'harmony'],
                         ['#f-both', 'both_clash_harmony'], ['#f-punish', 'punish'],
                         ['#f-harm', 'harm'], ['#f-broken', 'broken']];

function pillarFilter(withRelations) {
  const rate = $('#p-rate').value;
  const flt = {
    pillar: $('#p-search').value.trim(), stem_element: $('#p-stem-el').value,
    branch_element: $('#p-branch-el').value, sort: $('#p-sort').value,
    dir: $('#p-dir').value,
    rate_min: rate ? (rate === 'eq100' ? '100' : rate.slice(2)) : '',
    rate_eq: rate === 'eq100' ? '1' : '',
  };
  if (withRelations) {
    flt.min_n = $('#p-min').value;
    PILLAR_REL_KEYS.forEach(([sel, key]) => { flt[key] = $(sel).checked ? '1' : ''; });
  }
  return flt;
}

function setPillarFilterAvailability(on, why) {
  const mark = (node, flag) => {
    if (!node) return;
    node.disabled = !flag;
    const lab = node.closest('label') || node;
    lab.classList.toggle('opacity-40', !flag);
    lab.classList.toggle('cursor-not-allowed', !flag);
  };
  ['#p-min', '#p-tg'].forEach((id) => mark($(id), on));
  PILLAR_REL_KEYS.forEach(([id]) => mark($(id), on));
  const warn = $('#p-filter-warn');
  if (!warn) return;
  if (on || !why) { warn.classList.add('hidden'); warn.innerHTML = ''; return; }
  warn.classList.remove('hidden');
  warn.innerHTML = '<b>本视图下已置灰、不生效</b>：出现次数门槛、流月干十神、关系筛选（冲/合/刑/害/破）。' +
    '原因：' + esc(why) + '。要用这三项请切回「只看本票」。';
}

async function renderPillar() {
  const box = $('#pillartable'); box.innerHTML = ''; $('#pillardetail').innerHTML = '';
  const my = seqStart('pillar');
  const ep = S.epoch;
  S.pillarLayer = S.pillarLayer || 'month';
  S.pillarView = S.pillarView || 'stock';
  syncSegUi('#p-layer-seg', S.pillarLayer);
  const wantStock = S.pillarView === 'stock';
  const view = (wantStock && S.id) ? 'stock' : 'pool';
  syncSegUi('#p-view-seg', view);
  const vnote = $('#p-view-note');
  if (vnote) {
    vnote.textContent = view === 'stock'
      ? '【只看本票】每行都是 ' + S.id + ' 自己的历史窗口，换一支票数字就会变。'
      : (wantStock ? '【全市场池】还没有选中的标的，本票视图没有对象；先在上方搜索框选一支。'
                   : '【全市场池】每行是该柱/窗口在同类全部标的上的加总，与选中哪一支票无关。');
  }
  setPillarFilterAvailability(view === 'stock', '池层是全类别加总，这三项都按单支票定义');

  const layer = S.pillarLayer;
  const down = $('#p-dir').value === 'down';
  const DT = down ? '下跌' : '上涨';
  const flt = pillarFilter(view === 'stock');
  const tgFilter = $('#p-tg') ? $('#p-tg').value : '';

  let rowsAll, kind;
  if (view === 'pool') {
    rowsAll = await get('/api/window_layer_pool', Object.assign({ layer: layer }, flt));
    kind = 'pool';
  } else if (layer === 'month') {
    rowsAll = await get('/api/pillar60', Object.assign({ id: S.id }, flt));
    kind = 'stock_month';
  } else {
    rowsAll = await get('/api/window_layer_stock', Object.assign({ id: S.id, layer: layer }, flt));
    kind = 'stock_window';
  }
  if (seqStale('pillar', my) || ep !== S.epoch) return;
  const rows = tgFilter ? rowsAll.filter((r) => r.stem_ten_god === tgFilter) : rowsAll;
  const maxCov = kind === 'pool' && rowsAll.length
    ? Math.max.apply(null, rowsAll.map((r) => r.covered_stocks || 0)) : 0;
  pillarNote(tgFilter, rows.length, flt, rowsAll.length, view, layer, maxCov);

  let lastP = '', gIdx = 0;
  const groupClasses = rows.map((r, i) => {
    if (r.flow_pillar !== lastP) { lastP = r.flow_pillar; gIdx++; }
    const first = (i === 0 || rows[i - 1].flow_pillar !== r.flow_pillar);
    const bg = gIdx % 2 === 0 ? 'grp-even' : 'grp-odd';
    return first && layer !== 'month' ? bg + ' grp-border-top' : bg;
  });

  const rateBar = (r) => {
    const val = down ? r.down_rate : r.up_rate;
    if (val === null || val === undefined) return '<span class="dim">—</span>';
    const num = Math.min(Math.max(val * 100, 0), 100).toFixed(1);
    const cls = down ? 'c-down' : 'c-up';
    const bar = down ? 'bg-emerald-500' : 'bg-red-500';
    return '<div class="flex items-center justify-center gap-1.5 whitespace-nowrap">' +
      '<span class="tabular-num font-bold ' + cls + ' w-12 text-right">' + pct(val) + '</span>' +
      '<div class="w-12 theme-card-sub rounded-full h-1.5 overflow-hidden border border-slate-700/40">' +
      '<div class="' + bar + ' h-1.5 rounded-full" style="width: ' + num + '%"></div></div></div>';
  };
  const upDownFlat = (r) => '<span class="tabular-num c-up font-bold whitespace-nowrap">' +
    (r.ALL_up_n != null ? r.ALL_up_n : r.up_n_ALL) + '</span><span class="text-slate-600"> / </span>' +
    '<span class="tabular-num c-down font-bold whitespace-nowrap">' +
    (r.ALL_down_n != null ? r.ALL_down_n : r.down_n_ALL) + '</span><span class="text-slate-600"> / </span>' +
    '<span class="tabular-num text-slate-400 whitespace-nowrap">' +
    (r.ALL_flat_n != null ? r.ALL_flat_n : r.flat_n_ALL) + '</span>';
  // stats_pillar60 的涨跌幅是小数（-0.0104 = -1.04%），xw_*_rules 与池层已是百分数（-1.04）；
  // 两者混用会把个股月层的涨跌幅缩小 100 倍。
  const retPct = (x) => (x === null || x === undefined) ? '—'
    : (x > 0 ? '+' : '') + Number(x).toFixed(2) + '%';
  const retMean = kind === 'stock_month' ? ret : retPct;
  const winCol = layer === 'month' ? null
    : ['window_tag', '窗位及区间', (r) => {
        const full = r.window_tag || '';
        const m = full.match(/^([^\s(（]+)[\s(（]+([^)]*)[)）]?$/);
        if (m) {
          const winName = m[1].trim();
          const winRange = m[2].replace(/[)）]$/, '').trim();
          return '<div class="leading-tight flex flex-col items-center justify-center py-0.5 select-none" title="' + esc(full) + '">' +
            '<span class="px-1.5 py-0.2 rounded font-bold bg-slate-800 text-slate-300 border border-slate-700 text-[10.5px] whitespace-nowrap">' + esc(winName) + '</span>' +
            '<span class="text-[10px] text-slate-400 font-mono whitespace-nowrap mt-0.5">' + esc(winRange) + '</span>' +
            '</div>';
        }
        return '<span class="inline-block px-1.5 py-0.5 rounded font-bold bg-slate-800 text-slate-300 border border-slate-700 text-[11px] whitespace-nowrap" title="' + esc(full) + '">' + esc(full) + '</span>';
      }, 'center'];
  const tgCol = kind === 'pool' ? null
    : ['stem_ten_god', '流月干十神', (r) => r.ten_god_status === 'OK'
        ? '<span class="px-1.5 py-0.2 rounded text-[10px] font-semibold bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 whitespace-nowrap">' + esc(r.stem_ten_god) + '</span>'
        : '<span class="text-amber-500 text-[10px]">无档案</span>'];

  const cols = [
    ['flow_pillar', '流月柱', (r) => '<span class="font-bold font-mono text-sm c-up whitespace-nowrap">' + esc(r.flow_pillar) + '</span>' +
      (r.stem_element && r.branch_element ? '<span class="text-[10px] text-slate-500 ml-1">(' + esc(r.stem_element) + esc(r.branch_element) + ')</span>' : '')],
    winCol,
    tgCol,
    kind === 'pool'
      ? ['covered_stocks', '覆盖标的数', (r) => '<span class="tabular-num font-medium whitespace-nowrap">' + (r.covered_stocks ? r.covered_stocks.toLocaleString() : '—') + '</span>']
      : ['occurrence_count', '应有窗', (r) => '<span class="tabular-num font-medium whitespace-nowrap">' + (r.ALL_n_occurrences != null ? r.ALL_n_occurrences : r.occurrence_count) + '</span>'],
    kind === 'pool'
      ? ['valid_windows', '有效窗 / 空窗', (r) => '<span class="tabular-num whitespace-nowrap">' + (r.ALL_valid_n || 0).toLocaleString() + '</span><span class="text-slate-500"> / </span><span class="tabular-num text-slate-400">' + (r.ALL_invalid_n || 0).toLocaleString() + '</span>']
      : ['valid_windows', '有效窗 / 空窗', (r) => '<span class="tabular-num whitespace-nowrap">' + (r.ALL_valid_n != null ? r.ALL_valid_n : r.all_sample_n) + '</span><span class="text-slate-500"> / </span><span class="tabular-num text-slate-400">' + (r.ALL_invalid_n || 0) + '</span>'],
    ['up_down_flat', '涨 / 跌 / 平', upDownFlat],
    [down ? 'down_rate' : 'up_rate', '历史' + DT + '占比', rateBar],
    ['mean_body_return_ALL', '平均涨跌', (r) => '<span class="tabular-num font-bold ' + (r.mean_body_return_ALL > 0 ? 'c-up' : r.mean_body_return_ALL < 0 ? 'c-down' : 'c-flat') + ' whitespace-nowrap">' + retMean(r.mean_body_return_ALL) + '</span>'],
    ['median_body_return_ALL', '中位', (r) => '<span class="tabular-num whitespace-nowrap">' + retMean(r.median_body_return_ALL) + '</span>'],
  ];
  if (kind === 'stock_month') {
    cols.push(['is_five_year_repeat', '五年循环', (r) => r.is_five_year_repeat === '1'
      ? '<span class="px-1.5 py-0.2 rounded text-[10px] badge-up font-medium whitespace-nowrap">是(5年)</span>'
      : r.is_five_year_repeat === 'PARTIAL'
        ? '<span class="px-1.5 py-0.2 rounded text-[10px] badge-flat font-medium whitespace-nowrap">部分</span>'
        : '<span class="text-slate-400 whitespace-nowrap">否</span>']);
  }
  if (kind === 'stock_window') {
    cols.push(
      ['majority_direction', '多数方向', (r) => '<span class="px-1.5 py-0.2 rounded text-[10px] font-semibold whitespace-nowrap ' + (r.majority_direction === '上涨' ? 'badge-up' : r.majority_direction === '下跌' ? 'badge-down' : 'badge-flat') + '">' + esc(r.majority_direction || '—') + '</span>'],
      ['band', '档位', (r) => '<span class="tabular-num text-[11px] whitespace-nowrap">' + esc(r.band || '—') + '</span>', null, 'band 按 max(上涨占比,下跌占比) 分档；=100% 也可能是整段全跌，方向看「多数方向」列'],
      ['sample_grade', '样本档', (r) => '<span class="px-1.5 py-0.2 rounded text-[10px] ' + (r.sample_grade === '≥6 次' ? 'badge-up' : 'badge-flat') + ' font-bold whitespace-nowrap">' + esc(r.sample_grade || '—') + '</span>'],
      ['deviation_from_baseline_window_up_rate', '偏离同类同窗基线', (r) => {
        const v = r.deviation_from_baseline_window_up_rate;
        if (v === null || v === undefined) return '<span class="dim">—</span>';
        return '<span class="tabular-num whitespace-nowrap ' + (v > 0 ? 'c-up' : v < 0 ? 'c-down' : 'c-flat') + '">' + (v > 0 ? '+' : '') + pct(v) + '</span>';
      }]);
  }
  if (kind === 'stock_month') {
    cols.push(['sample_marker_ALL', '样本门槛', (r) => '<span class="px-1.5 py-0.2 rounded text-[10px] ' + (r.sample_marker_ALL === 'GE6' ? 'badge-up' : 'badge-flat') + ' font-bold inline-block text-center whitespace-nowrap">' + markerTxt(r.sample_marker_ALL) + '</span>']);
  }
  cols.push(
    ['next_occurrence', '下次出现', nextCell],
    ['action', kind === 'stock_month' ? '历次对账' : '个股窗位抽屉', (r) => '<button type="button" class="px-2 py-0.5 rounded-lg bg-indigo-600/30 hover:bg-indigo-600 text-indigo-300 hover:text-white border border-indigo-500/40 text-[10px] font-medium transition whitespace-nowrap">' + (kind === 'stock_month' ? '历次对账 ↗' : '窗位明细 ↗') + '</button>']);

  const tbl = table(cols.filter(Boolean), rows, (r, tr) => {
    document.querySelectorAll('#pillartable tbody tr.sel').forEach((x) => x.classList.remove('sel'));
    tr.classList.add('sel');
    if (kind === 'stock_month') showPillar(r); else showWindowDrawer(r);
  }, { cls: 'dense pillar60',
       rowClass: (r, i) => groupClasses[i] || '',
       widths: null });

  box.appendChild(tbl);
  if (!rows.length) {
    box.appendChild(el('p', 'noprof', kind === 'pool'
      ? '当前筛选条件下池层没有窗口。'
      : (S.pillarView === 'stock' && !S.id
          ? '先在上方搜索框选一支票，本票视图才有对象。'
          : '这支票在当前层级/筛选条件下没有窗口记录（旬层、段层只收完整流月）。')));
  } else if (kind === 'stock_month' && rows.length) {
    const pick = pickGoldenPillar(rows);
    const tr = box.querySelector('tbody tr');
    if (tr) { tr.classList.add('sel'); }
    showPillar(pick);
  }
}

function showWindowDrawer(r) {
  const stock = S.stock || {};
  const sec = stock.security || {};
  let id = sec.identity_code || S.id || '600519.SH';
  let name = sec.name || '';
  let code = sec.security_code || S.id || '';
  if (sec.security_type === 'INDEX' || id === '000001.SH') {
    id = '600519.SH';
    name = '贵州茅台';
    code = '600519.SH';
  }

  showOppDrawer({
    identity_code: id,
    security_code: code,
    name: name,
    flow_pillar: r.flow_pillar,
    _layer: S.pillarLayer,
    xun_no: r.xun_no,
    week_no: r.week_no,
    day_from: r.day_from,
    day_to: r.day_to,
    ALL_valid_n: r.ALL_valid_n,
    up_n_ALL: r.ALL_up_n,
    down_n_ALL: r.ALL_down_n,
    flat_n_ALL: r.ALL_flat_n,
    up_rate_ALL: r.up_rate,
    down_rate_ALL: r.down_rate,
    all_sample_n: r.ALL_valid_n,
    exclude_single_n: r.ALL_valid_n,
    mean_body_return_ALL: r.mean_body_return_ALL
  });
}

function pillarNote(tgFilter, shownN, flt, totalN, view, layer, maxCov) {
  const cal = S.cal || {};
  let t = '';
  const bits = [];
  if (flt) {
    if (flt.dir === 'down') bits.push('方向：下跌');
    if (flt.rate_min) {
      const T = flt.dir === 'down' ? '下跌' : '上涨';
      bits.push(T + '占比 ' + (flt.rate_eq ? '=100%' : '≥' + flt.rate_min + '%'));
    }
  }
  const layerName = layer === 'month' ? '月层' : (layer === 'week' ? '段层' : '旬层');
  const viewName = view === 'stock' ? '只看本票 ' + esc(S.id || '') : '全市场池';
  const total = totalN || (layer === 'month' ? 60 : (layer === 'week' ? 360 : 180));
  t = '范围：【' + viewName + '】 · 层级：【' + layerName + '】';
  if (bits.length) t += ' · 已设：' + bits.join(' · ');
  if (tgFilter) t += ' · 十神「' + esc(tgFilter) + '」';
  t += ' · 共 ' + shownN + ' / ' + total + (layer === 'month' ? ' 柱' : ' 窗');
  if (view === 'stock' && S.stock && S.stock.security) {
    const sec = S.stock.security;
    t += ' · ' + esc(sec.name || '') + '(' + esc(sec.security_code || '') + ') 正式历史月 ' +
      (sec.formal_month_count != null ? sec.formal_month_count : '—') + ' 个';
  }
  if (view === 'pool' && maxCov) {
    t += ' · 本表内覆盖标的数最高的一窗 ' + maxCov.toLocaleString() + ' 只（票·月加权）';
  }
  if (cal.current) t += ' · 当前流月 ' + esc(cal.current.month_pillar) + ' ' +
    esc(date10(cal.current.start_at)) + '→' + esc(date10(cal.current.end_at));
  if (cal.calendar) t += ' · 下次出现取自冻结历法 ' + esc(cal.calendar.calendar_version || '') +
    '（至 ' + esc(date10(cal.calendar.last_end)) + '），仅历法日期、非行情预测';
  const del = S.stock && S.stock.security && S.stock.security.delisted_date;
  if (del) t += ' · 该身份已于 ' + esc(del) + ' 退市，下次出现届时无行情';
  $('#p-count').innerHTML = t;
}

function pickGoldenPillar(rows) {
  if (S.dataset === 'ashare' && S.id === '000001.SH') {
    for (const p of ['壬寅', '癸未', '戊午'])
    { const hit = rows.find((r) => r.flow_pillar === p); if (hit) return hit; }
  }
  return rows.find((r) => r.occurrence_count > 0) || rows[0];
}
function markerTxt(m) {
  return { LT3: '<3 不构成候选', '3TO5': '3–5 小样本', GE6: '≥6' }[m] || m || '—';
}
function jsonChips(txt, cls) {
  if (!txt) return '<span class="dim">（无）</span>';
  let arr; try { arr = JSON.parse(txt); } catch (e) { return esc(txt); }
  if (!arr || !arr.length) return '<span class="dim">（无）</span>';
  return arr.map((x) => '<span class="chip ' + (cls || '') + '">' + esc(x) + '</span>').join('');
}

const qfq3 = (x) => {
  if (x === null || x === undefined || x === '') return '—';
  const n = Number(x);
  return Number.isFinite(n) ? n.toFixed(3) : String(x);
};

async function showPillar(row) {
  const box = $('#pillardetail'); box.innerHTML = '';
  const pillar = row.flow_pillar;
  const nx = row.next_occurrence;
  const my = seqStart('pillardetail');
  const ep = S.epoch;
  const rows = await get('/api/pillar_detail', { id: S.id, pillar });
  if (seqStale('pillardetail', my) || ep !== S.epoch) return;
  box.appendChild(el('h3', null, '流月柱 ' + pillar + ' · 该身份历史出现 ' + rows.length + ' 次' +
    (nx ? ' · 下次出现 ' + esc(date10(nx.start_at)) + '（' + esc(nx.start_term) + ' 起）' : '')));
  if (!rows.length) { box.appendChild(el('p', 'noprof', '零样本：该柱在本身份正式历史中未出现。'));
                      return; }
  const years = rows.map((r) => r.solar_year);
  const gaps = years.slice(1).map((y, i) => y - years[i]);
  box.appendChild(el('div', 'years',
    '<b>历次年份</b> ' + years.join(' / ') +
    (gaps.length ? '<b>　相邻间隔</b> ' + gaps.join(', ') +
      (gaps.every((g) => g === 5) ? '<span class="ok"> 严格五年一循环</span>'
       : gaps.some((g) => g === 5) ? '<span class="warn"> 部分五年间隔</span>' : '') : '')));
  box.appendChild(table([
    ['month_id', '月ID', (r) => '<span class="mono">' + esc(r.month_id) + '</span>'],
    ['solar_year', '年份'], ['start_term', '节气'],
    ['first_date', '区间', (r) => esc(r.first_date + ' ~ ' + r.last_date)],
    ['trading_days', '交易日'],
    ['direction', '涨跌', (r) => '<span class="pill ' + dirCls(r.direction) + '">' +
      dirTxt(r.direction) + '</span>'],
    ['body_return', '涨跌幅', (r) => '<span class="' + dirCls(r.direction) + '">' +
      ret(r.body_return) + '</span>'],
    ['raw_open', '行情证据(不复权开→收)', (r) => '<span class="mono">' +
      esc(r.raw_open + '→' + r.raw_close) + '</span>'],
    ['qfq_open', '前复权(仅展示)', (r) => '<span class="mono" title="' +
      esc((r.qfq_open || '') + '→' + (r.qfq_close || '')) + '">' +
      esc(qfq3(r.qfq_open) + '→' + qfq3(r.qfq_close)) + '</span>'],
    ['stem_ten_god', '流月干十神'],
    ['hidden_stem_summary', '流月支藏干十神', (r) => r.hidden_stem_summary
      ? '<span class="text-[10px] text-slate-400">' + esc(r.hidden_stem_summary) + '</span>'
      : '<span class="text-amber-500 text-[10px]">无档案</span>'],
    ['confirmed_event_count', '正式事件'], ['candidate_structure_count', '候选事件'],
    ['excess_return', '超额', (r) => ret(r.excess_return)],
    ['research_sample_status', '样本', (r) => sampleTag(r.research_sample_status)],
  ], rows, (r) => showMonth(r.month_id), { cls: 'dense pdetail',
    widths: ['6.5%', '3.5%', '3.5%', '17.5%', '3.5%', '3.5%', '4%', '11%', '8%',
             '8.5%', '5%', '9%', '5%', '5%', '6.5%'] }));
  box.appendChild(el('p', 'hint', '点击任意一行可展开该月逐条关系明细（含柱位、S/V/U 结构标记、' +
    '候选合化五行与是否成立）。合化是否成立为 NULL 表示<b>未知</b>，不是不成立。' +
    '前复权列只作展示（完整数值见悬停提示），不参与任何统计。'));
}

/* ---------------- page 4: relations ---------------- */
async function renderRelation() {
  const box = $('#relationtable'); box.innerHTML = '';
  $('#relationpillars').innerHTML = ''; $('#relationmonths').innerHTML = '';
  if (!S.id) return;
  const my = seqStart('relation');
  const ep = S.epoch;
  const rows = await get('/api/relation_types', { id: S.id, record_kind: S.relKind });
  if (seqStale('relation', my) || ep !== S.epoch) return;
  box.appendChild(el('h3', null, (S.relKind === 'OFFICIAL' ? '正式关系（22 类目录内）'
    : '候选结构（不计入正式关系次数）') + ' · 有样本的关系类型 ' + rows.length + ' 类'));
  if (!rows.length) box.appendChild(el('p', 'noprof',
    S.stock && S.stock.profile && S.stock.profile.profile_present === 1
      ? '该口径下无样本。' : '无命盘，暂不能计算冲合关系；行情统计见页面二、三。'));
  box.appendChild(table([
    ['relation_type', '关系类型', (r) => '<span class="px-2 py-0.5 rounded text-[11px] badge-up font-bold whitespace-nowrap">' + esc(r.relation_type) + '</span>' +
      (r.record_kind === 'CANDIDATE' ? ' <span class="px-1.5 py-0.2 rounded text-[10px] badge-flat font-medium">候选</span>' : '')],
    ['group_name', '族'],
    ['months_undisputed', '无争议月份数(按身份+月去重)'],
    ['months_any', '含争议月合计'], ['disputed_months', '其中交节争议'],
    ['up_n', '涨', (r) => '<span class="tabular-num c-up font-bold">' + r.up_n + '</span>'],
    ['down_n', '跌', (r) => '<span class="tabular-num c-down font-bold">' + r.down_n + '</span>'],
    ['flat_n', '平', (r) => '<span class="tabular-num text-slate-400">' + r.flat_n + '</span>'],
    ['up_rate', '上涨占比', (r) => {
      if (r.up_rate === null || r.up_rate === undefined) return '<span class="dim">—</span>';
      const p = pct(r.up_rate);
      const pctNum = Math.min(Math.max(r.up_rate * 100, 0), 100).toFixed(1);
      return '<div class="flex items-center justify-center gap-1.5 whitespace-nowrap"><span class="tabular-num font-bold c-up w-10 text-right">' + p + '</span><div class="w-12 theme-card-sub rounded-full h-1.5 overflow-hidden border border-slate-700/40"><div class="bg-red-500 h-1.5 rounded-full" style="width: ' + pctNum + '%"></div></div></div>';
    }],
    ['mean_return', '平均涨跌', (r) => '<span class="tabular-num font-bold ' + (r.mean_return > 0 ? 'c-up' : r.mean_return < 0 ? 'c-down' : 'c-flat') + '">' +
      ret(r.mean_return) + '</span>'],
    ['es_up_rate', '排单日后上涨占比', (r) => pct(r.es_up_rate)],
    ['events', '事件数(未按月去重)'], ['distinct_pillars', '涉及流月柱数'],
    ['positions_json', '柱位分布(事件数)', (r) => '<span class="mono small">' +
      esc(r.positions_json || '') + '</span>'],
    ['origins_json', '来源结构', (r) => '<span class="mono small">' +
      esc(r.origins_json || '') + '</span>'],
  ], rows, (r, tr) => {
    document.querySelectorAll('#relationtable tbody tr.sel').forEach((x) => x.classList.remove('sel'));
    tr.classList.add('sel'); S.relType = r.relation_type; showRelationPillars(r.relation_type);
  }, { cls: 'dense' }));
}

async function showRelationPillars(rtype) {
  const box = $('#relationpillars'); box.innerHTML = ''; $('#relationmonths').innerHTML = '';
  const my = seqStart('relationpillars');
  const ep = S.epoch;
  const rows = await get('/api/relation_pillars',
    { id: S.id, record_kind: S.relKind, type: rtype });
  if (seqStale('relationpillars', my) || ep !== S.epoch) return;
  box.appendChild(el('h3', null, rtype + ' 命中的完整流月柱（反向查询）· ' +
    rows.length + ' 柱'));
  box.appendChild(table([
    ['flow_pillar', '流月柱', (r) => '<span class="font-bold font-mono text-sm c-up whitespace-nowrap">' + esc(r.flow_pillar) + '</span>'],
    ['month_branch', '月支'], ['solar_term', '节气'],
    ['months_undisputed', '无争议月份'], ['months_any', '含争议'],
    ['disputed_months', '交节争议'],
    ['up_n', '涨', (r) => '<span class="tabular-num c-up font-bold">' + r.up_n + '</span>'],
    ['down_n', '跌', (r) => '<span class="tabular-num c-down font-bold">' + r.down_n + '</span>'],
    ['flat_n', '平', (r) => '<span class="tabular-num text-slate-400">' + r.flat_n + '</span>'],
    ['up_rate', '上涨占比', (r) => {
      if (r.up_rate === null || r.up_rate === undefined) return '<span class="dim">—</span>';
      const p = pct(r.up_rate);
      const pctNum = Math.min(Math.max(r.up_rate * 100, 0), 100).toFixed(1);
      return '<div class="flex items-center justify-center gap-1.5 whitespace-nowrap"><span class="tabular-num font-bold c-up w-10 text-right">' + p + '</span><div class="w-12 theme-card-sub rounded-full h-1.5 overflow-hidden border border-slate-700/40"><div class="bg-red-500 h-1.5 rounded-full" style="width: ' + pctNum + '%"></div></div></div>';
    }],
    ['mean_return', '平均涨跌', (r) => '<span class="tabular-num font-bold ' + (r.mean_return > 0 ? 'c-up' : r.mean_return < 0 ? 'c-down' : 'c-flat') + '">' +
      ret(r.mean_return) + '</span>'],
    ['events', '事件数'], ['positions', '柱位'],
  ], rows, (r, tr) => {
    document.querySelectorAll('#relationpillars tbody tr.sel')
      .forEach((x) => x.classList.remove('sel'));
    tr.classList.add('sel'); showRelationMonths(rtype, r.flow_pillar);
  }, { cls: 'dense' }));
  if (rows.length) await showRelationMonths(rtype, rows[0].flow_pillar);
}

async function showRelationMonths(rtype, pillar) {
  const box = $('#relationmonths'); box.innerHTML = '';
  const my = seqStart('relationmonths');
  const ep = S.epoch;
  const rows = await get('/api/relation_months',
    { id: S.id, record_kind: S.relKind, type: rtype, pillar });
  if (seqStale('relationmonths', my) || ep !== S.epoch) return;
  box.appendChild(el('h3', null, rtype + ' × ' + pillar + ' 每一次历史出现 · ' +
    rows.length + ' 次'));
  box.appendChild(table([
    ['month_id', '月ID', (r) => '<span class="mono">' + esc(r.month_id) + '</span>'],
    ['solar_year', '年份'], ['start_term', '节气'],
    ['flow_pillar', '流月柱', (r) => '<span class="font-mono font-bold">' + esc(r.flow_pillar) + '</span>'],
    ['stem_ten_god', '流月干十神', (r) => r.ten_god_status === 'OK'
      ? '<span class="px-1.5 py-0.2 rounded text-[10px] bg-indigo-500/10 border border-indigo-500/30 text-indigo-400">' + esc(r.stem_ten_god) + '</span>'
      : '<span class="text-amber-500 text-[10px]">无档案</span>'],
    ['hidden_stem_summary', '地支藏干十神', (r) => r.hidden_stem_summary
      ? '<span class="text-[10px] text-slate-400 whitespace-nowrap">' + esc(r.hidden_stem_summary) + '</span>'
      : '<span class="text-slate-600">—</span>'],
    ['first_date', '区间', (r) => esc(r.first_date + ' ~ ' + r.last_date)],
    ['direction', '涨跌', (r) => '<span class="pill ' + dirCls(r.direction) + '">' +
      dirTxt(r.direction) + '</span>'],
    ['body_return', '涨跌幅', (r) => '<span class="' + dirCls(r.direction) + '">' +
      ret(r.body_return) + '</span>'],
    ['event_count', '该月内该类型事件数'], ['positions', '作用柱位'],
    ['undisputed', '样本', (r) => r.undisputed ? '<span class="tag ok">无争议</span>'
      : '<span class="tag warn">不计入涨跌统计</span>'],
    ['events', '命中明细', (r) => r.events.map((e) => {
      const bits = [posTxt(e)];
      if (e.family === 'stem') bits.push('流月干' + (r.flow_pillar || '')[0] + ' vs 本命干（' +
        (e.flow_element || '') + ' vs ' + (e.natal_element || '') + '）');
      else if (e.family === 'branch') bits.push('流月支' + (r.flow_pillar || '')[1] + ' vs 本命支（' +
        (e.flow_element || '') + ' vs ' + (e.natal_element || '') + '）');
      if (e.structure_combo) bits.push('结构 ' + e.structure_combo +
        (e.present_branches && e.present_branches !== '[]' ? ' 已有' + e.present_branches : '') +
        (e.missing_branches && e.missing_branches !== '[]' ? ' 缺' + e.missing_branches : ''));
      if (e.partial_kind) bits.push(e.partial_kind);
      if (e.wang_branch_present !== null && e.wang_branch_present !== undefined)
        bits.push('旺支 ' + e.wang_branch_present);
      if (e.origin_type) bits.push('来源 ' + e.origin_type);
      if (e.svu) bits.push('标记 ' + e.svu);
      if (e.candidate_transformation_element) bits.push('候选合化 ' +
        e.candidate_transformation_element);
      bits.push(e.transformation_established === null ? '合化成立=未知'
        : '合化成立=' + (e.transformation_established ? '是' : '否'));
      if (e.establishment_rule_id) bits.push('规则 ' + e.establishment_rule_id);
      if (e.fact_direction_marker) bits.push('方向 ' + e.fact_direction_marker);
      return '<div class="ev"><b>' + esc(e.relation_type) + '</b> · ' + esc(bits.join(' · ')) +
        (e.note ? '<br><span class="small">' + esc(e.note) + '</span>' : '') + '</div>';
    }).join('')],
  ], rows, (r) => showMonth(r.month_id), { cls: 'dense' }));
}
function posChar(p) { return { year: '年', month: '月', day: '日', multi: '多' }[p] || p || ''; }
/* 多支结构（半合/三合/三会/三刑）没有单一柱位，柱位取引擎记录的 occupied_positions，
   这样页面能说清“流月寅与本命年支午、日支午半合”，而不是含糊写成“日主冲月支”。 */
function posTxt(r) {
  if (r.natal_position && r.natal_position !== 'multi') return posChar(r.natal_position);
  let occ = []; try { occ = JSON.parse(r.occupied_positions || '[]'); } catch (e) { occ = []; }
  return occ.length ? '多支·本命' + occ.map(posChar).join('/') + '支' : '多支';
}

/* ---------------- page 5: 流月机会表（全市场横向表，可往前/往后选月） ---------------- */
/* 各列权重基准：八字/五行竖排节省空间，上涨占比进度条置于数字下方；
   腾出的空间倾斜给命中关系特征列（18%），历次年份每2个换行，彻底消除错位重叠。 */
const OPP_COL_WEIGHTS = {
  name: 9.6,
  seg_info: 11.0,
  seg_status: 5.2,
  seg_hit: 4.8,
  seg_mean: 4.5,
  seg_min: 4.5,
  seg_len: 4.0,
  seg_actual: 5.6,
  seg_best: 4.8,
  seg_stab: 5.4,
  seg_cons: 6.4,
  window_tag: 7.0,
  valid_invalid: 4.8,
  natal_year_pillar: 6.6,
  natal_element: 4.0,
  flow_pillar: 3.8,
  up_n_ALL: 4.4,
  up_rate_ALL: 5.2,
  actual_return: 5.6,
  up_rate_excl_ALL: 4.8,
  mean_body_return_ALL: 4.8,
  median_body_return_ALL: 4.5,
  min_body_return_ALL: 4.5,
  max_body_return_ALL: 4.5,
  next_rate: 5.0,
  excess_mean: 4.6,
  stability: 4.6,
  consistency: 8.4,
  occurrence_years: 7.0,
  relation_types: 8.6,
  cap_tier: 3.2,
};

/* 机会表默认排序：稳健分（证据强度从强到弱）；用户可以在筛选栏的「排序」里换回平均涨跌等 */
const OPP_DEFAULT_SORT = 'stability';
/* 列设置：默认隐藏的列（整页同一柱的「流月(五行)」、命盘五行、历次年份）；导出始终含全部列 */
const OPP_COLS_KEY = 'bazi_opp_hidden_cols_v1';
const OPP_COLS_DEFAULT_HIDDEN = ['natal_element', 'flow_pillar', 'occurrence_years'];
function oppHiddenCols() {
  if (!S.oppHiddenCols) {
    let v = null;
    try { v = JSON.parse(localStorage.getItem(OPP_COLS_KEY) || 'null'); } catch (e) { v = null; }
    S.oppHiddenCols = new Set(Array.isArray(v) ? v : OPP_COLS_DEFAULT_HIDDEN);
  }
  return S.oppHiddenCols;
}
function saveOppHiddenCols() {
  try { localStorage.setItem(OPP_COLS_KEY, JSON.stringify([...oppHiddenCols()])); } catch (e) {}
}

/* 100%段模式是另一张表（12 列），沿用整月那张的权重会把名称撑太宽、把稳健/一致性挤成三行；
   这里单独一套权重，只在 segMode 生效，整月机会表不受影响。 */
const SEG_COL_WEIGHTS = {
  name: 7.6, natal_year_pillar: 6.4, seg_info: 13.0, seg_status: 5.2, seg_actual: 6.0,
  seg_hit: 5.2, seg_mean: 4.8, seg_min: 4.8, seg_best: 5.2, seg_len: 4.2,
  seg_stab: 7.0, seg_cons: 10.0,
};
function oppColWeight(key) {
  if (S.segMode && SEG_COL_WEIGHTS[key] != null) return SEG_COL_WEIGHTS[key];
  return OPP_COL_WEIGHTS[key] || 6.0;
}
function getOppColWidths(showCols) {
  const total = showCols.reduce((sum, c) => sum + oppColWeight(c[0]), 0);
  return showCols.map((c) => (oppColWeight(c[0]) / total * 100).toFixed(2) + '%');
}
const oppPolicy = () => (S.opp && S.opp.params && S.opp.params.policy) || 'ALL';
const oppDir = () => (S.opp && S.opp.params && S.opp.params.dir) || 'up';
const oppDirTxt = () => oppDir() === 'down' ? '下跌' : '上涨';
const oppRate = (r) => {
  const down = oppDir() === 'down';
  return oppPolicy() === 'ALL'
    ? (down ? r.down_rate_ALL : r.up_rate_ALL)
    : (down ? r.down_rate_ES : r.up_rate_ES);
};
const oppRateExcl = (r) => {
  const down = oppDir() === 'down';
  return oppPolicy() === 'ALL'
    ? (down ? r.down_rate_excl_ALL : r.up_rate_excl_ALL)
    : (down ? r.down_rate_excl_ES : r.up_rate_excl_ES);
};
const oppDenExcl = (r) => oppPolicy() === 'ALL' ? r.n_excl_ALL : r.n_excl_ES;
const oppMean = (r) => oppPolicy() === 'ALL'
  ? r.mean_body_return_ALL : r.mean_body_return_ES;
const oppDen = (r) => oppPolicy() === 'ALL' ? r.all_sample_n : r.exclude_single_n;
const firstNum = (...vs) => { for (const v of vs) if (typeof v === 'number') return v; return null; };
/* 两套接口的字段名不同：月层 payload 用 up_n_ALL，旬/段层用 ALL_up_n。
   以前这里只认一种，另一种取不到就变成 undefined，页面再用"历次出现次数"兜底 ⇒ 会显示成 7/0/0 这种假数。 */
const oppUpDown = (r) => oppPolicy() === 'ALL'
  ? [firstNum(r.ALL_up_n, r.up_n_ALL), firstNum(r.ALL_down_n, r.down_n_ALL), firstNum(r.ALL_flat_n, r.flat_n_ALL)]
  : [firstNum(r.EXCLUDE_SINGLE_up_n, r.up_n_EXCLUDE_SINGLE),
     firstNum(r.EXCLUDE_SINGLE_down_n, r.down_n_EXCLUDE_SINGLE),
     firstNum(r.EXCLUDE_SINGLE_flat_n, r.flat_n_EXCLUDE_SINGLE)];

/* ---------------- 页面五：表头多列排序 ----------------
   点击表头把该列追加到排序链；再次点击切换方向；标签按点击顺序决定优先级。
   后端用 msort=key:dir,... 排序后再截断，保证第一键选进来的行不会被客户端重排漏掉。 */
const OPP_SORT_META = {
  up_rate_ALL: {
    key: 'up_rate', def: 'desc',
    label: () => '历史' + oppDirTxt() + '占比',
    dir: (d) => d === 'desc' ? '高→低' : '低→高',
  },
  up_rate_excl_ALL: {
    key: 'up_rate_excl', def: 'desc',
    label: () => '当时胜率',
    dir: (d) => d === 'desc' ? '高→低' : '低→高',
  },
  mean_body_return_ALL: {
    key: 'mean', def: 'desc', label: () => '平均涨跌',
    dir: (d) => d === 'desc' ? '高→低' : '低→高',
  },
  excess_mean: {
    key: 'excess', def: 'desc', label: () => '同期超额',
    dir: (d) => d === 'desc' ? '高→低' : '低→高',
  },
  min_body_return_ALL: {
    key: 'min_gain', def: 'desc', label: () => '最差一次',
    dir: (d) => d === 'desc' ? '高→低' : '低→高',
  },
  max_body_return_ALL: {
    key: 'max_gain', def: 'desc', label: () => '最好一次',
    dir: (d) => d === 'desc' ? '高→低' : '低→高',
  },
  stability: {
    key: 'stability', def: 'asc',
    label: () => oppDir() === 'down' ? '历史跌幅差异' : '稳健度',
    dir: (d) => oppDir() === 'down'
      ? (d === 'asc' ? '低→高' : '高→低')
      : (d === 'asc' ? '稳→弱' : '弱→稳'),
  },
  consistency: {
    key: 'consistency', def: 'asc', label: () => '一致性',
    dir: (d) => d === 'asc' ? '一致→差异大' : '差异大→一致',
  },
};
const OPP_SORT_BY_KEY = Object.fromEntries(
  Object.values(OPP_SORT_META).map((m) => [m.key, m]));
const OPP_SORT_LIMIT = 6;

function oppMsortClean(list) {
  const out = []; const seen = new Set();
  (Array.isArray(list) ? list : []).forEach((x) => {
    if (!x || !OPP_SORT_BY_KEY[x.key] || seen.has(x.key)) return;
    out.push({ key: x.key, dir: x.dir === 'asc' ? 'asc' : 'desc' });
    seen.add(x.key);
  });
  return out.slice(0, OPP_SORT_LIMIT);
}
const oppMsortParam = () => (S.oppMsort || []).map((x) => x.key + ':' + x.dir).join(',');
const oppSortLabel = (key) => (OPP_SORT_BY_KEY[key] ? OPP_SORT_BY_KEY[key].label() : key);
const oppSortDir = (key, dir) => (OPP_SORT_BY_KEY[key] ? OPP_SORT_BY_KEY[key].dir(dir) : dir);

function syncOppSortUi() {
  S.oppMsort = oppMsortClean(S.oppMsort);
  // 最低/最高月涨幅只在上涨方向有列；切到下跌方向时不能留下一个作用在隐藏字段上的标签。
  if ($('#o-dir') && $('#o-dir').value === 'down') {
    S.oppMsort = S.oppMsort.filter((x) => x.key !== 'min_gain' && x.key !== 'max_gain');
  }
  const bar = $('#o-sort-chips'), list = $('#o-sort-chip-list'), sel = $('#o-sort');
  if (!bar || !list) return;
  const active = S.oppMsort.length > 0;
  bar.classList.toggle('hidden', !active);
  if (sel) {
    sel.disabled = active;
    sel.classList.toggle('hidden', active);
    if (active) {
      if (!sel.dataset.baseTitle) sel.dataset.baseTitle = sel.title || '';
      sel.title = '当前是多列排序；点上方标签可切换方向或删除，清除排序后恢复这里的选择。';
    } else if (sel.dataset.baseTitle) {
      sel.title = sel.dataset.baseTitle;
    }
  }
  if (!active) { list.innerHTML = ''; return; }
  list.innerHTML = S.oppMsort.map((x, i) =>
    '<span class="opp-sort-chip" data-i="' + i + '" title="点击切换方向；点 × 删除">' +
      '<b class="opp-sort-prio">' + (i + 1) + '</b>' +
      '<span class="opp-sort-name">' + esc(oppSortLabel(x.key)) + '</span>' +
      '<span class="opp-sort-dir">' + esc(oppSortDir(x.key, x.dir)) + '</span>' +
      '<button type="button" class="opp-sort-x" data-rm="' + i + '" title="删除这个排序">×</button>' +
    '</span>').join('');
}

function toggleOppSort(uiKey) {
  const meta = OPP_SORT_META[uiKey];
  if (!meta) return;
  const hit = S.oppMsort.find((x) => x.key === meta.key);
  if (hit) hit.dir = hit.dir === 'desc' ? 'asc' : 'desc';
  else S.oppMsort.push({ key: meta.key, dir: meta.def });
  syncOppSortUi();
  saveFiltersNow();
  renderOpportunity();
}

function removeOppSort(i) {
  S.oppMsort.splice(i, 1);
  syncOppSortUi();
  saveFiltersNow();
  renderOpportunity();
}

function clearOppSort() {
  S.oppMsort = [];
  syncOppSortUi();
  saveFiltersNow();
  renderOpportunity();
}

function bindOppSortHeaders(tbl, showCols) {
  if (S.segMode) return;  // 100%段按「本月位置」排序，表头多列排序不参与
  const ths = [...tbl.querySelectorAll('thead th')];
  showCols.forEach((c, i) => {
    const meta = OPP_SORT_META[c[0]];
    if (!meta) return;
    const th = ths[i];
    if (!th) return;
    const pos = S.oppMsort.findIndex((x) => x.key === meta.key);
    th.classList.add('opp-sortable');
    const baseTip = th.dataset.tip || th.title || '';
    const sortTip = '点击按此列加入多列排序；再次点击切换方向。标签按点击顺序优先。';
    th.dataset.tip = baseTip ? (baseTip + '\n\n' + sortTip) : sortTip;
    th.removeAttribute('title');
    if (pos >= 0) {
      th.classList.add('opp-sort-active');
      const badge = document.createElement('span');
      badge.className = 'opp-sort-head';
      badge.textContent = String(pos + 1);
      th.appendChild(document.createTextNode(' '));
      th.appendChild(badge);
    }
    th.addEventListener('click', () => toggleOppSort(c[0]));
  });
}

/* ---------------- 页面五 Excel 导出 ----------------
   导出列不是另起一张清单，而是从「这一屏真正渲染出来的 cols」逐个摊平得到的，
   所以表头改了导出一定跟着改；哪一列没有定义就整表拒绝导出，绝不静默留空列。 */
const OPP_CODE_CN = {
  MONTH_PILLAR: '月柱归属有争议', YEAR_AND_MONTH_PILLAR: '年柱与月柱归属均有争议',
  ELIGIBLE_UNDISPUTED: '可入样本（无争议）', EXCLUDED_NO_PROFILE: '剔除·无命盘',
  EXCLUDED_BOUNDARY_DISPUTED: '剔除·交节争议',
};
const oppCodeCn = (c) => (!c || c === 'NONE' || c === 'NOT_APPLICABLE')
  ? '' : (OPP_CODE_CN[c] || c);
const oppDirCn = (d) => ({ UP: '涨', DOWN: '跌', FLAT: '平' })[String(d || '').toUpperCase()] || '无行情';
const PCT1 = '0.0%';
const PCT2 = '+0.00%;-0.00%;0.00%';

/* 三态判定只写一份，页面渲染与导出共用（关系格不能显示成「（无）」了事） */
function oppRelParts(r) {
  if (!r.occurrence_count) return { kind: 'nohistory', items: [] };
  if (r.has_natal_profile === 0) return { kind: 'noprofile', items: [] };
  let arr = [];
  try { arr = JSON.parse(r.relation_types || '[]'); } catch (e) { arr = r.relation_types ? [r.relation_types] : []; }
  return { kind: arr.length ? 'list' : 'empty', items: arr };
}
const OPP_REL_TEXT = { nohistory: '历史无同柱，不推断', noprofile: '无命盘 · 不计算关系', empty: '（无）' };

const OPP_EXPORT = {
  name: () => [
    ['证券代码', (r) => r.security_code, 't', 12],
    ['名称', (r) => r.name, 't', 16],
    ['市场', (r) => oppMarketOf(r.security_code), 't', 8],
    ['类型', (r) => typeLabel(r.security_type), 't', 8],
    ['上市日', (r) => r.listed_date, 't', 11],
    ['退市日', (r) => r.delisted_date, 't', 11],
    ['无命盘', (r) => (r.has_natal_profile === 0 ? '是' : ''), 't', 8],
    ['交节争议', (r) => oppCodeCn(r.boundary_conflict), 't', 18],
  ],
  seg_info: () => [
    ['100%段（区间）', (r) => r._seg
      ? (r._seg.start_day + '日' + r._seg.start_time + '→' + r._seg.end_day + '日' + r._seg.end_time)
      : '', 't', 20],
    ['真实起始', (r) => (r._seg && r._seg.start_date
      ? (r._seg.start_exact ? '' : '≈') + r._seg.start_date + ' ' + r._seg.start_time : ''), 't', 18],
    ['真实结束', (r) => (r._seg && r._seg.end_date
      ? (r._seg.end_exact ? '' : '≈') + r._seg.end_date + ' ' + r._seg.end_time : ''), 't', 18],
  ],
  seg_actual: () => {
    const w = (S.segCtx && S.segCtx.offset !== 0) ? '该月' : '本月';
    return [
      [w + '实际涨跌幅', (r) => {
        const s = r._seg || {};
        const lv = s.status === 'not_current' ? s.month_actual : s.latest;
        return lv ? lv.e_bp / 100 : null;
      }, 'p2', 14],
      [w + '实际状态', (r) => {
        const s = r._seg || {};
        const lv = s.status === 'not_current' ? s.month_actual : s.latest;
        if (s.status === 'not_current') {
          if (!lv) return (S.segCtx && S.segCtx.offset > 0) ? '未到' : '无60分钟线';
          return lv.complete ? '已走完' : ('数据不全 ' + lv.covered + '/' + lv.total);
        }
        if (!lv) return s.status === 'upcoming' ? '未到' : '无行情';
        if (!lv.complete) return '进行中 ' + lv.covered + '/' + lv.total;
        return '已走完 ' + (lv.asof_dt || '');
      }, 't', 20],
    ];
  },
  seg_best: () => [['最好一次', (r) => (r._seg && r._seg.best_pct != null ? r._seg.best_pct / 100 : null), 'p2', 10]],
  seg_stab: () => [
    ['段内稳健度', (r) => (r._seg && r._seg.stab ? r._seg.stab.cn : ''), 't', 10],
    ['段内稳健度说明', (r) => (r._seg && r._seg.stab ? r._seg.stab.reason : ''), 't', 46],
  ],
  seg_cons: () => [
    ['段内一致性', (r) => (r._seg && r._seg.cons ? r._seg.cons.level : ''), 't', 10],
    ['段内一致性CV', (r) => (r._seg && r._seg.cons ? r._seg.cons.cv : null), 'f2', 12],
  ],
  seg_status: () => [
    ['本月状态', (r) => r._seg ? r._seg.status_text : '', 't', 14],
  ],
  seg_len: () => [
    ['长度（交易日）', (r) => r._seg ? r._seg.length_days : null, 'f2', 10],
  ],
  seg_hit: () => {
    const w = (S.segCtx && S.segCtx.offset !== 0) ? '该月' : '本月';
    return [
      ['段内命中', (r) => r._seg ? (r._seg.n_valid + '/' + r._seg.n_avail + ' · 100%') : '', 't', 12],
      ['含' + w + '重算', (r) => {
        const s = r._seg || {};
        const lv = s.status === 'not_current' ? s.month_actual : s.latest;
        if (!lv || !lv.complete) return '';
        const m = oppDir() === 'down' ? lv.e_bp < 0 : lv.e_bp > 0;
        return (s.n_valid + (m ? 1 : 0)) + '/' + (s.n_avail + 1);
      }, 't', 12],
    ];
  },
  seg_mean: () => [
    ['段平均（同段历次）', (r) => r._seg ? r._seg.mean_pct / 100 : null, 'p2', 12],
  ],
  seg_min: () => [
    ['段最差（同段历次）', (r) => r._seg ? (oppDir() === 'down' ? r._seg.max_pct : r._seg.min_pct) / 100 : null, 'p2', 12],
  ],
  window_tag: () => [
    ['窗位及区间', (r) => r.window_tag || (r.xun_name ? (r.xun_name + ' (' + r.day_from + '–' + (r.day_to || '下节') + '日)') : ''), 't', 16],
    ['旬/段编号', (r) => r.week_no != null ? ('第' + r.week_no + '段') : (r.xun_no != null ? ('第' + r.xun_no + '旬') : ''), 't', 10],
  ],
  valid_invalid: () => [
    ['有效窗口数', (r) => (oppPolicy() === 'ALL' ? r.ALL_valid_n : r.exclude_single_n), 'i', 10],
    ['空窗口数', (r) => r.ALL_invalid_n, 'i', 10],
    ['应有窗口总数', (r) => r.all_sample_n, 'i', 12],
  ],
  natal_year_pillar: () => [
    ['年柱', (r) => r.natal_year_pillar, 't', 7],
    ['月柱', (r) => r.natal_month_pillar, 't', 7],
    ['日柱', (r) => r.natal_day_pillar, 't', 7],
    ['日主', (r) => (r.has_natal_profile === 0 ? '' : (r.day_master_stem || '') + (r.day_master_element || '')), 't', 7],
  ],
  natal_element: () => [
    ['标的五行(年干支·月干支·日干支)', (r) => (r.has_natal_profile === 0 ? '无命盘'
      : ['year_stem_element', 'year_branch_element', 'month_stem_element', 'month_branch_element',
         'day_stem_element', 'day_branch_element'].map((k) => r[k] || '—').join(' ')), 't', 22],
  ],
  flow_pillar: () => [
    ['流月柱', (r) => r.flow_pillar, 't', 8],
    ['流月五行(干/支)', (r) => (r.stem_element || '—') + '/' + (r.branch_element || '—'), 't', 14],
  ],
  stem_element: () => [['流月五行(干/支)', (r) => (r.stem_element || '—') + '/' + (r.branch_element || '—'), 't', 14]],
  cap_tier: () => [
    ['市值档', (r) => r.cap_tier || '', 't', 8],
    ['自由流通市值(亿)', (r) => r.cap_free_mv_yi, 'f1', 12],
  ],
  up_n_ALL: () => [
    ['涨次数', (r) => oppUpDown(r)[0], 'i', 8],
    ['跌次数', (r) => oppUpDown(r)[1], 'i', 8],
    ['平次数', (r) => oppUpDown(r)[2], 'i', 8],
    ['出现次数(本表口径分母)', (r) => oppDen(r), 'i', 20],
  ],
  up_rate_ALL: (ctx) => [[ctx.label + '（含所选月，分母是左边出现次数）', (r) => oppRate(r), 'p1', 20]],
  actual_return: () => [
    ['所选月实际', (r) => (r.in_sample ? oppDirCn(r.actual_direction) : '该月无行情'), 't', 12],
    ['所选月涨跌幅', (r) => (r.in_sample ? r.actual_return : null), 'p2', 12],
    ['所选月相对基准超额', (r) => (r.in_sample ? r.actual_excess : null), 'p2', 18],
    ['所选月交易日数', (r) => (r.in_sample ? r.actual_trading_days : null), 'i', 15],
    ['所选月样本状态', (r) => oppCodeCn(r.actual_sample_status), 't', 18],
  ],
  up_rate_excl_ALL: () => [
    ['当时胜率(剔除所选月·回测)', (r) => oppRateExcl(r), 'p1', 22],
    ['剔除后分母', (r) => oppDenExcl(r), 'i', 11],
  ],
  mean_body_return_ALL: () => [['平均涨跌幅', (r) => oppMean(r), 'p2', 12]],
  median_body_return_ALL: () => [['中位数涨跌幅', (r) => r.median_body_return_ALL, 'p2', 12]],
  min_body_return_ALL: () => [
    ['最低月涨幅(同柱全月最低)', (r) => (r.stab_min_ret !== undefined && r.stab_min_ret !== null) ? r.stab_min_ret : r.min_body_return_ALL, 'p2', 14],
    ['最低月涨幅样本数', (r) => r.stab_n, 'i', 12],
  ],
  max_body_return_ALL: () => [
    ['最高月涨幅(同柱全月最高)', (r) => (r.stab_max_ret !== undefined && r.stab_max_ret !== null) ? r.stab_max_ret : r.max_body_return_ALL, 'p2', 14],
    ['最高月涨幅样本数', (r) => r.stab_n, 'i', 12],
  ],
  excess_mean: () => [['同期超额', (r) => r.excess_mean, 'p2', 11]],
  sample_marker_ALL: () => [['样本等级', (r) => oppGrade(r), 't', 9]],
  stability: (ctx) => [
    [ctx.stabHead, (r) => r.stability_level_cn || '—', 't', 8],
    [ctx.stabScoreHead, (r) => r.stability_score, 'f1', 8],
    ['样本数', (r) => r.stab_n, 'i', 8],
    ['样本等级', (r) => oppGrade(r), 't', 9],
    ['安全边际(距0)', (r) => r.stab_margin, 'p2', 11],
    ['边际/振幅', (r) => r.stab_margin_ratio, 'f2', 10],
    [ctx.stabShareHead, (r) => r.stab_extreme_share, 'p1', 12],
    ['同柱振幅', (r) => r.stab_range, 'p2', 10],
    [ctx.stabMeanHead, (r) => r.stab_mean_ex, 'p2', 13],
    ['稳健理由', (r) => r.stability_reason || '', 't', 40],
  ],
  consistency: () => [
    ['历史幅度一致性', (r) => r.cons_level || '—', 't', 12],
    ['方向样本k', (r) => r.cons_n, 'i', 9],
    ['总样本N', (r) => r.cons_total, 'i', 9],
    ['方向CV', (r) => r.cons_cv, 'f2', 8],
    ['方向最小幅度', (r) => r.cons_min, 'p2', 12],
    ['反向/非方向次数', (r) => r.cons_reverse_n, 'i', 14],
    ['全月最差收益', (r) => r.cons_worst_all, 'p2', 12],
    ['幅度底线≥1%', (r) => (Number(r.cons_floor) === 1 ? '达标' : '不足'), 't', 12],
    ['单次影响drop', (r) => r.cons_drop, 'p2', 12],
    ['一致性理由', (r) => r.consistency_reason || '', 't', 48],
  ],
  occurrence_years: () => [
    ['历次年份', (r) => (r.occurrence_years || '').split(';').map((s) => s.trim()).filter(Boolean).join('; '), 't', 24],
    ['相邻间隔(年)', (r) => r.year_gaps, 't', 12],
    ['是否五年重现', (r) => (String(r.is_five_year_repeat) === '1' ? '是' : ''), 't', 13],
  ],
  relation_types: (ctx) => [[ctx.label, (r) => {
    const p = oppRelParts(r);
    return p.kind === 'list' ? p.items.join(' / ') : OPP_REL_TEXT[p.kind];
  }, 't', 40]],
  next_rate: (ctx) => [
    ['下月 ' + ctx.nextPillar + ' ' + ctx.DT + '占比', (r) => r.next_rate, 'p1', 20],
    ['下月平均涨跌幅', (r) => r.next_mean, 'p2', 14],
    ['下月出现次数', (r) => r.next_n, 'i', 13],
    ['下月' + ctx.DT + '次数', (r) => (oppDir() === 'down' ? r.next_down : r.next_up), 'i', 13],
  ],
};

const OPP_FMT = { t: null, i: '0', f1: '0.0', f2: '0.00', p1: PCT1, p2: PCT2 };

/* 表上每一列摊平成导出列；任何一列没有定义就返回 null，让按钮说明原因而不是导出半张表。 */
function oppExportSpec(showCols, out) {
  const down = oppDir() === 'down';
  const ctx = { DT: oppDirTxt(), label: '',
    nextPillar: (out.next_month || {}).month_pillar || '',
    stabHead: down ? '历史跌幅差异' : '稳健度',
    stabScoreHead: down ? '稳健分' : '稳健分',
    stabShareHead: down ? '最惨一次占跌幅' : '最好一次占比',
    stabMeanHead: down ? '剔除最惨一次均值' : '剔除最好一次均值' };
  const cols = [];
  for (const c of showCols) {
    const build = OPP_EXPORT[c[0]];
    if (!build) return null;
    ctx.label = c[1];
    build(ctx).forEach(([h, v, f, w]) => cols.push({ h, v, z: OPP_FMT[f], w }));
  }
  cols.unshift({ h: '序号', v: (r) => r.rn, z: '0', w: 6 });
  return cols;
}

/* 本机时钟在美西，文件名与说明页的时间一律显式取北京时间 */
function bjStamp() {
  const p = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
  }).formatToParts(new Date()).reduce((a, x) => (a[x.type] = x.value, a), {});
  return '' + p.year + p.month + p.day + '-' + p.hour + p.minute;
}

const Y_N = (v) => (v ? '是' : '否');

/* 参数页照抄服务端已下发的口径与 caveats，页面不另写一套说法 */
function oppMetaAoa(b) {
  const { out, m } = b;
  const p = out.params || {};
  const t = m.totals || {};
  const a = m.actual_summary;
  const rows = [['页面五 流月机会表 · ' + (out.market_label || '全市场') + ' 按节气月查看（本文件是表上当前这一屏的原样导出）']];
  const put = (k, v) => rows.push([k, v === null || v === undefined || v === '' ? '—' : String(v)]);
  put('数据集', out.dataset + '（' + (out.market_label || '') + '）');
  put('所选流月', m.month_id + ' · ' + m.pillar + ' · ' + m.label);
  put('起于节气', m.start_term + ' ' + date10(m.start_at) + ' → ' + date10(m.end_at));
  put('该月性质', m.is_past ? '历史月（行情已入库）' : (m.is_current ? '本月' : '未来月（行情未入库）'));
  put('该月全市场行情行数', (m.market_rows_in_month || 0).toLocaleString('en-US'));
  put('门槛内证券数 / 本表已列', (t.in_scope || 0).toLocaleString('en-US') + ' / ' + b.rows.length +
    ((t.pages || 0) > 1
      ? '（第 ' + (t.page || 1) + ' / ' + t.pages + ' 页；本文件只含当前这一页）'
      : '（全表）'));
  if (a) put('本表该月实际', '涨 ' + a.up + ' / 跌 ' + a.down + ' / 平 ' + a.flat +
    (a.listed - a.with_data > 0 ? ' / 无行情 ' + (a.listed - a.with_data) : '') +
    '，本表均值 ' + pct(a.mean_return) + ' · 全市场该月上涨率 ' + pct(a.market_up_rate) +
    '、均值 ' + pct(a.market_mean_return));
  put('出现次数下限', p.min_n > 0 ? '≥' + p.min_n + ' 次' : '不限（含 0 次出现）');
  put('样本等级筛选', p.grade || '任意');
  put('占比筛选', (p.rate_min === null || p.rate_min === undefined) ? '不限'
    : (p.rate_eq ? '=100%' : '≥' + p.rate_min + '%') + '（方向 ' + (p.dir === 'down' ? '下跌' : '上涨') + '）');
  const SORT_CN = { mean: '平均涨跌幅', stability: '稳健分', consistency: '一致性（CV 升序）',
    up_rate: '历史占比（全部历史）', up_rate_excl: '历史占比（剔除所选月）',
    n: '出现次数', excess: '同期超额', branch: '月支分组', identity: '代码' };
  const msortMeta = Array.isArray(p.msort) ? p.msort : [];
  const sortMetaTxt = msortMeta.length
    ? '多列排序 ' + msortMeta.map((x, i) => (i + 1) + '.' + oppSortLabel(x.key) + ' ' + oppSortDir(x.key, x.dir)).join('，')
    : (SORT_CN[p.sort] || p.sort);
  put('排序', sortMetaTxt + ' · 方向 ' + (p.dir === 'down' ? '下跌（均值/超额为升序）' : '上涨'));
  put('稳健度筛选', !p.stab ? '不限'
    : p.stab === 'exclude_bad'
      ? (p.dir === 'down' ? '排除差异大' : '排除弱')
      : (p.dir === 'down' ? '只看差异小' : '只看稳'));
  put('一致性筛选', !p.cons ? '不限'
    : p.cons === 'exclude_bad' ? '排除差异大'
    : p.cons === 'only_good' ? '只看一致'
    : '只看一致且最小幅度≥1%');
  put('统计口径 policy', p.policy + (p.policy === 'EXCLUDE_SINGLE' ? '（排除单交易日月）' : '（含单交易日月）'));
  put('连续两月', p.both_months ? '是 · 下月 ' + ((out.next_month || {}).month_pillar || '') +
    '（' + ((out.next_month || {}).start_term || '') + ' ' + date10((out.next_month || {}).start_at || '') +
    ' → ' + date10((out.next_month || {}).end_at || '') + '）· 下月占比条件 ' + p.next_rate : '否');
  put('包含已退市 / 包含指数', Y_N(p.include_delisted) + ' / ' + Y_N(p.include_index));
  put('名称或代码搜索', p.q || '（空）');
  put('关系筛选', Object.keys(p.relation_filters || {}).length
    ? JSON.stringify(p.relation_filters) : '（无）');
  put('行情数据截至 input_data_as_of', out.input_data_as_of);
  put('最后有数据流月', out.latest_month_with_data);
  put('冻结历法版本', out.calendar_version);
  put('本次导出时间（北京）', bjStamp());
  put('导出方式', '浏览器本机由 xlsx.full.min.js(SheetJS 0.20.3) 生成，数据全部来自 /api/opportunities 的本机 SQLite 只读查询，未经网络上传。');
  put('涨跌口径', 'body_return = 该流月首个交易日开盘 → 最后交易日收盘，不是行情软件的月涨幅；平盘计入占比分母。');
  rows.push([], ['稳健度 / 历史跌幅差异口径']);
  put('稳健度（上涨方向）', '只看同一根柱的历史证据：样本数、最差一次涨幅、最好一次占累计涨幅。'
    + '安全边际按同柱振幅缩放（最差一次 ÷ max(同柱振幅,2%)）：'
    + '稳＝边际/振幅≥0.30 且 最好一次≤50%；弱＝边际/振幅<0.15 或 最好一次≥70%；其余一般。');
  put('历史跌幅差异（下跌方向）', '同柱振幅≥40% 或 最惨一次占跌幅≥70% 标“高”；'
    + '振幅<20% 且 最惨一次占比<50% 标“低”；其余“中”。'
    + '这是历史形状描述，不是反弹概率。');
  put('稳健分（两方向同一公式）', '100 × (40% 样本置信[95% Wilson 下界]'
    + ' + 35% min((安全边际/振幅)/0.50,1) + 25% (1−极值月占比))；'
    + '安全边际/振幅 = 最差一次 ÷ max(同柱振幅,2%)；只作排序辅助，不是概率。');
  put('个股整体波动', '只在点行抽屉展示，不参与稳健分/筛选；回测里它不降低同柱下一次胜率，主要影响回撤。');
  put('样本分母', '稳健度与稳健分按当前统计口径 policy 计算：ALL 含单交易日月，'
    + 'EXCLUDE_SINGLE 剔除单交易日月；与“出现次数”列同一分母。');
  put('历史幅度一致性（V2）', '只看所选方向的月份：上涨取正收益，下跌取跌幅绝对值；'
    + 'CV=样本标准差/均值；CV≤0.50 一致，0.50<CV≤0.80 一般，CV>0.80 差异大；'
    + '最小方向幅度≥1% 是独立底线；drop 只展示、不参与分档；k<2 样本不足。'
    + '它不是下一次上涨概率，也不把反向月份算进 CV。');
  put('一致性样本分母', 'k=方向月份数（上涨取正收益/下跌取负收益），N=该柱总月份数；'
    + '反向/非方向次数=N−k，包含平盘；全月最差收益是 N 个月里的最小 body_return。');
  rows.push([], ['口径与合规提示（与本页标题悬停内容同源，服务端下发）']);
  (out.caveats || []).forEach((c, i) => rows.push(['提示 ' + (i + 1), c]));
  return rows;
}

function oppSheet(aoa, spec) {
  const ws = XLSX.utils.aoa_to_sheet(aoa);
  (spec || []).forEach((c, ci) => {
    if (!c.z) return;
    for (let ri = 1; ri < aoa.length; ri++) {
      const cell = ws[XLSX.utils.encode_cell({ c: ci, r: ri })];
      if (cell && typeof cell.v === 'number') cell.z = c.z;
    }
  });
  if (spec) {
    ws['!cols'] = spec.map((c) => ({ wch: c.w }));
    ws['!autofilter'] = { ref: XLSX.utils.encode_range({ s: { r: 0, c: 0 },
      e: { r: aoa.length - 1, c: spec.length - 1 } }) };
  } else {
    ws['!cols'] = [{ wch: 30 }, { wch: 120 }];
  }
  return ws;
}

function syncExportBtn() {
  const btn = $('#o-export');
  if (!btn) return;
  const b = S.oppExport;
  const n = b ? b.rows.length : 0;
  btn.disabled = !n;
  btn.title = n
    ? ('导出当前这一屏：' + n + ' 行 × ' + b.spec.length +
       ' 列（全部列，含「列设置」里隐藏的列；按表上排序顺序），另附「口径与参数」页。' +
       '只在本机浏览器内生成，不上传、不访问网络。')
    : '当前表没有行可导出：把出现次数下限、样本等级或占比条件放宽后再试。';
  $('#o-export-msg').textContent = '';
}

function loadXlsxScript() {
  if (typeof XLSX !== 'undefined') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = '/static/xlsx.full.min.js?v=0.20.3';
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('无法加载导出组件 XLSX'));
    document.head.appendChild(s);
  });
}

async function exportOpportunity() {
  const msg = $('#o-export-msg');
  const b = S.oppExport;
  const say = (t, bad) => { msg.textContent = t;
    msg.className = 'text-xs whitespace-nowrap ' + (bad ? 'text-amber-400' : 'text-slate-400'); };
  if (!b || !b.rows.length) { say('当前表没有行可导出。', true); return; }
  try {
    await loadXlsxScript();
  } catch (e) {
    say('导出组件加载失败：' + (e && e.message ? e.message : String(e)), true); return;
  }
  const name = 'liuyue_board_' + (b.out.dataset || 'ashare') + '_' + b.m.month_id +
    '_top' + b.rows.length + '_' + bjStamp() + '.xlsx';
  try {
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, oppSheet(
      [b.spec.map((c) => c.h)].concat(b.rows.map((r) => b.spec.map((c) => {
        const v = c.v(r); return v === undefined ? null : v; }))), b.spec), '机会表');
    XLSX.utils.book_append_sheet(wb, oppSheet(oppMetaAoa(b), null), '口径与参数');
    const buf = XLSX.write(wb, { bookType: 'xlsx', type: 'array' });
    const url = URL.createObjectURL(new Blob([buf], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
    const a = el('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    say('已导出 ' + b.rows.length + ' 行 → ' + name);
  } catch (e) {
    say('导出失败：' + (e && e.message ? e.message : String(e)), true);
  }
}



const oppMarketOf = (code) => {
  const c = code || '';
  return c.endsWith('.SH') ? '沪市' : c.endsWith('.SZ') ? '深市' : c.endsWith('.BJ') ? '北交所' : '';
};

function capTierCell(r, asof) {
  if (!r.cap_tier) return '<span class="dim">—</span>';
  const mv = r.cap_free_mv_yi;
  const mvTxt = (mv === null || mv === undefined) ? '' :
    (Number(mv) >= 100 ? Number(mv).toFixed(0) : Number(mv).toFixed(2)) + '亿';
  const amt = r.cap_avg_amt_20d_yi;
  const amtTxt = (amt === null || amt === undefined) ? '' :
    ' · 20日均额 ' + Number(amt).toFixed(2) + '亿';
  return '<span class="rel-chip" title="自由流通市值 ' + mvTxt + amtTxt +
    '（' + esc(asof || '') + ' 月末快照，不复权）">' + esc(r.cap_tier) + '</span>';
}

function oppNameCell(r) {
  const isIndex = r.security_type === 'INDEX';
  const isEtf = r.security_type === 'ETF';
  const typeTag = isIndex ? '<span class="px-1 py-0.2 rounded text-[9.5px] bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 font-medium whitespace-nowrap">指数</span>' :
                  isEtf ? '<span class="px-1 py-0.2 rounded text-[9.5px] bg-blue-500/10 border border-blue-500/30 text-blue-400 font-medium whitespace-nowrap">ETF</span>' : '';
  const warnTags = [
    r.delisted_date ? '<span class="px-1 py-0.2 rounded text-[9.5px] badge-flat font-medium whitespace-nowrap">退市</span>' : '',
    r.has_natal_profile === 0 ? '<span class="px-1 py-0.2 rounded text-[9.5px] badge-flat font-medium whitespace-nowrap">无命盘</span>' : '',
    r.boundary_conflict && !['NONE', 'NOT_APPLICABLE'].includes(r.boundary_conflict)
      ? '<span class="px-1 py-0.2 rounded text-[9.5px] badge-flat font-medium whitespace-nowrap" title="' + esc(r.boundary_conflict) + '">交节争议</span>' : ''
  ].filter(Boolean).join(' ');

  if (S.segMode) {
    // 名称与代码上下叠：段模式列多、名称列被压窄，横排必然截断
    return '<div class="opp-name1 stack select-none" title="点击整行展开右侧抽屉，可直达个股档案">' +
      '<span class="nm">' + esc(r.name || r.security_code) + '</span>' +
      '<span class="opp-name2"><span class="cd">' + esc(r.security_code) + '</span>' + typeTag + warnTags + '</span>' +
    '</div>';
  }
  return '<div class="opp-name1 select-none" title="点击整行展开右侧抽屉，可直达个股档案">' +
    '<span class="nm">' + esc(r.name || r.security_code) + '</span>' +
    '<span class="cd">' + esc(r.security_code) + '</span>' + typeTag + warnTags +
  '</div>';
}

/* 窗位及区间：竖向两行紧凑排，上方窗位徽标（第X段/上中下旬），下方区间日期，彻底消除溢出压线 */
function oppWindowCell(r) {
  let full = r.window_tag || (r.xun_name ? (r.xun_name + ' (' + r.day_from + '–' + (r.day_to || '下节') + '日)') : '');
  let winName = '';
  let winRange = '';

  if (full) {
    const m = full.match(/^([^\s(（]+)[\s(（]+([^)]*)[)）]?$/);
    if (m) {
      winName = m[1].trim();
      winRange = m[2].replace(/[)）]$/, '').trim();
    } else {
      winName = full.trim();
    }
  } else if (r.week_no) {
    winName = '第' + r.week_no + '段';
    winRange = (r.day_from != null ? r.day_from : '') + '–' + (r.day_to != null ? r.day_to + '日' : '下节');
    full = winName + ' (' + winRange + ')';
  } else if (r.xun_name || r.xun_no) {
    winName = r.xun_name || (r.xun_no === 1 ? '上旬' : r.xun_no === 2 ? '中旬' : '下旬');
    winRange = (r.day_from != null ? r.day_from : '') + '–' + (r.day_to != null ? r.day_to + '日' : '下节');
    full = winName + ' (' + winRange + ')';
  }

  const isXun = S.oppLayer === 'xun';
  const badgeColor = isXun
    ? (r.xun_no === 1 ? 'border-sky-500/40 text-sky-300 bg-sky-500/15'
       : r.xun_no === 2 ? 'border-indigo-500/40 text-indigo-300 bg-indigo-500/15'
       : 'border-purple-500/40 text-purple-300 bg-purple-500/15')
    : (r.week_no <= 2 ? 'border-sky-500/40 text-sky-300 bg-sky-500/15'
       : r.week_no <= 4 ? 'border-indigo-500/40 text-indigo-300 bg-indigo-500/15'
       : 'border-purple-500/40 text-purple-300 bg-purple-500/15');

  if (!winRange) {
    return '<span class="inline-block px-1.5 py-0.5 rounded font-bold ' + badgeColor + ' border text-[11px] whitespace-nowrap" title="' + esc(full) + '">' + esc(winName) + '</span>';
  }

  return '<span class="inline-flex items-center gap-1 whitespace-nowrap select-none" title="' + esc(full) + '">' +
    '<span class="px-1.5 py-0.2 rounded font-bold ' + badgeColor + ' border text-[10.5px]">' + esc(winName) + '</span>' +
    '<span class="text-[10px] text-slate-400 font-mono">' + esc(winRange) + '</span>' +
    '</span>';
}

/* 生辰八字：年 / 月 / 日三柱排成一行，日柱加粗（日主所在柱） */
function oppBirthCell(r) {
  if (r.has_natal_profile === 0)
    return '<span class="dim text-[11px]" title="冻结档案缺该身份三柱，冲合关系不可计算">无命盘</span>';
  return '<span class="opp-bazi1" title="年柱 / 月柱 / 日柱；日主 ' +
    esc((r.day_master_stem || '') + (r.day_master_element || '')) +
    '。时柱档案里虽有，但关系引擎不使用（全库 hour_pillar_used=0），故不显示。">' +
    esc(r.natal_year_pillar || '—') + ' ' + esc(r.natal_month_pillar || '—') + ' ' +
    '<span class="dm">' + esc(r.natal_day_pillar || '—') + '</span></span>';
}

/* 标的五行：竖向紧凑排成年/月/日三柱干支五行，与左侧生辰八字逐行严格对应 */
function oppNatalElementCell(r) {
  if (r.has_natal_profile === 0)
    return '<span class="dim text-[11px]" title="冻结档案缺该身份三柱，没有命盘五行">无命盘</span>';
  const pair = (a, b) => esc(a || '—') + esc(b || '—');
  return '<div class="font-mono text-[11px] text-slate-400 leading-tight flex flex-col items-center justify-center py-0.5" title="年柱干/支 · 月柱干/支 · 日柱干/支，与左侧生辰八字逐行对应；日主 ' +
    esc((r.day_master_stem || '') + (r.day_master_element || '')) +
    '。这是' + (S.dataset === 'etf' ? '该 ETF' : '该股票') +
    '命盘自身的五行，不随流月变化；右侧「流月五行」才是当月柱的五行。">' +
    '<span class="whitespace-nowrap">' + pair(r.year_stem_element, r.year_branch_element) + '</span>' +
    '<span class="whitespace-nowrap">' + pair(r.month_stem_element, r.month_branch_element) + '</span>' +
    '<span class="whitespace-nowrap">' + pair(r.day_stem_element, r.day_branch_element) + '</span>' +
    '</div>';
}

/* 本表这些行在该月到底涨没涨：把「该月实际」列汇总一句，省得自己数 100 行。 */
function actualSummaryTxt(m) {
  const a = m.actual_summary;
  if (!a) return '';
  return ' · 本表该月实际：涨 ' + a.up + ' / 跌 ' + a.down + ' / 平 ' + a.flat +
    (a.listed - a.with_data > 0 ? ' / 无行情 ' + (a.listed - a.with_data) : '') +
    '，本表平均涨跌 ' + ret(a.mean_return) +
    ' · ' + ((S.meta && S.meta.market_label) || '全市场') +
    (((S.meta && S.meta.market_label) || '全市场').endsWith('ETF') ? ' ' : '') +
    '该月上涨家数占比 ' + pct(a.market_up_rate) + '、平均涨跌 ' + ret(a.market_mean_return);
}

const oppGrade = (r) => {
  const n = oppDen(r) || 0;
  return n >= 6 ? '≥6' : n >= 3 ? '3–5' : n > 0 ? '<3' : '0 次';
};

const OPP_STAB_CN = { stable: '稳', mid: '一般', weak: '弱', low: '低', high: '高', none: '—' };
const oppStabCls = (lv) => (lv === 'stable' || lv === 'low') ? 'badge-stab-good'
  : (lv === 'weak' || lv === 'high') ? 'badge-stab-bad'
  : lv === 'mid' ? 'badge-stab-mid' : 'badge-flat';

/* 稳健度/历史跌幅差异：服务端算好 level + reason；前端只负责显示，避免表/抽屉/导出三套阈值。 */
function oppStabilityCell(r) {
  const lv = r.stability_level || 'none';
  if (lv === 'none') return '<span class="dim text-[11px]">—</span>';
  const n = r.stab_n || 0;
  const score = (r.stability_score === null || r.stability_score === undefined)
    ? '' : Number(r.stability_score).toFixed(0) + '分';
  const tip = (r.stability_reason || '') + ' · 样本 ' + n + ' 次' + (score ? ' · ' + score : '');
  return '<span class="px-1.5 py-0.2 rounded text-[10px] ' + oppStabCls(lv) +
    ' font-bold whitespace-nowrap" title="' + esc(tip) + '">' +
    esc(r.stability_level_cn || OPP_STAB_CN[lv]) + ' · ' + n + '</span>';
}

/* V2 历史幅度一致性：方向条件化；k/N 同屏；反向月份和幅度不足用独立小标签。 */
const oppConsCls = (lv) => lv === '一致' ? 'badge-stab-good'
  : lv === '差异大' ? 'badge-stab-bad'
  : lv === '一般' ? 'badge-stab-mid' : 'badge-flat';
const oppConsTone = (lv) => lv === '一致' ? 'good'
  : lv === '差异大' ? 'bad' : lv === '一般' ? 'mid' : 'none';
const CONS_HEAD_TIP = '历史幅度一致性\n'
  + '只看所选方向的月份：上涨取正收益，下跌取跌幅绝对值。\n'
  + 'CV = 样本标准差 ÷ 均值；CV≤0.50 一致，0.50–0.80 一般，>0.80 差异大。\n'
  + '3/3 次 = 方向月份/总月份；反向 1 = 1 次不是该方向。\n'
  + '幅度不足 = 最小方向幅度 <1%；样本不足 = 方向月份 <2。\n'
  + '点行看抽屉明细；这不是下一次上涨概率。';

const MINRET_HEAD_TIP = '最差一次（历史最低月涨幅）\n'
  + '取该柱所有历史出现月份里最低的一次 body_return；100% 上涨时就是涨得最少的一次。\n'
  + '旬/段层：本月已经走完的窗口会按日更补进样本；整月层仍只收完整流月。\n'
  + '为负数说明该柱历史上出现过下跌月，不是“每次都涨”。\n'
  + '下方次数 = 该柱样本数。样本越多，最低值天然越低；跨样本数排序会偏向样本少的票，务必同屏看次数。\n'
  + '它是历史最差一次，不是未来下限，也不是上涨概率。';

const MAXRET_HEAD_TIP = '最好一次（历史最高月涨幅）\n'
  + '取该柱所有历史出现月份里最高的一次 body_return。\n'
  + '旬/段层：本月已经走完的窗口会按日更补进样本；整月层仍只收完整流月。\n'
  + '下方次数 = 该柱样本数。样本越多，最高值天然越高；务必同屏看次数。\n'
  + '它是历史最好一次，不是未来上限，也不是上涨概率。';

/* 所有列头的悬浮解释。表头只保留列名，不再放 ⓘ 图标；说明挂在 th.title 上。 */
const OPP_HEAD_TIPS = {
  name: '名称 / 代码\n点行可打开右侧速览与对账；退市、无命盘、交节争议会在名称下方标注。',
  window_tag: '窗位及区间\n上旬（0–10日）、中旬（10–20日）、下旬（20日–下节）；段为第1段至第6段。首根有效日开盘→末根有效日收盘。',
  valid_invalid: '有效窗口数 / 应有窗口总数\n空窗（无交易日）不计入分母；单根K窗单独统计。括号内为历史空窗数。',
  natal_year_pillar: '本命年柱 / 月柱 / 日柱（时柱不参与关系引擎）。\n用于判断该流月柱与本命三柱的冲合关系；不是当月走势。',
  natal_element: '命盘三柱各自的干/支五行，与左侧生辰八字逐字对应；不随流月变化。\n右侧「流月五行」才是当月柱的五行；两者不是同一个东西。',
  flow_pillar: '所选流月的节气月柱（如丙午）。\n同一根柱每 60 个月（约 5 年）重现一次，这是历法结构，不是预测能力。',
  stem_element: '流月柱的天干五行 / 地支五行；同一流月整表同值。\n只是当月柱的五行，不是个股命盘五行。',
  up_n_ALL: '该票在该流月柱历史上「上涨 / 下跌 / 平盘」的月份数。\n分母是出现次数；平盘单独计数，不并入涨跌。',
  up_rate_ALL: '所选方向的历史占比 = 该方向月份数 ÷ 该柱出现月份数。\n旬/段层：本月已经走完的窗口会按日更补进分子分母；没走完的窗口不算。整月层仍只收完整流月。\n若所选月已入库，分母含所选月本身，按它排序会有自证偏差；回测请看「当时胜率」。\n平盘计入分母；不是下一次上涨概率。',
  actual_return: '所选流月该票的实际走势与 body_return（首日开盘→末日收盘）。\n本月未走完时显示「进行中 + 截至最新交易日的临时值」，不计入历史统计；已经走完的窗口显示最终值，并已补进上面的历史统计。\n旬/段层已走完的窗口、整月层已入库的月份都会进入历史分母。数据来自每日 19:05 起的 NAS 日更切片。',
  up_rate_excl_ALL: '当时胜率（回测口径）：把所选月从同柱历史样本中剔除后计算的历史胜率。\n本月已走完的窗口也被当作「所选月那一次」剔除，所以它可能与上面的「历史上涨占比」不同。\n用于还原“站在当时月初还没开盘时”手头能拿到的客观战绩，避免偷看当月答案；分母见单元格悬停。所选月未入库时与历史胜率一致。',
  mean_body_return_ALL: '该票在该流月柱历次出现的 body_return 算术平均。\n下跌方向按它升序排（跌得最多在前），但数值仍是有符号收益，不是跌幅绝对值。',
  median_body_return_ALL: '该窗口历史收益中位数\n剔除极值影响后的典型收益表现。',
  excess_mean: '历次同柱 body_return 减去同期匹配基准（指数/市场）后的均值。\n不是行业中性，也不是相对全市场；基准匹配失败时显示 N/A。',
  stability: '上涨方向看稳健度：样本数、最差一次、最好一次占累计涨幅。\n下跌方向看历史跌幅差异：同柱振幅、最惨一次占跌幅。\n两者都是历史形状描述，不是反弹/上涨概率；个股整体波动只在抽屉里展示。',
  consistency: CONS_HEAD_TIP,
  occurrence_years: '该票在该流月柱出现过的年份，每行最多 3 个；悬停单元格可看相邻间隔。\n用来检查样本是不是集中在某几年，不是预测。',
  relation_types: '该票在该流月柱上的命理关系集合（从同柱历史事实聚合）。\n历史无同柱时不推断；无命盘时不计算关系；不是当月新增关系，也不是吉凶评级。',
  cap_tier: '所选流月月末的自由流通市值档：100亿以下、100-300亿、300-500亿、500-1000亿、1000亿以上（下界含、上界不含）。\n本月/未来月回落到最近一个已完结流月；不是流通市值 circ_mv。',
  next_rate: '勾选「连续两月」时，下一根流月柱上该票的历史方向占比与平均涨跌。\n两根柱的样本来自同一段历史，不是两次独立试验，也不构成连续两月上涨/下跌的概率。',
  min_body_return_ALL: MINRET_HEAD_TIP,
  max_body_return_ALL: MAXRET_HEAD_TIP,
};

/* 最低月涨幅：全月最小值，允许为负；100% 上涨时与安全边际/方向最小幅度同值。
   只做描述列，不参与筛选/排序。 */
function oppMinReturnCell(r) {
  const v = (r.stab_min_ret !== undefined && r.stab_min_ret !== null)
    ? r.stab_min_ret
    : ((r.min_body_return_ALL !== undefined && r.min_body_return_ALL !== null)
      ? r.min_body_return_ALL
      : ((typeof r.ALL_worst_return_pct === 'number') ? r.ALL_worst_return_pct / 100 : null));
  const n = r.stab_n || 0;
  if (v === null || v === undefined) return '<span class="dim text-[11px]" title="该柱没有历史样本">—</span>';
  const cls = v > 0 ? 'c-up' : v < 0 ? 'c-down' : 'c-flat';
  const tip = '历史同柱 ' + n + ' 次里最低的一次月涨幅 ' + ret(v) + '。' +
    (v < 0 ? '负值说明历史上有下跌月。' : '100% 上涨时它就是涨得最少的一次。') +
    ' 样本越多最低值天然越低，不是未来下限。';
  return '<span class="tabular-num font-bold ' + cls + ' text-xs whitespace-nowrap" title="' + esc(tip) + '">' + ret(v) + '</span>';
}

/* 最高月涨幅：全月最大值 */
function oppMaxReturnCell(r) {
  const v = (r.stab_max_ret !== undefined && r.stab_max_ret !== null)
    ? r.stab_max_ret
    : ((r.max_body_return_ALL !== undefined && r.max_body_return_ALL !== null)
      ? r.max_body_return_ALL
      : ((typeof r.ALL_best_return_pct === 'number') ? r.ALL_best_return_pct / 100 : null));
  const n = r.stab_n || 0;
  if (v === null || v === undefined) return '<span class="dim text-[11px]" title="该柱没有历史样本">—</span>';
  const cls = v > 0 ? 'c-up' : v < 0 ? 'c-down' : 'c-flat';
  const tip = '历史同柱 ' + n + ' 次里最高的一次月涨幅 ' + ret(v) + '。' +
    ' 样本越多最高值天然越高，不是未来上限。';
  return '<span class="tabular-num font-bold ' + cls + ' text-xs whitespace-nowrap" title="' + esc(tip) + '">' + ret(v) + '</span>';
}

function oppConsistencyCell(r) {
  const n = r.cons_n || 0;
  const total = r.cons_total || r.stab_n || 0;
  if (!n || n < 2 || r.cons_level === '—') {
    return '<div class="opp-cons cons-cell" title="方向样本不足（k=' + n + '），无法计算 CV">' +
      '<span class="cons-badge none">样本不足</span>' +
      '<span class="cons-k">' + n + '/' + total + '</span></div>';
  }
  const lv = r.cons_level || '—';
  const tone = oppConsTone(lv);
  const cv = (r.cons_cv === null || r.cons_cv === undefined) ? '—' : Number(r.cons_cv).toFixed(2);
  const reverse = r.cons_reverse_n || 0;
  const floor = Number(r.cons_floor) === 1;
  const tip = (r.consistency_reason || '') + ' · 点击行看明细';
  const tags = [];
  if (!floor) tags.push('<span class="cons-tag warn">幅度不足</span>');
  if (reverse > 0) tags.push('<span class="cons-tag rev">反向' + reverse + '</span>');
  return '<div class="opp-cons cons-cell cons-inline" title="' + esc(tip) + '">' +
    '<span class="cons-badge ' + tone + '">' + esc(lv) + '</span>' +
    '<span class="cons-k">' + n + '/' + total + '</span>' +
    '<span class="cons-meta">CV <b>' + cv + '</b></span>' +
    (tags.length ? '<span class="cons-tags">' + tags.join('') + '</span>' : '') +
  '</div>';
}

/* 三态必须分开：有历史→chips；0 次→不推断；无命盘→不算关系（不能显示成「（无）」）。
   表格里用 compact：只露前两个，其余收成「+N」，全部关系在悬停里；抽屉里仍然全列。 */
function oppRelCell(r, opts) {
  const p = oppRelParts(r);
  if (p.kind === 'list') {
    const compact = !!(opts && opts.compact) && p.items.length > 2;
    const shown = compact ? p.items.slice(0, 2) : p.items;
    return '<div class="flex flex-wrap gap-0.5 justify-center items-center py-0.5"' +
      (compact ? ' title="' + esc(p.items.join('、')) + '"' : '') + '>' +
      shown.map((it) => '<span class="rel-chip">' + esc(it) + '</span>').join('') +
      (compact ? '<span class="rel-chip rel-more">+' + (p.items.length - 2) + '</span>' : '') +
      '</div>';
  }
  if (p.kind === 'noprofile') {
    return '<span class="px-1.5 py-0.5 rounded text-[10px] badge-flat font-medium">无命盘</span>';
  }
  return '<span class="dim text-[10px]">' + OPP_REL_TEXT[p.kind] + '</span>';
}

/* 方向轴：同一个 sort / rate 控件在涨、跌两种方向下复用，只换文案，
   避免两套控件各写一份而漂移。请求参数由 oppFilter 的 dir 决定。 */
function syncDirUi() {
  const down = $('#o-dir').value === 'down';
  const T = down ? '下跌' : '上涨';
  [...$('#o-rate').options].forEach((o) => {
    o.text = T + '占比 ' + (RATE_TXT[o.value] || '');
  });
  const stabTxt = down
    ? { '': '历史跌幅差异 不限', exclude_bad: '历史跌幅差异 排除差异大', only_good: '历史跌幅差异 只看差异小' }
    : { '': '稳健度 不限', exclude_bad: '稳健度 排除弱', only_good: '稳健度 只看稳' };
  [...$('#o-stab').options].forEach((o) => { if (stabTxt[o.value]) o.text = stabTxt[o.value]; });
  const texts = {
    mean: down ? '按平均涨跌（跌得最多在前）' : '按平均涨跌',
    stability: '按稳健分（证据强度）',
    consistency: '按一致性（CV 升序）',
    up_rate: '历史' + T + '占比（全部历史）',
    up_rate_excl: '按当时' + T + '胜率（剔除当月·回测）',
    n: '按出现次数',
    excess: down ? '按同期超额（跑输最多在前）' : '按同期超额',
    branch: '按月支分组',
    identity: '按代码',
  };
  [...$('#o-sort').options].forEach((o) => { if (texts[o.value]) o.text = texts[o.value]; });
  $('#o-sort').title = '排序口径（默认按稳健分，即证据强度从强到弱）：' +
    '· 按平均涨跌＝该票该柱历史各次出现的 body_return 均值，不是预测。占比并列时也按它排。' +
    '· 按' + (down ? '稳健分' : '稳健分') + '＝40% 样本置信 + 35% 安全边际 + 25% 集中度；' +
    '只作排序辅助，不是概率，也不会改变历史占比。' +
    '· 按一致性＝所选方向月份的 CV 升序；CV=样本标准差/均值，越小越整齐；不是概率。' +
    '· 历史' + T + '占比（全部历史）＝' + T + '次数 ÷ 出现次数。' +
    '· 历史' + T + '占比（剔除所选月·回测）＝把所选月从分子分母去掉再算，避免自证。' +
    '· 按出现次数＝样本量；同期超额＝相对基准。' +
    (down ? '下跌方向下，平均涨跌与同期超额为升序（跌得最多 / 跑输最多在前）。' : '');
  $('#o-rate').title = '按「历史' + T + '占比」列过滤（' + T + '次数÷出现次数，平盘计入分母）。' +
    '0 次出现的行没有占比，会被排除。勾选「连续两月」时这一项只作用于本月柱，' +
    '下月柱由右边「下月占比」单独控制。';
  $('#o-stab').title = down
    ? '历史跌幅差异只看同一根柱的历史证据：同柱振幅 ≥40% 或 最惨一次占跌幅 ≥70% 标「高」，' +
      '振幅 <20% 且 最惨一次占比 <50% 标「低」，其余「中」。' +
      '这是历史形状描述，不是反弹概率；同月对照后差异不稳健。'
    : '稳健度只看同一根柱的历史证据：样本数、最差一次涨幅、最好一次占累计涨幅。' +
      '安全边际按同柱振幅缩放（最差一次 ÷ max(同柱振幅,2%)）：稳＝边际/振幅 ≥0.30 且 ' +
      '最好一次 ≤50%；弱＝边际/振幅 <0.15 或 最好一次 ≥70%；其余一般。' +
      '它不是上涨概率，也不把个股整体波动算进来。';
  $('#o-cons').title = '历史幅度一致性只看' + T + '方向的月份：' +
    'CV=样本标准差/均值；CV≤0.50 一致，0.50<CV≤0.80 一般，CV>0.80 差异大；' +
    '最小' + (down ? '跌幅' : '涨幅') + '≥1% 是独立幅度底线；k<2 样本不足；' +
    'drop 只展示、不参与分档；这不是下一次上涨概率。';
  $('#o-next-rate').title = '只作用于下月柱的' + T + '占比：同本月（默认，沿用左侧的占比条件）' +
    ' / 不限 / ≥x% / =100%。要看「两个月都 100%」，把左侧设成 =100% 再把这里也设成 =100%。';
  const bothLabel = $('#o-both-months').closest('label');
  if (bothLabel) bothLabel.title = '连续两月：同时要求这只票在下一根流月柱上也满足出现次数下限' +
    '与样本等级（这两项沿用上面），下月的' + T + '占比由右边单独控制。下月柱由冻结历法给出' +
    '（例如本月戊戌 → 下月己亥）。两根柱的样本来自同一只票的同一段历史，不是两次独立试验，' +
    '不代表会连续' + T + '。';
  syncOppSortUi();
}

function oppFilter() {
  const on = (id) => $(id).checked ? '1' : '';
  const rate = $('#o-rate').value;          // '' | ge50 | … | eq100
  return { months: '1', offset: $('#o-month').value || '0',
           min_n: $('#o-min').value || '0',
           grade: $('#o-grade').value,
           dir: $('#o-dir').value,
           rate_min: rate ? (rate === 'eq100' ? '100' : rate.slice(2)) : '',
           rate_eq: rate === 'eq100' ? '1' : '',
           stab: $('#o-stab').value,
           cons: $('#o-cons').value,
           both_months: $('#o-both-months').checked ? '1' : '',
           next_rate: $('#o-both-months').checked ? $('#o-next-rate').value : '',
           policy: $('#o-es').checked ? 'EXCLUDE_SINGLE' : 'ALL',
           sort: $('#o-sort').value, msort: oppMsortParam(),
           cap: $('#o-cap').value,
           cap_tier: S.dataset === 'etf' ? '' : ($('#o-cap-tier').value || ''),
           q: $('#o-search').value.trim(), rel: $('#o-rel').value,
           clash: on('#o-clash'), harmony: on('#o-harmony'), both_clash_harmony: on('#o-both'),
           punish: on('#o-punish'), harm: on('#o-harm'), broken: on('#o-broken'),
           include_delisted: S.dataset === 'etf' ? '' : on('#o-delist'),
           include_index: S.dataset === 'etf' ? '' : on('#o-idx'),
           layer: S.oppLayer || 'month',
           window_no: S.oppWindowNo || '' };
}

/* 月份叫法由服务端下发（m.label），两端各写一份必然漂移。 */

/* 目标流月：月份格子。年份按 start_at 的自然年分组（2026-M12 小寒落在 2027-01，就归 2027 年），
   每格 = 该自然月开始的节气月；柱名绿色表示该月行情已入库，会多出「该月实际」两列。 */
const MP = { year: null };
const mpMonths = () => (S.cal && S.cal.months) || [];
const mpYearOf = (m) => Number(m.start_at.slice(0, 4));
const mpMonthOf = (m) => Number(m.start_at.slice(5, 7));
const mpFutureEndTxt = () => {
  const fu = mpMonths().filter((m) => m.offset > 0);
  if (!fu.length) return '';
  const m = fu[fu.length - 1];
  return mpYearOf(m) + '年' + mpMonthOf(m) + '月';
};
const mpSelected = () => mpMonths().find((m) => String(m.offset) === String($('#o-month').value)) || null;

function mpDraw() {
  const sel = mpSelected();
  const years = mpMonths().map(mpYearOf);
  const ymin = Math.min(...years), ymax = Math.max(...years);
  if (MP.year === null) MP.year = sel ? mpYearOf(sel) : ymax;
  MP.year = Math.min(Math.max(MP.year, ymin), ymax);
  const byMonth = {};
  mpMonths().forEach((m) => { if (mpYearOf(m) === MP.year) byMonth[mpMonthOf(m)] = m; });
  $('#mp-year').textContent = MP.year;
  $('#mp-prev').disabled = MP.year <= ymin;
  $('#mp-next').disabled = MP.year >= ymax;
  const grid = $('#mp-grid'); grid.innerHTML = '';
  for (let mm = 1; mm <= 12; mm++) {
    const it = byMonth[mm];
    if (!it) { grid.appendChild(el('div', 'mp-cell empty')); continue; }
    const on = sel && it.offset === sel.offset;
    const b = el('button', 'mp-cell' + (on ? ' sel' : '') + (it.is_current ? ' now' : '') +
      (it.has_data ? ' has' : ''));
    b.type = 'button';
    b.title = it.month_id + ' · ' + date10(it.start_at) + ' → ' + date10(it.end_at) + ' · ' +
      it.month_pillar + ' · ' + it.label + ' · ' +
      (it.has_data ? '该月行情已入库（所选月实际走势已闭环）' : '该月行情未入库（所选月走势显示进行中/—）');
    b.innerHTML = '<b>' + mm + '月</b><div class="mp-pil">' + esc(it.month_pillar) + '</div>' +
      '<div class="mp-tag">' + (it.is_current ? '本月' : esc(it.start_term)) + '</div>';
    b.onclick = () => mpPick(it);
    grid.appendChild(b);
  }
  const has = mpMonths().filter((m) => m.has_data).length;
  const futEnd = mpFutureEndTxt();
  $('#mp-foot').innerHTML = '柱名绿色＝该月行情已入库，灰色＝未入库。' +
    '共 ' + mpMonths().length + ' 个节气月（往前 60 + 本月' +
    (futEnd ? ' + 往后至 ' + futEnd : '') + '），其中 ' + has + ' 个月有行情。' +
    '同一根柱每 60 个月（约 5 年）重现一次，往后重复的月份共用这根柱的历史统计。';
}

function mpPick(m) {
  $('#o-month').value = String(m.offset);
  $('#o-month').dispatchEvent(new Event('change', { bubbles: true }));
  mpClose();
}

function mpSync() {
  const sel = mpSelected();
  if (!sel) return;
  // 月份统一叫「戊戌 · 2026-M09（寒露月）」，下一行是节气区间与交节倒计时
  const md = (s) => s.slice(5, 7) + '/' + s.slice(8, 10);
  const nextM = mpMonths().find((m) => m.offset === sel.offset + 1);
  $('#o-month-txt').innerHTML = esc(sel.month_pillar) + ' · ' + esc(sel.month_id) +
    '<span class="rv-month-alias">（' + esc(sel.start_term) + '月）</span>' +
    '<span class="rv-month-tag">' + esc(sel.label) + '</span>';
  const sub = $('#o-month-sub');
  if (sub) {
    const days = sel.is_current && S.cal && S.cal.current ? S.cal.current.days_until_end : null;
    sub.textContent = sel.start_term + ' ' + md(date10(sel.start_at)) + ' → ' +
      (nextM ? nextM.start_term + ' ' : '') + md(date10(sel.end_at)) +
      (days !== null && days !== undefined ? ' · 距交节 ' + days + ' 天' : '');
  }
  $('#o-month-btn').title = '当前所选：' + date10(sel.start_at) + ' → ' + date10(sel.end_at) +
    ' · ' + sel.start_term + '起月 · ' + sel.label +
    '。点开是月份格子，左右箭头翻年。往前 60 个月是一个完整的 60 柱周期，往后可翻到 ' +
    mpFutureEndTxt() + '。';
  MP.year = mpYearOf(sel);
  mpDraw();
  syncOppNowBadge();
}

function syncOppNowBadge() {
  const wrap = $('#o-now-wrap');
  if (!wrap) return;
  const win = S.cal?.current_window;
  if (!win) {
    wrap.classList.add('hidden');
    wrap.classList.remove('flex');
    return;
  }
  wrap.classList.remove('hidden');
  wrap.classList.add('flex');

  // 所选月以选月控件为准（S.oppMonthOffset 从未被赋值，旧写法永远判成「非所选月」）
  const selOffset = Number($('#o-month').value || 0);
  const curMonth = (S.cal?.months || []).find((m) => m.offset === selOffset);
  const isCurrentMonth = curMonth ? curMonth.is_current : (selOffset === 0);

  const txtEl = $('#o-now-text');
  const cdEl = $('#o-now-countdown');
  if (txtEl) {
    txtEl.textContent = win.xun_name + ' · ' + asSegment(win.week_name);
  }
  if (cdEl) {
    cdEl.textContent = isCurrentMonth ? `(${win.week_left_str || ''})` : '(非所选月)';
    cdEl.classList.toggle('text-indigo-300', isCurrentMonth);
    cdEl.classList.toggle('text-slate-400', !isCurrentMonth);
  }

  // 旬层按钮高亮
  const xPills = $('#o-xun-pills');
  if (xPills) {
    xPills.querySelectorAll('button').forEach((b) => {
      const isCur = isCurrentMonth && (b.dataset.w === String(win.xun_no));
      b.classList.toggle('current-window-glow', isCur);
      let tag = b.querySelector('.now-pill-tag');
      if (isCur) {
        if (!tag) {
          tag = document.createElement('span');
          tag.className = 'now-pill-tag ml-1';
          tag.innerHTML = '<span class="pulse-dot"></span>当下';
          b.appendChild(tag);
        }
      } else if (tag) {
        tag.remove();
      }
    });
  }

  // 段层按钮高亮
  const wPills = $('#o-week-pills');
  if (wPills) {
    wPills.querySelectorAll('button').forEach((b) => {
      const isCur = isCurrentMonth && (b.dataset.w === String(win.week_no));
      b.classList.toggle('current-window-glow', isCur);
      let tag = b.querySelector('.now-pill-tag');
      if (isCur) {
        if (!tag) {
          tag = document.createElement('span');
          tag.className = 'now-pill-tag ml-1';
          tag.innerHTML = '<span class="pulse-dot"></span>当下';
          b.appendChild(tag);
        }
      } else if (tag) {
        tag.remove();
      }
    });
  }
}

function locateOppNow() {
  const win = S.cal?.current_window;
  if (!win) return;
  // 1. 若非当前月，自动切回本月
  if (Number($('#o-month').value || 0) !== 0) {
    const om = $('#o-month');
    if (om) {
      om.value = '0';
      om.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  // 2. 切换到段层 + 当前段
  S.oppLayer = 'week';
  S.oppWindowNo = String(win.week_no);
  if ($('#o-min')) $('#o-min').value = S.dataset === 'etf' ? '3' : '6';

  const oSeg = $('#o-layer-seg');
  if (oSeg) {
    oSeg.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.v === 'week';
      b.classList.toggle('on', on);
      b.classList.toggle('bg-indigo-600', on);
      b.classList.toggle('text-white', on);
      b.classList.toggle('shadow-sm', on);
      b.classList.toggle('text-slate-400', !on);
    });
  }
  const xPills = $('#o-xun-pills');
  if (xPills) { xPills.classList.add('hidden'); xPills.classList.remove('flex'); }
  const wPills = $('#o-week-pills');
  if (wPills) {
    wPills.classList.remove('hidden');
    wPills.classList.add('flex');
    wPills.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.w === String(win.week_no);
      b.classList.toggle('on', on);
      b.classList.toggle('bg-indigo-600', on);
      b.classList.toggle('text-white', on);
      b.classList.toggle('shadow-sm', on);
      b.classList.toggle('text-slate-400', !on);
    });
  }
  if ($('#o-layer-note')) {
    $('#o-layer-note').textContent = '【段层】所选流月 × 6 个节气段窗口机会排行；已自动定位至当下窗位。';
  }
  const bothWrap = $('#o-both-months') ? $('#o-both-months').closest('label') : null;
  if (bothWrap) bothWrap.classList.add('hidden');
  if ($('#o-stab')) $('#o-stab').classList.add('hidden');
  if ($('#o-cons')) $('#o-cons').classList.add('hidden');

  syncOppNowBadge();
  renderOpportunity();
  saveFiltersNow();
}

function mpOpen() {
  const sel = mpSelected();
  MP.year = sel ? mpYearOf(sel) : MP.year;
  mpDraw();
  $('#o-month-pop').classList.remove('hidden');
}

const mpClose = () => $('#o-month-pop').classList.add('hidden');
const mpToggle = () => $('#o-month-pop').classList.contains('hidden') ? mpOpen() : mpClose();

/* 顶栏设置菜单、列设置两个弹出层：同一时间只开一个，点外面或按 Esc 关闭 */
const RV_POPS = [['#rv-gear-btn', '#rv-gear-pop'], ['#o-cols-btn', '#o-cols-pop']];
function rvCloseAll() {
  RV_POPS.forEach(([b, p]) => {
    const pop = $(p), btn = $(b);
    if (pop) pop.classList.add('hidden');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  });
}
function initRvPops() {
  const onOpen = { '#o-cols-pop': renderOppColsPop };
  RV_POPS.forEach(([b, p]) => {
    const btn = $(b), pop = $(p);
    if (!btn || !pop) return;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = pop.classList.contains('hidden');
      rvCloseAll();
      if (!open) return;
      if (onOpen[p]) onOpen[p]();
      pop.classList.remove('hidden');
      btn.setAttribute('aria-expanded', 'true');
    });
    pop.addEventListener('click', (e) => e.stopPropagation());
  });
  // 设置菜单里点了任一项（改密码、退出、切主题）就收起菜单
  const gear = $('#rv-gear-pop');
  if (gear) gear.addEventListener('click', (e) => { if (e.target.closest('.rv-menu-item')) rvCloseAll(); });
  const colsReset = $('#o-cols-reset');
  if (colsReset) colsReset.onclick = () => {
    S.oppHiddenCols = new Set(OPP_COLS_DEFAULT_HIDDEN);
    saveOppHiddenCols();
    if (S.opp) paintOpportunity(S.opp);
    renderOppColsPop();
  };
  document.addEventListener('click', rvCloseAll);
}

/* 机会表是独立界面：不显示个股卡与个股页签，避免和全市场表混在一屏里。 */
function setBoardMode(on, opts = {}) {
  if (!NAV.ready && opts.syncUrl !== false) return;
  const was = document.body.classList.contains('board');
  document.body.classList.toggle('board', on);
  if (on) S.fromBoard = false;
  const stockBtn = $('#back-stock');
  const boardBtn = $('#open-board');
  if (stockBtn && boardBtn) {
    boardBtn.classList.toggle('on', !!on);
    stockBtn.classList.toggle('on', !on);
  }
  const segBtn = $('#open-seg');
  if (!on) {
    S.segMode = false;
    document.body.classList.remove('segmode');
    const so = $('#seg-opts'); if (so) so.classList.add('hidden');
  }
  if (segBtn) segBtn.classList.toggle('on', !!S.segMode);
  if (S.segMode) {
    if (boardBtn) boardBtn.classList.remove('on');
    if (stockBtn) stockBtn.classList.remove('on');
  }
  const q = $('#q');
  if (q) {
    if (on) q.value = '';
    else if (S.stock && S.stock.security && S.stock.security.security_code) q.value = S.stock.security.security_code;
  }
  syncReturnBar();
  if (on && (!was || opts.forceRender)) renderOpportunity();
  if (opts.syncUrl !== false) {
    applyUrl(navState({ board: !!on }), opts.push ? 'push' : 'replace');
  }
}

/* 已生效条件：每个不是「不限」的筛选变成一个可单独删除的标签。删除 = 把该控件改回不限并触发 change，
   后续重绘与会话保存都走控件原有的监听，不另起一套状态。 */
function renderOppChips() {
  const box = $('#o-chips');
  if (!box) return;
  const optTxt = (id) => { const s = $(id); return s && s.selectedOptions[0] ? s.selectedOptions[0].text : ''; };
  const fire = (e) => e.dispatchEvent(new Event('change', { bubbles: true }));
  const clearSel = (id, v) => () => { const e = $(id); e.value = v; fire(e); };
  const clearChk = (id) => () => { const e = $(id); e.checked = false; fire(e); };
  const monthLayer = (S.oppLayer || 'month') === 'month';
  const isEtf = S.dataset === 'etf';
  const chips = [];
  if ($('#o-min').value) chips.push([optTxt('#o-min').replace('（默认）', ''), clearSel('#o-min', '')]);
  if ($('#o-grade').value) chips.push([optTxt('#o-grade'), clearSel('#o-grade', '')]);
  if ($('#o-rate').value) chips.push([optTxt('#o-rate'), clearSel('#o-rate', '')]);
  if (monthLayer && $('#o-both-months').checked) {
    chips.push(['连续两月 · ' + optTxt('#o-next-rate'), clearChk('#o-both-months')]);
  }
  if (monthLayer && $('#o-stab').value) chips.push([optTxt('#o-stab'), clearSel('#o-stab', '')]);
  if (monthLayer && $('#o-cons').value) chips.push([optTxt('#o-cons'), clearSel('#o-cons', '')]);
  if (!isEtf && $('#o-cap-tier').value) chips.push(['市值档 ' + $('#o-cap-tier').value, clearSel('#o-cap-tier', '')]);
  if ($('#o-es').checked) chips.push(['不含单交易日月', clearChk('#o-es')]);
  const rels = [['#o-clash', '含相冲'], ['#o-harmony', '含相合'], ['#o-both', '冲合并存'],
                ['#o-punish', '含刑'], ['#o-harm', '含相害'], ['#o-broken', '含六破']];
  rels.forEach(([id, t]) => { if ($(id).checked) chips.push([t, clearChk(id)]); });
  if ($('#o-rel').value) chips.push(['关系 ' + $('#o-rel').value, clearSel('#o-rel', '')]);
  if (!isEtf && $('#o-delist').checked) chips.push(['包含已退市', clearChk('#o-delist')]);
  if (!isEtf && $('#o-idx').checked) chips.push(['包含指数', clearChk('#o-idx')]);
  const q = $('#o-search').value.trim();
  if (q) chips.push(['表内过滤 ' + q, clearSel('#o-search', '')]);
  chips.push([optTxt('#o-cap'), null]);   // 前 N 名总有一个值，只展示，改在上面的「范围」里
  const sortV = $('#o-sort').value;
  if (!S.oppMsort.length) {
    const isDef = sortV === OPP_DEFAULT_SORT;
    // 默认项只写「稳健分（默认）」，括号里的口径说明放到悬停里
    chips.push(['排序 ' + (isDef ? '稳健分（默认）' : optTxt('#o-sort').replace(/^按/, '')),
                isDef ? null : clearSel('#o-sort', OPP_DEFAULT_SORT)]);
  }
  box.innerHTML = '';
  chips.forEach(([txt, clear]) => {
    const c = el('span', 'rv-chip' + (clear ? '' : ' rv-chip-ro'));
    c.appendChild(document.createTextNode(txt));
    if (clear) {
      const b = el('button', null, '×');
      b.type = 'button';
      b.title = '移除这个条件';
      b.setAttribute('aria-label', '移除 ' + txt);
      b.onclick = clear;
      c.appendChild(b);
    }
    box.appendChild(c);
  });
}

/* 列设置面板：列清单取自最近一次绘制的完整列（含旬/段层、连续两月等动态列） */
function renderOppColsPop() {
  const list = $('#o-cols-list');
  if (!list) return;
  const cols = (S.oppColsAll || []).filter((c) => c[0] !== 'name');
  const hidden = oppHiddenCols();
  list.innerHTML = '';
  list.appendChild(el('div', 'rv-cols-sec', '名称 / 代码（固定显示）'));
  cols.forEach(([key, label]) => {
    const lab = el('label');
    const cb = el('input');
    cb.type = 'checkbox';
    cb.checked = !hidden.has(key);
    cb.onchange = () => {
      if (cb.checked) hidden.delete(key); else hidden.add(key);
      saveOppHiddenCols();
      if (S.opp) paintOpportunity(S.opp);
      renderOppColsPop();
    };
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(label));
    list.appendChild(lab);
  });
  const shown = cols.filter((c) => !hidden.has(c[0])).length;
  $('#o-cols-n').textContent = '显示 ' + (shown + 1) + ' / ' + (cols.length + 1) + ' 列';
}

function segBoardFilter() {
  const f = oppFilter();
  f.dir = $('#seg-dir').value || 'up';
  // 机会表默认的「占比≥80%」等条件会误伤段列表，这里只保留月份/方向；
  // 100%段自己的门槛在 seg_n / seg_len / seg_mean / seg_all。
  f.min_n = '0'; f.grade = ''; f.rate_min = ''; f.rate_eq = '';
  f.stab = ''; f.cons = ''; f.both_months = ''; f.next_rate = '';
  f.policy = 'ALL'; f.sort = 'mean'; f.msort = ''; f.cap = '500';
  f.cap_tier = ''; f.q = ''; f.rel = '';
  f.clash = ''; f.harmony = ''; f.both_clash_harmony = '';
  f.punish = ''; f.harm = ''; f.broken = '';
  f.include_delisted = ''; f.include_index = '';
  f.layer = 'month'; f.window_no = '';
  f.seg_n = $('#seg-n').value || '5';
  f.seg_len = $('#seg-len').value || '5';
  f.seg_mean = $('#seg-mean').value || '5';
  f.seg_all = $('#seg-all').checked ? '1' : '0';
  return f;
}

function oppSegInfoCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  const pct2 = (v) => (v === null || v === undefined ? '—' : Number(v).toFixed(2) + '%');
  const t = s.start_day + '日' + s.start_time + '→' + s.end_day + '日' + s.end_time;
  const dt = (d, tm, exact) => (d ? (exact ? '' : '≈') + d.slice(5) + ' ' + tm : '');
  const real = (s.start_date || s.end_date)
    ? (dt(s.start_date, s.start_time, s.start_exact) + ' → ' + dt(s.end_date, s.end_time, s.end_exact))
    : '';
  const tip = '历史完整 ' + s.n_avail + ' 次 · 平均 ' + pct2(s.mean_pct) +
    ' · 最差 ' + pct2(s.min_pct) + ' · 本月是下一次跟踪（不计入历史分母）' +
    (real ? '\n该流月真实时间：' + real + (s.start_exact && s.end_exact ? '' : '（≈ 为按工作日外推，休市日未计入）') : '') +
    (s.now_text ? '\n' + s.now_text : '');
  return '<div class="leading-tight whitespace-nowrap font-mono text-slate-200" title="' + esc(tip) + '">' +
    '<div>' + esc(t) + '</div>' +
    (real ? '<div class="text-[9.5px] text-slate-500">' + esc(real) + '</div>' : '') + '</div>';
}

function oppSegStatusCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  const cls = s.status === 'ongoing' ? 'text-indigo-300'
    : s.status === 'upcoming' ? 'text-amber-400'
    : s.status === 'passed' ? 'text-slate-400' : 'text-slate-500';
  const dd = s.delta_days == null ? '—' : s.delta_days;
  if (s.status === 'not_current') {
    const ctx = S.segCtx || {};
    const ma = s.month_actual;
    if (ctx.offset > 0) {
      return '<span class="dim" title="未来月份，行情还没发生">未到</span>';
    }
    if (ma && ma.complete) {
      return '<span class="text-slate-400" title="该月已经走完，实际结果见右列">已走完</span>';
    }
    if (ma) {
      return '<span class="text-amber-400" title="该月只有 ' + ma.covered + '/' + ma.total + ' 根 60 分钟线（停牌或数据缺失）">数据不全</span>';
    }
    return '<span class="dim" title="这个月没有可用的 60 分钟线（NAS 月度归档断在 2026-07，日更 60 分钟线只保留最近约 40 个交易日）">无60分钟线</span>';
  }
  const txt = s.status === 'passed' ? ('已过去 ' + dd + ' 天')
    : s.status === 'upcoming' ? ('未到 ' + dd + ' 天') : s.status_text;
  const remain = (s.status === 'ongoing' && s.remain_days != null)
    ? '<div class="text-[9.5px] text-slate-400 whitespace-nowrap" title="这段还剩 ' + s.remain_days +
      ' 个交易日（' + (s.remain_bars != null ? s.remain_bars : '—') + ' 根 60 分钟线）走完">还剩 ' +
      Number(s.remain_days).toFixed(2) + ' 天</div>' : '';
  return '<div class="leading-tight text-center" title="' + esc(s.status_text) + '"><span class="font-semibold ' +
    cls + '">' + esc(txt) + '</span>' + remain + '</div>';
}

function oppSegLenCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  return '<span class="tabular-num">' + Number(s.length_days).toFixed(2) + ' 天</span>';
}

function oppSegHitCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  const isDown = oppDir() === 'down';
  const cls = isDown ? 'c-down' : 'c-up';
  const lv = s.status === 'not_current' ? s.month_actual : s.latest;
  const word = s.status === 'not_current' ? '该月' : '本月';
  // 这段已经走完：把真实结果并进同一条纪录（历史 n + 该月 = n+1）
  const inHist = !!(S.segCtx && S.segCtx.month_in_history) && s.status === 'not_current';
  const done = !!(lv && lv.complete) && !inHist;
  const matched = done && (isDown ? lv.e_bp < 0 : lv.e_bp > 0);
  const inclN = s.n_avail + 1;
  const inclHit = s.n_valid + (matched ? 1 : 0);
  const tip = '历史完整 ' + s.n_avail + ' 次，命中 ' + s.n_valid + ' 次；本月是下一次跟踪（有行情 ' +
    s.n_seen + ' 次 / 全柱 ' + s.n_months + ' 次）' +
    (done ? '\n含' + word + '重算：' + inclHit + '/' + inclN + '（' + word + ' ' +
      (lv.e_bp > 0 ? '+' : '') + (lv.e_bp / 100).toFixed(2) + '%，' +
      (matched ? '仍同向' : '已反向') + '）' : '');
  return '<div class="leading-tight text-center" title="' + esc(tip) + '">' +
    '<div class="tabular-num font-bold ' + cls + '">' + s.n_valid + '/' + s.n_avail + '</div>' +
    '<div class="' + cls + ' text-[9.5px] font-semibold">100%</div>' +
    (done ? '<div class="text-[9.5px] ' + (inclHit === inclN ? cls : 'text-amber-400') + '">含' + word + ' ' +
      inclHit + '/' + inclN + '</div>' : '') + '</div>';
}

/* 本月实际：这一段在本月已经真实走出来的结果（60 分钟线），不是历史统计 */
function oppSegActualCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  const isDown = oppDir() === 'down';
  const notCur = s.status === 'not_current';
  const lv = notCur ? s.month_actual : s.latest;
  if (!lv) return '<span class="dim text-[11px]">' + (s.status === 'upcoming' ? '未到'
    : notCur ? '—' : '无行情') + '</span>';
  const bp = lv.e_bp;
  const pctTxt = (bp > 0 ? '+' : '') + (bp / 100).toFixed(2) + '%';
  const cls = bp > 0 ? 'c-up' : bp < 0 ? 'c-down' : 'c-flat';
  if (!lv.complete) {
    if (s.status === 'passed') {
      return '<span class="dim text-[11px]" title="本月这段已经过去，但 60 分钟线只有 ' + lv.covered + '/' +
        lv.total + ' 根（停牌或数据缺失），算不全">数据不全</span>';
    }
    const prog = Math.round((lv.covered / Math.max(lv.total, 1)) * 100);
    return '<div class="leading-tight text-center" title="本月这段进行中：已走 ' + lv.covered + '/' + lv.total +
      ' 根 60 分钟线（截至 ' + esc(lv.asof_dt || '') + '），这是未走完的临时值">' +
      '<div class="text-[10px] text-indigo-300 font-semibold">进行中 ' + prog + '%</div>' +
      '<div class="tabular-num font-bold ' + cls + '">' + pctTxt + '</div></div>';
  }
  const matched = isDown ? bp < 0 : bp > 0;
  if (notCur) {
    const mid = (S.segCtx && S.segCtx.month_id) || '';
    // 按「这条行里到底有没有这个月」判断：库里有的股票该月数据不全，不会被计入历史
    const inRow = mid && (s.month_ids || '').split(';').includes(mid);
    const inHist = !!(S.segCtx && S.segCtx.month_in_history) && !!inRow;
    const tip = '该月实际（' + esc(mid) + '，' +
      (inHist ? '已计入历史：就是历史里的这一次，年份胶囊里能找到'
              : (inRow === false && S.segCtx && S.segCtx.month_in_history
                  ? '没有计入历史：这只票该月 60 分钟数据不全，命中次数/平均里不含它'
                  : '未计入历史统计')) + '）：' +
      (matched ? '与历史同向' : '与历史反向');
    return '<div class="leading-tight text-center" title="' + tip + '">' +
      '<span class="px-1 py-0.2 rounded text-[10px] ' + (bp > 0 ? 'badge-up' : bp < 0 ? 'badge-down' : 'badge-flat') +
      ' font-bold whitespace-nowrap">' + (bp > 0 ? '涨' : bp < 0 ? '跌' : '平') + ' ' + pctTxt + '</span>' +
      (inHist ? '<div class="text-[9px] text-slate-500">已计入历史</div>'
              : '<div class="text-[9px] text-amber-500">' +
                (inRow === false && S.segCtx && S.segCtx.month_in_history ? '该月数据不全' : '未计入历史') +
                '</div>') + '</div>';
  }
  return '<div class="leading-tight text-center" title="本月这段已走完（真实 60 分钟线，' + esc(lv.asof_dt || '') +
    '）：' + (matched ? '与历史同向，100% 纪录本月仍成立' : '与历史反向，100% 纪录本月被打破') + '">' +
    '<span class="px-1 py-0.2 rounded text-[10px] ' + (bp > 0 ? 'badge-up' : bp < 0 ? 'badge-down' : 'badge-flat') +
    ' font-bold whitespace-nowrap">' + (bp > 0 ? '涨' : bp < 0 ? '跌' : '平') + ' ' + pctTxt + '</span></div>';
}

/* 最好一次：这条段历次同柱月里最赚的一次 */
function oppSegBestCell(r) {
  const s = r._seg;
  if (!s || s.best_pct === null || s.best_pct === undefined) return '<span class="dim">—</span>';
  const v = s.best_pct;
  const cls = v > 0 ? 'c-up' : v < 0 ? 'c-down' : 'c-flat';
  const txt = (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  return '<span class="tabular-num font-bold ' + cls + '" title="同段最赚的一次：' + esc(s.best_month || '') +
    ' ' + txt + '（样本 ' + s.n_valid + ' 次；样本越多最高值天然越高，不是未来上限）">' + txt + '</span>';
}

function oppSegStabCell(r) {
  const st = r._seg && r._seg.stab;
  if (!st || st.level === 'none') return '<span class="dim">—</span>';
  const tip = '段内稳健度：' + (st.reason || '') + '；同段 ' + st.n + ' 次 · 段内振幅 ' +
    (st.range == null ? '—' : (st.range * 100).toFixed(2) + '%') + '。阈值与流月机会表同一套。';
  return '<span class="px-1.5 py-0.2 rounded text-[10px] ' + oppStabCls(st.level) +
    ' font-bold whitespace-nowrap" title="' + esc(tip) + '">' + esc(st.cn || OPP_STAB_CN[st.level] || '—') +
    ' · ' + st.n + '</span>';
}

function oppSegConsCell(r) {
  const c = r._seg && r._seg.cons;
  if (!c || c.n < 2 || c.level === '—') return '<span class="dim">—</span>';
  const tone = oppConsTone(c.level);
  const cv = c.cv == null ? '—' : Number(c.cv).toFixed(2);
  const tags = [];
  if (!c.floor) tags.push('<span class="cons-tag warn">幅度不足</span>');
  return '<div class="opp-cons cons-cell cons-inline" title="段内一致性：只算这条段自己的 ' + c.n +
    ' 次；CV≤0.50 一致、0.50–0.80 一般、>0.80 差异大；幅度不足 = 最小幅度 <1%">' +
    '<span class="cons-badge ' + tone + '">' + esc(c.level) + '</span>' +
    '<span class="cons-k">' + c.n + '/' + c.total + '</span>' +
    '<span class="cons-meta">CV <b>' + cv + '</b></span>' +
    (tags.length ? '<span class="cons-tags">' + tags.join('') + '</span>' : '') + '</div>';
}

function oppSegMeanCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  const cls = s.mean_pct > 0 ? 'c-up' : s.mean_pct < 0 ? 'c-down' : 'c-flat';
  const txt = (s.mean_pct > 0 ? '+' : '') + Number(s.mean_pct).toFixed(2) + '%';
  return '<span class="tabular-num font-bold ' + cls + '">' + txt + '</span>';
}

function oppSegMinCell(r) {
  const s = r._seg;
  if (!s) return '<span class="dim">—</span>';
  const v = oppDir() === 'down' ? s.max_pct : s.min_pct;
  const cls = v > 0 ? 'c-up' : v < 0 ? 'c-down' : 'c-flat';
  const txt = (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  return '<span class="tabular-num font-bold ' + cls + '">' + txt + '</span>';
}

async function renderOpportunity() {
  const box = $('#opptables'); box.innerHTML = ''; $('#oppdetail').innerHTML = '';
  renderOppChips();
  const f = oppFilter();
  // 筛选/排序/页大小一变就回第 1 页；只有翻页本身不改这些值。
  const sig = JSON.stringify(f);
  if (sig !== S.oppFilterSig) {
    S.oppFilterSig = sig;
    // 筛选一变页码回第 1 页，快照也要一起写回，否则刷新后会带着旧页码复活
    if (S.oppPage !== 1) { S.oppPage = 1; scheduleFiltersSave(); }
  }
  if (!S.segMode) f.page = String(S.oppPage || 1);
  const my = seqStart('opp');
  const ep = S.epoch;
  const out = await get(S.segMode ? '/api/seg/board' : '/api/opportunities',
                        S.segMode ? segBoardFilter() : f);
  if (seqStale('opp', my) || ep !== S.epoch) return;
  // 书签里恢复的页码可能已经超出末页（换过筛选/数据变了）：回第 1 页重取
  if (!S.segMode && S.oppPage > 1 && !(out.rows || []).length) {
    S.oppPage = 1;
    scheduleFiltersSave();
    return renderOpportunity();
  }
  S.opp = out;
  S.segCtx = out.seg_ctx || null;
  if (S.segMode) {
    const now = $('#seg-now');
    if (now) now.textContent = (S.segCtx ? S.segCtx.now_text : '') +
      (S.segCtx && S.segCtx.month_remain_days != null
        ? ' · 本月还剩约 ' + S.segCtx.month_remain_days + ' 个交易日' : '') +
      (S.segCtx ? ' · 本柱命中 ' + S.segCtx.total + ' 只' +
        (out.rows.length < S.segCtx.total ? '（本表 ' + out.rows.length + ' 只，其余无机会表行/退市）' : '') : '');
  }
  paintOpportunity(out);
}

/* 100%段：这个流月因为源头没有 60 分钟数据而没能入库 —— 必须写在页面上，
   否则「5/5」会被读成「每次都中」，其实是「2026 年那一次没验过」。 */
function segGapNote(gaps) {
  const md = (d) => String(d).slice(5);
  const compact = (days) => {
    const ds = (days || []).slice().sort();
    const out = [];
    let i = 0;
    while (i < ds.length) {
      let j = i;
      while (j + 1 < ds.length) {
        const a = Date.parse(ds[j] + 'T00:00:00Z'), b = Date.parse(ds[j + 1] + 'T00:00:00Z');
        if (b - a !== 86400000) break;
        j++;
      }
      out.push(i === j ? md(ds[i]) : (md(ds[i]) + '～' + md(ds[j])));
      i = j + 1;
    }
    return out.join('、');
  };
  const wrap = el('div', 'rounded-xl border border-amber-700/50 bg-amber-950/30 px-3 py-2 mb-2 text-[11px] leading-relaxed');
  wrap.innerHTML = gaps.map((g) => {
    const list = compact(g.missing_days);
    const tip = (g.reason || '') + (g.unblock ? '\n补法：' + g.unblock : '');
    return '<div class="text-amber-200/90" title="' + esc(tip) + '">⚠ <b>' + esc(g.month_id) + '（' +
      esc(g.pillar) + '）</b> 缺 <b>' + (g.missing_count || (g.missing_days || []).length) + '</b> 个交易日' +
      (list ? '（' + esc(list) + '）' : '') + ' —— <b>2026 年那一次没验过</b>，下面的命中次数/平均都不含它。' +
      (g.unblock ? '<span class="text-amber-300/60"> ' + esc(g.unblock) + '</span>' : '') + '</div>';
  }).join('');
  return wrap;
}

function paintOpportunity(out) {
  const box = $('#opptables'); box.innerHTML = '';
  if (S.segMode && S.segCtx && S.segCtx.gaps && S.segCtx.gaps.length) {
    box.appendChild(segGapNote(S.segCtx.gaps));
  }
  S.oppExport = null;
  syncExportBtn();
  syncOppSortUi();
  syncOppNowBadge();
  const DT = oppDirTxt();
  const bt = $('#boardtitle');
  const bh = $('#board-h');
  const bs = $('#board-sub');
  if (bh) bh.textContent = S.segMode ? '100%段' : '流月机会表';
  if (bs) bs.textContent = S.segMode
    ? '只看：本月同一流月柱、历史上每次方向都一样的段。段内命中=100%；点一行看机会表同一个抽屉（整月走势、稳定性、历次对账）。'
    : '全市场 × 所选流月，按历史同柱表现列出标的。只描述历史，不预测未来。';
  if (bt) {
    bt.textContent = (S.segMode ? '100%段' : '流月机会表') + ' · ' + (out.market_label || '全市场') +
      ((out.market_label || '全市场').endsWith('ETF') ? ' ' : '') +
      '按节气月查看（本页与上方个股无关）';
    bt.dataset.tip = out.caveats.join('\n\n');
    bt.removeAttribute('title');
    bt.classList.add('cursor-help');
  }
  const cav = $('#o-caveats');
  if (cav) {
    cav.dataset.tip = '口径红线：窗收益 = 窗口内首有效日开盘 → 末有效日收盘；空窗不计入分母；' +
      '平盘严格要求涨跌幅 = 0；池层的覆盖股票数按票·月加权。\n\n' + out.caveats.join('\n\n');
  }
  const so = $('#o-sort').querySelector('option[value="' + out.params.sort + '"]');
  const GRADE_TXT = { '0': '0 次', '1-2': '1–2 次', '3-5': '3–5 次', '6+': '≥6 次' };
  const gTxt = out.params.grade ? '样本等级 ' + (GRADE_TXT[out.params.grade] || out.params.grade)
    : '样本等级 任意';
  const rTxt = (out.params.rate_min === null || out.params.rate_min === undefined)
    ? (out.params.both_months ? '本月占比 不限' : DT + '占比 不限')
    : (out.params.both_months ? '本月占比 ' : DT + '占比 ') +
      (out.params.rate_eq ? '=100%' : '≥' + out.params.rate_min + '%');
  const nrt = out.params.next_rate;
  const nrTxt = !out.params.both_months ? ''
    : !nrt || nrt === 'inherit' ? '下月占比 同本月'
    : nrt === 'none' ? '下月占比 不限'
    : nrt === 'eq100' ? '下月占比 =100%' : '下月占比 ≥' + nrt + '%';
  const isDownDir = oppDir() === 'down';
  const stabTxt = !out.params.stab ? (isDownDir ? '历史跌幅差异 不限' : '稳健度 不限')
    : out.params.stab === 'exclude_bad'
      ? (isDownDir ? '历史跌幅差异 排除差异大' : '稳健度 排除弱')
      : (isDownDir ? '历史跌幅差异 只看差异小' : '稳健度 只看稳');
  const consTxt = !out.params.cons ? '一致性 不限'
    : out.params.cons === 'exclude_bad' ? '一致性 排除差异大'
    : out.params.cons === 'only_good' ? '一致性 只看一致'
    : '一致性 只看一致且≥1%';
  const sortGloss = out.params.sort === 'up_rate' ? '，即' + DT + '次数÷出现次数'
    : out.params.sort === 'up_rate_excl' ? '，即把所选月从分子分母去掉再算'
    : out.params.sort === 'mean' ? '，即历史各次出现的平均涨跌'
    : out.params.sort === 'stability' ? '，即证据强度排序，不是概率'
    : out.params.sort === 'consistency' ? '，即所选方向 CV 升序，越小越整齐' : '';
  const msorts = Array.isArray(out.params.msort) ? out.params.msort : [];
  const sortTxt = msorts.length
    ? '多列排序 ' + msorts.map((x, i) => (i + 1) + '.' + oppSortLabel(x.key) + ' ' + oppSortDir(x.key, x.dir)).join('，')
    : '排序 ' + (so ? so.text : out.params.sort) + sortGloss;
  const sc = { stable: 0, mid: 0, weak: 0, low: 0, high: 0, none: 0 };
  out.rows.forEach((r) => { if (sc[r.stability_level] !== undefined) sc[r.stability_level]++; });
  const scTxt = isDownDir
    ? '本屏历史跌幅差异 低' + sc.low + ' / 中' + sc.mid + ' / 高' + sc.high
    : '本屏稳健度 稳' + sc.stable + ' / 一般' + sc.mid + ' / 弱' + sc.weak;
  const cs = { '一致': 0, '一般': 0, '差异大': 0, '—': 0 };
  out.rows.forEach((r) => { if (cs[r.cons_level] !== undefined) cs[r.cons_level]++; });
  const csTxt = '本屏一致性 一致' + cs['一致'] + ' / 一般' + cs['一般'] +
    ' / 差异大' + cs['差异大'] + (cs['—'] ? ' / 样本不足' + cs['—'] : '');
  // 筛选条件已经变成上面的「已生效」标签；这一行只留数据口径与本屏分布
  let noteHtml = '北京时间 ' + esc(out.asof) + ' · 历史库行情截至 ' +
    esc(out.input_data_as_of || '—') + ' · 最后有数据流月 ' + esc(out.latest_month_with_data || '—') +
    ' · 冻结历法 ' + esc(out.calendar_version || '') +
    ' · 口径 ' + (out.params.policy === 'EXCLUDE_SINGLE' ? '不含单交易日月' : '含单交易日月') +
    ' · ' + scTxt + ' · ' + csTxt +
    (out.params.cap_tier
      ? ' · 市值档按 ' + esc(out.params.cap_asof_month || '—') + ' 月末快照' : '');
  if (S.dataset === 'etf') {
    noteHtml += ' · ETF 库 0 退市 / 0 指数，两项开关已隐藏；' +
      'ETF 平均历史约 44 个月，默认 ≥3 次会排除多数标的，可降到 ≥1 次再看';
  }
  $('#o-note').innerHTML = noteHtml;
  const sortChip = [...document.querySelectorAll('#o-chips .rv-chip')].find((c) => c.textContent.startsWith('排序'));
  if (sortChip) sortChip.title = sortTxt;
  const cnt = $('#o-count');
  if (cnt) cnt.textContent = '';
  if (!out.months.length) { box.appendChild(el('p', 'p-6 text-center text-slate-400 text-sm', '冻结历法里没有可列出的流月。')); return; }
  out.months.forEach((m) => {
    const t = m.totals;
    const rows = out.rows.filter((r) => r.flow_pillar === m.pillar);
    const hasData = m.market_rows_in_month > 0;
    // 月份名、区间、交节倒计时已经在时间轴的选月按钮上；这里只留结果数和该月行情状态
    if (cnt) {
      const pg = t.page || 1, pgs = t.pages || 0;
      cnt.innerHTML = '门槛内 <b>' + (t.in_scope || 0).toLocaleString('en-US') + '</b> 只' +
        (pgs > 1
          ? ' · 第 <b>' + pg + '</b>/' + pgs + ' 页 · 本页 <b>' + (t.returned || 0) + '</b> 只'
          : '');
    }
    const flipText = m.is_current ? '距交节 ' + m.days_to_flip + ' 天后换下一柱'
      : m.is_past ? '已过去 ' + (-m.days_to_flip) + ' 天'
      : '距起 ' + m.days_to_flip + ' 天';
    const liveState = out.live_month || null;
    const winLayerNote = S.oppLayer === 'xun' || S.oppLayer === 'week';
    const liveNote = m.is_current
      ? (liveState && liveState.status === 'ok'
          ? (winLayerNote
              ? ' · 本月已走完的段已按日更补入历史统计（至 ' + esc(liveState.latest_trade_date) + '）；未走完的段只显示临时值'
              : ' · 本月实际已按日更切片更新到 ' + esc(liveState.latest_trade_date) + '（未走完，临时值）')
          : liveState && liveState.reason ? ' · 本月日更未就绪：' + esc(liveState.reason) : '')
      : '';
    const monthNote = ' · ' + esc(m.label) + ' ' + esc(m.pillar) + '：' + flipText + '，' +
      (hasData ? '该月行情 ' + m.market_rows_in_month.toLocaleString('en-US') + ' 行已入库' + actualSummaryTxt(m)
        : m.is_current
          ? (winLayerNote
              ? '本月整月尚未计入历史库'
              : '本月尚未计入历史库（历史统计只收完整流月）')
          : '该月行情未入库') +
      liveNote +
      (out.params.both_months && out.next_month
        ? ' · 叠加下月 ' + esc(out.next_month.month_pillar) + '（' + esc(out.next_month.start_term) +
          ' ' + esc(date10(out.next_month.start_at)) + ' → ' +
          esc(date10(out.next_month.end_at)) + '）' : '');
    $('#o-note').innerHTML = noteHtml + monthNote;
    if (!rows.length) {
      const gsel = $('#o-grade');
      const gmax = { '0': 0, '1-2': 2, '3-5': 5, '6+': Infinity }[gsel.value];
      const minN = out.params.min_n || 0;
      box.appendChild(el('p', 'p-6 text-center text-slate-400 text-sm', (gmax !== undefined && gmax < minN)
        ? '没有返回行：出现次数下限 ≥' + minN + ' 已经把所有「' +
          gsel.selectedOptions[0].text.replace('样本等级 ', '') + '」的行排除了，两个条件互相矛盾。' +
          '把出现次数下限改成「不限」，或把样本等级改回「任意」。'
        : out.params.both_months
          ? '没有返回行：没有' + (S.dataset === 'etf' ? ' ETF ' : '股票') +
            '同时满足本月（' + esc(m.pillar) + '）与下月（' +
            esc(out.next_month.month_pillar) + '）的两套条件（' + rTxt + ' · ' + nrTxt +
            '）。把其中一个条件放宽再试。'
          : out.params.cap_tier
            ? '没有返回行：当前市值档筛选为「' + esc(out.params.cap_tier) + '」，该月没有符合条件的' +
              (S.dataset === 'etf' ? ' ETF ' : '股票') + '。把「市值档」改回「全部」，或换一个月再试。'
          : '该月在当前筛选条件下没有返回行（出现次数下限过高、样本等级/占比过窄、' +
            '稳健度/历史跌幅差异/一致性条件过严，或关系筛选太严）。' +
            (out.params.stab === 'only_good'
              ? '当前是「只看稳/只看差异小」，可先改回「不限」看全部。' : '') +
            (out.params.cons
              ? '当前是「' + consTxt + '」，可先改回「一致性 不限」看全部。' : '')));
      return;
    }
    const isWindowLayer = S.oppLayer === 'xun' || S.oppLayer === 'week';
    const cols = [
      ['name', '名称 / 代码', oppNameCell, null, OPP_HEAD_TIPS.name],
    ];
    if (S.segMode) {
      // 「本月状态 / 本月实际」只对本月有意义：选了往月/未来月就没有进行中这回事，隐藏而不是显示一排—
      const isCurMonth = Number($('#o-month').value || 0) === 0;
      const mLabel = isCurMonth ? '本月' : '该月';
      const segCols = [
        ['natal_year_pillar', '生辰八字', oppBirthCell, null, OPP_HEAD_TIPS.natal_year_pillar],
        ['seg_info', '100%段（区间）', oppSegInfoCell, 'center',
          '本月同一流月柱的 100% 段。\n第二行是该流月的真实日历时间（按所选月份）；≈ 表示该日还未来到，按工作日外推（休市日未计入）。'],
        ['seg_status', mLabel + '状态', oppSegStatusCell, 'center',
          '本月：进行中 / 未到 / 已过去（并给剩余交易日）。\n该月：已走完 / 数据不全 / 无 60 分钟线。'],
        ['seg_actual', mLabel + '实际', oppSegActualCell, 'center',
          '这一段在所选月份里真实走出来的结果（60 分钟线）。\n当前月进行中的是未走完的临时值；已走完的会写明与历史同向还是反向。\n历史月份的结果如果在冻结库里，会注明「已计入历史」。'],
        ['seg_hit', '段内命中', oppSegHitCell, 'center',
          '这条段在历次同柱月里都同向：命中次数/可计算次数 = 100%；当前这段不计入历史分母。\n这段已走完时，第三行「含本月/含该月」= 历史命中数并入这一次的真实结果后重算。'],
        ['seg_mean', '段平均', oppSegMeanCell, 'center', '这条段在历次同柱月的平均收益（不是整月收益）。'],
        ['seg_min', '段最差', oppSegMinCell, 'center', '这条段在历次同柱月里最差的一次收益（不是整月）。'],
        ['seg_best', '最好一次', oppSegBestCell, 'center', '这条段在历次同柱月里最赚的一次收益（不是整月）。'],
        ['seg_len', '长度', oppSegLenCell, 'center', '这条段的交易日长度。'],
        ['seg_stab', '段内稳健度', oppSegStabCell, 'center',
          '只用这条段自己的历史：最差一次离 0 的边际 / 段内振幅、最好一次占累计。\n阈值与流月机会表同一套；样本越多越容易偏严。'],
        ['seg_cons', '段内一致性', oppSegConsCell, 'center',
          '只用这条段自己的历史：CV = 样本标准差 ÷ 均值；≤0.50 一致、0.50–0.80 一般、>0.80 差异大。\n幅度不足 = 最小幅度 <1%。'],
      ];
      cols.splice(1, 0, ...segCols);
    }
    if (isWindowLayer) {
      cols.push(['window_tag', '窗位及区间', oppWindowCell, 'center', OPP_HEAD_TIPS.window_tag]);
    }
    if (!S.segMode) cols.push(['natal_year_pillar', '生辰八字', oppBirthCell, null, OPP_HEAD_TIPS.natal_year_pillar]);
    cols.push(
      ['natal_element', S.dataset === 'etf' ? 'ETF五行' : '标的五行', oppNatalElementCell, null, OPP_HEAD_TIPS.natal_element],
      ['flow_pillar', '流月(五行)', (r) =>
        '<div class="leading-tight text-center" title="流月柱 ' + esc(r.flow_pillar) + ' · 五行 ' + esc(r.stem_element || '') + '/' + esc(r.branch_element || '') + '">' +
          '<div class="font-bold c-up whitespace-nowrap">' + esc(r.flow_pillar) + '</div>' +
          '<div class="text-[10px] text-slate-400 whitespace-nowrap">' + esc(r.stem_element || '') + '/' + esc(r.branch_element || '') + '</div>' +
        '</div>', 'center', OPP_HEAD_TIPS.flow_pillar]
    );
    if (isWindowLayer) {
      cols.push(['valid_invalid', '有效/总窗(空)', (r) => {
        const valN = oppPolicy() === 'ALL' ? r.ALL_valid_n : r.exclude_single_n;
        const allN = r.all_sample_n;
        const invN = r.ALL_invalid_n || 0;
        return '<span class="tabular-num whitespace-nowrap text-xs"><span class="font-bold text-slate-100">' + (valN != null ? valN : '—') + '</span><span class="text-slate-500 mx-0.5">/</span><span class="text-slate-400">' + (allN != null ? allN : '—') + '</span>' +
          (invN > 0 ? ' <span class="text-amber-400 text-[10px]" title="空窗 ' + invN + ' 次（无交易日不入分母）">(' + invN + '空)</span>' : '') + '</span>';
      }, 'center', OPP_HEAD_TIPS.valid_invalid]);
    }
    cols.push(
      ['up_n_ALL', '涨/跌/平', (r) => {
        const ud = oppUpDown(r);
        return '<span class="tabular-num whitespace-nowrap text-xs"><span class="c-up font-bold">' + ud[0] + '</span><span class="text-slate-600 mx-0.5">/</span><span class="c-down font-bold">' + ud[1] + '</span><span class="text-slate-600 mx-0.5">/</span><span class="text-slate-400">' + ud[2] + '</span></span>';
      }, null, OPP_HEAD_TIPS.up_n_ALL],
      ['up_rate_ALL', '历史' + DT + '占比', (r) => {
        const rateVal = oppRate(r);
        if (rateVal === null || rateVal === undefined) return '<span class="dim">—</span>';
        const p = pct(rateVal);
        const isDown = oppDir() === 'down';
        const txtColor = isDown ? 'c-down' : 'c-up';
        return '<span class="tabular-num font-bold ' + txtColor + ' text-xs whitespace-nowrap">' + p + '</span>';
      }, null, OPP_HEAD_TIPS.up_rate_ALL],
      ['actual_return', '实际走势', (r) => {
        const lv = r.live_actual;
        if (m.is_current && lv) {
          if (lv.status === 'waiting') {
            return '<span class="px-1.5 py-0.5 rounded text-[10.5px] bg-slate-700/60 text-slate-300 font-medium whitespace-nowrap border border-slate-600/50" title="这一格还没开始：' + esc(lv.window_start) + ' 起">未开始</span>' +
              '<div class="text-[9.5px] text-slate-500 whitespace-nowrap">' + esc(String(lv.window_start || '').slice(5)) + ' 起</div>';
          }
          if (lv.status === 'no_bars') {
            return '<span class="dim text-[11px]" title="日更切片里这一格没有该票成交（停牌或空窗）">无行情</span>';
          }
          if (lv.ret !== null && lv.ret !== undefined) {
            const isDone = lv.status === 'done';
            const badgeCls = dirBadge(lv.direction);
            const sub = (isDone ? '已走完' : '进行中') + ' · 至' + String(lv.asof || '').slice(5);
            const dayTxt = isDone
              ? (lv.total_days || lv.elapsed || 0) + ' 个交易日'
              : '第' + (lv.elapsed || 0) + '个交易日';
            const tip = isDone
              ? '这一格已走完：窗首日开盘→末日收盘（日更切片计算，' + dayTxt + '），已补入上面的历史统计。'
              : '这一格未走完：显示截至 ' + (lv.latest_trade_date || '') + ' 的临时值（首日开盘→最新收盘，' +
                dayTxt + '），不计入历史统计；收盘数据由日更任务自动刷新。';
            return '<span class="px-1.5 py-0.5 rounded text-[10.5px] ' + badgeCls + ' font-bold whitespace-nowrap" title="' + esc(tip) + '">' + dirTxt(lv.direction) + ' ' + ret(lv.ret) + '</span>' +
              '<div class="text-[9.5px] text-slate-400 whitespace-nowrap" title="' + esc(tip) + '">' + esc(sub) + '</div>';
          }
        }
        if (!hasData) {
          return m.is_current
            ? '<span class="px-1.5 py-0.5 rounded text-[10.5px] bg-slate-700/60 text-slate-300 font-medium whitespace-nowrap border border-slate-600/50" title="本节气月尚未结束，暂无闭环行情">进行中</span>'
            : '<span class="dim text-[11px]" title="该流月尚未到来，暂无行情">—</span>';
        }
        if (r.actual_return === null || r.actual_return === undefined) {
          return '<span class="dim text-[11px]" title="所选窗/月无行情（可能停牌或空窗）">无行情</span>';
        }
        const badgeCls = dirBadge(r.actual_direction);
        const dTxt = dirTxt(r.actual_direction);
        const rTxt = ret(r.actual_return);
        return '<span class="px-1.5 py-0.5 rounded text-[10.5px] ' + badgeCls + ' font-bold whitespace-nowrap">' + dTxt + ' ' + rTxt + '</span>';
      }, null, OPP_HEAD_TIPS.actual_return]
    );
    const both = !isWindowLayer && !!(out.params.both_months && out.next_month);
    const tail = [
      ['mean_body_return_ALL', '平均涨跌', (r) => {
        const val = oppMean(r);
        const cls = val > 0 ? 'c-up' : val < 0 ? 'c-down' : 'c-flat';
        return '<span class="tabular-num font-bold ' + cls + ' text-xs whitespace-nowrap">' + ret(val) + '</span>';
      }],
      ['excess_mean', '同期超额', (r) => {
        const val = r.excess_mean;
        if (val === null || val === undefined) return '<span class="dim">—</span>';
        const cls = val > 0 ? 'c-up' : val < 0 ? 'c-down' : 'c-flat';
        return '<span class="tabular-num font-bold ' + cls + ' text-xs whitespace-nowrap">' + ret(val) + '</span>';
      }, 'center', isWindowLayer ? '与同市场指数同窗收益之差（逐次平均）' : undefined],
      ['stability', oppDir() === 'down' ? '历史跌幅差异' : '稳健度', oppStabilityCell, 'center', isWindowLayer ? '同窗历史：边际/振幅与集中度；阈值与月层同一套' : undefined],
      ['consistency', '一致性', oppConsistencyCell, 'center', CONS_HEAD_TIP],
      ['occurrence_years', '历次年份', (r) => {
        const ys = (r.occurrence_years || '').split(';').map((s) => s.trim()).filter(Boolean);
        if (!ys.length) return '<span class="dim">—</span>';
        return '<span class="font-mono text-[10.5px] text-slate-300 whitespace-nowrap" title="' + (isWindowLayer ? '' : '相邻间隔 ' + esc(r.year_gaps || '')) + '">' +
          esc(ys.join('/')) + '</span>';
      }, 'center', isWindowLayer ? '该格历史出现的年份' : undefined],
      ['relation_types', '命中关系', (r) => oppRelCell(r, { compact: true }), 'center', (out.rel_head ? out.rel_head + '。\n' : '') + OPP_HEAD_TIPS.relation_types],
      ['cap_tier', '市值档', (r) => capTierCell(r, out.params.cap_asof_month)],
    ];
    if (both) {
      tail.splice(2, 0, ['next_rate', '下月' + DT + '占比', (r) =>
        '<span class="tabular-num font-bold ' + (oppDir() === 'down' ? 'c-down' : 'c-up') + '" title="下月 ' + esc(out.next_month.month_pillar) + ' 柱：出现 ' + r.next_n +
        ' 次、' + DT + ' ' + (oppDir() === 'down' ? r.next_down : r.next_up) +
        ' 次、超额均值 ' + ret(r.next_excess) + '">' +
        pct(r.next_rate) + '<div class="text-[9.5px] text-slate-400">均 ' + ret(r.next_mean) + '</div></span>']);
    }
    if (oppDir() === 'up') {
      const meanIdx = tail.findIndex((c) => c[0] === 'mean_body_return_ALL');
      tail.splice(meanIdx + 1, 0,
        ['min_body_return_ALL', '最差一次', oppMinReturnCell, 'center', MINRET_HEAD_TIP],
        ['max_body_return_ALL', '最好一次', oppMaxReturnCell, 'center', MAXRET_HEAD_TIP]);
    }
    let showCols = cols.concat(tail);
    if (S.segMode) {
      const lb = {
        up_n_ALL: '整月涨/跌/平', up_rate_ALL: '整月' + DT + '占比',
        actual_return: '整月实际', mean_body_return_ALL: '整月平均',
        min_body_return_ALL: '整月最差', max_body_return_ALL: '整月最好',
        excess_mean: '整月超额', consistency: '整月一致性',
        stability: oppDir() === 'down' ? '整月跌幅差异' : '整月稳健度',
      };
      showCols.forEach((c) => { if (lb[c[0]]) c[1] = lb[c[0]]; });
      // 100%段模式只回答五件事：哪只票、哪一段、现在到哪、历史几次全中、平均/最差。
      // 机会表的整月列不在本表展开，点一行去抽屉看完整版。
      const keep = new Set(['name', 'natal_year_pillar', 'seg_info', 'seg_status', 'seg_actual',
                            'seg_hit', 'seg_mean', 'seg_min', 'seg_best', 'seg_len',
                            'seg_stab', 'seg_cons']);
      showCols = showCols.filter((c) => keep.has(c[0]));
    }
    // 表头说明只维护在 OPP_HEAD_TIPS 一处；这里补上尚未显式挂 title 的列（含动态列）。
    for (const c of showCols) {
      if (!c[4] && OPP_HEAD_TIPS[c[0]]) c[4] = OPP_HEAD_TIPS[c[0]];
    }
    // 导出始终用全部列；屏幕上按「列设置」隐藏部分列（名称列固定显示）
    const spec = oppExportSpec(showCols, out);
    if (spec) S.oppExport = { spec, rows, out, m };
    S.oppColsAll = showCols.map((c) => [c[0], c[1]]);
    const hiddenCols = oppHiddenCols();
    const visCols = showCols.filter((c) => c[0] === 'name' || !hiddenCols.has(c[0]));
    const tableWrap = el('div', 'w-full opp-scroll');
    const tbl = table(visCols, rows, (r, tr) => {
      document.querySelectorAll('#opptables tbody tr.sel').forEach((x) => x.classList.remove('sel'));
      tr.classList.add('sel');
      if (S.segMode) showSegDrawer(r); else showOppDrawer(r);
    }, { cls: 'dense opp',
         rowData: (r) => {
           const d = { code: r.security_code, n: oppDen(r), rate: oppRate(r) };
           if (both) d.nextRate = r.next_rate;
           return d;
         },
         rowClass: (r, i) => {
           if (!isWindowLayer || S.oppWindowNo) return '';
           const isDiff = i > 0 && rows[i - 1].identity_code !== r.identity_code;
           return isDiff ? 'grp-border-top' : '';
         },
         widths: getOppColWidths(visCols) });
    bindOppSortHeaders(tbl, visCols);
    tableWrap.appendChild(tbl);
    box.appendChild(tableWrap);
    const pgs = t.pages || 0;
    if (!S.segMode && pgs > 1) {
      const pg = t.page || 1;
      const size = t.page_size || rows.length || 100;
      const start = (pg - 1) * size + 1;
      const end = start + rows.length - 1;
      const bar = el('div', 'rv-pager');
      const prev = el('button', 'rv-btn rv-btn-sm', '← 上一页');
      prev.type = 'button'; prev.disabled = pg <= 1;
      prev.onclick = () => goOppPage(pg - 1);
      const next = el('button', 'rv-btn rv-btn-sm', '下一页 →');
      next.type = 'button'; next.disabled = pg >= pgs;
      next.onclick = () => goOppPage(pg + 1);
      const info = el('span', 'rv-pager-info');
      info.innerHTML = '第 <b>' + pg + '</b> / ' + pgs + ' 页 · 第 ' + start.toLocaleString('en-US') +
        '–' + end.toLocaleString('en-US') + ' 只（共 ' + (t.in_scope || 0).toLocaleString('en-US') + ' 只）';
      bar.appendChild(prev); bar.appendChild(info); bar.appendChild(next);
      box.appendChild(bar);
    }
    syncExportBtn();
  });
}

/* 机会表翻页：只改页码，不改任何筛选；翻完自动写回会话快照与地址栏历史 */
function goOppPage(n) {
  const t = (S.opp && S.opp.months && S.opp.months[0] && S.opp.months[0].totals) || {};
  const pages = t.pages || 1;
  const p = Math.min(Math.max(1, Math.round(n) || 1), pages);
  if (p === (S.oppPage || 1)) return;
  S.oppPage = p;
  scheduleFiltersSave();
  renderOpportunity().then(() => {
    const box = $('#opptables');
    if (box) box.scrollIntoView({ block: 'start', behavior: 'smooth' });
  });
}

/* ---------------- 创新交互：右侧平滑滑出式抽屉 (Slide Drawer) ---------------- */
function closeOppDrawer(e) {
  if (e && e.target && e.target.id !== 'slide-drawer') return;
  const drawer = $('#slide-drawer');
  if (drawer) drawer.classList.add('hidden');
}

async function openStockFromBoard(identityCode, securityCode, tab, pillar) {
  closeOppDrawer();
  setBoardMode(false, { syncUrl: false });
  S.fromBoard = true;
  if (securityCode) $('#q').value = securityCode;
  if (pillar) $('#p-search').value = pillar;
  await loadStock(identityCode);
  const targetTab = tab || 'stock';
  activateTab(targetTab, { noUrl: true, force: true });
  if (targetTab === 'pillar') renderPillar();
  syncReturnBar();
  pushView({ id: identityCode, tab: currentTab(), board: false, fromBoard: true });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

const gotoStockDetail = (identityCode, securityCode) =>
  openStockFromBoard(identityCode, securityCode, 'stock');

const gotoStockPillar = (identityCode, securityCode, pillar) =>
  openStockFromBoard(identityCode, securityCode, 'pillar', pillar);

let drawerWindowMode = 'xun'; // 'xun' | 'week'
let drawerWindowData = null;
let drawerWinCtx = null;
let drawerArrowSeq = 0;

/* ---------------- 旬/段同窗逐日走势：历史各年 + 平均 + 本月进行中 ---------------- */
function interpWinPct(pts, x) {
  if (!pts || !pts.length) return null;
  if (x <= pts[0][0]) return pts[0][1];
  for (let i = 1; i < pts.length; i++) {
    if (x <= pts[i][0]) {
      const x0 = pts[i - 1][0], y0 = pts[i - 1][1], x1 = pts[i][0], y1 = pts[i][1];
      const t = x1 === x0 ? 0 : (x - x0) / (x1 - x0);
      return y0 + (y1 - y0) * t;
    }
  }
  return pts[pts.length - 1][1];
}

function highlightWinPath(containerId, year, on) {
  const p = document.getElementById('wpath-' + containerId + '-' + year);
  if (!p) return;
  p.setAttribute('stroke', on ? '#ffffff' : p.dataset.color);
  p.setAttribute('stroke-width', on ? '3' : '1.6');
}

function renderNaturalWeekChart(box, nw, winName) {
  if (!box) return;
  box.innerHTML = '';
  if (!nw || !nw.pts || nw.pts.length < 2 || !nw.n_bars) {
    box.classList.add('hidden');
    return;
  }
  const winTxt = winName ? '「' + winName + '」' : '下面那个窗口';
  box.classList.remove('hidden');
  const W = 360, H = 104, padL = 34, padR = 24, padT = 10, padB = 20;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const pts = nw.pts.map((q) => [q[0], q[1] / 10000]);
  const ys = [0].concat(pts.map((q) => q[1]));
  let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
  const ypad = (ymax - ymin) * 0.18 || 0.01;
  ymin -= ypad; ymax += ypad;
  const mapX = (x) => padL + Math.max(0, Math.min(1, x)) * innerW;
  const mapY = (y) => padT + (1 - (y - ymin) / (ymax - ymin)) * innerH;
  const md = (s) => (s ? s.slice(5) : '');
  const line = pts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0]).toFixed(1)} ${mapY(q[1]).toFixed(1)}`).join(' ');
  const last = pts[pts.length - 1];
  const ex = mapX(last[0]), ey = mapY(last[1]);
  const retTxt = (nw.e_bp > 0 ? '+' : '') + (nw.e_bp / 100).toFixed(2) + '%';
  const retCol = (nw.e_bp || 0) >= 0 ? '#f87171' : '#34d399';
  const asofTxt = md(nw.asof);
  let todaySvg = '';
  if (nw.today_x != null) {
    const tx = mapX(nw.today_x);
    todaySvg = `<line x1="${tx.toFixed(1)}" y1="${padT}" x2="${tx.toFixed(1)}" y2="${padT + innerH}" stroke="#22d3ee" stroke-width="1" stroke-dasharray="2,3" opacity="0.45"/>`
      + `<text x="${tx.toFixed(1)}" y="${padT + 8}" font-size="8" fill="#67e8f9" text-anchor="middle">今天 ${md(nw.today)}</text>`;
  }
  const zeroY = mapY(0).toFixed(1);
  box.innerHTML = `
    <div class="relative bg-slate-950 rounded-xl border border-slate-800 p-2 space-y-2">
      <div class="flex items-center justify-between gap-2 px-1">
        <div class="text-xs font-bold text-slate-200">本周实际走势 · 自然周 ${md(nw.start)}→${md(nw.end)}</div>
        <span class="px-1.5 py-0.5 rounded border border-cyan-900/60 bg-cyan-950/40 text-[10px] font-bold text-cyan-300">数据到 ${asofTxt} · ${nw.n_bars} 个交易日</span>
      </div>
      <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto overflow-visible font-mono">
        <line x1="${padL}" y1="${zeroY}" x2="${padL + innerW}" y2="${zeroY}" stroke="#64748b" stroke-width="1" stroke-dasharray="4,3"/>
        <text x="${padL - 5}" y="${Number(zeroY) + 3}" font-size="8" fill="#94a3b8" text-anchor="end">0%</text>
        <text x="${padL - 5}" y="${padT + 6}" font-size="8" fill="#64748b" text-anchor="end">${ymax > 0 ? '+' : ''}${(ymax * 100).toFixed(0)}%</text>
        ${todaySvg}
        <path d="${line}" fill="none" stroke="#22d3ee" stroke-width="7" stroke-linejoin="round" stroke-linecap="round" opacity="0.16"/>
        <path d="${line}" fill="none" stroke="#22d3ee" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="5,2.6"/>
        <circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="3.2" fill="#22d3ee" stroke="#082f49" stroke-width="1.2"/>
        <text x="${Math.min(ex + 5, W - 4).toFixed(1)}" y="${Math.max(padT + 7, ey - 6).toFixed(1)}" font-size="8.5" font-weight="bold" fill="${retCol}" text-anchor="${ex > W - 92 ? 'end' : 'start'}">${asofTxt} 收盘 ${retTxt}</text>
        <text x="${padL}" y="${H - 3}" font-size="8.5" fill="#64748b" text-anchor="start">周一 ${md(nw.start)}</text>
        <text x="${padL + innerW}" y="${H - 3}" font-size="8.5" fill="#64748b" text-anchor="end">周日 ${md(nw.end)}</text>
      </svg>
      <div class="text-[10px] text-slate-500 px-1 leading-relaxed">只画本周实际收盘（${esc(nw.first || asofTxt)}→${esc(nw.last || asofTxt)}），<b class="text-slate-400">不参与下面${esc(winTxt)}的 100% 统计</b>；不是预测。</div>
    </div>`;
}

function renderWindowPathChart(box, d, isUp) {
  const W = 360, H = 136, padL = 34, padR = 24, padT = 10, padB = 22;
  const isMonth = d.layer === 'month';
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const cid = 'dw' + Date.now().toString(36);
  const series = (d.hist || []).map((h) => ({
    h, pts: (h.pts || []).map((q) => [q[0], q[1] / 10000]),
  }));
  const live = d.live || {};
  const livePts = (live.pts || []).map((q) => [q[0], q[1] / 10000]);
  const drawLive = (live.status === 'ongoing' || live.status === 'done') && livePts.length >= 2;
  // “今天”用服务端日历的北京 asof，不用浏览器本地日期（本机时区不是北京）
  const today = (S.cal && S.cal.asof) || null;
  let todayX = null;
  if (live.status === 'ongoing' && live.start && live.end && today) {
    const t0 = Date.parse(live.start + 'T00:00:00Z');
    const t1 = Date.parse(live.end + 'T00:00:00Z');
    const tt = Date.parse(today + 'T00:00:00Z');
    if (t1 > t0 && tt >= t0 && tt <= t1) todayX = (tt - t0) / (t1 - t0);
  }
  const lagging = todayX != null && live.asof && today && live.asof < today;

  const ys = [0];
  series.forEach((s) => s.pts.forEach((q) => ys.push(q[1])));
  if (drawLive) livePts.forEach((q) => ys.push(q[1]));
  let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
  const ypad = (ymax - ymin) * 0.12 || 0.02;
  ymin -= ypad; ymax += ypad;
  const mapX = (x) => (padL + Math.max(0, Math.min(1, x)) * innerW).toFixed(1);
  const mapY = (y) => (padT + (1 - (y - ymin) / (ymax - ymin)) * innerH).toFixed(1);

  const span = ymax - ymin;
  const step = span > 0.5 ? 0.2 : span > 0.26 ? 0.1 : span > 0.13 ? 0.05 : 0.02;
  let grid = '';
  for (let v = Math.ceil(ymin / step) * step; v <= ymax + 1e-9; v += step) {
    const isZero = Math.abs(v) < 1e-9;
    grid += `<line x1="${padL}" y1="${mapY(v)}" x2="${padL + innerW}" y2="${mapY(v)}" stroke="${isZero ? '#64748b' : '#1e293b'}" stroke-width="1"${isZero ? ' stroke-dasharray="4,3"' : ''}/>`;
    grid += `<text x="${padL - 5}" y="${Number(mapY(v)) + 3}" font-size="8" fill="${isZero ? '#94a3b8' : '#64748b'}" text-anchor="end">${v > 0 ? '+' : ''}${(v * 100).toFixed(0)}%</text>`;
  }

  const color = isUp ? 'rgba(244,63,94,0.55)' : 'rgba(56,189,248,0.5)';
  const lines = series.map((s) => {
    const dd = s.pts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
    const tip = `${s.h.month_id} ${s.h.first}→${s.h.last}（${s.h.e_bp > 0 ? '+' : ''}${(s.h.e_bp / 100).toFixed(2)}%）\n低点第${s.h.lk}日 · 高点第${s.h.hk}日 · ${s.h.n}个交易日`;
    return `<path id="wpath-${cid}-${s.h.year}" data-color="${color}" d="${dd}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" class="cursor-pointer transition-all"><title>${esc(tip)}</title></path>`;
  }).join('');

  // 窗尾是周末/休市：真实线到最后一个交易日为止，再用灰色虚线平推到窗尾，明确标「休市」
  let closeExt = '', closeLabels = '', closeYears = [];
  series.forEach((s) => {
    const lastQ = s.pts[s.pts.length - 1];
    if (!lastQ) return;
    const xLast = lastQ[0], yLast = lastQ[1];
    if (s.h.last && s.h.end && s.h.last < s.h.end && xLast < 0.999) {
      const x0 = Number(mapX(xLast)), x1 = Number(mapX(1)), y0 = Number(mapY(yLast));
      closeExt += `<path d="M ${x0} ${y0} L ${x1} ${y0}" fill="none" stroke="#94a3b8" stroke-width="1.3" stroke-dasharray="3,3" opacity="0.8"><title>窗尾休市（或停牌）：沿用 ${esc(s.h.last)} 收盘，不是真实行情</title></path>`;
      closeLabels += `<text x="${x1 - 2}" y="${Math.max(padT + 8, y0 - 3)}" font-size="7.5" fill="#94a3b8" text-anchor="end">休市</text>`;
      closeYears.push(s.h.year);
    }
  });

  const gridX = [];
  for (let i = 0; i <= 24; i++) gridX.push(i / 24);
  const meanPts = gridX.map((x) => {
    const vals = series.map((s) => interpWinPct(s.pts, x)).filter((v) => v != null);
    return [x, vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0];
  });
  const meanD = meanPts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
  const meanEnd = meanPts[meanPts.length - 1][1];
  // 平均值标签压在灰色「休市」标签之上：先画休市，再给平均值铺一层深底色
  const meanLblY = Math.max(padT + 8, Number(mapY(meanEnd)) - 6);

  let liveSvg = '';
  if (drawLive) {
    const ld = livePts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
    const last = livePts[livePts.length - 1];
    const ex = Number(mapX(last[0])), ey = Number(mapY(last[1]));
    const retTxt = (live.e_bp > 0 ? '+' : '') + (live.e_bp / 100).toFixed(2) + '%';
    const retCol = (live.e_bp || 0) >= 0 ? '#f87171' : '#34d399';
    const anchor = ex > W - 92 ? 'end' : 'start';
    const lx = anchor === 'end' ? ex - 5 : ex + 5;
    const asofTxt = live.asof ? live.asof.slice(5) : '—';
    liveSvg = `
      <path d="${ld}" fill="none" stroke="#22d3ee" stroke-width="7" stroke-linejoin="round" stroke-linecap="round" opacity="0.16"/>
      <path d="${ld}" fill="none" stroke="#22d3ee" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="5,2.6"/>
      <circle cx="${ex}" cy="${ey}" r="3.2" fill="#22d3ee" stroke="#082f49" stroke-width="1.2"/>
      <text x="${lx}" y="${Math.max(padT + 7, ey - 6)}" font-size="8.5" font-weight="bold" fill="${retCol}" text-anchor="${anchor}">${asofTxt} 收盘 ${retTxt}</text>
    `;
  }

  let todaySvg = '', liveExt = '';
  if (todayX != null) {
    const tx = Number(mapX(todayX));
    const tax = tx > W - 58 ? 'end' : 'middle';
    todaySvg = `<line x1="${tx}" y1="${padT}" x2="${tx}" y2="${padT + innerH}" stroke="#22d3ee" stroke-width="1" stroke-dasharray="2,3" opacity="0.45"/>`
      + `<text x="${tx + (tax === 'end' ? -2 : 0)}" y="${padT + 8}" font-size="8" fill="#67e8f9" text-anchor="${tax}" opacity="0.9">今天 ${esc(today.slice(5))}</text>`;
  }
  if (drawLive && lagging) {
    const last = livePts[livePts.length - 1];
    const x0 = Number(mapX(last[0])), y0 = Number(mapY(last[1])), x1 = Number(mapX(todayX));
    if (x1 > x0 + 0.5) {
      const extAnchor = x1 > W - 76 ? 'end' : 'start';
      liveExt = `<path d="M ${x0} ${y0} L ${x1} ${y0}" fill="none" stroke="#22d3ee" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="5,2.6" opacity="0.9"/>`
        + `<circle cx="${x1}" cy="${y0}" r="2.6" fill="#082f49" stroke="#22d3ee" stroke-width="1.4"/>`
        + `<text x="${x1 + (extAnchor === 'end' ? -4 : 4)}" y="${Math.max(padT + 7, y0 - 6)}" font-size="8" fill="#67e8f9" text-anchor="${extAnchor}">今日未收盘</text>`;
    }
  }

  const fmtBp = (bp) => (bp == null ? '—' : (bp > 0 ? '+' : '') + (bp / 100).toFixed(2) + '%');
  const med = (arr) => {
    const a = arr.filter((x) => x != null).sort((x, y) => x - y);
    return a.length ? (a.length % 2 ? a[Math.floor(a.length / 2)]
      : (a[a.length / 2 - 1] + a[a.length / 2]) / 2) : null;
  };
  const medLow = med(series.map((s) => s.h.lk));
  const medHigh = med(series.map((s) => s.h.hk));
  const shortYears = series.filter((s) => s.h.last && s.h.end && s.h.last < s.h.end)
    .map((s) => s.h.year);
  const st = d.stats || {};
  const upTxt = st.up_n == null ? '—' : (st.up_n + '/' + st.valid_n);
  const rateTxt = st.up_rate == null ? '—' : (st.up_rate * 100).toFixed(0) + '%';
  const downTxt = st.down_n == null ? '—' : (st.down_n + '/' + st.valid_n);
  const downRateTxt = (st.valid_n && st.down_n != null)
    ? (st.down_n / st.valid_n * 100).toFixed(0) + '%' : '—';
  const badgeTxt = isUp ? ('历史上涨 ' + upTxt + ' · ' + rateTxt)
    : ('历史下跌 ' + downTxt + ' · ' + downRateTxt);

  const pills = series.map((s) => {
    const e = (s.h.e_bp / 100).toFixed(1);
    const sign = s.h.e_bp > 0 ? '+' + e + '%' : e + '%';
    const cls = s.h.dir === 'UP' ? 'text-red-300 border-red-900/60 bg-red-950/40'
      : s.h.dir === 'DOWN' ? 'text-emerald-300 border-emerald-900/60 bg-emerald-950/40'
        : 'text-slate-300 border-slate-700 bg-slate-800/40';
    return `<span onmouseenter="highlightWinPath('${cid}','${s.h.year}',true)" onmouseleave="highlightWinPath('${cid}','${s.h.year}',false)" class="px-1.5 py-0.5 rounded border text-[10px] font-mono cursor-pointer transition hover:bg-slate-800 hover:text-white ${cls}" title="${esc(s.h.month_id + ' ' + s.h.first + '→' + s.h.last + ' · 低点第' + s.h.lk + '日 · 高点第' + s.h.hk + '日')}">${s.h.year}年 (${sign})</span>`;
  }).join('');

  let liveHtml = '';
  const asof = live.asof || '—';
  if ((live.status === 'ongoing' || live.status === 'done') && !live.no_bars) {
    const cmp = livePts.filter((q) => q[0] > 0);
    let dev = null;
    if (cmp.length) {
      let sum = 0, cnt = 0;
      cmp.forEach((q) => {
        const mv = interpWinPct(meanPts, q[0]);
        if (mv != null) { sum += Math.abs(q[1] - mv); cnt++; }
      });
      if (cnt) dev = (sum / cnt) * 100;
    }
    const verdict = dev == null ? '首日暂不比较'
      : dev <= 2 ? '<span class="text-emerald-300 font-bold">高度贴合平均线</span>'
      : dev <= 5 ? '<span class="text-amber-300 font-bold">大体跟随平均线</span>'
      : '<span class="text-rose-300 font-bold">明显偏离平均线</span>';
    const label = isMonth ? (live.status === 'done' ? '本月已完成' : '本月进行中')
      : (live.status === 'done' ? '本月该窗已完成' : '本月同窗进行中');
    liveHtml = `
      <div class="text-[10px] text-cyan-100/90 bg-cyan-950/30 border border-cyan-900/50 rounded px-1.5 py-1 leading-relaxed space-y-0.5">
        <div>• <b>${label}</b>（截至 ${esc(asof)}，已 ${live.n_bars} 个交易日）：累计 <b>${fmtBp(live.e_bp)}</b>（窗首日开盘→最新收盘，复权）· 与历史平均线：${verdict}${dev == null ? '' : `（平均偏离 ${dev.toFixed(1)} 个百分点）`}</div>
        <div>• 本月窗口 ${esc(live.start || '—')}→${esc(live.end || '—')} · 低点第 <b>${live.lk ?? '—'}</b> 日 · 高点第 <b>${live.hk ?? '—'}</b> 日（历史中枢：低点第 ${medLow ?? '—'} 日 / 高点第 ${medHigh ?? '—'} 日）</div>
        ${lagging ? `<div class="text-cyan-300/80">• <b>数据时点</b>：今天 ${esc(today)} 还没有新收盘（休市或日更 19:05 未到），青蓝线只到 ${esc(asof)}；图中青蓝虚点只标「今天」的位置，不是今日行情。</div>` : ''}
      </div>`;
  } else {
    let why = '';
    if (live.status === 'waiting') {
      why = `本窗 <b>${esc(live.start)}</b> 才开始（还有 ${live.days_to_start} 天）；到点后青蓝虚线自动出现。`;
    } else if (live.status === 'not_current_pillar') {
      const nx = d.next_occurrence;
      why = `当前流月是 <b>${esc(d.current_pillar || '—')}</b>，本柱不是当前柱，所以没有进行中曲线。`
        + (nx ? ` 该柱下次出现 <b>${esc((nx.start_at || '').slice(0, 10))}</b>（冻结历法，不是预测）。` : '');
    } else if (live.status === 'not_current_month') {
      why = `${esc(live.reason || '所选月份不是本月')}，本图只画历史，不借用本月曲线。`;
    } else if (live.status === 'live_not_ready') {
      why = `当月层还没就绪：${esc(live.reason || '日更任务未跑')}。本图只画历史。`;
    } else if (live.no_bars) {
      why = '本窗已开始，但这只票在本窗内还没有成交（停牌？），暂无可画的本月线。';
    } else if (live.status === 'no_current_month') {
      why = '冻结历法里没有当前月，无法判断进行中窗口。';
    } else {
      why = esc(live.reason || '没有进行中曲线。');
    }
    liveHtml = `<div class="text-[10px] text-slate-400 bg-slate-900/60 border border-slate-800 rounded px-1.5 py-1 leading-relaxed">${why}</div>`;
  }

  const md5 = (s) => (s ? esc(s.slice(5)) : '');
  let midMD = '';
  if (live.start && live.end) {
    const t0 = Date.parse(live.start + 'T00:00:00Z');
    const t1 = Date.parse(live.end + 'T00:00:00Z');
    if (t1 >= t0) midMD = new Date((t0 + t1) / 2).toISOString().slice(5, 10);
  }
  const axisLbl = (base, md) => (md ? base + ' ' + md : base);

  box.innerHTML = `
    <div class="relative bg-slate-950 rounded-xl border border-slate-800 p-2 space-y-2">
      <div class="flex items-center justify-between gap-2 px-1">
        <div class="text-xs font-bold text-slate-200">同窗逐日走势 · <span class="text-indigo-400 font-mono">${esc(d.pillar)}</span> · ${esc(d.win_name)}</div>
        <span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${isUp ? 'badge-up' : 'badge-down'}">${esc(badgeTxt)}</span>
      </div>
      <div class="text-[10px] text-slate-500 px-1">${isMonth ? '窗位：整个流月（交节后第 0 天 → 窗尾）' : '窗位：交节后第 ' + (d.stats && d.stats.day_from != null ? d.stats.day_from : '—') + '–' + (d.stats && d.stats.day_to != null ? d.stats.day_to : '下节') + ' 日'} · 共 ${d.n_hist} 次历史${series.length !== d.n_hist ? '（' + series.length + ' 条可绘，源日线缺失的年份跳过）' : ''} · 每根细线 = 一个真实年份的逐日收盘</div>
      <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto overflow-visible font-mono">
        <line x1="${mapX(1 / 3)}" y1="${padT}" x2="${mapX(1 / 3)}" y2="${padT + innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        <line x1="${mapX(2 / 3)}" y1="${padT}" x2="${mapX(2 / 3)}" y2="${padT + innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        ${grid}
        ${todaySvg}
        ${lines}
        ${closeExt}
        ${closeLabels}
        <path d="${meanD}" fill="none" stroke="#fbbf24" stroke-width="2.6" stroke-linecap="round"/>
        <rect x="${Number(mapX(1)) - 56}" y="${meanLblY - 8}" width="56" height="10.5" rx="2" fill="#020617" opacity="0.78"/>
        <text x="${Number(mapX(1)) - 2}" y="${meanLblY}" font-size="8.5" fill="#fbbf24" text-anchor="end">平均 ${meanEnd > 0 ? '+' : ''}${(meanEnd * 100).toFixed(1)}%</text>
        ${liveSvg}
        ${liveExt}
        <text x="${mapX(0)}" y="${H - 4}" font-size="8.5" fill="#64748b" text-anchor="start">${axisLbl('窗首', md5(live.start))}</text>
        <text x="${mapX(0.5)}" y="${H - 4}" font-size="8.5" fill="#64748b" text-anchor="middle">${axisLbl('窗中', midMD)}</text>
        <text x="${mapX(1)}" y="${H - 4}" font-size="8.5" fill="#64748b" text-anchor="end">${axisLbl('窗尾', md5(live.end))}</text>
      </svg>
      <div class="flex flex-wrap items-center justify-between gap-1 text-[10px] text-slate-400 px-1 border-t border-slate-900 pt-1.5">
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 bg-amber-400 inline-block"></span> 黄线 = 历史平均线</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 inline-block" style="background:${color}"></span> 细线 = 历史各年真实逐日走势</span>
        ${closeYears.length ? `<span class="flex items-center gap-1"><span class="w-2.5 h-0.5 inline-block" style="background:#94a3b8"></span> 灰虚线 = 窗尾休市（沿用最后收盘）</span>` : ''}
        ${drawLive ? `<span class="flex items-center gap-1"><span class="inline-block" style="width:11px;border-top:2px dashed #22d3ee"></span> 青蓝虚线 = 本月同窗${live.status === 'done' ? '（已完成）' : '（进行中）'}</span>` : ''}
      </div>
      <div class="text-[10px] text-slate-500 px-1 leading-relaxed">纵轴 = 累计涨跌幅%（<b class="text-slate-400">各自窗首日开盘 = 0%</b>）；横轴 = 各年自身窗口内的日历进度（窗首→窗尾，刻度日期为当前窗口）。历史线用冻结日线（至 2026-10-14），本月线用日更当月层。${shortYears.length ? `窗尾为周末/休市的年份（${shortYears.join('、')}）用灰虚线沿用最后收盘平推到窗尾，灰线不是真实行情。` : ''}只做描述性对照，不是买点。</div>
      ${liveHtml}
      <div class="flex flex-wrap gap-1 px-1">${pills}</div>
    </div>
  `;
}

async function renderDrawerPathSection(row, layer, winNo, monthId) {
  const box = $('#drawer-path-box');
  const wbox = $('#drawer-weekchart-box');
  if (!box) return;
  box.innerHTML = '';
  if (wbox) { wbox.innerHTML = ''; wbox.classList.add('hidden'); }
  const L = (layer === 'xun' || layer === 'week') ? layer : 'month';
  const W = L === 'month' ? 1 : winNo;
  if (!W) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  box.innerHTML = '<div class="text-xs text-slate-500 py-2">加载同窗逐日走势...</div>';
  const my = seqStart('drawerpath');
  const ep = S.epoch;
  try {
    const d = await get('/api/window_path', {
      id: row.identity_code, pillar: row.flow_pillar, layer: L, win: W,
      month: monthId || '',
    });
    if (seqStale('drawerpath', my) || ep !== S.epoch) return;
    box.innerHTML = '';
    // 旧后端会把不认识的层回落到段层：宁可不画，也不能把段窗口当成整月
    if (d.layer !== L) {
      if (wbox) { wbox.innerHTML = ''; wbox.classList.add('hidden'); }
      box.innerHTML = '<div class="rounded-xl theme-card border border-slate-700/60 p-3 text-[11px] text-slate-400">同窗逐日走势暂不可用：当前服务返回的是「' + esc(d.layer) + '」层，不是请求的「' + esc(L === 'month' ? '整月' : L === 'xun' ? '旬' : '周') + '」层（服务需重启一次）。为避免拿错窗口，这里先不画。</div>';
      return;
    }
    if (!d.available) {
      if (wbox) { wbox.innerHTML = ''; wbox.classList.add('hidden'); }
      box.innerHTML = '<div class="rounded-xl theme-card border border-slate-700/60 p-3 text-[11px] text-slate-400">同窗逐日走势不可用：' + esc(d.reason || '未知原因') + '</div>';
      return;
    }
    const upRate = (row.up_rate_ALL != null ? row.up_rate_ALL : row.up_rate);
    const isUp = upRate == null ? (oppDir() !== 'down') : upRate >= 0.5;
    renderNaturalWeekChart(wbox, L === 'month' ? null : d.natural_week, d.win_name);
    renderWindowPathChart(box, d, isUp);
    box.insertAdjacentHTML('beforeend', windowPatternCard(d, d.hist, isUp, L));
  } catch (e) {
    if (seqStale('drawerpath', my)) return;
    box.innerHTML = '<div class="rounded-xl theme-card border border-slate-700/60 p-3 text-[11px] text-slate-400">同窗逐日走势加载失败：' + esc(e && e.message ? e.message : e) + '</div>';
  }
}

async function renderDrawerWindowSection(identityCode, flowPillar) {
  const box = $('#drawer-windows-box');
  if (!box) return;
  box.innerHTML = '<div class="text-xs text-slate-500 py-2">加载月内窗位数据...</div>';

  try {
    const data = await get('/api/stock_window_rules', { id: identityCode, pillar: flowPillar });
    drawerWindowData = data;
    drawerWinCtx = { id: identityCode, pillar: flowPillar };
    updateDrawerWindowView();
  } catch (err) {
    box.innerHTML = '';
  }
}

async function renderDrawerArrowTable() {
  const box = $('#dw-arrow-box');
  if (!box) return;
  if (!drawerWinCtx) { box.innerHTML = '<div class="text-[10px] text-slate-500 py-1">—</div>'; return; }
  const layer = drawerWindowMode === 'week' ? 'week' : 'xun';
  const N = layer === 'xun' ? 3 : 6;
  const isUp = oppDir() !== 'down';
  const my = ++drawerArrowSeq;
  box.innerHTML = '<div class="text-[10px] text-slate-500 py-1">按日线重算各窗口极值…</div>';
  const arr = await Promise.all(Array.from({ length: N }, (_, i) => get('/api/window_path', {
    id: drawerWinCtx.id, pillar: drawerWinCtx.pillar, layer, win: i + 1,
  }).catch(() => null)));
  if (my !== drawerArrowSeq) return;
  if (!arr.some((d) => d && d.available)) {
    const why = (arr.find((d) => d && d.reason) || {}).reason || '未知原因';
    box.innerHTML = '<div class="text-[10px] text-slate-500 py-1">窗口极值不可用：' + esc(why) + '</div>';
    return;
  }
  const cells = arr.map((d, i) => {
    if (!d) return '<tr><td class="px-2 py-1 text-slate-400">第 ' + (i + 1) + ' 窗</td><td colspan="4" class="px-2 py-1 text-slate-500">读取失败</td></tr>';
    if (!d.available) return '<tr><td class="px-2 py-1 text-slate-400">' + esc(d.win_name || ('第 ' + (i + 1) + ' 窗')) + '</td><td colspan="4" class="px-2 py-1 text-slate-500">' + esc(d.reason || '不可用') + '</td></tr>';
    const c = arrowStats(d.hist, isUp);
    const st = d.stats || {};
    const rng = st.day_from == null ? '' : (st.day_to == null ? ' ' + st.day_from + '–下节' : ' ' + st.day_from + '–' + st.day_to + ' 日');
    const icon = (ok) => '<span title="' + GATE_TIP_UI + '">' + (ok ? '✅' : '⚠️') + '</span>';
    const low = c ? (c.N === 1 ? '<span class="text-slate-400">1 次 · 第 ' + c.meanLow + ' 日</span>'
      : icon(c.pc(c.lowFirst) >= 60) + ' <b>' + c.pc(c.lowFirst) + '%</b> 前 1/3 · 第 ' + c.meanLow.toFixed(1) + ' 日')
      : '<span class="text-slate-500">—</span>';
    const high = c ? (c.N === 1 ? '<span class="text-slate-500">只有 1 次</span>'
      : icon(c.pc(c.highLast) >= 60) + ' <b>' + c.pc(c.highLast) + '%</b> 末 1/3 · 第 ' + c.meanHigh.toFixed(1) + ' 日')
      : '<span class="text-slate-500">—</span>';
    const ord = c ? (c.N === 1 ? '<span class="text-slate-500">只有 1 次</span>'
      : '<b>' + c.pc(c.ordered) + '%</b> ' + (isUp ? '先低后高' : '先高后低') + ' · ' + (c.span >= 0 ? c.span.toFixed(1) + ' 日' : '高比低早 ' + Math.abs(c.span).toFixed(1) + ' 日'))
      : '<span class="text-slate-500">—</span>';
    return '<tr class="border-t border-slate-800/60">'
      + '<td class="px-2 py-1 text-slate-200 whitespace-nowrap">' + esc(d.win_name || '') + '<span class="text-slate-500">' + esc(rng) + '</span></td>'
      + '<td class="px-2 py-1 text-center whitespace-nowrap">' + (c ? c.N : 0) + '<span class="text-slate-500">/' + (d.n_hist || 0) + '</span></td>'
      + '<td class="px-2 py-1 whitespace-nowrap">' + low + '</td>'
      + '<td class="px-2 py-1 whitespace-nowrap">' + high + '</td>'
      + '<td class="px-2 py-1 whitespace-nowrap">' + ord + '</td>'
      + '</tr>';
  }).join('');
  box.innerHTML = '<div class="text-[11px] font-bold text-slate-200 mb-1">窗口' + (isUp ? '涨' : '跌') + '极值时序 <span class="text-[10px] font-normal text-slate-500">按冻结日线重算</span></div>'
    + '<div class="overflow-x-auto"><table class="w-full text-left text-[10.5px]">'
    + '<thead><tr class="text-[10px] text-slate-400 border-b border-slate-800">'
    + '<th class="px-2 py-1">窗位</th><th class="px-2 py-1 text-center">次数</th>'
    + '<th class="px-2 py-1">最低点</th><th class="px-2 py-1">最高点</th><th class="px-2 py-1">单边性</th></tr></thead>'
    + '<tbody>' + cells + '</tbody></table></div>'
    + '<div class="text-[10px] text-slate-500 mt-1">次数 = 同向样本 / 该窗位全部样本；✅/⚠️：占比 ≥60% 标 ✅，不到标 ⚠️（本页门槛，非显著性）。</div>';
}

function updateDrawerWindowView() {
  const box = $('#drawer-windows-box');
  if (!box || !drawerWindowData) return;
  const data = drawerWindowData;

  box.innerHTML = '';
  const wrap = el('div', 'space-y-3 pt-2 border-t border-slate-700/60');
  
  // Header
  const head = el('div', 'flex items-center justify-between');
  head.innerHTML = `
    <span class="text-xs font-bold text-slate-200 flex items-center gap-1.5">
      <svg class="w-4 h-4 text-amber-400" fill="currentColor" viewBox="0 0 20 20"><path d="M5 3a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2V5a2 2 0 00-2-2H5zM5 11a2 2 0 00-2 2v2a2 2 0 002 2h2a2 2 0 002-2v-2a2 2 0 00-2-2H5zM11 5a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V5zM11 13a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"></path></svg>
      月内窗位小表（个股层：3 旬 / 6 段）
    </span>
    <div class="seg inline-flex p-0.5 rounded-lg theme-card-sub border border-slate-700 text-xs">
      <button id="dw-btn-xun" type="button" class="px-2.5 py-1 rounded font-semibold ${drawerWindowMode === 'xun' ? 'on bg-indigo-600 text-white shadow-sm' : 'text-slate-400 hover:text-indigo-400'} transition">3 旬视图</button>
      <button id="dw-btn-week" type="button" class="px-2.5 py-1 rounded font-medium ${drawerWindowMode === 'week' ? 'on bg-indigo-600 text-white shadow-sm' : 'text-slate-400 hover:text-indigo-400'} transition">6 段视图</button>
    </div>
  `;
  wrap.appendChild(head);

  // Cards
  const cardsBox = el('div', drawerWindowMode === 'xun' ? 'grid grid-cols-3 gap-2 text-xs' : 'grid grid-cols-3 sm:grid-cols-6 gap-1.5 text-xs');
  cardsBox.id = 'dw-cards';
  if (drawerWindowMode === 'xun') {
    (data.xun_rules || []).forEach((w) => {
      const isPerfect = w.ALL_up_rate === 1.0;
      const ratePct = pct(w.ALL_up_rate);
      const meanVal = w.mean_return;
      const meanCls = meanVal > 0 ? 'c-up' : (meanVal < 0 ? 'c-down' : 'c-flat');
      const meanStr = ret(meanVal / 100.0);
      const card = el('div', `p-2.5 rounded-xl border ${isPerfect ? 'border-red-500/40 bg-red-950/20' : 'theme-card-sub border-slate-700/60'} space-y-1`);
      card.innerHTML = `
        <div class="flex items-center justify-between">
          <span class="font-bold text-slate-200 text-xs">${esc(w.window_tag)}</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-bold ${isPerfect ? 'badge-up' : 'badge-flat'}">
            ${isPerfect ? '满格 100%' : '上涨占比 ' + ratePct}
          </span>
        </div>
        <div class="text-[11px] text-slate-400">
          涨/跌/平: <b class="c-up">${w.ALL_up_n}</b> / <b class="c-down">${w.ALL_down_n}</b> / <b>${w.ALL_flat_n}</b> (共${w.ALL_valid_n}次)
        </div>
        <div class="flex items-center justify-between pt-1 border-t border-slate-700/40 text-[11px]">
          <span class="text-slate-500">平均涨跌</span>
          <span class="font-bold font-mono ${meanCls}">${meanStr}</span>
        </div>
      `;
      cardsBox.appendChild(card);
    });
  } else {
    (data.week_rules || []).forEach((w) => {
      const isHigh = w.ALL_up_rate >= 0.8;
      const ratePct = pct(w.ALL_up_rate);
      const meanVal = w.mean_return;
      const meanCls = meanVal > 0 ? 'c-up' : (meanVal < 0 ? 'c-down' : 'c-flat');
      const meanStr = ret(meanVal / 100.0);
      const card = el('div', `p-2 rounded-xl border ${isHigh ? 'border-red-500/40 bg-red-950/20' : 'theme-card-sub border-slate-700/60'} space-y-0.5`);
      card.innerHTML = `
        <div class="font-bold text-[10px] text-slate-200 truncate">${esc(w.window_tag)}</div>
        <div class="text-[10px] ${isHigh ? 'c-up font-bold' : 'text-slate-400'}">
          ${ratePct} (${w.ALL_up_n}/${w.ALL_valid_n})
        </div>
        <div class="font-bold font-mono text-[10px] ${meanCls}">${meanStr}</div>
      `;
      cardsBox.appendChild(card);
    });
  }
  wrap.appendChild(cardsBox);

  // 窗口极值（最低点 / 最高点 / 单边性）：按冻结日线重算，与上面的涨跌统计分开
  const arrowBox = el('div', 'rounded-xl theme-card border border-slate-700/60 p-2');
  arrowBox.id = 'dw-arrow-box';
  wrap.appendChild(arrowBox);

  // 证据穿透表
  const tblWrap = el('div', 'rounded-xl theme-card border border-slate-700/60 overflow-hidden');
  const tbl = el('table', 'w-full text-left text-xs divide-y divide-slate-800 font-mono');
  const thead = el('thead', 'bg-slate-900 text-slate-400 text-[11px]');
  const trH = el('tr');
  if (drawerWindowMode === 'xun') {
    trH.innerHTML = `
      <th class="px-2.5 py-1.5">年份</th>
      <th class="px-2.5 py-1.5">流月</th>
      <th class="px-2.5 py-1.5">上旬 (0–10)</th>
      <th class="px-2.5 py-1.5">中旬 (10–20)</th>
      <th class="px-2.5 py-1.5">下旬 (20–下节)</th>
    `;
  } else {
    trH.innerHTML = `
      <th class="px-2 py-1.5">年份</th>
      <th class="px-2 py-1.5">第1段 (0-5)</th>
      <th class="px-2 py-1.5">第2段 (5-10)</th>
      <th class="px-2 py-1.5">第3段 (10-15)</th>
      <th class="px-2 py-1.5">第4段 (15-20)</th>
      <th class="px-2 py-1.5">第5段 (20-25)</th>
      <th class="px-2 py-1.5">第6段 (25-下节)</th>
    `;
  }
  thead.appendChild(trH);
  tbl.appendChild(thead);

  const tbody = el('tbody', 'divide-y divide-slate-800/60');
  if (drawerWindowMode === 'xun') {
    const byYear = {};
    (data.xun_occ || []).forEach((o) => {
      if (!byYear[o.solar_year]) byYear[o.solar_year] = { year: o.solar_year, month_id: o.month_id, xuns: {} };
      byYear[o.solar_year].xuns[o.xun_no] = o;
    });
    Object.values(byYear).forEach((row) => {
      const tr = el('tr');
      const cellHtml = (xNo) => {
        const item = row.xuns[xNo];
        if (!item || !item.valid_bars) return '<span class="text-slate-600">无交易日</span>';
        const isUp = item.direction === 'UP';
        const isDown = item.direction === 'DOWN';
        const col = isUp ? 'c-up' : (isDown ? 'c-down' : 'c-flat');
        const badgeCol = isUp ? 'badge-up' : (isDown ? 'badge-down' : 'badge-flat');
        const retStr = (item.window_return_pct > 0 ? '+' : '') + item.window_return_pct.toFixed(2) + '%';
        return '<span class="px-1 py-0.2 rounded text-[10px] ' + badgeCol + ' font-bold mr-1">' + item.direction + '</span>' +
          '<span class="' + col + ' font-bold">' + retStr + '</span>' +
          '<span class="text-[10px] text-slate-500 ml-0.5">(' + item.valid_bars + 'K)</span>';
      };
      tr.innerHTML = `
        <td class="px-2.5 py-1.5 font-bold text-slate-300">${row.year}年</td>
        <td class="px-2.5 py-1.5 text-slate-400">${esc(row.month_id)}</td>
        <td class="px-2.5 py-1.5">${cellHtml(1)}</td>
        <td class="px-2.5 py-1.5">${cellHtml(2)}</td>
        <td class="px-2.5 py-1.5">${cellHtml(3)}</td>
      `;
      tbody.appendChild(tr);
    });
  } else {
    const byYear = {};
    (data.week_occ || []).forEach((o) => {
      if (!byYear[o.solar_year]) byYear[o.solar_year] = { year: o.solar_year, weeks: {} };
      byYear[o.solar_year].weeks[o.week_no] = o;
    });
    Object.values(byYear).forEach((row) => {
      const tr = el('tr');
      const cellHtml = (wNo) => {
        const item = row.weeks[wNo];
        if (!item || !item.valid_bars) return '<span class="text-slate-600 text-[10px]">无K</span>';
        const isUp = item.direction === 'UP';
        const isDown = item.direction === 'DOWN';
        const col = isUp ? 'c-up' : (isDown ? 'c-down' : 'c-flat');
        const retStr = (item.window_return_pct > 0 ? '+' : '') + item.window_return_pct.toFixed(1) + '%';
        return `<span class="${col} font-semibold">${retStr}</span>`;
      };
      tr.innerHTML = `
        <td class="px-2 py-1.5 font-bold text-slate-300">${row.year}</td>
        <td class="px-2 py-1.5">${cellHtml(1)}</td>
        <td class="px-2 py-1.5">${cellHtml(2)}</td>
        <td class="px-2 py-1.5">${cellHtml(3)}</td>
        <td class="px-2 py-1.5">${cellHtml(4)}</td>
        <td class="px-2 py-1.5">${cellHtml(5)}</td>
        <td class="px-2 py-1.5">${cellHtml(6)}</td>
      `;
      tbody.appendChild(tr);
    });
  }
  tbl.appendChild(tbody);
  tblWrap.appendChild(tbl);
  wrap.appendChild(tblWrap);
  box.appendChild(wrap);
  renderDrawerArrowTable();

  const btnX = $('#dw-btn-xun'); if (btnX) btnX.onclick = () => { drawerWindowMode = 'xun'; updateDrawerWindowView(); };
  const btnW = $('#dw-btn-week'); if (btnW) btnW.onclick = () => { drawerWindowMode = 'week'; updateDrawerWindowView(); };
}

function showDrawerSections(showBoard) {
  ['drawer-jump', 'drawer-stability', 'drawer-windows-box', 'drawer-month-note',
   'drawer-month-box'].forEach((id) => {
    const el = $('#' + id); if (el) el.style.display = showBoard ? '' : 'none';
  });
  const tb = $('#drawer-table-box'); if (tb) tb.style.display = '';
  const gp = $('#drawer-btn-goto-pillar'); if (gp) gp.style.display = showBoard ? '' : 'none';
  const hint = $('#drawer-month-hint'); if (hint) hint.style.display = showBoard ? '' : 'none';
}

function renderSegPathChart(box, d, isUp) {
  const hist = d.hist || [];
  if (!hist.length) {
    box.innerHTML = '<div class="rounded-xl theme-card border border-slate-700/60 p-3 text-[11px] text-slate-400">这段没有可绘制的历史 60 分钟线。</div>';
    return;
  }
  const W = 720, H = 300, padL = 46, padR = 30, padT = 18, padB = 34;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const cid = 'sg' + Date.now().toString(36);
  const series = hist.map((h) => ({ h, pts: (h.pts || []).map((q) => [q[0], q[1] / 10000]) }));
  const live = d.live || {};
  const livePts = (live.pts || []).map((q) => [q[0], q[1] / 10000]);
  const drawLive = livePts.length >= 2;
  const axis = d.axis || {};
  const md = (s) => (s ? String(s).slice(5) : '');
  const dayTxt = (n) => '第' + n + '日';
  const midDay = Math.floor((d.start_day + d.end_day) / 2);
  const nBars = hist[0].n;
  const startTxt = '第' + d.start_day + '日 ' + d.start_time;
  const endTxt = '第' + d.end_day + '日 ' + d.end_time;

  const ys = [0];
  series.forEach((s) => s.pts.forEach((q) => ys.push(q[1])));
  if (drawLive) livePts.forEach((q) => ys.push(q[1]));
  let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
  const ypad = (ymax - ymin) * 0.12 || 0.02;
  ymin -= ypad; ymax += ypad;
  const mapX = (x) => (padL + Math.max(0, Math.min(1, x)) * innerW).toFixed(1);
  const mapY = (y) => (padT + (1 - (y - ymin) / (ymax - ymin)) * innerH).toFixed(1);
  const span = ymax - ymin;
  const step = span > 0.5 ? 0.2 : span > 0.26 ? 0.1 : span > 0.13 ? 0.05 : 0.02;
  let grid = '';
  for (let v = Math.ceil(ymin / step) * step; v <= ymax + 1e-9; v += step) {
    const isZero = Math.abs(v) < 1e-9;
    grid += `<line x1="${padL}" y1="${mapY(v)}" x2="${padL + innerW}" y2="${mapY(v)}" stroke="${isZero ? '#64748b' : '#1e293b'}" stroke-width="1"${isZero ? ' stroke-dasharray="4,3"' : ''}/>`;
    grid += `<text x="${padL - 5}" y="${Number(mapY(v)) + 3}" font-size="8" fill="${isZero ? '#94a3b8' : '#64748b'}" text-anchor="end">${v > 0 ? '+' : ''}${(v * 100).toFixed(0)}%</text>`;
  }

  const color = isUp ? 'rgba(244,63,94,0.55)' : 'rgba(56,189,248,0.5)';
  const lines = series.map((s) => {
    const dd = s.pts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
    const r = s.h.e_bp / 100;
    const tip = `${s.h.month_id} ${s.h.first}→${s.h.last}（${r > 0 ? '+' : ''}${r.toFixed(2)}%）\n段内低点第${s.h.lk}根 · 高点第${s.h.hk}根 · 共${s.h.n}根60分钟`;
    return `<path id="wpath-${cid}-${s.h.year}" data-color="${color}" d="${dd}" fill="none" stroke="${color}" stroke-width="1.7" stroke-linejoin="round" class="cursor-pointer transition-all"><title>${esc(tip)}</title></path>`;
  }).join('');

  const gridX = [];
  for (let i = 0; i <= 48; i++) gridX.push(i / 48);
  const meanPts = gridX.map((x) => {
    const vals = series.map((s) => interpWinPct(s.pts, x)).filter((v) => v != null);
    return [x, vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0];
  });
  const meanD = meanPts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
  const meanEnd = meanPts[meanPts.length - 1][1];
  const meanLblY = Math.max(padT + 8, Number(mapY(meanEnd)) - 6);

  const fmtPct = (bp) => (bp == null ? '—' : (bp > 0 ? '+' : '') + (bp / 100).toFixed(2) + '%');
  let liveSvg = '', futureExt = '', nowSvg = '';
  if (drawLive) {
    const ld = livePts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
    const last = livePts[livePts.length - 1];
    const ex = Number(mapX(last[0])), ey = Number(mapY(last[1]));
    const retCol = (live.e_bp || 0) >= 0 ? '#f87171' : '#34d399';
    const anchor = ex > W - 150 ? 'end' : 'start';
    const lx = anchor === 'end' ? ex - 5 : ex + 5;
    const asofTxt = live.asof_dt ? esc(String(live.asof_dt).slice(5)) : esc(live.asof || '—');
    liveSvg = `
      <path d="${ld}" fill="none" stroke="#22d3ee" stroke-width="7" stroke-linejoin="round" stroke-linecap="round" opacity="0.16"/>
      <path d="${ld}" fill="none" stroke="#22d3ee" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="5,2.6"/>
      <circle cx="${ex}" cy="${ey}" r="3.2" fill="#22d3ee" stroke="#082f49" stroke-width="1.2"/>
      <text x="${lx}" y="${Math.max(padT + 7, ey - 6)}" font-size="8.5" font-weight="bold" fill="${retCol}" text-anchor="${anchor}">${asofTxt} 收盘 ${fmtPct(live.e_bp)}</text>`;
    if (!live.complete) {
      const x1 = Number(mapX(1));
      futureExt = `<path d="M ${ex} ${ey} L ${x1} ${ey}" fill="none" stroke="#94a3b8" stroke-width="1.6" stroke-dasharray="3,3" opacity="0.85"><title>这段还没走完：沿用 ${esc(live.asof_dt || live.asof || '')} 收盘，不是未来行情</title></path>`
        + `<text x="${x1 - 2}" y="${Math.max(padT + 8, ey - 4)}" font-size="7.5" fill="#94a3b8" text-anchor="end">未到</text>`;
      nowSvg = `<line x1="${ex}" y1="${padT}" x2="${ex}" y2="${padT + innerH}" stroke="#22d3ee" stroke-width="1" stroke-dasharray="2,3" opacity="0.4"/>`
        + `<text x="${Math.min(ex + 2, W - 4)}" y="${padT + 8}" font-size="8" fill="#67e8f9" text-anchor="${ex > W - 90 ? 'end' : 'start'}">现在 ${asofTxt}</text>`;
    }
  }

  const med = (arr) => {
    const a = arr.filter((x) => x != null).sort((x, y) => x - y);
    return a.length ? (a.length % 2 ? a[Math.floor(a.length / 2)]
      : (a[a.length / 2 - 1] + a[a.length / 2]) / 2) : null;
  };
  const medLow = med(series.map((s) => s.h.lk));
  const medHigh = med(series.map((s) => s.h.hk));
  const st = d.stats || {};
  const rateTxt = st.up_rate == null ? '—' : (st.up_rate * 100).toFixed(0) + '%';
  const badgeTxt = isUp ? ('历史上涨 ' + (st.up_n ?? '—') + '/' + (st.valid_n ?? '—') + ' · ' + rateTxt)
    : ('历史下跌 ' + (st.down_n ?? '—') + '/' + (st.valid_n ?? '—') + ' · ' + rateTxt);

  const pills = series.map((s) => {
    const e = (s.h.e_bp / 100).toFixed(2);
    const sign = s.h.e_bp > 0 ? '+' + e + '%' : e + '%';
    const cls = s.h.e_bp > 0 ? 'text-red-300 border-red-900/60 bg-red-950/40'
      : s.h.e_bp < 0 ? 'text-emerald-300 border-emerald-900/60 bg-emerald-950/40'
        : 'text-slate-300 border-slate-700 bg-slate-800/40';
    return `<span onmouseenter="highlightWinPath('${cid}','${s.h.year}',true)" onmouseleave="highlightWinPath('${cid}','${s.h.year}',false)" class="px-1.5 py-0.5 rounded border text-[10px] font-mono cursor-pointer transition hover:bg-slate-800 hover:text-white ${cls}" title="${esc(s.h.month_id + ' ' + s.h.first + '→' + s.h.last + ' · 段内低点第' + s.h.lk + '根 · 高点第' + s.h.hk + '根')}">${s.h.year}年 (${sign})</span>`;
  }).join('');

  let liveHtml = '';
  if (drawLive) {
    const cmp = livePts.filter((q) => q[0] > 0);
    let dev = null;
    if (cmp.length) {
      let sum = 0, cnt = 0;
      cmp.forEach((q) => {
        const mv = interpWinPct(meanPts, q[0]);
        if (mv != null) { sum += Math.abs(q[1] - mv); cnt++; }
      });
      if (cnt) dev = (sum / cnt) * 100;
    }
    const verdict = dev == null ? '首根暂不比较'
      : dev <= 2 ? '<span class="text-emerald-300 font-bold">高度贴合平均线</span>'
      : dev <= 5 ? '<span class="text-amber-300 font-bold">大体跟随平均线</span>'
      : '<span class="text-rose-300 font-bold">明显偏离平均线</span>';
    const label = live.complete ? '本月这段已走完（实际 60 分钟线）' : '本月这段进行中';
    const asof = live.asof_dt || live.asof || '—';
    liveHtml = `
      <div class="text-[10px] text-cyan-100/90 bg-cyan-950/30 border border-cyan-900/50 rounded px-1.5 py-1 leading-relaxed space-y-0.5">
        <div>• <b>${label}</b>（截至 ${esc(asof)}，已走 ${live.covered ?? '—'}/${live.total ?? '—'} 根 60 分钟线）：累计 <b>${fmtPct(live.e_bp)}</b>（段首前收盘→最新收盘，前复权）· 与历史平均线：${verdict}${dev == null ? '' : `（平均偏离 ${dev.toFixed(1)} 个百分点）`}</div>
        <div>• 本月段位 ${esc(startTxt)}→${esc(endTxt)} · 段内低点第 <b>${live.lk ?? '—'}</b> 根 · 高点第 <b>${live.hk ?? '—'}</b> 根（历史中枢：低点第 ${medLow ?? '—'} 根 / 高点第 ${medHigh ?? '—'} 根）</div>
        ${live.approx ? `<div class="text-amber-300/80">• <b>本月线是日线近似</b>：${esc(live.note || '')}</div>` : ''}
      </div>`;
  } else {
    let why = '';
    if (live.status === 'upcoming' || d.status === 'upcoming') {
      why = `本月这段还没开始（${esc(startTxt)}），到点后青蓝线自动出现；现在还只能看历史 ${d.n_hist} 次。`;
    } else if (live.status === 'passed' || d.status === 'passed') {
      why = '本月这段已经走完，但没有可画的本月 60 分钟线（停牌或 60 分钟线未同步）。';
    } else if (d.status === 'not_current') {
      why = '所选流月不是本月，本图只画历史，不借用本月曲线。';
    } else {
      why = esc(live.note || '没有本月 60 分钟线，本图只画历史。');
    }
    liveHtml = `<div class="text-[10px] text-slate-400 bg-slate-900/60 border border-slate-800 rounded px-1.5 py-1 leading-relaxed">${why}</div>`;
  }

  const ax = (d0, date, exact) => (date ? ' ' + (exact ? '' : '≈') + md(date) : '');
  const startAxis = '段首 ' + dayTxt(d.start_day) + ax(d.start_day, axis.start, axis.start_exact);
  const midAxis = '段中 ' + dayTxt(midDay) + ax(midDay, axis.mid, axis.mid_exact);
  const endAxis = '段尾 ' + dayTxt(d.end_day) + ax(d.end_day, axis.end, axis.end_exact);

  box.innerHTML = `
    <div class="relative bg-slate-950 rounded-xl border border-slate-800 p-2 space-y-2">
      <div class="flex items-center justify-between gap-2 px-1">
        <div class="text-xs font-bold text-slate-200">同段 60 分钟走势 · <span class="text-indigo-400 font-mono">${esc(d.pillar)}</span> · ${esc(startTxt)}→${esc(endTxt)}（${d.length_days} 天）</div>
        <span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${isUp ? 'badge-up' : 'badge-down'}">${esc(badgeTxt)}</span>
      </div>
      <div class="text-[10px] text-slate-500 px-1">段位：流月内 ${esc(startTxt)}→${esc(endTxt)}（${nBars} 根 60 分钟线） · 共 ${d.n_hist} 次历史${series.length !== d.n_hist ? '（' + series.length + ' 条可绘，源 60 分钟缺失的年份跳过）' : ''} · 每根细线 = 一个真实年份的同段 60 分钟收盘</div>
      <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto overflow-visible font-mono">
        <line x1="${mapX(1 / 3)}" y1="${padT}" x2="${mapX(1 / 3)}" y2="${padT + innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        <line x1="${mapX(2 / 3)}" y1="${padT}" x2="${mapX(2 / 3)}" y2="${padT + innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        ${grid}
        ${nowSvg}
        ${lines}
        ${futureExt}
        <path d="${meanD}" fill="none" stroke="#fbbf24" stroke-width="2.6" stroke-linecap="round"/>
        <rect x="${Number(mapX(1)) - 56}" y="${meanLblY - 8}" width="56" height="10.5" rx="2" fill="#020617" opacity="0.78"/>
        <text x="${Number(mapX(1)) - 2}" y="${meanLblY}" font-size="8.5" fill="#fbbf24" text-anchor="end">平均 ${fmtPct(Math.round(meanEnd * 10000))}</text>
        ${liveSvg}
        <text x="${mapX(0)}" y="${H - 6}" font-size="8.5" fill="#64748b" text-anchor="start">${esc(startAxis)}</text>
        <text x="${mapX(0.5)}" y="${H - 6}" font-size="8.5" fill="#64748b" text-anchor="middle">${esc(midAxis)}</text>
        <text x="${mapX(1)}" y="${H - 6}" font-size="8.5" fill="#64748b" text-anchor="end">${esc(endAxis)}</text>
      </svg>
      <div class="flex flex-wrap items-center justify-between gap-1 text-[10px] text-slate-400 px-1 border-t border-slate-900 pt-1.5">
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 bg-amber-400 inline-block"></span> 黄线 = 历史平均线</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 inline-block" style="background:${color}"></span> 细线 = 历史各年同段真实 60 分钟走势</span>
        ${drawLive ? `<span class="flex items-center gap-1"><span class="inline-block" style="width:11px;border-top:2px dashed #22d3ee"></span> 青蓝虚线 = 本月同段${live.complete ? '（已完成）' : '（进行中）'}</span>` : ''}
        ${futureExt ? `<span class="flex items-center gap-1"><span class="w-2.5 h-0.5 inline-block" style="background:#94a3b8"></span> 灰虚线 = 段尾未到（沿用最后收盘）</span>` : ''}
      </div>
      <div class="text-[10px] text-slate-500 px-1 leading-relaxed">纵轴 = 累计涨跌幅%（<b class="text-slate-400">各自段首前一根 60 分钟收盘 = 0%</b>；段首是流月第一根时用开盘）；横轴 = 段内 60 分钟进度（段首→段尾，刻度日期按所选流月；≈ 表示该日还未来到，按工作日外推）。历史线用冻结 60 分钟线（至 2026-07-17），本月线用 NAS 日更 60 分钟线（前复权）。只做描述性对照，不是买点。</div>
      ${liveHtml}
      <div class="flex flex-wrap gap-1 px-1">${pills}</div>
    </div>
  `;
}

async function showSegDrawer(row) {
  const s = row._seg;
  if (!s) return showOppDrawer(row);
  const drawer = $('#slide-drawer');
  if (!drawer) return;
  showDrawerSections(false);
  ['drawer-stability', 'drawer-windows-box', 'drawer-table-box'].forEach((id) => {
    const el = $('#' + id); if (el) el.style.display = '';
  });
  ['drawer-seg-box', 'drawer-weekchart-box', 'drawer-month-note',
   'drawer-month-box', 'drawer-jump'].forEach((id) => {
    const el = $('#' + id); if (el) el.style.display = 'none';
  });
  const isDown = oppDir() === 'down';
  const pct2 = (v) => (v === null || v === undefined ? '—' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2) + '%');
  const nameEl = $('#drawer-title-name');
  if (nameEl) nameEl.textContent = (row.security_code || '') + ' ' + (row.name || '');
  const pilEl = $('#drawer-title-pillar');
  if (pilEl) pilEl.textContent = '× ' + (row.flow_pillar || '');
  const badge = $('#drawer-title-badge');
  if (badge) {
    badge.className = 'px-2 py-0.5 rounded text-[10px] font-bold ' + (isDown ? 'badge-down' : 'badge-up');
    badge.textContent = s.n_valid + '/' + s.n_avail + ' · 100%';
  }
  const sub = $('#drawer-subtitle');
  if (sub) sub.textContent = '100%段 · 第' + s.start_day + '日 ' + s.start_time + ' → 第' +
    s.end_day + '日 ' + s.end_time + '（' + s.length_days + ' 天）· 历史 ' +
    s.n_valid + '/' + s.n_avail + ' 次全部' + (isDown ? '下跌' : '上涨') +
    (s.status === 'not_current' ? ' · 所选月不是本月，只画历史' : ' · 本月 ' + s.status_text);
  const metrics = $('#drawer-metrics');
  if (metrics) {
    const box = (label, val, cls) => '<div class="p-2 rounded-lg bg-slate-800/40 border border-slate-700/40"><div class="text-[10px] text-slate-400">' + label + '</div><div class="text-sm font-bold font-mono mt-0.5 ' + cls + '">' + val + '</div></div>';
    metrics.innerHTML =
      box('历史命中', s.n_valid + '/' + s.n_avail, isDown ? 'c-down' : 'c-up') +
      box('涨/跌/平', (isDown ? '0/' + s.n_valid + '/0' : s.n_valid + '/0/0'), 'text-slate-100') +
      box(isDown ? '下跌占比' : '上涨占比', '100%', isDown ? 'c-down' : 'c-up') +
      box('段平均', pct2(s.mean_pct), s.mean_pct > 0 ? 'c-up' : 'c-down') +
      box('段最差', pct2(isDown ? s.max_pct : s.min_pct), (isDown ? s.max_pct : s.min_pct) > 0 ? 'c-up' : 'c-down') +
      box('段最好', pct2(isDown ? s.min_pct : s.max_pct), (isDown ? s.min_pct : s.max_pct) > 0 ? 'c-up' : 'c-down') +
      box('长度', s.length_days + ' 天', 'text-slate-100') +
      box('本月状态', s.status === 'not_current' ? '—'
        : s.status === 'passed' ? ('已过去 ' + (s.delta_days == null ? '—' : s.delta_days) + ' 天')
        : s.status === 'upcoming' ? ('未到（还差 ' + (s.delta_days == null ? '—' : s.delta_days) + ' 天）') : s.status_text,
        s.status === 'ongoing' ? 'text-indigo-300' : s.status === 'upcoming' ? 'text-amber-400' : 'text-slate-400');
  }
  const gotoBtn = $('#drawer-btn-goto-stock');
  if (gotoBtn) gotoBtn.onclick = () => gotoStockDetail(row.identity_code, row.security_code);
  const pillarBtn = $('#drawer-btn-goto-pillar');
  if (pillarBtn) {
    pillarBtn.style.display = '';
    pillarBtn.onclick = () => gotoStockPillar(row.identity_code, row.security_code, row.flow_pillar);
    const pn = $('#drawer-btn-pillar-name'); if (pn) pn.textContent = row.flow_pillar || '';
  }
  const pathBox = $('#drawer-path-box');
  if (pathBox) { pathBox.classList.remove('hidden'); pathBox.innerHTML = '<div class="text-xs text-slate-500 py-2">加载这段的走势...</div>'; }
  drawer.classList.remove('hidden');
  try {
    const d = await get('/api/seg/path', {
      code: row.identity_code, pillar: row.flow_pillar, s: s.s, e: s.e,
      offset: S.segCtx ? S.segCtx.offset : 0,
    });
    renderSegPathChart(pathBox, d, !isDown);
    const rets = (d.hist || []).map((h) => h.e_bp / 10000);
    const n = rets.length;
    if (!n) {
      ['drawer-stability', 'drawer-windows-box', 'drawer-table-box'].forEach((id) => {
        const el = $('#' + id); if (el) el.innerHTML = '<div class="text-xs text-slate-500 py-2">没有可统计的历史同段。</div>';
      });
    } else {
      const sum = rets.reduce((a, b) => a + b, 0);
      const mean = sum / n;
      const min = Math.min(...rets), max = Math.max(...rets);
      const std = Math.sqrt(rets.reduce((a, b) => a + (b - mean) * (b - mean), 0) / Math.max(n - 1, 1));
      const cv = mean !== 0 ? std / Math.abs(mean) : null;
      const sumPos = rets.filter((r) => r > 0).reduce((a, b) => a + b, 0);
      const sumNeg = rets.filter((r) => r < 0).reduce((a, b) => a - b, 0);
      const worst = isDown ? max : min;
      const best = isDown ? min : max;
      const extremeShare = isDown ? (sumNeg ? Math.abs(worst) / sumNeg : null) : (sumPos ? best / sumPos : null);
      const meanEx = n > 1 ? (sum - max) / (n - 1) : null;
      const reverse = rets.filter((r) => (isDown ? r >= 0 : r <= 0)).length;
      const up = rets.filter((r) => r > 0).length;
      const down = rets.filter((r) => r < 0).length;
      const pctDec = (v) => (v === null || v === undefined ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(2) + '%');
      const head = (title, badge) => '<div class="flex items-center justify-between mb-3"><b class="text-xs text-slate-200">' + title + '</b>' + (badge || '') + '</div>';
      const stat = (label, val, cls) => '<div><div class="text-[10px] text-slate-400">' + label + '</div><div class="text-sm font-bold font-mono mt-0.5 ' + (cls || 'text-slate-100') + '">' + val + '</div></div>';
      $('#drawer-stability').innerHTML =
        '<div class="rounded-xl theme-card border border-slate-700/60 p-3 mb-3">' +
          head('段内稳定性检查 · ' + esc(d.pillar), '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold badge-up">' + (isDown ? '跌' : '涨') + ' · ' + n + ' 次</span>') +
          '<div class="grid grid-cols-4 gap-2 text-center">' +
            stat('最差一次', pctDec(worst), worst > 0 ? 'c-up' : 'c-down') +
            stat(isDown ? '最差一次占跌幅' : '最好一次占比', extremeShare == null ? '—' : (extremeShare * 100).toFixed(1) + '%', 'text-slate-100') +
            stat('段内振幅', '+' + ((max - min) * 100).toFixed(2) + '%', 'text-slate-100') +
            stat(isDown ? '剔除最差一次均值' : '剔除最好一次均值', pctDec(meanEx), meanEx > 0 ? 'c-up' : 'c-down') +
          '</div>' +
          '<div class="text-[10px] text-slate-400 mt-2">最差 ' + pctDec(worst) + ' · 最好 ' + pctDec(best) + ' · 振幅/均值 ' + (mean !== 0 ? ((max - min) / Math.abs(mean)).toFixed(2) : '—') + '</div>' +
        '</div>' +
        '<div class="rounded-xl theme-card border border-slate-700/60 p-3">' +
          head('段内幅度一致性 · 方向样本 ' + (isDown ? down : up) + '/' + n, '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold ' + (reverse === 0 ? 'badge-up' : 'badge-down') + '">' + (reverse === 0 ? '全部同向' : '反向 ' + reverse + ' 次') + '</span>') +
          '<div class="grid grid-cols-4 gap-2 text-center">' +
            stat('方向CV', cv == null ? '—' : cv.toFixed(2), 'text-slate-100') +
            stat(isDown ? '最大跌幅' : '最小涨幅', pctDec(isDown ? max : min), isDown ? 'c-down' : 'c-up') +
            stat('反向/非方向', reverse + ' 次', reverse ? 'c-down' : 'text-slate-300') +
            stat('全段最差', pctDec(worst), 'c-down') +
          '</div>' +
          '<div class="text-[10px] text-slate-400 mt-2">只统计这条段的历史 ' + n + ' 次；不是下一次上涨概率。</div>' +
        '</div>';
      const maxAbs = Math.max(...rets.map(Math.abs), 0.0001);
      $('#drawer-windows-box').innerHTML =
        '<div class="rounded-xl theme-card border border-slate-700/60 p-3">' +
          head('这 ' + n + ' 次同段收益', '') +
          d.hist.map((h, i) => {
            const r = h.e_bp / 10000;
            const w = Math.min(100, Math.abs(r) / maxAbs * 100);
            const cls = r > 0 ? 'c-up' : 'c-down';
            return '<div class="flex items-center gap-2 mb-1.5 text-[11px]">' +
              '<span class="w-16 shrink-0 font-mono text-slate-300">' + esc(h.month_id) + '</span>' +
              '<div class="flex-1 h-3 rounded bg-slate-900/60 overflow-hidden"><div class="h-3 ' + (r > 0 ? 'bg-rose-500/70' : 'bg-emerald-500/70') + '" style="width:' + w + '%"></div></div>' +
              '<span class="w-16 text-right font-mono font-bold ' + cls + '">' + (r > 0 ? '+' : '') + (r * 100).toFixed(2) + '%</span></div>';
          }).join('') +
        '</div>';
      $('#drawer-table-box').innerHTML =
        '<div class="rounded-xl theme-card border border-slate-700/60 overflow-hidden">' +
          '<div class="px-3 py-2 text-xs font-semibold text-slate-200">这 ' + n + ' 次同段明细</div>' +
          '<table class="w-full text-xs"><thead><tr class="text-slate-400 border-b border-slate-700/60">' +
          '<th class="text-left px-3 py-1.5">月份</th><th class="text-left px-3 py-1.5">区间</th>' +
          '<th class="text-right px-3 py-1.5">收益</th><th class="text-right px-3 py-1.5">低点</th><th class="text-right px-3 py-1.5">高点</th></tr></thead><tbody>' +
          d.hist.map((h) => '<tr class="border-b border-slate-800/60"><td class="px-3 py-1.5 font-mono">' + esc(h.month_id) +
            '</td><td class="px-3 py-1.5 font-mono text-slate-400">' + esc((h.first || '') + '→' + (h.last || '')) +
            '</td><td class="px-3 py-1.5 text-right font-mono font-bold ' + (h.e_bp > 0 ? 'c-up' : 'c-down') + '">' +
            (h.e_bp > 0 ? '+' : '') + (h.e_bp / 100).toFixed(2) + '%</td>' +
            '<td class="px-3 py-1.5 text-right font-mono text-slate-400">第' + h.lk + '根</td>' +
            '<td class="px-3 py-1.5 text-right font-mono text-slate-400">第' + h.hk + '根</td></tr>').join('') +
          '</tbody></table></div>';
    }
  } catch (e) {
    if (pathBox) pathBox.innerHTML = '<div class="rounded-xl theme-card border border-slate-700/60 p-3 text-[11px] text-slate-400">这段走势加载失败：' + esc(e && e.message ? e.message : e) + '</div>';
  }
}

async function showOppDrawer(row) {
  const segBox = $('#drawer-seg-box');
  if (segBox) { segBox.classList.add('hidden'); segBox.innerHTML = ''; }
  showDrawerSections(true);
  const drawer = $('#slide-drawer');
  if (!drawer) return showOppDetail(row);
  const pathBoxEarly = $('#drawer-path-box');
  if (pathBoxEarly) { pathBoxEarly.innerHTML = ''; pathBoxEarly.classList.add('hidden'); }
  const weekBoxEarly = $('#drawer-weekchart-box');
  if (weekBoxEarly) { weekBoxEarly.innerHTML = ''; weekBoxEarly.classList.add('hidden'); }
  seqStart('drawerpath');  // 让上一行尚未返回的路径请求作废，别写进新行的抽屉

  const m = (S.opp && S.opp.months && S.opp.months.find((x) => x.pillar === row.flow_pillar)) || {};
  const my = seqStart('oppdrawer');
  const ep = S.epoch;
  // 页面三的「窗位明细」走 _layer 显式传入的层级，不能借用机会表最后一次选过的层
  const drawLayer = row._layer || S.oppLayer;
  const drawWin = drawLayer === 'week' ? row.week_no : (drawLayer === 'xun' ? row.xun_no : null);
  const winLayer = (drawLayer === 'xun' || drawLayer === 'week') && drawWin;
  const [rows, stockStats, monthRows] = await Promise.all([
    winLayer
      ? get('/api/window_cell_detail', { id: row.identity_code, pillar: row.flow_pillar, layer: drawLayer,
                                          win: drawWin })
      : get('/api/pillar_detail', { id: row.identity_code, pillar: row.flow_pillar }),
    get('/api/stock_stability', { id: row.identity_code }).catch(() => null),
    // 旬/段层主表是窗口行；同柱整月历次另取一份，放在抽屉底部折叠区
    winLayer ? get('/api/pillar_detail', { id: row.identity_code, pillar: row.flow_pillar }).catch(() => [])
             : null,
  ]);
  if (seqStale('oppdrawer', my) || ep !== S.epoch) return;

  // Title
  const titleName = $('#drawer-title-name');
  if (titleName) titleName.textContent = (row.security_code || '') + ' ' + (row.name || '');
  const titlePillar = $('#drawer-title-pillar');
  if (titlePillar) titlePillar.textContent = '× ' + (row.flow_pillar || '');

  // Badge
  const badge = $('#drawer-title-badge');
  if (badge) {
    if (row.actual_direction) {
      const cls = dirCls(row.actual_direction);
      badge.className = 'px-2 py-0.5 rounded text-[10px] font-bold ' + dirBadge(row.actual_direction);
      badge.textContent = (cls === 'up' ? '所选月走强' : cls === 'down' ? '所选月走弱' : '所选月平盘') + ' ' + ret(row.actual_return);
    } else {
      const isDown = oppDir() === 'down';
      badge.className = 'px-2 py-0.5 rounded text-[10px] font-bold ' + (isDown ? 'badge-down' : 'badge-up');
      badge.textContent = '历次对账 · ' + (isDown ? '下跌占比 ' : '上涨占比 ') + pct(oppRate(row));
    }
  }

  // Subtitle
  const sub = $('#drawer-subtitle');
  if (sub) {
    sub.innerHTML = '命中关系：' + oppRelCell(row) + ' · 历次出现 <b class="text-slate-200">' + rows.length + '</b> 次（每5年一周期）';
  }

  // Action Buttons
  const who = S.dataset === 'etf' ? ' ETF ' : '个股';
  const gotoBtn = $('#drawer-btn-goto-stock');
  if (gotoBtn) {
    const txtSpan = gotoBtn.querySelector('span:first-child');
    if (txtSpan) txtSpan.textContent = '进入' + who + '详情页';
    gotoBtn.onclick = () => gotoStockDetail(row.identity_code, row.security_code);
  }

  const pillarBtn = $('#drawer-btn-goto-pillar');
  if (pillarBtn) {
    const pName = $('#drawer-btn-pillar-name');
    if (pName) pName.textContent = row.flow_pillar || '';
    pillarBtn.onclick = () => gotoStockPillar(row.identity_code, row.security_code, row.flow_pillar);
  }

  // Key Metrics Strip
  const ud = oppUpDown(row);
  const nValid = firstNum(row.ALL_valid_n, row.all_sample_n, row.ALL_n_occurrences, row.occurrence_count);
  const udOk = ud[0] !== null && ud[1] !== null && ud[2] !== null && nValid !== null
               && (ud[0] + ud[1] + ud[2]) === nValid;
  const rateVal = udOk ? ud[0] / Math.max(ud[0] + ud[1] + ud[2], 1) : null;
  const ratePct = udOk ? pct(rateVal) : '—';
  const rateNum = Math.min(Math.max((rateVal || 0) * 100, 0), 100).toFixed(1);
  const isDown = oppDir() === 'down';
  const barColor = isDown ? 'bg-emerald-500' : 'bg-red-500';
  const txtColor = isDown ? 'c-down' : 'c-up';
  const meanVal = oppMean(row);
  const meanCls = meanVal > 0 ? 'c-up' : meanVal < 0 ? 'c-down' : 'c-flat';

  const metricsBox = $('#drawer-metrics');
  if (metricsBox) {
    metricsBox.innerHTML =
      '<div class="p-2 rounded-lg bg-slate-800/40 border border-slate-700/40">' +
        '<div class="text-[10px] text-slate-400">历次出现</div>' +
        '<div class="text-sm font-bold font-mono mt-0.5 text-slate-200">' + rows.length + ' 次</div>' +
      '</div>' +
      '<div class="p-2 rounded-lg bg-slate-800/40 border border-slate-700/40">' +
        '<div class="text-[10px] text-slate-400">涨 / 跌 / 平</div>' +
        '<div class="text-xs font-bold font-mono mt-1">' + (udOk
          ? '<span class="c-up font-bold">' + ud[0] + '</span> / <span class="c-down font-bold">' + ud[1] + '</span> / <span class="text-slate-400">' + ud[2] + '</span>'
          : '<span class="c-down">数据不一致（涨+跌+平 ≠ 有效样本 ' + (nValid === null ? '—' : nValid) + '）</span>') + '</div>' +
      '</div>' +
      '<div class="p-2 rounded-lg bg-slate-800/40 border border-slate-700/40">' +
        '<div class="text-[10px] text-slate-400">' + (isDown ? '下跌占比' : '上涨占比') + '</div>' +
        '<div class="flex items-center justify-center gap-1 mt-0.5">' +
          '<span class="text-xs font-bold font-mono ' + txtColor + '">' + ratePct + '</span>' +
          '<div class="w-8 theme-card-sub rounded-full h-1 overflow-hidden border border-slate-700/40"><div class="' + barColor + ' h-1 rounded-full" style="width:' + rateNum + '%"></div></div>' +
        '</div>' +
      '</div>' +
      '<div class="p-2 rounded-lg bg-slate-800/40 border border-slate-700/40">' +
        '<div class="text-[10px] text-slate-400">平均涨跌</div>' +
        '<div class="text-xs font-bold font-mono mt-0.5 ' + meanCls + '">' + ret(meanVal) + '</div>' +
      '</div>';
  }

  // Stability: same-pillar evidence (from the row) + whole-stock volatility (drawer only).
  const stabBox = $('#drawer-stability');
  if (stabBox) {
    const lv = row.stability_level || 'none';
    const hasStab = lv !== 'none' && row.stab_n;
    if (row.stab_min_ret === undefined || row.stab_min_ret === null) {
      const w = row.ALL_worst_return_pct;
      if (typeof w === 'number') row.stab_min_ret = w / 100;
    }
    const scoreTxt = (row.stability_score === null || row.stability_score === undefined)
      ? '' : Number(row.stability_score).toFixed(0) + ' 分';
    const vals = rows.map((x) => Number(x.body_return)).filter((x) => Number.isFinite(x));
    const maxAbs = Math.max(...vals.map((x) => Math.abs(x)), 0.0001);
    const bars = rows.map((x) => {
      const v = Number(x.body_return);
      if (!Number.isFinite(v)) return '';
      const w = Math.min(Math.abs(v) / maxAbs * 100, 100);
      const col = v > 0 ? 'bg-red-500' : v < 0 ? 'bg-emerald-500' : 'bg-slate-500';
      return '<div class="flex items-center gap-2 text-[10px]">' +
        '<span class="w-11 text-slate-400 tabular-nums">' + esc(x.solar_year) + '</span>' +
        '<div class="flex-1 h-1.5 theme-card-sub rounded-full overflow-hidden" title="' +
          esc(x.month_id + ' ' + x.flow_pillar) + '"><div class="' + col +
          ' h-1.5 rounded-full" style="width:' + w.toFixed(1) + '%"></div></div>' +
        '<span class="w-16 text-right tabular-nums ' + (v > 0 ? 'c-up' : v < 0 ? 'c-down' : 'c-flat') +
          '">' + ret(v) + '</span></div>';
    }).join('');
    const metric = (label, val, cls) => '<div><div class="text-[10px] text-slate-400">' + label +
      '</div><div class="text-xs font-bold font-mono mt-0.5 ' + (cls || 'text-slate-200') + '">' + val + '</div></div>';
    const stabMetrics = !hasStab ? '' : (isDown
      ? metric('最轻一次', ret(-row.stab_margin), 'c-down') +
        metric('最惨一次占跌幅', pct(row.stab_extreme_share), 'text-slate-200') +
        metric('同柱振幅', pct(row.stab_range), 'text-slate-200') +
        metric('剔除最惨一次均值', ret(row.stab_mean_ex), row.stab_mean_ex > 0 ? 'c-up' : 'c-down')
      : metric('最差一次', ret(row.stab_margin), row.stab_margin > 0 ? 'c-up' : 'c-down') +
        metric('最好一次占比', pct(row.stab_extreme_share), 'text-slate-200') +
        metric('同柱振幅', pct(row.stab_range), 'text-slate-200') +
        metric('剔除最好一次均值', ret(row.stab_mean_ex), row.stab_mean_ex > 0 ? 'c-up' : 'c-down'));
    const stock = stockStats && stockStats.n ? stockStats : null;
    const kindCls = !stock ? '' : stock.kind === '暴涨暴跌型' ? 'badge-stab-bad'
      : stock.kind === '高波动型' ? 'badge-stab-mid' : 'badge-flat';
    const stockCard = !stock
      ? '<div class="rounded-xl theme-card border border-slate-700/60 p-3 mt-2 text-[11px] text-slate-400">' +
        '个股整体波动数据不可用（接口未返回）。只影响这张参考卡，不影响稳健度与筛选。</div>'
      : '<div class="rounded-xl theme-card border border-slate-700/60 p-3 mt-2">' +
        '<div class="flex items-center justify-between gap-2 mb-2">' +
          '<div class="text-xs font-bold text-slate-200">个股整体波动 · 全历史 ' + stock.n + ' 个月</div>' +
          '<span class="px-2 py-0.5 rounded text-[10px] ' + kindCls + ' font-bold">' + esc(stock.kind) + '</span>' +
        '</div>' +
        '<div class="grid grid-cols-4 gap-2 text-center text-xs">' +
          metric('最大单月', ret(stock.max_ret), 'c-up') +
          metric('最小单月', ret(stock.min_ret), 'c-down') +
          metric('月波动', pct(stock.std), 'text-slate-200') +
          metric('单月≤-40%', stock.n_dn40 + ' 次', 'text-slate-200') +
        '</div>' +
        '<p class="text-[11px] text-slate-400 mt-2 leading-relaxed">' +
          '最大：' + (stock.top_up || []).map((x) => esc(x.month_id + ' ' + x.flow_pillar) + ' ' + ret(x.body_return)).join('；') +
          '<br>最小：' + (stock.top_down || []).map((x) => esc(x.month_id + ' ' + x.flow_pillar) + ' ' + ret(x.body_return)).join('；') +
        '</p>' +
        '<p class="text-[10px] text-slate-500 mt-2 leading-relaxed">只作风险参考，不参与稳健分/筛选；' +
          '它不会让同柱下一次更可能上涨，主要影响回撤。</p>' +
      '</div>';
    const consCard = (() => {
      const k = row.cons_n || 0;
      const total = row.cons_total || row.stab_n || 0;
      if (!k || k < 2 || row.cons_level === '—') {
        return '<div class="rounded-xl theme-card border border-slate-700/60 p-3 mt-2">' +
          '<div class="text-xs font-bold text-slate-200">历史幅度一致性</div>' +
          '<p class="text-[11px] text-slate-400 mt-1">方向样本不足（k=' + k + '/' + total + '），无法计算 CV。</p>' +
          '</div>';
      }
      const cvTxt = row.cons_cv == null ? '—' : Number(row.cons_cv).toFixed(2);
      const minTxt = row.cons_min == null ? '—' : pct(row.cons_min);
      const worstTxt = row.cons_worst_all == null ? '—' : ret(row.cons_worst_all);
      const dropTxt = row.cons_drop == null ? '—' : pct(row.cons_drop);
      const floorTxt = Number(row.cons_floor) === 1 ? '达标' : '不足';
      const rev = row.cons_reverse_n || 0;
      return '<div class="rounded-xl theme-card border border-slate-700/60 p-3 mt-2">' +
        '<div class="flex items-center justify-between gap-2 mb-2">' +
          '<div class="text-xs font-bold text-slate-200">历史幅度一致性 · 方向样本 ' + k + '/' + total + '</div>' +
          '<span class="px-2 py-0.5 rounded text-[10px] ' + oppConsCls(row.cons_level) + ' font-bold">' + esc(row.cons_level || '—') + '</span>' +
        '</div>' +
        '<div class="grid grid-cols-4 gap-2 text-center">' +
          metric('方向CV', cvTxt, 'text-slate-200') +
          metric(isDown ? '最小跌幅' : '最小涨幅', minTxt, 'c-up') +
          metric('反向/非方向', rev + ' 次', rev ? 'c-down' : 'text-slate-200') +
          metric('全月最差', worstTxt, 'c-down') +
        '</div>' +
        '<div class="grid grid-cols-2 gap-2 text-center mt-2">' +
          metric('幅度底线≥1%', floorTxt, Number(row.cons_floor) === 1 ? 'c-up' : 'c-down') +
          metric('单次影响 drop', dropTxt, 'text-slate-200') +
        '</div>' +
        '<p class="text-[11px] text-slate-400 mt-2 leading-relaxed">' + esc(row.consistency_reason || '') + '</p>' +
        '<p class="text-[10px] text-slate-500 mt-2 leading-relaxed">只看' + (isDown ? '下跌' : '上涨') + '月份；' +
          '反向月份不计入 CV，但必须看次数与全月最差；这不是下一次上涨概率。</p>' +
      '</div>';
    })();
    stabBox.innerHTML =
      '<div class="rounded-xl theme-card border border-slate-700/60 p-3">' +
        '<div class="flex items-center justify-between gap-2 mb-2">' +
          '<div class="text-xs font-bold text-slate-200">' + (isDown ? '同柱历史跌幅差异检查' : '同柱稳定性检查') +
            ' · <span class="text-indigo-400 font-mono">' + esc(row.flow_pillar || '') + '</span></div>' +
          '<span class="px-2 py-0.5 rounded text-[10px] ' + oppStabCls(lv) + ' font-bold">' +
            esc(row.stability_level_cn || '—') + ' · ' + (row.stab_n || 0) + ' 次' + (scoreTxt ? ' · ' + scoreTxt : '') +
          '</span>' +
        '</div>' +
        (hasStab ? '<div class="grid grid-cols-4 gap-2 text-center">' + stabMetrics + '</div>' : '') +
        '<p class="text-[11px] text-slate-400 mt-2 leading-relaxed">' + esc(row.stability_reason || '') + '</p>' +
        (bars ? '<div class="mt-2 space-y-1">' + bars + '</div>' : '') +
      '</div>' + consCard + stockCard;
  }

  // 所选月说明：原先只在表格下方详情区出现，现在并到抽屉里
  const note = $('#drawer-month-note');
  if (note && row._layer) note.innerHTML = '';
  else if (note) {
    const hasData = m.market_rows_in_month > 0;
    note.innerHTML = !m.month_id ? '' : '所选月：' + esc(m.label || '') + ' ' + esc(date10(m.start_at)) + '→' +
      esc(date10(m.end_at)) + (hasData
        ? '，行情已在库内（该月' + esc((S.meta && S.meta.market_label) || '全市场') + ' ' +
          (m.market_rows_in_month || 0).toLocaleString('en-US') + ' 行）。标了「◀ 所选月」的那一行才是本页所选月份的走势，其余各月与本页无关。'
        : '，尚无行情行（库里最后有数据流月 ' + esc((S.opp && S.opp.latest_month_with_data) || '—') +
          '）。以下每一行都是历史月份，不是所选月的实际走势。');
  }

  // Table：月层带「正式事件 / 候选事件」两列；旬/段层窗口行没有这两项
  const histCols = (withEvents) => [
    ['month_id', '月ID', (r) => '<span class="font-mono text-indigo-400 font-semibold underline whitespace-nowrap cursor-pointer">' + esc(r.month_id) + '</span>' +
      (r.month_id === m.month_id ? '<span class="ml-1 text-[10px] px-1 py-0.2 rounded bg-amber-500/10 text-amber-500 border border-amber-500/30 whitespace-nowrap">◀ 所选月</span>' : '')],
    ['solar_year', '年份', (r) => '<span class="font-mono font-medium whitespace-nowrap">' + esc(r.solar_year) + '</span>'],
    ['start_term', '节气', (r) => '<span class="text-slate-400 whitespace-nowrap">' + esc(r.start_term) + '</span>'],
    ['first_date', '区间', (r) => '<span class="font-mono text-[10px] text-slate-400 whitespace-nowrap">' +
      (r.first_date ? esc(r.first_date + ' ~ ' + r.last_date)
        : r.first_ts ? esc(date10(r.first_ts) + ' ~ ' + date10(r.last_ts)) : '—') + '</span>'],
    ['trading_days', '交易日', (r) => '<span class="tabular-num">' +
      esc(r.trading_days != null ? r.trading_days : r.valid_bars != null ? r.valid_bars : '—') + '</span>'],
    ['direction', '涨跌', (r) => '<span class="px-2 py-0.5 rounded text-[10px] ' + dirBadge(r.direction) + ' font-bold whitespace-nowrap">' + dirTxt(r.direction) + '</span>'],
    ['body_return', '涨跌幅', (r) => '<span class="tabular-num font-bold ' + (r.body_return > 0 ? 'c-up' : r.body_return < 0 ? 'c-down' : 'c-flat') + ' whitespace-nowrap">' + ret(r.body_return) + '</span>'],
    ['stem_ten_god', '干十神', (r) => '<span class="text-amber-500 whitespace-nowrap">' + esc(r.stem_ten_god || '—') + '</span>'],
    ['hidden_stem_summary', '藏干十神', (r) => '<span class="text-[10px] text-slate-400 whitespace-nowrap">' + esc(r.hidden_stem_summary || '—') + '</span>'],
  ].concat(withEvents ? [
    ['confirmed_event_count', '正式事件'],
    ['candidate_structure_count', '候选事件'],
  ] : []).concat([
    ['excess_return', '超额', (r) => '<span class="tabular-num font-bold ' + (r.excess_return > 0 ? 'c-up' : r.excess_return < 0 ? 'c-down' : 'c-flat') + ' whitespace-nowrap">' + ret(r.excess_return) + '</span>'],
    ['research_sample_status', '样本', (r) => sampleTag(r.research_sample_status)],
  ]);
  const histTable = (list, withEvents) => {
    const w = el('div', 'overflow-x-auto');
    w.appendChild(table(histCols(withEvents), list, (r) => showMonth(r.month_id, row.identity_code), { cls: 'dense' }));
    return w;
  };
  const dbox = $('#drawer-table-box');
  if (dbox) {
    dbox.innerHTML = '';
    if (!rows.length) {
      dbox.innerHTML = '<div class="p-6 text-center text-xs text-slate-400">零样本：该柱在本身份正式历史中未出现</div>';
    } else {
      dbox.appendChild(histTable(rows, !winLayer));
    }
  }
  const mbox = $('#drawer-month-box');
  if (mbox) {
    mbox.innerHTML = '';
    if (monthRows && monthRows.length) {
      const det = el('details', 'rounded-xl theme-card border border-slate-700/60 overflow-hidden');
      det.appendChild(el('summary', 'px-3 py-2 text-xs font-bold text-slate-200 cursor-pointer',
        '同柱整月历次明细（月层 · ' + monthRows.length + ' 次）'));
      det.appendChild(histTable(monthRows, true));
      mbox.appendChild(det);
    }
  }

  await renderDrawerWindowSection(row.identity_code, row.flow_pillar);
  renderDrawerPathSection(row, drawLayer, winLayer ? drawWin : null,
    row._layer ? null : (m.month_id || null));

  drawer.classList.remove('hidden');
}

async function showOppDetail(row) {
  const box = $('#oppdetail'); box.innerHTML = '';
  const m = (S.opp && S.opp.months && S.opp.months.find((x) => x.pillar === row.flow_pillar)) || {};
  const my = seqStart('oppdetail');
  const ep = S.epoch;
  const rows = await get('/api/pillar_detail', { id: row.identity_code,
                                                 pillar: row.flow_pillar });
  if (seqStale('oppdetail', my) || ep !== S.epoch) return;
  const hasData = m.market_rows_in_month > 0;
  box.appendChild(el('h3', null, esc(row.identity_code) + ' ' + esc(row.name || '') + ' × ' +
    esc(row.flow_pillar) + ' · 历史出现 ' + rows.length + ' 次 · ' +
    esc(m.label || '') + ' ' + esc(date10(m.start_at)) + '→' + esc(date10(m.end_at)) +
    (hasData ? ' 的行情已在库内（该月' + ((S.meta && S.meta.market_label) || '全市场') +
      ' ' + (m.market_rows_in_month || 0).toLocaleString('en-US') + ' 行）'
      : ' 尚无行情行（库里最后有数据流月 ' +
        esc(S.opp.latest_month_with_data || '—') + '）')));
  box.appendChild(el('div', 'years', '<b>' + esc(S.opp.rel_head || '') + '</b> ' +
    oppRelCell(row) + '<div class="small">历次出现 ' +
    esc((row.occurrence_years || '').split(';').join(' / ') || '无') +
    (hasData ? ' · 标了「◀ 所选月」的那一行才是本页所选月份的走势，其余各月与本页无关。'
             : ' · 以下每一行都是历史月份，不是所选月的实际走势。') + '</div>'));
  if (row.stability_level && row.stability_level !== 'none') {
    box.appendChild(el('div', 'years', '<b>' + (oppDir() === 'down' ? '历史跌幅差异' : '稳健度') +
      '</b> <span class="px-2 py-0.5 rounded text-[10px] font-bold ' + oppStabCls(row.stability_level) +
      '">' + esc(row.stability_level_cn || '') + ' · ' + (row.stab_n || 0) + ' 次</span> ' +
      esc(row.stability_reason || '') +
      '<div class="small">同柱振幅 ' + pct(row.stab_range) + ' · 最差一次 ' +
      ret(row.stab_margin) + ' · ' + (oppDir() === 'down' ? '稳健分 ' : '稳健分 ') +
      (row.stability_score == null ? '—' : Number(row.stability_score).toFixed(1)) + '</div>'));
  }
  if (row.cons_level) {
    const k = row.cons_n || 0;
    const total = row.cons_total || row.stab_n || 0;
    const lv = row.cons_level || '—';
    box.appendChild(el('div', 'years', '<b>历史幅度一致性</b> ' +
      '<span class="px-2 py-0.5 rounded text-[10px] font-bold ' + oppConsCls(lv) + '">' +
      esc(lv) + ' · ' + k + '/' + total + '</span> ' +
      esc(row.consistency_reason || '') +
      '<div class="small">方向CV ' + (row.cons_cv == null ? '—' : Number(row.cons_cv).toFixed(2)) +
      ' · ' + (Number(row.cons_floor) === 1 ? '幅度达标' : '幅度不足') +
      ' · drop ' + (row.cons_drop == null ? '—' : pct(row.cons_drop)) + '（仅展示）</div>'));
  }
  if (!rows.length) {
    box.appendChild(el('p', 'noprof', '零样本：该柱在本身份正式历史中未出现，关系不可由历史推得。'));
  } else {
    box.appendChild(table([
      ['month_id', '月ID', (r) => '<span class="mono">' + esc(r.month_id) + '</span>' +
        (r.month_id === m.month_id ? '<span class="tag">◀ 所选月</span>' : '')],
      ['solar_year', '年份'], ['start_term', '节气'],
      ['first_date', '区间', (r) => esc(r.first_date + ' ~ ' + r.last_date)],
      ['trading_days', '交易日'],
      ['direction', '涨跌', (r) => '<span class="pill ' + dirCls(r.direction) + '">' +
        dirTxt(r.direction) + '</span>'],
      ['body_return', '涨跌幅', (r) => '<span class="' + dirCls(r.direction) + '">' +
        ret(r.body_return) + '</span>'],
      ['stem_ten_god', '流月干十神'],
      ['confirmed_event_count', '正式事件'],
      ['candidate_structure_count', '候选事件'],
      ['excess_return', '超额', (r) => ret(r.excess_return)],
      ['research_sample_status', '样本', (r) => sampleTag(r.research_sample_status)],
    ], rows, (r) => showMonth(r.month_id, row.identity_code), { cls: 'dense' }));
  }
  const bar = el('div', 'bar');
  const who = S.dataset === 'etf' ? ' ETF ' : '票';
  const b1 = el('button', 'ghost', '把该' + who + '设为头部查询对象');
  b1.onclick = () => openStockFromBoard(row.identity_code, row.security_code, 'stock');
  const b2 = el('button', 'ghost', '在该' + who + '的页面三查看此柱');
  b2.onclick = () => openStockFromBoard(row.identity_code, row.security_code, 'pillar', row.flow_pillar);
  bar.appendChild(b1); bar.appendChild(b2);
  bar.appendChild(el('span', 'note', '点任一历史行可展开该月逐条关系明细。'));
  box.appendChild(bar);
}

/* ---------------- month drill-down modal ---------------- */
async function showMonth(monthId, identity) {
  const m = await get('/api/month_detail', { id: identity || S.id, month: monthId });
  const box = $('#mbody'); box.innerHTML = '';
  $('#modal').classList.remove('hidden');
  const off = m.events.filter((e) => e.record_kind === 'OFFICIAL');
  const cand = m.events.filter((e) => e.record_kind === 'CANDIDATE');
  box.appendChild(el('h2', null, m.security_code + '　' + monthId + '　' + m.flow_pillar + '月'));
  box.appendChild(statTable([
    ['节气起月', esc(m.start_term)], ['区间', esc(m.first_date + ' ~ ' + m.last_date)],
    ['交易日', m.trading_days],
    ['涨跌', '<span class="pill ' + dirCls(m.direction) + '">' + dirTxt(m.direction) + ' ' +
      ret(m.body_return) + '</span>'],
    ['不复权开/收(行情证据)', '<span class="mono">' + esc(m.raw_open + ' → ' + m.raw_close) +
      '</span>'],
    ['复权因子首/末', '<span class="mono">' + esc(m.factor_first + ' / ' + m.factor_last) + '</span>'],
    ['前复权开/收', '<span class="mono">' + esc((m.qfq_open || '') + ' → ' + (m.qfq_close || '')) +
      '</span>（读取时虚拟计算，只作展示，不参与任何统计）'],
    ['收益口径', esc(m.endpoint_basis) + '　' + esc(m.return_decimal || '')],
    ['单交易日月 / 上市首月 / 退市末月', m.single_day + ' / ' + m.partial_listing + ' / ' +
      m.partial_delisting],
    ['质量标记', '<span class="mono small">' + esc(m.quality_flags || '') + '</span>'],
    ['样本状态', sampleTag(m.research_sample_status)],
    ['流月干十神', esc(m.stem_ten_god || '—')],
    ['流月支藏干十神', m.hidden_stem_summary ? esc(m.hidden_stem_summary) : '—'],
    ['藏干明细（源顺序）', (m.flow_hidden_stem_ten_gods || []).length
      ? (m.flow_hidden_stem_ten_gods || []).map((h) => esc(h.role) + ' ' + esc(h.stem) + '（' +
          esc(h.element) + '）→ ' + esc(h.ten_god)).join('；')
      : '<span class="text-amber-500">无档案，不推测</span>'],
    ['同期基准 ' + esc(m.benchmark_code || '—'), '涨跌 ' + ret(m.benchmark_return) +
      '　超额 ' + ret(m.excess_return) + '　' + esc(m.comparison_status || '')],
    ['规则版本', '<span class="mono small">' + esc(m.relation_rule_version) + '</span>'],
    ['行哈希 row_sha256', '<span class="mono small">' + esc(m.row_sha256 || '') + '</span>'],
  ]));
  box.appendChild(el('h4', null, '正式关系（' + off.length + ' 条，按柱位逐条）'));
  box.appendChild(relationList(off));
  box.appendChild(el('h4', null, '候选结构（' + cand.length +
    ' 条，不计入正式关系次数）'));
  box.appendChild(relationList(cand, true));
}

function relationList(rows, isCand) {
  if (!rows.length) return el('p', 'dim', '（无）');
  return table([
    ['relation_type', '关系'], ['family', '族'], ['natal_position', '本命柱位', posTxt],
    ['flow_stem', '流月干'], ['flow_branch', '流月支'],
    ['flow_element', '流月五行'], ['natal_element', '本命五行'],
    ['svu', 'S/V/U'], ['origin_type', 'origin'],
    ['structure_combo', '结构'], ['present_branches', '已有支'],
    ['missing_branches', '缺支'], ['partial_kind', 'partial'],
    ['candidate_transformation_element', '候选合化'],
    ['transformation_established', '合化成立', (r) => r.transformation_established === null
      ? '<span class="warn">未知</span>' : (r.transformation_established ? '是' : '否')],
    ['establishment_rule_id', '规则ID'],
    ['note', '备注', (r) => '<span class="small">' + esc(r.note || '') + '</span>'],
  ], rows, null, { cls: 'dense tiny' });
}

/* ---------------- meta tab ---------------- */
async function renderMeta() {
  const box = $('#metabody'); box.innerHTML = '';
  const ep = S.epoch;
  const [m, src, cat, mkt] = await Promise.all([get('/api/meta'), get('/api/sources'),
    get('/api/catalog'), get('/api/market_relation_types')]);
  if (ep !== S.epoch) return;
  box.appendChild(el('h3', null, '本地数据库规模'));
  box.appendChild(statTable(Object.entries({
    '当前数据集': m.dataset_label,
    '证券身份': m.securities, '有命盘': m.with_profile,
    '无命盘': m.without_profile, '历史流月(monthly_facts)': m.monthly_facts,
    '无争议可计算': m.undisputed, '交节争议': m.disputed, '缺档案阻塞': m.blocked,
    '关系明细行(monthly_relations)': m.monthly_relations,
    '其中正式事件': m.official_events, '其中候选结构': m.candidate_events,
    '60柱统计行': m.stats_pillar60, '12支统计行': m.stats_branch12,
    '关系类型统计行': m.stats_relation_types,
    '关系×流月柱统计行': m.stats_relation_pillar,
    '关系规则版本': (m.rule_versions || []).join(' / '),
    '十神规则版本': m.ten_god_rule_version || '—',
    '藏干规则版本': m.ten_god_hidden_rule_version || '—',
    '十神映射校验': m.ten_god_rule_source
      ? '100 格冻结映射 · ' + m.ten_god_rule_source.reference_mismatches + ' 处不一致 · 无权重'
      : '—',
    '统计运行ID': m.stats_run ? m.stats_run.run_id : (m.dataset === 'etf' ? '未生成' : '冻结表'),
    '统计生成时间': m.stats_run ? m.stats_run.built_at_utc : '—',
  }).map(([k, v]) => [k, typeof v === 'number' ? v.toLocaleString('en-US') : v])));
  box.appendChild(el('h3', null, '来源与版本（全部只读引用，未修改）'));
  box.appendChild(table([['kind', '类别'], ['name', '名称'],
    ['version', '版本'], ['rows', '行数'],
    ['sha256', 'SHA256', (r) => '<span class="mono small">' + esc(r.sha256 || '') + '</span>'],
    ['note', '说明', (r) => '<span class="small">' + esc(r.note) + '</span>'],
    ['path', '路径', (r) => '<span class="mono small">' + esc(r.path) + '</span>']], src,
    null, { cls: 'dense tiny' }));
  box.appendChild(el('h3', null, '关系目录（22 类正式 + 候选，沿用 V1.3，未增删）'));
  box.appendChild(table([['ordinal', '序'], ['relation_type', '类型'], ['group_name', '族'],
    ['kind', '层级'], ['rule_version', '规则版本']], cat, null, { cls: 'dense' }));
  box.appendChild(el('h3', null, m.market_stats_note ||
    '全市场 22 类关系基准（冻结 CSV 原样导入，用于对账）'));
  box.appendChild(table([['category_id', 'ID'], ['item', '项目'], ['item_kind', '层级'],
    ['months_dedup', '月份(去重)'], ['identities', '身份数'],
    ['confirmed_events', '事件数'], ['ALL_n', '无争议样本n'], ['ALL_up', '涨'],
    ['ALL_down', '跌'], ['ALL_flat', '平'],
    ['ALL_up_rate', '上涨占比', (r) => pct(r.ALL_up_rate)],
    ['deviation_from_baseline_up_rate', '偏离基线', (r) => r.deviation_from_baseline_up_rate === null
      ? '—' : (r.deviation_from_baseline_up_rate * 100).toFixed(2) + 'pp'],
    ['counterexample_months', '反例月'], ['majority_direction', '多数方向']], mkt,
    null, { cls: 'dense tiny' }));
  box.appendChild(el('p', 'hint', '合规提示：全部数字为<b>历史描述性统计</b>，样本高度重叠' +
    '（同一节气月被' + (S.dataset === 'etf'
      ? (S.meta.securities || 0).toLocaleString('en-US') + ' 只 ETF'
      : '数千只票') + '共享），不构成概率、不构成吉凶评级、不构成买卖建议。'));
}

/* ---------------- page 5: 十神历史涨跌（流月天干十神） ---------------- */
function tgModeTxt() {
  const n = (S.stock && S.stock.month_totals && S.stock.month_totals.single_day) || 0;
  if (S.tgMode === 'EXCLUDE_SINGLE') {
    return n === 0 ? '不含单交易日月（本标的单交易日月为 0，两种口径一致）'
      : '不含单交易日月（已排除 ' + n + ' 个）';
  }
  return '含单交易日月' + (n > 0 ? '（其中 ' + n + ' 个）' : '');
}

function tgFilterBits() {
  const bits = ['口径 ' + tgModeTxt()];
  if (S.tgBranch) bits.push('流月地支 ' + S.tgBranch);
  if (S.tgRel) bits.push('关系 ' + S.tgRel);
  return bits;
}

async function renderTenGodStats() {
  const box = $('#tengodtable'), sum = $('#tengodsummary'), det = $('#tengoddetail');
  if (!box || !sum || !det) return;
  box.innerHTML = ''; sum.innerHTML = ''; det.innerHTML = '';
  if (!S.id) return;
  const my = seqStart('tengod');
  const ep = S.epoch;
  const params = { id: S.id, sample_mode: S.tgMode };
  if (S.tgBranch) params.flow_branch = S.tgBranch;
  if (S.tgRel) params.relation_type = S.tgRel;
  const d = await get('/api/ten_god_stats', params);
  if (seqStale('tengod', my) || ep !== S.epoch) return;
  S.tgData = d;
  $('#tg-note').innerHTML = tgFilterBits().join(' · ') +
    ' · 规则 ' + esc(d.rule_version || '');

  if (d.status !== 'OK') {
    sum.appendChild(el('div',
      'rounded-xl border border-amber-500/30 bg-amber-500/10 text-amber-500 text-xs p-3',
      '暂无十神数据：该身份缺本命档案（或日主非法）。按规则不猜测十神，也不把这些月份算进任何一类。' +
      '（同筛选下有效月事实 ' + (d.accounting ? d.accounting.total_rows : 0) + ' 条，全部标记为缺档案）'));
    return;
  }

  const a = d.accounting || {};
  const card = (label, value, sub, cls) =>
    '<div class="p-3 rounded-xl theme-card border">' +
      '<div class="text-[11px] text-slate-400">' + label + '</div>' +
      '<div class="text-lg font-bold tabular-num mt-0.5 ' + (cls || '') + '">' + value + '</div>' +
      (sub ? '<div class="text-[10px] text-slate-500 mt-0.5">' + sub + '</div>' : '') +
    '</div>';
  sum.appendChild(el('div', 'grid grid-cols-2 lg:grid-cols-4 gap-3',
    card('本命日主（唯一基准）', esc(d.day_master || '—'), esc(d.day_master || '') + ' · 十神基准') +
    card('同筛选有效月事实', (a.total_rows || 0).toLocaleString('en-US'), 'monthly_facts 唯一键计数') +
    card('已归类到 10 类', (a.classified_n || 0).toLocaleString('en-US'),
      '合计应与左侧相等', (a.classified_n === a.total_rows) ? 'c-up' : 'c-down') +
    card('缺档案未归类', (a.missing_profile || 0).toLocaleString('en-US'),
      '单独报告，不设第 11 类', (a.missing_profile ? 'text-amber-500' : 'text-slate-400'))));
  sum.appendChild(el('p', 'text-[11px] text-slate-500 mt-2',
    '口径：' + esc(d.denominator_note || '') + ' 规则版本 ' + esc(d.rule_version || '') +
    '；藏干规则 ' + esc(d.hidden_rule_version || '') + '。'));

  box.appendChild(table([
    ['ten_god', '流月天干十神', (r) => '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 whitespace-nowrap">' + esc(r.ten_god) + '</span>'],
    ['n', '有效月数', (r) => '<span class="tabular-num font-bold">' + r.n + '</span>'],
    ['up', '涨', (r) => '<span class="tabular-num c-up font-bold">' + r.up + '</span>'],
    ['down', '跌', (r) => '<span class="tabular-num c-down font-bold">' + r.down + '</span>'],
    ['flat', '平', (r) => '<span class="tabular-num text-slate-400">' + r.flat + '</span>'],
    ['up_rate', '上涨占比', (r) => {
      if (r.up_rate === null || r.up_rate === undefined) return '<span class="dim">—</span>';
      const w = Math.min(Math.max(r.up_rate * 100, 0), 100).toFixed(1);
      return '<div class="flex items-center justify-center gap-2 whitespace-nowrap"><span class="tabular-num font-bold c-up w-10 text-right">' + pct(r.up_rate) + '</span>' +
        '<div class="w-14 theme-card-sub rounded-full h-1.5 overflow-hidden border border-slate-700/40"><div class="bg-red-500 h-1.5 rounded-full" style="width:' + w + '%"></div></div></div>';
    }],
    ['mean_return', '平均涨跌', (r) => '<span class="tabular-num font-bold ' + (r.mean_return > 0 ? 'c-up' : r.mean_return < 0 ? 'c-down' : 'c-flat') + '">' + ret(r.mean_return) + '</span>' +
      (r.return_n < r.n ? '<span class="text-[10px] text-slate-500 ml-1">n=' + r.return_n + '</span>' : '')],
    ['index_comparison', '同期指数均值', (r) => r.index_comparison
      ? '<span class="tabular-num">' + ret(r.index_comparison.benchmark_mean_return) + '</span>'
      : '<span class="text-slate-500" title="' + esc(r.index_comparison_reason || 'NO_MATCHED_BENCHMARK') + '：该组没有可匹配的同期指数样本">N/A</span>'],
    ['excess', '同期超额', (r) => r.index_comparison
      ? '<span class="tabular-num font-bold ' + (r.index_comparison.excess_mean > 0 ? 'c-up' : r.index_comparison.excess_mean < 0 ? 'c-down' : 'c-flat') + '">' + ret(r.index_comparison.excess_mean) + '</span>'
      : '<span class="text-slate-500">N/A</span>'],
    ['outperform', '跑赢/可比', (r) => r.index_comparison
      ? '<span class="tabular-num text-slate-300">' + r.index_comparison.outperform_n + ' / ' + r.index_comparison.benchmark_n + '</span>'
      : '<span class="text-slate-500">N/A</span>'],
    ['single_day_n', '单交易日', (r) => r.single_day_n > 0
      ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-amber-500/20 border border-amber-500/50 text-amber-400 tabular-num" title="本十神包含 ' + r.single_day_n + ' 个单交易日月">' + r.single_day_n + '</span>'
      : '<span class="tabular-num text-slate-500">0</span>'],
    ['stability_note', '样本提示', (r) => r.stability_note
      ? '<span class="text-[10px] text-amber-500">' + esc(r.stability_note) + '</span>'
      : '<span class="text-[10px] text-slate-500">—</span>'],
  ], d.groups, (r, tr) => {
    S.tgGod = r.ten_god;
    document.querySelectorAll('#tengodtable tbody tr.sel').forEach((x) => x.classList.remove('sel'));
    tr.classList.add('sel');
    showTenGodMonths(r.ten_god);
  }, { cls: 'dense' }));

  // 如果有之前的选中项则保持，否则优先选中存在单交易日受影响的十神，最后回退首个有效项
  const targetGod = (S.tgGod && d.groups.find((g) => g.ten_god === S.tgGod))
    || d.groups.find((g) => (g.single_day_n || 0) > 0)
    || d.groups.find((g) => g.n > 0)
    || d.groups[0];

  if (targetGod) {
    S.tgGod = targetGod.ten_god;
    const trs = document.querySelectorAll('#tengodtable tbody tr');
    d.groups.forEach((g, idx) => {
      if (g.ten_god === targetGod.ten_god && trs[idx]) trs[idx].classList.add('sel');
    });
    await showTenGodMonths(targetGod.ten_god);
  }
}

async function showTenGodMonths(god) {
  const box = $('#tengoddetail');
  if (!box) return;
  box.innerHTML = '';
  const my = seqStart('tgm');
  const ep = S.epoch;
  const params = { id: S.id, ten_god: god, sample_mode: S.tgMode };
  if (S.tgBranch) params.flow_branch = S.tgBranch;
  if (S.tgRel) params.relation_type = S.tgRel;
  const d = await get('/api/ten_god_months', params);
  if (seqStale('tgm', my) || ep !== S.epoch) return;
  box.appendChild(el('h3', null, '流月天干十神「' + esc(god) + '」逐次历史明细 · ' +
    d.n + ' 个月 · ' + tgFilterBits().join(' · ')));
  if (!d.rows.length) {
    box.appendChild(el('p', 'dim', '该十神在同筛选下没有历史月份（可能是零样本，或身份缺本命档案）。'));
    return;
  }
  box.appendChild(table([
    ['month_id', '月ID', (r) => '<span class="mono">' + esc(r.month_id) + '</span>'],
    ['solar_year', '年份'],
    ['flow_pillar', '流月柱', (r) => '<span class="font-mono font-bold">' + esc(r.flow_pillar) + '</span>'],
    ['start_term', '节气'],
    ['first_date', '区间', (r) => '<span class="text-[11px]">' + esc(r.first_date + ' ~ ' + r.last_date) + '</span>'],
    ['trading_days', '交易日'],
    ['direction', '涨跌', (r) => '<span class="pill ' + dirCls(r.direction) + '">' + dirTxt(r.direction) + '</span>'],
    ['body_return', '涨跌幅', (r) => '<span class="tabular-num font-bold ' + (r.body_return > 0 ? 'c-up' : r.body_return < 0 ? 'c-down' : 'c-flat') + '">' + ret(r.body_return) + '</span>'],
    ['benchmark_return', '同期指数', (r) => r.comparison_status === 'MATCHED'
      ? '<span class="tabular-num">' + ret(r.benchmark_return) + '</span>'
      : '<span class="text-slate-500" title="' + esc(r.comparison_status || '') + '">N/A</span>'],
    ['excess_return', '超额', (r) => r.comparison_status === 'MATCHED'
      ? '<span class="tabular-num font-bold ' + (r.excess_return > 0 ? 'c-up' : r.excess_return < 0 ? 'c-down' : 'c-flat') + '">' + ret(r.excess_return) + '</span>'
      : '<span class="text-slate-500">N/A</span>'],
    ['official_types', '命中关系', (r) => chipText(r.official_types)],
    ['hidden_stem_summary', '藏干十神', (r) => '<span class="text-[10px] text-slate-400 whitespace-nowrap">' + esc(r.hidden_stem_summary || '—') + '</span>'],
    ['single_day', '单交易日', (r) => r.single_day ? '<span class="tag warn">是</span>' : '<span class="text-slate-500">否</span>'],
  ], d.rows, (r) => showMonth(r.month_id), { cls: 'dense' }));
  box.appendChild(el('p', 'hint', '点击任意一行可展开该月完整关系明细。本表直接读取冻结 monthly_facts，' +
    '不重算月线、不改复权口径；未来月不会出现在这里，因为没有历史行情。'));
}

/* ---------------- 分段控制器 (Segmented Control) 统一高亮同步 ---------------- */
function syncSegUi(container, activeVal) {
  const c = typeof container === 'string' ? $(container) : container;
  if (!c) return;
  const onCls = 'on px-3 py-1 rounded-md font-semibold bg-indigo-600 text-white shadow-sm transition';
  const offCls = 'px-3 py-1 rounded-md font-medium text-slate-400 hover:text-indigo-400 transition';
  c.querySelectorAll('button').forEach((b) => {
    const isAct = b.dataset.v === activeVal;
    b.className = isAct ? onCls : offCls;
    b.classList.toggle('on', isAct);
  });
}

/* ---------------- dataset switch ---------------- */
function resetStockFilters() {
  $('#q').value = '';
  $('#suggest').innerHTML = ''; $('#suggest').classList.add('hidden');
  $('#b-pillar').value = ''; $('#b-stem-el').value = ''; $('#b-branch-el').value = '';
  $('#b-sort').value = 'branch'; $('#b-min').value = '';
  $('#b-exclude-single').checked = false; S.branchPolicy = 'ALL';
  $('#p-search').value = ''; $('#p-stem-el').value = ''; $('#p-branch-el').value = '';
  $('#p-sort').value = 'pillar'; $('#p-min').value = '';
  if ($('#p-tg')) $('#p-tg').value = '';
  ['#f-clash', '#f-harmony', '#f-both', '#f-punish', '#f-harm', '#f-broken']
    .forEach((i) => $(i).checked = false);
  S.relKind = 'OFFICIAL';
  syncSegUi('#r-kind', 'OFFICIAL');
  S.selBranch = null; S.relType = null;
  S.tgMode = 'ALL'; S.tgBranch = ''; S.tgRel = '';
  syncSegUi('#tg-mode', 'ALL');
  if ($('#tg-branch')) $('#tg-branch').value = '';
  if ($('#tg-rel')) $('#tg-rel').value = '';
}

function applyMeta(meta) {
  S.meta = meta;
  const ds = meta.dataset || S.dataset || 'ashare';
  const marketLabel = meta.market_label || (ds === 'etf' ? '全 ETF' : '全市场');
  const appTitle = meta.app_title || (ds === 'etf'
    ? '全ETF 八字冲合关系 × 历史涨跌 本地数据库'
    : '全A股 八字冲合关系 × 历史涨跌 本地数据库');
  const statsReady = meta.stats_ready !== false;
  document.body.dataset.dataset = ds;
  document.title = ds === 'etf' ? appTitle + ' · V1'
    : '八字流月冲合与历史涨跌查询 · V1';
  // 顶栏只放短名，完整名放在悬停里
  $('#app-title').textContent = ds === 'etf' ? '八字流月 · 历史规律（ETF）' : '八字流月 · 历史规律';
  $('#app-title').title = appTitle;
  $('#q').placeholder = meta.search_placeholder ||
    (ds === 'etf' ? '输入 ETF 代码或名称，如 510050.SH / 上证50ETF'
                  : '输入股票代码或名称，如 000001.SH / 茅台');
  $('#tab-stock-label').textContent = '档案与来源';
  const backTxt = $('#back-stock-txt');
  if (backTxt) backTxt.textContent = ds === 'etf' ? 'ETF 档案' : '个股档案';
  $('#open-board').title = (ds === 'etf' ? '全 ETF ' : '全市场') +
    '按节气月横向查看：一次只显示一个流月，含该柱历史涨跌统计与该月触发关系；' +
    '往前选历史月时会多出该月实际涨跌两列，可用来回看验证。';
  $('#boardtitle').textContent = '流月机会表 · ' + marketLabel +
    (marketLabel.endsWith('ETF') ? ' ' : '') + '按节气月查看（本页与上方个股无关）';
  $('#ds').querySelectorAll('button').forEach((b) => {
    const isAct = b.dataset.ds === ds;
    b.className = isAct
      ? 'px-2.5 py-1 rounded-md font-semibold bg-indigo-600 text-white shadow-sm transition-all'
      : 'px-2.5 py-1 rounded-md font-medium text-slate-400 hover:text-indigo-400 transition-all';
  });
  $('#o-delist-wrap').classList.toggle('hidden',
    !!(meta.noop_controls && meta.noop_controls.include_delisted.hidden));
  $('#o-idx-wrap').classList.toggle('hidden',
    !!(meta.noop_controls && meta.noop_controls.include_index.hidden));
  $('#o-cap-tier-wrap').classList.toggle('hidden', ds === 'etf');
  const banner = $('#banner');
  if (banner) {
    if (!statsReady) {
      banner.classList.add('err');
      banner.textContent = ds === 'etf'
        ? 'ETF 统计表未就绪：请先运行 bazi_month_relation_etf_v1/scripts/09_build_etf_stats.py'
        : '统计表未就绪：页面二至五会为空';
    } else {
      banner.classList.remove('err');
      banner.textContent = meta.banner || 'V1.3研究规则｜未经命理最终裁定｜历史描述性统计，非预测';
    }
  } else if (!statsReady) {
    let errBanner = $('#stats-err-banner');
    if (!errBanner) {
      errBanner = el('div', 'p-2.5 bg-red-500/20 text-red-300 text-xs text-center border-b border-red-500/40');
      errBanner.id = 'stats-err-banner';
      document.body.prepend(errBanner);
    }
    errBanner.textContent = ds === 'etf'
      ? 'ETF 统计表未就绪：请先运行 bazi_month_relation_etf_v1/scripts/09_build_etf_stats.py'
      : '统计表未就绪：页面二至五会为空';
  }
}

function populateDatasetOptions(pillars, cat) {
  const dl = $('#pillar-options'); dl.innerHTML = '';
  pillars.forEach((p) => {
    const o = el('option'); o.value = p.flow_pillar; dl.appendChild(o);
  });
  const rel = $('#o-rel');
  while (rel.options.length > 1) rel.remove(1);
  cat.filter((c) => c.kind === 'OFFICIAL').forEach((c) => {
    const o = el('option', null, c.relation_type); o.value = c.relation_type;
    rel.appendChild(o);
  });
}

function populateTenGodFilters() {
  const bs = $('#tg-branch');
  if (bs && bs.options.length === 1) {
    Object.keys(BRANCH_TERM).forEach((b) => {
      const o = el('option', null, b + '（' + BRANCH_TERM[b] + '）');
      o.value = b; bs.appendChild(o);
    });
  }
  const rs = $('#tg-rel');
  if (rs) {
    while (rs.options.length > 1) rs.remove(1);
    (S.catalog || []).filter((c) => c.kind === 'OFFICIAL').forEach((c) => {
      const o = el('option', null, c.relation_type);
      o.value = c.relation_type; rs.appendChild(o);
    });
  }
}

async function switchDataset(ds, force, opts = {}) {
  if (!force && S.dataset === ds) return;
  const wasBoard = document.body.classList.contains('board');
  if (!opts.fromInit) saveFiltersNow();
  S.epoch++;
  reqSeq++;
  Object.keys(SEQ).forEach((k) => SEQ[k]++);
  S.dataset = ds;
  S.stock = null; S.id = null; S.opp = null; S.selBranch = null; S.relType = null;
  // 换数据集时同样把旬/段层的「出现次数」下限对齐（A股 ≥6 / ETF ≥3），否则 ETF 会一行都出不来
  if ($('#o-min') && S.oppLayer && S.oppLayer !== 'month') $('#o-min').value = ds === 'etf' ? '3' : '6';
  S.fromBoard = opts.fromBoard !== undefined ? !!opts.fromBoard : false;
  syncReturnBar();
  syncDayunLink();
  $('#modal').classList.add('hidden');
  $('#o-month-pop').classList.add('hidden');
  $('#suggest').classList.add('hidden'); $('#suggest').innerHTML = '';
  ['#stockhead', '#overviewbody', '#stockcard', '#stockbody', '#branchtable', '#branchdetail', '#pillartable',
   '#pillardetail', '#relationtable', '#relationpillars', '#relationmonths',
   '#opptables', '#oppdetail', '#metabody', '#tengodnow', '#tengodsummary',
   '#tengodtable', '#tengoddetail'].forEach((s) => {
    const e = $(s); if (e) e.innerHTML = '';
  });
  resetStockFilters();
  resetBoardFilters();
  const ep = S.epoch;
  const [meta, pillars, cal, cat] = await Promise.all([
    get('/api/meta'), get('/api/pillar_options'), get('/api/calendar_now'),
    get('/api/catalog')]);
  if (ep !== S.epoch) return;
  if (!meta.dataset && ds !== 'ashare') {
    throw new Error('当前服务端版本过旧：请关闭正在运行的查询终端窗口，重新双击启动程序');
  }
  if (meta.dataset && meta.dataset !== ds) {
    throw new Error('服务端数据集与页面选择不一致：请重新启动查询程序');
  }
  applyMeta(meta);
  S.cal = cal; S.catalog = cat;
  populateDatasetOptions(pillars, cat);
  populateTenGodFilters();
  restoreFilters(opts.filters || savedFilters(ds));
  if (!mpSelected()) $('#o-month').value = '0';
  mpSync();
  const targetId = opts.id || meta.default_identity;
  S.id = targetId;
  const board = opts.board !== undefined ? !!opts.board : wasBoard;
  setBoardMode(board, { syncUrl: false, render: false });
  if (board) {
    // 机会表模式：用户正处于全市场看板，立即并发优先渲染机会表，毫秒级出内容！
    const oppPromise = renderOpportunity();
    // 此时后台不阻塞渲染个股数据，用户切回个股时依然秒开
    loadStock(targetId, { syncQ: !!(opts.id || opts.syncQ) }).then(() => {
      const targetTab = MAIN_TABS.has(opts.tab) ? opts.tab : null;
      if (targetTab) activateTab(targetTab, { noUrl: true, force: true });
    }).catch((e) => console.error(e));
    await oppPromise;
  } else {
    await loadStock(targetId, { syncQ: !!(opts.id || opts.syncQ) });
    const targetTab = MAIN_TABS.has(opts.tab) ? opts.tab : null;
    if (targetTab) activateTab(targetTab, { noUrl: true, force: true });
    else {
      const active = document.querySelector('.tabs button.on');
      if (active && active.dataset.tab === 'archive') await renderMeta();
      if (active && active.dataset.tab === 'lowpoint' && window.onV5TabActive) {
        window.onV5TabActive(S.id);
      }
    }
  }
  if (opts.push) pushView({ id: targetId, tab: currentTab(), board, fromBoard: S.fromBoard });
  else if (opts.replace) replaceView({ id: targetId, tab: currentTab(), board, fromBoard: S.fromBoard });
}

/* ---------------- 数据库更新时间 / 手动更新入口 (T3) ----------------
 * 时间口径：后端 /api/db_status 与 /api/db_update/state 的 *_bjt 串一律已带 +08:00，
 * 下面只做字符串截取/拼装，绝不构造日期对象做时区换算（本机浏览器在 America/Los_Angeles，
 * 与北京差 15 小时，换算会把 20:00 显示成凌晨 5 点并跨日——该错误已实测存在）。
 * 显示全部走 style.css 新增的专用类 .db-stamp/.upd-btn/.upd-panel，
 * 不用 hidden md:* 组合：.hidden{display:none!important} 会把它压掉（V1.2.1 老坑）。
 */
/* preview/token/previewing 是「两段式」新增的三个字段：
   点「更新」先打只读 preview，把结论摆出来，用户再点第二次才真的发 start。
   token 是一次性的：start 成功或被拒都清空，下一次必须重新检查。 */
const DBUP = { status: null, state: null, runId: null, timer: null, busy: false,
               terminal: false, sending: false, fetchFailed: false, lastMain: null,
               preview: null, token: null, previewing: false, note: null,
               /* 轮询兜底（P1-2）：连续 POLL_MAX_TICKS 次（5 秒一次 = 20 分钟）还没等到终态，
                  就停轮询、把这一轮标成「状态未知（本轮未回报结束）」并解禁按钮。
                  这是**读数兜底**，不是新状态：终态一律以后端 state/plan 为准。 */
               pollTicks: 0, unresolvedRunId: null };
const POLL_INTERVAL_MS = 5000;
const POLL_MAX_TICKS = 240;          // 240 × 5s = 20 分钟
const UNKNOWN_STATE_LABEL = '状态未知（本轮未回报结束）';

/* 状态未知/从未更新时给的可照做一句话（与页面既有月份口径一致，不新造概念）。 */
const DB_HOWTO = '打开终端运行包内 启动十神版.command，调度器会在流月首个交易日 20:00 自动更新';

/* "2026-11-09T22:41:03+08:00" -> "2026-11-09 22:41"。纯截取，无时区换算。 */
function bjtMinute(s) {
  if (typeof s !== 'string') return null;
  const m = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  return m ? (m[1] + ' ' + m[2]) : null;
}

/* get() 是 GET 专用，这里另加一个极简 POST：只给 /api/db_update/preview 与 /start 用，
   并把 HTTP 状态码与原 error 码带出来（401 PREVIEW_STALE 一类分支要靠它）。 */
async function postJson(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify(body) });
  let j = {};
  try { j = await r.json(); } catch (e) { j = {}; }
  if (r.status === 401 && j && j.error === 'UNAUTHORIZED') {
    window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname + window.location.search);
    return new Promise(() => {});
  }
  if (!r.ok) {
    const raw = (j && typeof j.error === 'object' && j.error)
      ? (j.error.message || j.error.code || '') : (j && j.error ? j.error : '');
    const err = new Error(String(raw || r.statusText));
    err.httpStatus = r.status;
    err.payload = j;
    throw err;
  }
  return j;
}

function stageDone(x) {
  const s = String((x && x.status) || '').toUpperCase();
  return /DONE|SUCCESS|OK|FINISH|PASS|SKIP/.test(s) && !/RUNNING/.test(s);
}
function stageBad(x) {
  const s = String((x && x.status) || '').toUpperCase();
  return /FAIL|ERROR|REJECT|ROLLBACK|ABORTED|CANCELLED/.test(s);
}

/* /api/db_update/state 有两种可见的形状：
   契约里写的是扁平 {status, stage, stages:[...]}；T1 实际实现把运行态嵌进 j.state。
   这里统一归一化，两种都能画，不猜也不造字段。 */
function normState(j) {
  if (!j) return null;
  const s = (j.state && typeof j.state === 'object') ? j.state : {};
  const pick = (a, b) => (a !== undefined && a !== null ? a : b);
  return {
    run_id: pick(j.run_id, s.run_id),
    status: pick(s.status, j.status),
    stage: pick(s.stage, j.stage),
    trigger: pick(j.trigger, s.trigger),
    started_at_bjt: pick(s.started_at_bjt, j.started_at_bjt),
    finished_at_bjt: pick(s.finished_at_bjt, j.finished_at_bjt),
    stages: Array.isArray(s.stages) ? s.stages : (Array.isArray(j.stages) ? j.stages : []),
    log_tail: Array.isArray(j.log_tail) ? j.log_tail
      : (Array.isArray(s.log_tail) ? s.log_tail : []),
    error: pick(j.error, s.error),
  };
}
/* 终态判定：后端 state.status 枚举以 RUNNING 为唯一「进行中」，其余按终态收尾；
   finished_at_bjt 一旦有值也视为终态（SUCCESS/FAILED/ERROR/ABORTED/…）。 */
function isTerminalState(j) {
  const d = normState(j) || {};
  const s = String(d.status || '').toUpperCase();
  if (d.finished_at_bjt) return true;
  if (!s) return false;
  return !/RUNNING|PENDING|QUEUED|STARTING|WAITING|LOCKED/.test(s);
}

/* 页头那行的主文案 + 颜色类 + 可选的可照做一句话。 */
function dbStampParts() {
  const st = DBUP.status;
  if (!st) {
    if (DBUP.fetchFailed) {
      return { main: (DBUP.lastMain || '数据更新') + ' · 状态未知', hint: DB_HOWTO, cls: 'is-warn' };
    }
    return { main: '数据状态读取中…', hint: '', cls: '' };
  }
  const status = String(st.status || '').toUpperCase();
  const ts = bjtMinute(st.last_success_bjt);
  let main, hint = '', cls = '';
  if (st.last_success_bjt === null || st.last_success_bjt === undefined) {
    main = '数据尚未自动更新过';
    hint = DB_HOWTO;
  } else {
    main = '数据更新 ' + (ts || String(st.last_success_bjt)) + ' 北京';
  }
  const tail = [];
  const base = main;
  /* 异常才变色。T1 实际把 GATE_FAILED/BUILD_FAILED/LOCK_STALE 这类细码放在 error.code，
     顶层 status 收敛为 RUNNING/SUCCESS/FAILED/…，故两处都查，文案一律回显后端原码。 */
  const errCode = (st.error && st.error.code) ? String(st.error.code).toUpperCase() : '';
  const errMsg = (st.error && st.error.message) ? String(st.error.message) : '';
  const BAD = ['GATE_FAILED', 'BUILD_FAILED', 'LOCK_STALE', 'LOCK_STALE_RENAMED',
               'STALE_LOCK_RENAMED', 'ROLLBACK_DONE'];
  const WARN = ['SESSION_NOT_CLOSED', 'STOPPED_MAX_ATTEMPTS', 'CALENDAR_MISSING',
                'PIPELINE_ENTRY_MISSING', 'DISABLED', 'NO_RUN_ID'];
  if (status === 'SOURCE_NOT_READY' || errCode === 'SOURCE_NOT_READY') {
    tail.push('· 源未就绪(' + (st.source_max_trade_date == null ? '—' : st.source_max_trade_date) + ')');
    cls = 'is-warn';
  } else if (status === 'MONTH_NOT_ENDED' || errCode === 'MONTH_NOT_ENDED') {
    tail.push('· 当月未结束');
    if (!cls) cls = '';
  } else if (status === 'RUNNING' && pollUnresolved()) {
    /* 轮询已到上限而后端仍报 RUNNING：这一轮没回报结束 ⇒ 如实说「状态未知」，
       不再显示「更新中」把用户吊在那里（按钮同时解禁，见 updRunning）。 */
    tail.push('· ' + UNKNOWN_STATE_LABEL); cls = 'is-warn';
  } else if (status === 'RUNNING') {
    tail.push(runningLabel());
  } else if (BAD.indexOf(status) >= 0) {
    tail.push('· ' + status); cls = 'is-bad';
  } else if (BAD.indexOf(errCode) >= 0 || status === 'ERROR' || errCode === 'ERROR') {
    tail.push('· ' + (BAD.indexOf(status) >= 0 ? status : errCode || status)); cls = 'is-bad';
  } else if (status === 'FAILED') {
    /* rc!=0 的硬失败：码与原因都要出现在正文里（P1-2：以前 NO_ROOT_CONFIGURED 这类
       只在日志里，页面上只剩一个词，用户看不见为什么）。 */
    tail.push('· FAILED' + (errCode && errCode !== 'FAILED' ? '(' + errCode + ')' : ''));
    if (errMsg) tail.push('· ' + clip(errMsg, 25));
    cls = 'is-bad';
  } else if (WARN.indexOf(status) >= 0) {
    tail.push('· ' + status); cls = 'is-warn';
  } else if (errCode && status !== 'OK' && status !== 'SUCCESS') {
    tail.push('· ' + errCode + (errMsg ? ' ' + clip(errMsg, 25) : '')); cls = 'is-warn';
  }
  if (st.scheduler && st.scheduler.alive === false) {
    tail.push('· 调度器离线');
    if (!cls) cls = 'is-warn';
  }
  if (typeof st.lag_trading_days === 'number' && st.lag_trading_days >= 3) {
    tail.push('· 滞后 ' + st.lag_trading_days + ' 日');
  }
  if (tail.length) main += ' ' + tail.join(' ');
  return { main: main, base: base, hint: hint, cls: cls };
}

/* 「更新中 3/13」：优先用轮询到的 stages 计数，其次用 db_status 内嵌的 state.stages，
   再退化成后端 stage 原样回显，不自造数字。 */
function runningStages() {
  const polled = normState(DBUP.state);
  if (polled && Array.isArray(polled.stages) && polled.stages.length) return polled.stages;
  const embedded = normState(DBUP.status && DBUP.status.state);
  if (embedded && Array.isArray(embedded.stages) && embedded.stages.length) return embedded.stages;
  return null;
}
function runningLabel() {
  const sg = runningStages();
  if (sg) {
    let done = 0;
    sg.forEach((x) => { if (stageDone(x)) done += 1; });
    return '· 更新中 ' + done + '/' + sg.length;
  }
  const d = normState(DBUP.state) || {};
  const stage = d.stage || (DBUP.status && (DBUP.status.stage ||
                (DBUP.status.state && DBUP.status.state.stage)));
  if (typeof stage === 'string' && stage) {
    const m = stage.match(/(\d+)\s*\/\s*(\d+)/);
    if (m) return '· 更新中 ' + m[1] + '/' + m[2];
    return '· 更新中（' + stage + '）';
  }
  return '· 更新中';
}

function renderDbStamp() {
  const box = $('#db-stamp'), mainEl = $('#db-stamp-main'), hintEl = $('#db-stamp-hint');
  if (!box || !mainEl) return;
  const p = dbStampParts();
  if (p.base) DBUP.lastMain = p.base;   // 断连降级时保留最后一次已知的时间
  mainEl.textContent = p.main;
  if (hintEl) hintEl.textContent = p.hint || '';
  const datesEl = $('#db-stamp-dates');
  if (datesEl) {
    const t = DBUP.status ? stampDatesText() : '';
    datesEl.textContent = t;
    datesEl.classList.toggle('hidden', !t);
  }
  box.classList.remove('is-warn', 'is-bad');
  if (p.cls) box.classList.add(p.cls);
  box.title = stampTitle();
  syncUpdBtn();
}

/* 悬浮标题：把后端已有的细节原样给出，不在正文里堆字。 */
function stampTitle() {
  const st = DBUP.status;
  if (!st) return '数据库状态读取失败：' + DB_HOWTO;
  const d = normState(st.state) || {};
  const bits = ['后端状态 ' + (st.status || '—')];
  /* 用词修正（P2）：`rule_version` 是**关系引擎规则版本**（G-E 真正比的那个数），
     不是「调度规则」；调度器自己的构建串另有 `scheduler_build` 字段，两个都摆出来。 */
  if (st.rule_version) bits.push('关系引擎版本 ' + st.rule_version);
  if (st.scheduler_build) bits.push('调度器构建 ' + st.scheduler_build);
  if (st.asof_bjt) bits.push('接口时刻 ' + (bjtMinute(st.asof_bjt) || st.asof_bjt) + ' 北京');
  if (st.db_last_trade_date != null) bits.push('库内最后交易日 ' + st.db_last_trade_date);
  if (st.source_max_trade_date != null) bits.push('源最大交易日 ' + st.source_max_trade_date);
  if (typeof st.lag_trading_days === 'number') bits.push('滞后 ' + st.lag_trading_days + ' 个交易日');
  if (st.next_planned_bjt != null) bits.push('下次计划 ' + (bjtMinute(st.next_planned_bjt) || st.next_planned_bjt) + ' 北京');
  if (d.stage) bits.push('当前阶段 ' + d.stage);
  if (st.error) bits.push('错误 ' + String(st.error.code || '') + ' ' + String(st.error.message || ''));
  return bits.join(' · ');
}

/* 页头正文里那两个日期（P2：触屏与键盘用户拿不到 title，正文必须有）。
   照后端字段原样回显，取不到就整段不出现——不自造、不写「—」占位糊弄。 */
function stampDatesText() {
  const st = DBUP.status || {};
  const db = st.db_last_trade_date;
  const src = st.source_max_trade_date;
  const ok = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v);
  if (!ok(db) && !ok(src)) return '';
  const bits = [];
  if (ok(db)) bits.push('库 ' + db);
  if (ok(src)) bits.push('源 ' + src);
  return bits.join(' / ');
}

/* 轮询已到上限、这一轮又没等到终态 ⇒ 该轮标成「未回报结束」（只影响前端兜底，不改判后端）。 */
function pollUnresolved() {
  return !!(DBUP.unresolvedRunId && DBUP.runId && DBUP.unresolvedRunId === DBUP.runId);
}

/* 任何“进行中”状态下禁用按钮，杜绝第二次请求。 */
function updRunning() {
  if (DBUP.sending) return true;
  const st = DBUP.status || {};
  if (String(st.status || '').toUpperCase() === 'RUNNING' && !pollUnresolved()) return true;
  return !!(DBUP.runId && !DBUP.terminal && !pollUnresolved());
}
/* 后端 available_actions（["preview"] / ["preview","start"] / []）在这里只做**警示**，
   不做拦截，理由有两条（负责人裁定 + 用户可见的死锁教训）：
   1) preview 是纯只读检查。现网 `work/update.disabled` 在位时后端会把动作收成 []，
      以前前端据此连只读检查都不让跑，用户永远看不见闸门逐条，还把标题写成「检查没跑成」——
      停用屏蔽只读检查是没有道理的。现在：停用状态下照样跑 preview，
      拒绝发生在「确认并写库」那一步（后端 start 返回 503 DISABLED，前端也把主按钮做成说明态）。
   2) available_actions 是一次缓存读数：锁失效或哨兵被删后不刷新页面，就会把「手动保底」
      入口永久锁住。真禁点的只有 updRunning()（真有在跑的轮次/请求）。
   字段缺失（老后端/未联调）时连警示也没有。 */
function backendBlocksPreview() {
  const st = DBUP.status;
  if (!st) return null;
  const acts = st.available_actions;
  if (!Array.isArray(acts) || acts.indexOf('preview') >= 0) return null;
  if (st.disabled === true) return null;      // 停用不挡只读检查（只挡写库那一步）
  if (!acts.length) return '后端当前不接受任何更新动作（多半正在跑）';
  return '后端没开放 preview 动作';
}
function syncUpdBtn() {
  const b = $('#db-update-btn');
  if (!b) return;
  const running = updRunning();
  const warn = backendBlocksPreview();
  /* 只读检查期间同样禁点（杜绝连点），但文案要说清「是在检查，不是在跑更新」。
     按钮尺寸/字号一律不动，只换文字；文字由这里唯一负责，失败后能自动恢复。 */
  b.disabled = running || DBUP.previewing;
  /* B 方案：预览会做一次封印件逐件校验（约 1 分钟），文案要提前说清，别让用户以为按钮死了。 */
  b.textContent = DBUP.previewing ? '检查中(封印校验约1分钟)…' : '更新';
  b.title = (!running && !DBUP.previewing && warn) ? warn + '：点一下会先重读状态并如实答复' : '';
  b.setAttribute('aria-label', DBUP.previewing
    ? '正在做只读检查（含封印件逐件校验，约 1 分钟，不写库），请稍候'
    : running
      ? '数据库更新进行中，暂不可再次发起'
      : (warn ? warn + '（点一下只重读状态，不会发起写库）'
              : '手动更新数据库（先做只读检查，再确认写库）'));
}

function refreshDbStatus() {
  return get('/api/db_status').then((j) => {
    DBUP.status = j; DBUP.fetchFailed = false; renderDbStamp();
    const running = String((j && j.status) || '').toUpperCase() === 'RUNNING';
    if (running && !DBUP.runId) {
      /* T1 已把在跑的 run_id 直接放进 db_status.running_run_id，优先用它；
         没有再退回无参 state（老契约里 state 端点缺省会报 NO_RUN_ID）。 */
      if (j.running_run_id) {
        DBUP.runId = j.running_run_id; DBUP.terminal = false;
        startPolling();
      } else {
        tryAdoptRunningRun();
      }
    }
  }).catch(() => {
    DBUP.fetchFailed = true; DBUP.status = null; renderDbStamp();
  });
}

/* 更新可能由调度器发起，前端没有 run_id：先无参试要一次 state，拿不到就静默放弃。 */
function tryAdoptRunningRun() {
  return get('/api/db_update/state').then((j) => {
    const d = normState(j);
    if (d && d.run_id && !isTerminalState(j)) {
      DBUP.runId = d.run_id; DBUP.state = j; DBUP.terminal = false;
      renderPanel(j); startPolling();
    }
  }).catch(() => { /* 静默：页头保持只显文字，不影响任何页签 */ });
}

/* ---------------- 确认弹窗（两段式：先只读检查，再确认写库） ----------------
 * 用户裁定：这个按钮平时不用，它是「自动那条线没跑成时的手动保底」。
 * 所以点第一下只打 POST /api/db_update/preview（纯只读、绝不写库），把闸门结论摆出来；
 * 全过才给可点的「确认并写库」，有不过就把主按钮做成不可点的说明态——不给一个点了必失败的按钮。
 */

/* 闸门清单 / 可确认判定：后端没给闸门就一律当作「不可确认」，不猜也不放行。
   disabled / all_gates_pass 是后端已经算好的两件事，只要给了就以它为准——
   闸门全过但后端停用（start 会 503 DISABLED）时，绝不能给一个点了必失败的按钮。
   三态判据：`pass` 只有 **true** 才算过；false=未过；null=「本预览不判」，同样不算过
   （判不准就说不判，绝不替用户放行）。 */
function gatesOf(preview) {
  return (preview && Array.isArray(preview.gates)) ? preview.gates : [];
}
function gateUndecided(preview) {
  return gatesOf(preview).filter((g) => !g || g.pass === null || g.pass === undefined);
}
function previewAllPass(preview) {
  const gs = gatesOf(preview);
  if (!gs.length || !gs.every((g) => !!g && g.pass === true)) return false;
  if (preview && preview.all_gates_pass === false) return false;
  if (preview && preview.disabled === true) return false;
  /* 注意边界：这里说的「全过」只覆盖**本预览真判了的那几条**（后端 gates_covered_by_preview）。
     流水线还有 G_F/G_G 两道要跑到那一步才有结论，预览给不了 ⇒ 文案必须写「预览覆盖 X/Y」，
     不许说成「七闸全过」。这一条不构成放行依据，所以也不因此把确认按钮禁死
     （手动入口是自动那条线没跑成时的保底，禁死就等于把用户关在门外）。 */
  return true;
}
/* 「预览能不能替流水线说七闸全过」——只要后端声明有未覆盖的闸门，答案就是不能。 */
function previewClaimsAllSeven(preview) {
  if (!preview) return false;
  const total = Number(preview.gates_total_in_pipeline || 0);
  const covered = Array.isArray(preview.gates_covered_by_preview)
    ? preview.gates_covered_by_preview.length : 0;
  const notCovered = Array.isArray(preview.gates_not_covered_by_preview)
    ? preview.gates_not_covered_by_preview.length : 0;
  return !!total && !notCovered && covered >= total;
}
/* 不能确认时的那句原因（按钮文案用）。顺序要紧：
   停用哨兵是全局挡 → 再点出第一条未过的闸门（用户要的就是这句具体话）
   → 最后才是「后端 overall 说没过但说不出哪条」的兜底。 */
function blockedReason(preview) {
  if (preview && preview.disabled === true) return '自动与手动都已被停用哨兵挡住（work/update.disabled）';
  const gs = gatesOf(preview);
  const bad = gs.filter((g) => !g || g.pass !== true)[0] || null;
  if (bad) return String(bad.name || bad.id || '闸门');
  if (!gs.length) return '后端没给检查结论';
  if (preview && preview.all_gates_pass === false) return '后端判定闸门未全过（未给出是哪一条）';
  return null;
}
/* 数字优先用本次检查返回的，回落到 db_status 已有字段；两处都没有→null（显示「未知」）。 */
function pvField(key) {
  const p = DBUP.preview || {};
  const st = DBUP.status || {};
  if (p[key] !== undefined) return p[key];
  if (st[key] !== undefined) return st[key];
  return null;
}
/* 源/库这两个字段该是「某一天」：先挑真日期，都不是日期就原样回显后端给的值（不自己编）。 */
const isDateStr = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v);
function dateVal(key) {
  const p = (DBUP.preview || {})[key];
  const s = (DBUP.status || {})[key];
  if (isDateStr(p)) return p;
  if (isDateStr(s)) return s;
  if (p !== undefined && p !== null) return p;
  if (s !== undefined && s !== null) return s;
  return null;
}
function clip(s, n) {
  const t = String(s == null ? '' : s);
  return t.length > n ? t.slice(0, n) + '…' : t;
}
/* 不可点主按钮的说明文案：把「为什么还不能更新」直接写在按钮上，长句截断。 */
function blockedLabel(preview, failure) {
  if (failure) return clip('检查没跑成，还不能写库：' + failure, 40);
  const name = blockedReason(preview) || '检查结论';
  if (name.indexOf('源') >= 0) {
    const src = dateVal('source_max_trade_date');
    /* 后端给的若不是干净日期，就不硬拼进按钮那句长文案（会重复成「源末交易日 源末交易日…」），
       具体原因走闸门逐条与按钮 title。 */
    return clip('源没到，还不能更新：源末交易日 ' + (isDateStr(src) ? src : '见下方原因'), 40);
  }
  return clip('还不能更新：' + name, 40);
}

function confirmBody(preview, failure) {
  const box = $('#upd-confirm-body');
  if (!box) return;
  if (failure) {
    box.innerHTML =
      '<div class="upd-fail">只读检查这一步没跑成：' + esc(failure) + '</div>' +
      '<ul>' +
      '<li>后端没有收到写库请求，本次没有改动任何数据。</li>' +
      '<li>关掉本弹窗，稍后再点一次「更新」就是重跑检查。</li>' +
      '</ul>';
    return;
  }
  const gs = gatesOf(preview);
  const gateRows = gs.length
    ? gs.map((g) => {
        /* 三态显示：true=通过 / false=未通过 / null 或 undefined=本预览不判（不是通过，也不是失败） */
        const undecided = !g || g.pass === null || g.pass === undefined;
        const pass = !!g && g.pass === true;
        const res = pass ? '通过' : (undecided ? '不判' : '未通过');
        const cls = pass ? 'pass' : (undecided ? 'undecided' : 'fail');
        return '<div class="upd-gate ' + cls + '">' +
          '<span class="g-res">' + res + '</span>' +
          '<span class="g-name">' + esc((g && (g.name || g.id)) || '闸门') +
            (g && g.id ? '（' + esc(g.id) + '）' : '') + '</span>' +
          '<span class="g-detail">' + esc(clip(g && g.detail != null ? g.detail : '后端未给原因', 200)) + '</span>' +
          '</div>';
      }).join('')
    : '<div class="upd-fail">后端这次没有返回闸门清单，无法确认可以写库。</div>';

  const meta = [];
  const add = (k, v) => {
    if (v === null || v === undefined || v === '') return;
    meta.push('<div class="m-k">' + k + '</div><div class="m-v">' + v + '</div>');
  };
  const src = dateVal('source_max_trade_date');
  const db = dateVal('db_last_trade_date');
  const lag = pvField('lag_trading_days');
  const st = DBUP.status || {};
  const p = preview || {};
  const est = p.estimated_minutes != null ? p.estimated_minutes : null;
  const estBasis = p.estimated_minutes_basis || null;
  const disk = p.disk_free_gib != null ? p.disk_free_gib : null;
  const next = p.next_step || null;
  const acts = pvField('available_actions');
  add('源到哪一天', src == null ? '未知' : esc(src));
  add('库到哪一天', db == null ? '未知' : esc(db));
  add('滞后几个交易日', (typeof lag === 'number') ? esc(lag) + ' 个' : '未知');
  /* 耗时口径必须标清是「估算」还是「实测」：本机从未跑完过一次真跑，只能是估算。 */
  add('预计耗时', (typeof est === 'number')
    ? ('约 ' + esc(est) + ' 分钟（估算，非实测）'
       + (estBasis ? '｜口径：' + esc(clip(estBasis, 120)) : ''))
    : '后端未给');
  add('磁盘余量', (typeof disk === 'number')
    ? (esc(disk) + ' GiB' + (p.volume_path_label ? '｜写入卷：' + esc(p.volume_path_label) : '')
       + (p.disk_min_free_bytes ? '（下限 '
         + (Number(p.disk_min_free_bytes) / 1073741824).toFixed(1) + ' GiB，读编排器同一实现）' : ''))
    : '后端未给');
  /* 检查结论的时效：深判据是短缓存值（后端会给出算好的时刻），不假装是此刻实时。 */
  if (p.preview_basis_asof_bjt && p.gates && p.gates.length) {
    add('闸门结论算于', esc(bjtMinute(p.preview_basis_asof_bjt) || p.preview_basis_asof_bjt)
      + ' 北京（缓存 ' + (Number(p.preview_basis_ttl_seconds) || 0) + ' 秒内复用）');
  }
  /* 覆盖范围照实摆：共 7 道、本预览判了哪几道、没判的为什么没判。 */
  if (p.gates_total_in_pipeline != null) {
    const covered = Array.isArray(p.gates_covered_by_preview)
      ? p.gates_covered_by_preview.join('、') : '—';
    add('检查覆盖面', '本预览判 ' + esc(covered) + '（流水线共 '
      + esc(p.gates_total_in_pipeline) + ' 道闸）');
  }
  if (Array.isArray(p.gates_not_covered_by_preview) && p.gates_not_covered_by_preview.length) {
    add('本预览没判的闸门', p.gates_not_covered_by_preview.map((x) =>
      esc((x && x.id) || '?') + '：' + esc(clip((x && x.why) || '未给原因', 80))).join('；'));
  }
  /* 上一轮编排器自己报的写入目标冲突数：让用户看见「这一轮本身还写不进去」。 */
  if (p.last_run_write_conflicts != null) {
    add('本轮写入目标冲突数', esc(p.last_run_write_conflicts) + ' 个'
      + '（上一轮 ' + esc(p.last_run_id || '?') + ' / ' + esc(p.last_run_status || '?')
      + (p.last_run_finished_at_bjt ? ' 结束于 ' + esc(bjtMinute(p.last_run_finished_at_bjt)) : '')
      + ' 北京，编排器 dry-run 原样回报）');
  }
  /* 后端已算好的几件事：只要给了就照实摆出来，用户才知道结论什么时候会过期。 */
  if (p.expires_at_bjt != null) {
    add('结论有效期', esc(bjtMinute(p.expires_at_bjt) || p.expires_at_bjt)
      + ' 北京（这枚令牌绑定检查时的状态快照：状态没变、没过期前可重复用；'
      + '状态一变就 401 需重新检查）');
  }
  if (p.disabled === true) add('停用哨兵', '存在：自动与手动都不会触发、写库那一步会被拒（本页面不会代删）');
  if (p.writes_anything === false) add('这一步（只读检查）', '不写库：本接口自身一个字节都不写');
  /* 「会不会写库」按 mode / execute_confirmed 分两态说，不再一句话糊过去。 */
  if (p.start_writes_db === false) {
    add('点「确认」会写库吗', '不会：work/execute.confirmed 不在位 ⇒ 本轮只会演练（dry-run），不写库');
  } else if (p.start_writes_db === true) {
    add('点「确认」会写库吗', '会：work/execute.confirmed 在位 ⇒ 本轮会真跑并写库');
  }
  if (typeof p.mode === 'string' && p.mode) add('后端模式', esc(p.mode));
  if (typeof next === 'string' && next) add('后端说的下一步', esc(clip(next, 240)));
  add('可用动作', Array.isArray(acts) && acts.length ? esc(acts.join(' / ')) : null);
  /* 自动与手动的次数分开报（P2）：手动发起不占自动重试额度，混在一起会让用户
     误以为「自动重试没用完」。数字一律来自后端。 */
  const aAuto = p.attempts_auto != null ? p.attempts_auto : st.attempts_auto;
  const aManual = p.attempts_manual != null ? p.attempts_manual : st.attempts_manual;
  if (aAuto != null) {
    add('尝试次数', '自动已尝试 ' + esc(aAuto) + ' 次'
      + (aManual != null ? (' · 手动发起 ' + esc(aManual) + ' 次（不占自动额度）') : '')
      + (st.max_attempts_per_month ? (' · 自动月上限 ' + esc(st.max_attempts_per_month) + ' 次') : ''));
  }
  if (st.last_success_bjt) {
    add('上次成功更新', esc(bjtMinute(st.last_success_bjt) || st.last_success_bjt) + ' 北京');
  } else {
    add('上次成功更新', '从未成功过 · ' + esc(DB_HOWTO));
  }
  if (st.next_planned_bjt != null) {
    add('下次自动计划', esc(bjtMinute(st.next_planned_bjt) || st.next_planned_bjt) + ' 北京');
  }

  const undecidedNote = gateUndecided(preview).length
    ? '<li>标「不判」的闸门是本次检查判不了的（原因写在每一条里），'
      + '判不了不等于过，也不会替它放行。</li>'
    : '';
  const notCoveredNote = (Array.isArray(p.gates_not_covered_by_preview)
                          && p.gates_not_covered_by_preview.length)
    ? '<li>本检查只覆盖流水线的部分闸门（见「检查覆盖面」），'
      + '「检查通过」指的是<b>被覆盖的那几道</b>都过了，不等于七道闸全过。</li>'
    : '';
  box.innerHTML =
    '<div class="upd-gates">' + gateRows + '</div>' +
    '<ul>' +
    '<li>上面是后端刚跑的<b>只读</b>检查结论：这一趟不落库、不改任何数据。</li>' +
    notCoveredNote + undecidedNote +
    '<li>点「确认」发起的这一轮' + (p.start_writes_db
      ? '会走完整条更新链路并<b>写库</b>（execute.confirmed 在位）'
      : '只会走演练（dry-run）：<b>不写库</b>，真跑仍受写库前闸门与人工确认文件 work/execute.confirmed 约束')
    + '；耗时是估算值，见上方口径。</li>' +
    '<li>更新期间页面仍读旧库：现在看到的行情、筛选与机会表不会中途变化。</li>' +
    '<li>失败会把原因写进状态里（不再只显示「更新中」）；进度与日志在右下角面板可见。</li>' +
    '</ul>' +
    '<div class="upd-meta">' + meta.join('') + '</div>';
}

/* 第一击：只读检查。期间按钮禁用并显示「检查中」，杜绝连点打出一串 preview。 */
async function runUpdPreview() {
  if (DBUP.previewing || DBUP.sending || updRunning()) return;
  DBUP.previewing = true;
  DBUP.preview = null;
  DBUP.token = null;
  DBUP.note = null;          // 新一轮检查：把上一轮的面板说明（如 409 跟进）清掉
  syncUpdBtn();
  let preview = null, failure = null;
  try {
    /* 源/库/滞后取最新一次读数，弹窗里的数字不靠首屏缓存；本身带 catch，不会抛。 */
    await refreshDbStatus();
    /* 后端明确「现在不给做任何动作」时连只读检查都不必打——但这一句要用刚重读到的状态判，
       不能用点进来之前的旧缓存，否则锁一失效就把保底入口永久锁死。 */
    const blocked = backendBlocksPreview();
    if (blocked) {
      failure = '未发起检查：' + blocked + '。本次没有向后端要检查、更没有写库。';
    } else {
      preview = await postJson('/api/db_update/preview', {});
      DBUP.preview = preview;
      DBUP.token = (preview && typeof preview.preview_token === 'string' && preview.preview_token)
        ? preview.preview_token : null;
    }
  } catch (e) {
    const payload = e.payload || {};
    const code = String(payload.error || e.message || '') || '接口无响应';
    failure = code + (e.httpStatus ? '（HTTP ' + e.httpStatus + '）' : '');
  }
  DBUP.previewing = false;
  openConfirm(preview, failure);
  renderDbStamp();          // 顺带把按钮从「检查中」恢复成可点的「更新」
}

function openConfirm(preview, failure) {
  const d = $('#upd-confirm');
  if (!d) return;
  if (updRunning()) { renderDbStamp(); return; }
  const err = $('#upd-confirm-err');
  if (err) err.textContent = '';
  const title = $('#upd-confirm-title');
  if (title) {
    /* 标题如实表述（P1-1 第 3 条）：检查通过≠七闸全过，且真跑还受写库前那道人工确认文件约束。
       失败时也分两种：接口真没跑成 vs 停用哨兵在位（后者检查是跑成了的，只是不许写库）。 */
    if (failure) title.textContent = '只读检查结果：本次检查没有跑成';
    else if (previewAllPass(preview) && DBUP.token) {
      title.textContent = previewClaimsAllSeven(preview)
        ? '前置检查结论 · 覆盖的闸门全过'
        : '前置检查结论 · 真跑仍受写库前闸门与人工确认文件约束';
    } else title.textContent = '检查未通过 · 现在还不能确认';
  }
  confirmBody(preview, failure);
  d.classList.remove('hidden');
  const go = $('#upd-go');
  if (go) {
    const ok = !failure && previewAllPass(preview) && !!DBUP.token;
    go.disabled = !ok;
    /* 不可点时不能还长得像一个能点的紫按钮：换成中性虚线说明态。 */
    go.classList.toggle('is-blocked', !ok);
    go.setAttribute('aria-disabled', ok ? 'false' : 'true');
    /* 按钮文案按 mode / execute_confirmed 分两态（P1-1 第 3 条）：
       execute.confirmed 不在位时**任何文案都不许说「会写库」**。 */
    const writes = !!(preview && preview.start_writes_db);
    go.textContent = ok ? (writes ? '确认并写库' : '确认并发起演练（不写库）')
                       : blockedLabel(preview, failure);
    const bad = gatesOf(preview).filter((g) => !g || g.pass !== true)[0] || null;
    go.title = ok
      ? (writes ? '本轮会写库：work/execute.confirmed 在位，start 起的流水线会真跑并替换正式库'
                : '本轮只会演练，不写库：work/execute.confirmed 不在位，'
                  + 'start 起的流水线仍是 dry-run（写入目标一个字节都不落）')
      : esc(clip((bad && (bad.detail || bad.name || bad.id))
                 || blockedReason(preview) || failure || '没有可确认的检查结论', 300));
    if (ok) go.focus(); else { const c = $('#upd-cancel'); if (c) c.focus(); }
  }
}
function closeConfirm() {
  const d = $('#upd-confirm');
  if (d) d.classList.add('hidden');
}

async function submitStart() {
  const go = $('#upd-go');
  if (updRunning()) return;
  /* 第二击才写库：必须带着本次只读检查拿到的 preview_token，没结论一律不发。 */
  if (!DBUP.token) {
    closeConfirm();
    openPanel();
    panelStatus('没有可用的检查结论（令牌为空），请先点页头「更新」重跑只读检查。本次未写入任何数据。', 'is-warn');
    renderDbStamp();
    return;
  }
  DBUP.sending = true;
  if (go) { go.disabled = true; go.textContent = '发起中…'; }
  syncUpdBtn();
  const errEl = $('#upd-confirm-err');
  if (errEl) errEl.textContent = '';
  try {
    const j = await postJson('/api/db_update/start', { confirm: true, preview_token: DBUP.token });
    DBUP.sending = false;
    DBUP.token = null; DBUP.preview = null; DBUP.note = null;   // 发过一轮就重新检查（旧快照不再复用）
    DBUP.runId = j && j.run_id ? j.run_id : null;
    DBUP.terminal = false;
    DBUP.pollTicks = 0; DBUP.unresolvedRunId = null;   // 新一轮：轮询上限重新起算
    closeConfirm();
    openPanel();
    startPolling();
    renderDbStamp();
  } catch (e) {
    DBUP.sending = false;
    const payload = e.payload || {};
    const code = String(payload.error || e.message || '');
    /* 409/503/400 的 error 文案原样回显，不自造成功提示。 */
    if (errEl) errEl.textContent = '后端返回：' + code + (e.httpStatus ? '（HTTP ' + e.httpStatus + '）' : '');
    /* 弹窗一关错误就看不见，所以失败一律同时写进右下角面板（用户看得见的那块）。 */
    DBUP.token = null; DBUP.preview = null;   // 失败之后旧结论不再可用，必须重新检查
    closeConfirm();
    openPanel();
    if (code === 'ALREADY_RUNNING' && payload.run_id) {
      /* 已经有人在跑：不报错、不重复发起，改成跟进对方的进度。
         这句要留在面板上（renderPanel 每轮都会重写状态行），所以存进 DBUP.note。 */
      DBUP.runId = payload.run_id; DBUP.terminal = false;
      DBUP.note = '已有人在跑这一轮（ALREADY_RUNNING）· 改为跟进对方进度';
      panelStatus(DBUP.note, 'is-warn');
      startPolling();
    } else if (e.httpStatus === 401 || code === 'PREVIEW_STALE') {
      /* P2：原码必须留在用户看得见的那行里。以前只把改写后的句子写进面板，
         PREVIEW_STALE / HTTP 401 这个事实随弹窗一起消失了。 */
      panelStatus('检查结论已过期，请重新检查（点页头「更新」再跑一次只读检查）'
        + ' · 后端原码：' + (code || 'PREVIEW_STALE')
        + (e.httpStatus ? '（HTTP ' + e.httpStatus + '）' : '')
        + ' · 本次未写入任何数据。', 'is-warn');
    } else if (e.httpStatus === 400 || code === 'NO_PREVIEW_TOKEN') {
      panelStatus('后端不接受本次检查结论：' + (code || 'HTTP 400') + ' · 请重新检查 · 本次未写入任何数据。', 'is-warn');
    } else if (e.httpStatus === 503 || code === 'DISABLED') {
      /* 停用哨兵在位：只读检查照跑（闸门逐条已经摆出来了），被拒的只是「确认发起/写库」
         这一步。后端原码 503 DISABLED 原样回显，不写成「更新失败」也不写成成功。 */
      panelStatus('后端拒绝发起：自动与手动都被停用哨兵挡住（work/update.disabled）'
        + ' · 后端原码：' + (code || 'DISABLED') + '（HTTP ' + (e.httpStatus || 503) + '）'
        + ' · 本次未发起、未写入任何数据。', 'is-warn');
    } else {
      panelStatus('未能发起更新：' + (code || ('HTTP ' + (e.httpStatus || '接口无响应'))) +
        ' · 本次未写入任何数据。', 'is-bad');
    }
    renderDbStamp();   // 回到「可点更新」的状态（除非真有跑着的轮次）
  }
}

/* ---------------- 进度面板与轮询 ---------------- */
function openPanel() {
  const p = $('#upd-panel');
  if (p) p.classList.remove('hidden');
}
function closePanel() {
  const p = $('#upd-panel');
  if (p) p.classList.add('hidden');
}

/* 面板那一行状态：轮询前的失败/降级提示也写这里，弹窗关了用户仍看得见。 */
function panelStatus(text, cls) {
  const stEl = $('#upd-panel-status');
  if (!stEl) return;
  stEl.textContent = text;
  stEl.classList.remove('is-warn', 'is-bad');
  if (cls) stEl.classList.add(cls);
}

function renderPanel(j) {
  const stEl = $('#upd-panel-status'), sgEl = $('#upd-stages'), logEl = $('#upd-log');
  if (!stEl || !sgEl || !logEl) return;
  const d = normState(j) || {};
  const bits = [];
  /* 409 跟进对方进度这类说明要一直在，不能因为每轮重写状态行就消失。 */
  if (DBUP.note) bits.push(DBUP.note);
  if (d.status) bits.push('状态 ' + d.status);
  if (d.stage) bits.push('阶段 ' + d.stage);
  if (d.trigger) bits.push('触发 ' + d.trigger);
  if (d.run_id) bits.push('run ' + d.run_id);
  const s = bjtMinute(d.started_at_bjt);
  if (s) bits.push('开始 ' + s + ' 北京');
  const f = bjtMinute(d.finished_at_bjt);
  if (f) bits.push('结束 ' + f + ' 北京');
  stEl.textContent = bits.join(' · ');
  stEl.classList.remove('is-warn', 'is-bad');
  /* rc!=0 / 终态带错误：码与原因都要摆出来（P1-2：NO_ROOT_CONFIGURED 这类硬失败
     以前完全静默，页面只剩「更新中」）。原样回显后端字段，不改写、不缩写含义。 */
  if (d.error) {
    const e = d.error;
    const code = String((e && e.code) || '');
    const msg = String((e && e.message) || '');
    stEl.textContent += ' · 错误 ' + (code ? code + '：' : '') + (msg || code || String(e));
    stEl.classList.add('is-bad');
  } else if (pollUnresolved()) {
    stEl.textContent += ' · ' + UNKNOWN_STATE_LABEL + '，按钮已解禁，可重跑只读检查';
    stEl.classList.add('is-warn');
  }
  sgEl.innerHTML = '';
  (Array.isArray(d.stages) ? d.stages : []).forEach((x) => {
    const cls = stageBad(x) ? 'bad' : stageDone(x) ? 'done'
      : /RUNNING|PROCESSING/i.test(String(x.status || '')) ? 'run' : '';
    const row = el('div', 'upd-stage ' + cls);
    row.innerHTML = '<span class="s-node">' + esc(x.node == null ? '—' : x.node) + '</span>' +
      '<span class="s-stat">' + esc(x.status == null ? '—' : x.status) + '</span>' +
      '<span class="s-dur">' + (typeof x.duration_s === 'number' ? x.duration_s.toFixed(1) + 's' : '') + '</span>' +
      (x.note ? '<span class="s-note">' + esc(x.note) + '</span>' : '');
    sgEl.appendChild(row);
  });
  logEl.textContent = Array.isArray(d.log_tail) ? d.log_tail.join('\n') : '';
  logEl.scrollTop = logEl.scrollHeight;
  renderDbStamp();
}

function pollOnce() {
  if (!DBUP.runId || DBUP.busy) return Promise.resolve();
  DBUP.busy = true;
  /* 轮询上限兜底（P1-2）：后端一旦给出终态就会走到 isTerminalState 分支停掉这里；
     若连续 POLL_MAX_TICKS 次（20 分钟）都没等到，就停轮询并如实标「状态未知」，
     同时解禁按钮——不许把用户永久吊在「更新中」。 */
  DBUP.pollTicks += 1;
  if (DBUP.pollTicks > POLL_MAX_TICKS) {
    DBUP.busy = false;
    DBUP.unresolvedRunId = DBUP.runId;
    DBUP.terminal = true;                 // 让 visibilitychange / refresh 不再重启轮询
    stopPolling();
    panelStatus(UNKNOWN_STATE_LABEL + '：连续 ' + POLL_MAX_TICKS + ' 次（约 20 分钟）'
      + '读到的状态都没有结束时刻与原因。已停止轮询并解禁「更新」按钮，'
      + '点它会重跑只读检查；本轮是否真的结束以后端 work/update_state.json 为准。',
      'is-warn');
    renderDbStamp();
    return Promise.resolve();
  }
  return get('/api/db_update/state', { run_id: DBUP.runId }).then((j) => {
    DBUP.busy = false;
    DBUP.state = j;
    renderPanel(j);
    if (isTerminalState(j)) {
      DBUP.terminal = true;
      DBUP.unresolvedRunId = null;
      stopPolling();
      refreshDbStatus();          // 让页头那行时间立刻跟着刷新
      renderDbStamp();
    }
  }).catch((e) => {
    DBUP.busy = false;
    const stEl = $('#upd-panel-status');
    if (stEl) { stEl.textContent = '进度读取失败：' + (e.message || ''); stEl.classList.add('is-warn'); }
  });
}

function startPolling() {
  /* 已到上限的那一轮不再自动复轮（refreshDbStatus/visibilitychange 都可能再进来），
     除非换了一个新的 run_id（新一轮）。 */
  if (DBUP.unresolvedRunId && DBUP.unresolvedRunId === DBUP.runId) return;
  stopPolling();
  DBUP.pollTicks = 0;
  pollOnce();
  DBUP.timer = setInterval(pollOnce, POLL_INTERVAL_MS);
}
function stopPolling() {
  if (DBUP.timer) { clearInterval(DBUP.timer); DBUP.timer = null; }
}

function initDbStatusUi() {
  const btn = $('#db-update-btn');
  /* 第一击只做只读检查（preview），检查结论摆进既有的 #upd-confirm 弹窗；
     弹窗里的「确认并写库」才是第二击，才发 start。 */
  if (btn) btn.onclick = () => { runUpdPreview().catch(() => { DBUP.previewing = false; syncUpdBtn(); }); };
  const cancel = $('#upd-cancel');
  if (cancel) cancel.onclick = closeConfirm;
  const x = $('#upd-confirm-close');
  if (x) x.onclick = closeConfirm;
  const go = $('#upd-go');
  if (go) go.onclick = () => { submitStart().catch(() => {}); };
  const pc = $('#upd-panel-close');
  if (pc) pc.onclick = closePanel;
  const d = $('#upd-confirm');
  if (d) d.onclick = (e) => { if (e.target === d) closeConfirm(); };
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (d && !d.classList.contains('hidden')) closeConfirm();
  });
  /* 页面不可见时暂停轮询，回到前台且仍未终态时恢复。 */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else if (DBUP.runId && !DBUP.terminal) startPolling();
  });
  addEventListener('pagehide', stopPolling);
  renderDbStamp();
  refreshDbStatus();   // fire-and-forget：失败只在页头降级，不抛未捕获异常
}

/* ---------------- wiring ---------------- */
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

async function init() {
  AppTip.init();
  ['木', '火', '土', '金', '水'].forEach((e) => {
    ['#p-stem-el', '#b-stem-el', '#p-branch-el', '#b-branch-el'].forEach((s) => {
      $(s).appendChild(el('option', null, e)).value = e;
    });
  });
  document.querySelectorAll('.tabs button').forEach((b) => {
    b.onclick = () => activateTab(b.dataset.tab);
  });
  $('#q').addEventListener('input', () => {
    $('#suggest').innerHTML = ''; $('#suggest').classList.add('hidden');
  }, { passive: true });
  $('#q').addEventListener('input', debounce(async () => {
    const q = $('#q').value.trim(); const box = $('#suggest'); box.innerHTML = '';
    if (!q) { box.classList.add('hidden'); return; }
    const ep = S.epoch;
    const rows = await get('/api/search', { q, limit: 30 });
    if (ep !== S.epoch) return;
    box.classList.remove('hidden');
    rows.forEach((r) => {
      const has = r.has_natal_profile === 1;
      const it = el('div', 'sg' + (has ? '' : ' nop'),
        '<b>' + esc(r.security_code) + '</b> ' + esc(r.name || '') +
        '<i>' + typeLabel(r.security_type) +
        (has ? '' : ' · 无命盘') + '</i>');
      it.onclick = () => { if (!NAV.ready) return;
                           box.classList.add('hidden'); $('#q').value = r.security_code;
                           S.fromBoard = false;
                           // 搜索框在机会表里也常驻：选中标的就进入个股档案
                           if (document.body.classList.contains('board')) setBoardMode(false, { syncUrl: false });
                           syncReturnBar();
                           loadStock(r.identity_code, { navigate: true }); };
      box.appendChild(it);
    });
    if (!rows.length) box.appendChild(el('div', 'sg', '无匹配'));
  }, 220));
  $('#b-exclude-single').addEventListener('change', () => {
    S.branchPolicy = $('#b-exclude-single').checked ? 'EXCLUDE_SINGLE' : 'ALL';
    renderBranch();
  });
  $('#b-reset').onclick = () => { $('#b-pillar').value = '';
    ['#b-stem-el', '#b-branch-el', '#b-min'].forEach((i) => $(i).value = '');
    $('#b-sort').value = 'branch';
    $('#b-exclude-single').checked = false; S.branchPolicy = 'ALL';
    renderBranch(); saveFiltersNow(); };
  ['#b-pillar', '#b-stem-el', '#b-branch-el', '#b-sort', '#b-min'].forEach((id) => {
    $(id).addEventListener('change', debounce(renderBranch, 200));
    if (id === '#b-pillar') $(id).addEventListener('input', debounce(renderBranch, 300));
  });
  const rkm = $('#r-kind');
  if (rkm) rkm.querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      S.relKind = b.dataset.v;
      syncSegUi(rkm, S.relKind);
      renderRelation();
      saveFiltersNow();
    };
  });
  $('#o-month').addEventListener('change', debounce(renderOpportunity, 200));
  $('#o-month').addEventListener('change', mpSync);
  $('#o-month-btn').onclick = (e) => { e.stopPropagation(); mpToggle(); };
  $('#mp-prev').onclick = () => { MP.year -= 1; mpDraw(); };
  $('#mp-next').onclick = () => { MP.year += 1; mpDraw(); };
  $('#mp-now').onclick = () => {
    const m = mpMonths().find((x) => x.offset === 0);
    if (m) mpPick(m);
  };
  document.addEventListener('click', (e) => { if (!e.target.closest || !e.target.closest('#mp')) mpClose(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      mpClose();
      rvCloseAll();
      closeOppDrawer();
      const m = $('#modal');
      if (m) m.classList.add('hidden');
    }
  });
  $('#o-dir').addEventListener('change', syncDirUi);
  ['#o-min', '#o-grade', '#o-stab', '#o-cons', '#o-rate', '#o-both-months', '#o-next-rate', '#o-sort', '#o-cap',
   '#o-cap-tier', '#o-dir', '#o-rel', '#o-es', '#o-clash', '#o-harmony', '#o-both', '#o-punish', '#o-harm',
   '#o-broken', '#o-delist', '#o-idx']
    .forEach((id) => $(id).addEventListener('change', debounce(renderOpportunity, 200)));
  $('#o-cons').addEventListener('change', () => {
    const v = $('#o-cons').value;
    if (!S.oppMsort.length && v.indexOf('only_good') === 0 && $('#o-sort').value === 'mean') {
      $('#o-sort').value = 'consistency';
    }
  });
  const sortList = $('#o-sort-chip-list');
  if (sortList) {
    sortList.onclick = (e) => {
      const rm = e.target.closest('[data-rm]');
      if (rm) {
        e.stopPropagation();
        removeOppSort(Number(rm.dataset.rm));
        return;
      }
      const chip = e.target.closest('[data-i]');
      if (!chip) return;
      const item = S.oppMsort[Number(chip.dataset.i)];
      if (!item) return;
      item.dir = item.dir === 'desc' ? 'asc' : 'desc';
      syncOppSortUi();
      saveFiltersNow();
      renderOpportunity();
    };
  }
  const sortClear = $('#o-sort-clear');
  if (sortClear) sortClear.onclick = clearOppSort;
  $('#o-both-months').addEventListener('change', syncNextRateUi);
  syncNextRateUi();
  syncDirUi();
  $('#o-search').addEventListener('input', debounce(renderOpportunity, 350));
  $('#o-search').addEventListener('change', debounce(renderOpportunity, 200));
  $('#o-reset').onclick = () => {
    S.oppMsort = [];
    S.oppLayer = 'month';
    S.oppWindowNo = '';
    const oSeg = $('#o-layer-seg');
    if (oSeg) {
      oSeg.querySelectorAll('button').forEach((b, idx) => {
        if (idx === 0) { b.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm'); b.classList.remove('text-slate-400'); }
        else { b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm'); b.classList.add('text-slate-400'); }
      });
    }
    const xPills = $('#o-xun-pills');
    if (xPills) { xPills.classList.add('hidden'); xPills.classList.remove('flex'); }
    const wPills = $('#o-week-pills');
    if (wPills) { wPills.classList.add('hidden'); wPills.classList.remove('flex'); }
    if ($('#o-layer-note')) $('#o-layer-note').textContent = '【月层】整月历史规律；展示所选流月的全市场机会标的。';
    const bothWrap = $('#o-both-months') ? $('#o-both-months').closest('label') : null;
    if (bothWrap) bothWrap.classList.remove('hidden');
    if ($('#o-stab')) $('#o-stab').classList.remove('hidden');
    if ($('#o-cons')) $('#o-cons').classList.remove('hidden');

    $('#o-min').value = '3'; $('#o-sort').value = OPP_DEFAULT_SORT; $('#o-cap').value = '100';
    $('#o-grade').value = ''; $('#o-stab').value = ''; $('#o-cons').value = ''; $('#o-rate').value = ''; $('#o-both-months').checked = false;
    $('#o-next-rate').value = 'inherit'; $('#o-next-rate').classList.add('hidden');
    $('#o-cap-tier').value = '';
    $('#o-dir').value = 'up'; syncDirUi();
    syncOppSortUi();
    $('#o-search').value = ''; $('#o-rel').value = '';
    ['#o-es', '#o-clash', '#o-harmony', '#o-both', '#o-punish', '#o-harm', '#o-broken',
     '#o-delist', '#o-idx'].forEach((i) => $(i).checked = false);
    renderOpportunity(); saveFiltersNow(); };
  $('#o-export').onclick = exportOpportunity;
  initRvPops();
  $('#open-board').onclick = () => {
    S.segMode = false;
    document.body.classList.remove('segmode');
    const so = $('#seg-opts'); if (so) so.classList.add('hidden');
    const sb = $('#open-seg'); if (sb) sb.classList.remove('on');
    const ln = $('#o-layer-note'); if (ln) ln.textContent = '【月层】整月历史规律；展示所选流月的全市场机会标的。';
    setBoardMode(true, { push: true, forceRender: true });
  };
  $('#back-stock').onclick = () => {
    S.segMode = false;
    document.body.classList.remove('segmode');
    const so = $('#seg-opts'); if (so) so.classList.add('hidden');
    const sb = $('#open-seg'); if (sb) sb.classList.remove('on');
    setBoardMode(false, { push: true });
  };
  $('#open-today').onclick = () => { window.location.href = '/static/today.html'; };
  const n2Btn = $('#open-n2');
  if (n2Btn) n2Btn.onclick = () => { window.location.href = '/n2'; };
  $('#open-seg').onclick = () => {
    S.segMode = true;
    S.oppLayer = 'month';
    document.body.classList.add('segmode');
    const so = $('#seg-opts'); if (so) so.classList.remove('hidden');
    const sd = $('#seg-dir'); if (sd) sd.value = ($('#o-dir').value || 'up');
    $('#o-month').value = '0';
    if (S.cal) mpDraw();
    const ln = $('#o-layer-note'); if (ln) ln.textContent = '【100%段】本月同柱 100% 段：进行中 → 未到 → 已过去；每只票显示一条。';
    setBoardMode(true, { push: true, forceRender: true });
    const sb = $('#open-seg'); if (sb) sb.classList.add('on');
  };
  ['#seg-dir', '#seg-n', '#seg-len', '#seg-mean', '#seg-all'].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('change', () => { if (S.segMode) renderOpportunity(); });
  });
  const returnBoardBtn = $('#return-board-btn');
  if (returnBoardBtn) returnBoardBtn.onclick = () => setBoardMode(true, { push: true });
  const themeBtn = $('#theme-toggle');
  const applyTheme = (isLight) => {
    document.documentElement.classList.toggle('light', isLight);
    document.documentElement.classList.toggle('dark', !isLight);
    document.body.classList.toggle('theme-light', isLight);
    const icon = $('#theme-icon');
    if (icon) icon.textContent = isLight ? '🌙' : '☀️';
    if (themeBtn) themeBtn.title = isLight ? '当前浅色模式，点击切换到曜石黑深色' : '当前深色模式，点击切换到象牙白浅色';
  };
  const isLight = localStorage.getItem('bazi_theme') === 'light';
  applyTheme(isLight);
  if (themeBtn) {
    themeBtn.onclick = () => {
      const nowLight = !document.documentElement.classList.contains('light');
      applyTheme(nowLight);
      localStorage.setItem('bazi_theme', nowLight ? 'light' : 'dark');
      syncDayunTheme();
    };
  }
  const vSeg = $('#p-view-seg');
  if (vSeg) {
    vSeg.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => {
        S.pillarView = btn.dataset.v === 'pool' ? 'pool' : 'stock';
        syncSegUi('#p-view-seg', S.pillarView);
        renderPillar();
        saveFiltersNow();
      };
    });
  }
  const fgSeg = $('#flow-grain-seg');
  if (fgSeg) {
    fgSeg.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => {
        activateTab(btn.dataset.v === 'branch' ? 'branch' : 'pillar');
        saveFiltersNow();
      };
    });
  }
  const pSeg = $('#p-layer-seg');
  if (pSeg) {
    pSeg.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => {
        pSeg.querySelectorAll('button').forEach((b) => {
          b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
          b.classList.add('text-slate-400');
        });
        btn.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
        btn.classList.remove('text-slate-400');
        S.pillarLayer = btn.dataset.v;
        const notes = {
          month: '【月层】60 柱整月规律；本票视图取冻结 stats_pillar60（全部正式月）。',
          xun: '【旬层】60 柱 × 上/中/下旬 = 180 窗；本票视图取 xw_xun_rules（只收完整流月）。',
          week: '【段层】60 柱 × 6 个节气段 = 360 窗；本票视图取 xw_week_rules（只收完整流月）。'
        };
        if ($('#p-layer-note')) $('#p-layer-note').textContent = notes[S.pillarLayer] || '';
        renderPillar();
        saveFiltersNow();
      };
    });
  }
  const oSeg = $('#o-layer-seg');
  if (oSeg) {
    oSeg.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => {
        oSeg.querySelectorAll('button').forEach((b) => {
          b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
          b.classList.add('text-slate-400');
        });
        btn.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
        btn.classList.remove('text-slate-400');
        S.oppLayer = btn.dataset.v;
        S.oppWindowNo = ''; // reset window filter on layer change

        const xPills = $('#o-xun-pills');
        const wPills = $('#o-week-pills');
        if (xPills) {
          if (S.oppLayer === 'xun') {
            xPills.classList.remove('hidden');
            xPills.classList.add('flex');
            xPills.querySelectorAll('button').forEach((b, idx) => {
              if (idx === 0) { b.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm'); b.classList.remove('text-slate-400'); }
              else { b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm'); b.classList.add('text-slate-400'); }
            });
          } else {
            xPills.classList.add('hidden');
            xPills.classList.remove('flex');
          }
        }
        if (wPills) {
          if (S.oppLayer === 'week') {
            wPills.classList.remove('hidden');
            wPills.classList.add('flex');
            wPills.querySelectorAll('button').forEach((b, idx) => {
              if (idx === 0) { b.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm'); b.classList.remove('text-slate-400'); }
              else { b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm'); b.classList.add('text-slate-400'); }
            });
          } else {
            wPills.classList.add('hidden');
            wPills.classList.remove('flex');
          }
        }

        const notes = {
          month: '【月层】整月历史规律；展示所选流月的全市场机会标的。',
          xun: '【旬层】所选流月 × 上/中/下旬 3 个十日窗口机会排行；可按窗位聚焦查看。',
          week: '【段层】所选流月 × 6 个节气段窗口机会排行；可按段位聚焦查看。'
        };
        if ($('#o-layer-note')) $('#o-layer-note').textContent = notes[S.oppLayer] || '';

        const bothWrap = $('#o-both-months') ? $('#o-both-months').closest('label') : null;
        const stabSel = $('#o-stab');
        const consSel = $('#o-cons');
        if (S.oppLayer !== 'month') {
          if (bothWrap) bothWrap.classList.add('hidden');
          if (stabSel) stabSel.classList.add('hidden');
          if (consSel) consSel.classList.add('hidden');
        } else {
          if (bothWrap) bothWrap.classList.remove('hidden');
          if (stabSel) stabSel.classList.remove('hidden');
          if (consSel) consSel.classList.remove('hidden');
        }

        renderOpportunity();
        saveFiltersNow();
      };
    });
  }

  const oXunPills = $('#o-xun-pills');
  if (oXunPills) {
    oXunPills.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => {
        oXunPills.querySelectorAll('button').forEach((b) => {
          b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
          b.classList.add('text-slate-400');
        });
        btn.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
        btn.classList.remove('text-slate-400');
        S.oppWindowNo = btn.dataset.w || '';
        renderOpportunity();
      };
    });
  }

  const oWeekPills = $('#o-week-pills');
  if (oWeekPills) {
    oWeekPills.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => {
        oWeekPills.querySelectorAll('button').forEach((b) => {
          b.classList.remove('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
          b.classList.add('text-slate-400');
        });
        btn.classList.add('on', 'bg-indigo-600', 'text-white', 'shadow-sm');
        btn.classList.remove('text-slate-400');
        S.oppWindowNo = btn.dataset.w || '';
        renderOpportunity();
      };
    });
  }
  const btnLocateNow = $('#o-btn-locate-now');
  if (btnLocateNow) btnLocateNow.onclick = locateOppNow;
  $('#p-dir').addEventListener('change', () => { syncPillarDirUi(); renderPillar(); });
  ['#p-search', '#p-stem-el', '#p-branch-el', '#p-sort', '#p-min', '#p-tg', '#p-dir', '#p-rate'].forEach((id) => {
    $(id).addEventListener('change', debounce(renderPillar, 200));
    if (id === '#p-search') $(id).addEventListener('input', debounce(renderPillar, 300));
  });
  const ptg = $('#p-tg');
  if (ptg && ptg.options.length === 1) {
    ['比肩', '劫财', '食神', '伤官', '偏财', '正财', '七杀', '正官', '偏印', '正印']
      .forEach((g) => { const o = el('option', null, g); o.value = g; ptg.appendChild(o); });
  }
  ['#f-clash', '#f-harmony', '#f-both', '#f-punish', '#f-harm', '#f-broken'].forEach((id) =>
    $(id).addEventListener('change', renderPillar));
  $('#p-reset').onclick = () => { ['#p-search'].forEach((i) => $(i).value = '');
    ['#p-stem-el', '#p-branch-el', '#p-min', '#p-tg', '#p-rate'].forEach((i) => $(i).value = '');
    $('#p-dir').value = 'up'; syncPillarDirUi();
    $('#p-sort').value = 'pillar';
    ['#f-clash', '#f-harmony', '#f-both', '#f-punish', '#f-harm', '#f-broken'].forEach((i) =>
      $(i).checked = false);
    renderPillar(); saveFiltersNow(); };
  $('#mclose').onclick = () => $('#modal').classList.add('hidden');
  $('#modal').onclick = (e) => { if (e.target.id === 'modal') $('#modal').classList.add('hidden'); };
  const dJump = $('#drawer-jump');
  if (dJump) dJump.addEventListener('click', (e) => {
    const b = e.target.closest('[data-jump]');
    const target = b && document.getElementById(b.dataset.jump);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  const dClose = $('#drawer-close-btn'); if (dClose) dClose.onclick = () => closeOppDrawer();
  const dBotClose = $('#drawer-bottom-close'); if (dBotClose) dBotClose.onclick = () => closeOppDrawer();
  const sDrawer = $('#slide-drawer'); if (sDrawer) sDrawer.onclick = (e) => closeOppDrawer(e);
  // 十神历史涨跌页签：口径 / 地支 / 关系筛选
  const tgm = $('#tg-mode');
  if (tgm) tgm.querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      S.tgMode = b.dataset.v;
      syncSegUi(tgm, S.tgMode);
      renderTenGodStats();
      saveFiltersNow();
    };
  });
  if ($('#tg-branch')) $('#tg-branch').onchange = () => {
    S.tgBranch = $('#tg-branch').value; renderTenGodStats();
  };
  if ($('#tg-rel')) $('#tg-rel').onchange = () => {
    S.tgRel = $('#tg-rel').value; renderTenGodStats();
  };
  if ($('#tg-reset')) $('#tg-reset').onclick = () => {
    S.tgMode = 'ALL'; S.tgBranch = ''; S.tgRel = '';
    syncSegUi(tgm, 'ALL');
    $('#tg-branch').value = ''; $('#tg-rel').value = '';
    renderTenGodStats();
    saveFiltersNow();
  };
  $('#ds').querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      if (NAV.ready) switchDataset(b.dataset.ds, false, { push: true });
      // 启动期点击不丢弃：先记账，首屏完成后自动补切（否则刚启动切 ETF 会毫无反应）
      else NAV.pendingDs = b.dataset.ds;
    };
  });
  document.addEventListener('change', (e) => {
    const id = e.target && e.target.id;
    if (id && FILTER_IDS.includes(id)) saveFiltersNow();
  });
  document.addEventListener('input', (e) => {
    const id = e.target && e.target.id;
    if (id && FILTER_IDS.includes(id)) scheduleFiltersSave();
  });
  addEventListener('pagehide', saveFiltersNow);
  ['#dayun-tab', '#dayun-link'].forEach((sel) => {
    const a = $(sel);
    if (a) a.addEventListener('click', (e) => {
      if (!NAV.ready) { e.preventDefault(); return; }
      saveFiltersNow(); syncDayunLink();
    });
  });
  syncPillarDirUi();
  const st = readUrlState();
  await switchDataset(st.dataset, true, {
    fromInit: true,
    id: st.id || null,
    tab: st.tab,
    board: st.board,
    fromBoard: st.fromBoard,
    filters: savedFilters(st.dataset),
  });
  replaceView({ id: S.id, tab: st.tab, board: st.board, fromBoard: st.fromBoard });
  NAV.ready = true;
  window.__appReady = true;
  // 旧地址 /seg 会跳到 /?seg=1：这里直接进「100%段」，并把参数留在地址栏，刷新仍是这一段
  try {
    if (BOOT_SEG && $('#open-seg')) {
      $('#open-seg').click();
      const sp = new URLSearchParams(location.search);
      sp.set('seg', '1');
      history.replaceState(history.state, '', location.pathname + '?' + sp.toString() + location.hash);
    }
  } catch (e) { console.error('seg 入口初始化失败', e); }
  // 兑现启动期记下的切换意图（以最后一次点击为准；与当前数据集相同则无需动作）
  if (NAV.pendingDs && NAV.pendingDs !== S.dataset) {
    const pendingDs = NAV.pendingDs;
    NAV.pendingDs = null;
    switchDataset(pendingDs, false, { push: true }).catch((e) => console.error(e));
  }
  /* 数据库更新时间那行小字：必须在首屏闸门之后 fire-and-forget，
     绝不插进 NAV.ready 之前阻塞首屏；内部自带 catch，不抛未捕获异常。 */
  try { initDbStatusUi(); } catch (e) { /* 静默降级，不影响任何页签 */ }
  try { initAuthUi(); } catch (e) { console.error('认证 UI 初始化异常', e); }
  addEventListener('popstate', (e) => {
    applyHistoryState(readUrlState(), e.state).catch((err) => console.error(err));
  });
}

function initAuthUi() {
  const btnLogout = document.getElementById('btn-logout');
  if (btnLogout) {
    btnLogout.onclick = async () => {
      if (!confirm('确认退出系统安全登录？')) return;
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
      } catch (e) {}
      window.location.href = '/login';
    };
  }

  const btnChangePwd = document.getElementById('btn-change-pwd');
  const pwdModal = document.getElementById('pwd-modal');
  const pwdClose = document.getElementById('pwd-modal-close');
  const pwdCancel = document.getElementById('pwd-cancel-btn');
  const pwdSubmit = document.getElementById('pwd-submit-btn');
  const pwdOld = document.getElementById('pwd-old');
  const pwdNew = document.getElementById('pwd-new');
  const pwdConfirm = document.getElementById('pwd-confirm');
  const pwdErr = document.getElementById('pwd-err');
  const pwdOk = document.getElementById('pwd-ok');

  function openPwdModal() {
    if (!pwdModal) return;
    if (pwdOld) pwdOld.value = '';
    if (pwdNew) pwdNew.value = '';
    if (pwdConfirm) pwdConfirm.value = '';
    if (pwdErr) { pwdErr.textContent = ''; pwdErr.classList.add('hidden'); }
    if (pwdOk) { pwdOk.textContent = ''; pwdOk.classList.add('hidden'); }
    pwdModal.classList.remove('hidden');
    setTimeout(() => { if (pwdOld) pwdOld.focus(); }, 60);
  }

  function closePwdModal() {
    if (pwdModal) pwdModal.classList.add('hidden');
  }

  if (btnChangePwd) btnChangePwd.onclick = openPwdModal;
  if (pwdClose) pwdClose.onclick = closePwdModal;
  if (pwdCancel) pwdCancel.onclick = closePwdModal;

  if (pwdSubmit) {
    pwdSubmit.onclick = async () => {
      const oldVal = (pwdOld && pwdOld.value) || '';
      const newVal = (pwdNew && pwdNew.value) || '';
      const confirmVal = (pwdConfirm && pwdConfirm.value) || '';

      if (pwdErr) pwdErr.classList.add('hidden');
      if (pwdOk) pwdOk.classList.add('hidden');

      if (!oldVal) {
        pwdErr.textContent = '请输入当前原密码';
        pwdErr.classList.remove('hidden');
        if (pwdOld) pwdOld.focus();
        return;
      }
      if (!newVal || newVal.length < 6) {
        pwdErr.textContent = '新密码长度不能少于 6 位';
        pwdErr.classList.remove('hidden');
        if (pwdNew) pwdNew.focus();
        return;
      }
      if (newVal !== confirmVal) {
        pwdErr.textContent = '两次输入的新密码不一致';
        pwdErr.classList.remove('hidden');
        if (pwdConfirm) pwdConfirm.focus();
        return;
      }
      if (oldVal === newVal) {
        pwdErr.textContent = '新密码不能与原密码相同';
        pwdErr.classList.remove('hidden');
        if (pwdNew) pwdNew.focus();
        return;
      }

      pwdSubmit.disabled = true;
      pwdSubmit.textContent = '修改中...';
      try {
        const resp = await fetch('/api/auth/change_password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_password: oldVal, new_password: newVal })
        });
        const res = await resp.json().catch(() => ({}));
        if (resp.ok && res.success) {
          pwdOk.textContent = '✅ 密码修改成功！请牢记新密码。';
          pwdOk.classList.remove('hidden');
          setTimeout(() => {
            closePwdModal();
          }, 1500);
        } else {
          pwdErr.textContent = res.error || res.message || '密码修改失败';
          pwdErr.classList.remove('hidden');
        }
      } catch (err) {
        pwdErr.textContent = '请求异常，请检查服务连接';
        pwdErr.classList.remove('hidden');
      } finally {
        pwdSubmit.disabled = false;
        pwdSubmit.textContent = '确认修改';
      }
    };
  }
}

init().catch((e) => {
  document.body.insertBefore(el('div', 'banner err', '本地库读取失败：' + esc(e.message)),
    document.body.firstChild);
});
