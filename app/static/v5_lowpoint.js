/* 「八 · 月内低点研究」—— 1:1 复刻流月 100% 极值看板与月内走势形态叠加
   纯实时数据驱动 · 零硬编码 · 与主站顶栏标的双向自动联动 */
(function () {
  'use strict';

// -------------------------------------------------------------
// 状态与数据管理 (纯实时，零写死)
// -------------------------------------------------------------
// 首拉跟随地址栏里主站的标的；没有就等主站 onV5StockChange 同步，不写死任何股票
let currentStock = new URLSearchParams(location.search).get('id') || '';
let currentDim = 'P60'; // 默认流月模式，直接展示100%极值
let currentEncodingMode = 'neutral';
let rawGroupsData = [];
let activeRows = [];
let selectedGroupKey = null;
let currentProbFilter = 'all'; // 'all', '100_all', '100_up', '100_down'
let currentSort = 'up_rate_desc'; // 默认按胜率降序，100%置顶
let v5FetchSeq = 0;
let v5Restoring = true;       // 启动恢复控件期间不要用默认值覆盖会话快照
let v5UserStateReady = false; // 主站标的选择接管前，后台首拉不写快照
let v5MainSynced = false;     // 主站是否已经通过 onV5StockChange 同步过标的
let resolvedSymbol = '';      // 服务端解析后的规范代码（带后缀），live 叠加按它取数
let liveMonth = null;         // 当月进行中数据（/static/live_month.json，由夜间日更脚本生成）
let liveMonthPromise = null;

/* 第八栏自己的视图状态：切走再回来不会变，刷新/后退后按会话快照恢复 */
const V5_UI_KEY = 'bazi_v5_ui_v1';
const V5_PROB_FILTERS = ['all', '100_all', '100_up', '100_down'];
const V5_SORTS = ['up_rate_desc', 'up_rate_asc', 'excess_early', 'excess_late', 'n_desc', 'key'];
function readV5Ui() {
  try {
    const x = JSON.parse(sessionStorage.getItem(V5_UI_KEY) || '{}');
    return x && typeof x === 'object' ? x : {};
  } catch (e) { return {}; }
}
let savedV5Ui = readV5Ui();
if (savedV5Ui.dim === 'B12' || savedV5Ui.dim === 'P60') currentDim = savedV5Ui.dim;
if (savedV5Ui.encoding === 'shape' || savedV5Ui.encoding === 'neutral') currentEncodingMode = savedV5Ui.encoding;
if (V5_PROB_FILTERS.includes(savedV5Ui.probFilter)) currentProbFilter = savedV5Ui.probFilter;
if (V5_SORTS.includes(savedV5Ui.sort)) currentSort = savedV5Ui.sort;
if (savedV5Ui.stock === currentStock && savedV5Ui.groupKey) selectedGroupKey = savedV5Ui.groupKey;

function saveV5Ui() {
  if (v5Restoring) return;
  savedV5Ui = {
    stock: currentStock, dim: currentDim, encoding: currentEncodingMode,
    probFilter: currentProbFilter, sort: currentSort, groupKey: selectedGroupKey,
  };
  try { sessionStorage.setItem(V5_UI_KEY, JSON.stringify(savedV5Ui)); } catch (e) {}
}

function setEncodingMode(mode) {
  currentEncodingMode = mode;
  document.getElementById('btn-mode-neutral').className = mode === 'neutral' ?
    'px-2 py-1 rounded bg-indigo-600 text-white font-medium' : 'px-2 py-1 rounded text-slate-400 hover:text-white';
  document.getElementById('btn-mode-shape').className = mode === 'shape' ?
    'px-2 py-1 rounded bg-indigo-600 text-white font-medium' : 'px-2 py-1 rounded text-slate-400 hover:text-white';
  renderTable();
  saveV5Ui();
}

function changeStock(sym) {
  if (!sym) return;
  sym = String(sym).trim();
  const prev = currentStock;
  currentStock = sym;
  if (prev !== sym) {
    selectedGroupKey = (savedV5Ui.stock === sym && savedV5Ui.groupKey) ? savedV5Ui.groupKey : null;
  }
  v5UserStateReady = true;
  const inputEl = document.getElementById('input-stock');
  if (inputEl) inputEl.value = sym;
  saveV5Ui();
  fetchData();
}

function submitStock() {
  const val = document.getElementById('input-stock').value.trim();
  if (val) changeStock(val);
}

function changeDim(dim, opts = {}) {
  currentDim = dim;
  const isP60 = dim === 'P60';
  document.getElementById('btn-dim-p60').className = isP60 ?
    'px-2.5 py-1 rounded bg-indigo-600 text-white font-bold flex items-center gap-1' :
    'px-2.5 py-1 rounded text-slate-400 hover:text-white flex items-center gap-1';
  document.getElementById('btn-dim-b12').className = !isP60 ?
    'px-2.5 py-1 rounded bg-indigo-600 text-white font-bold' :
    'px-2.5 py-1 rounded text-slate-400 hover:text-white';

  document.getElementById('filter-pills-box').style.display = isP60 ? 'inline-flex' : 'none';
  saveV5Ui();
  if (!opts.noFetch) fetchData();
}

function setProbFilter(flt) {
  currentProbFilter = flt;
  const btns = {
    'all': 'btn-flt-all',
    '100_all': 'btn-flt-100all',
    '100_up': 'btn-flt-100up',
    '100_down': 'btn-flt-100down'
  };
  Object.keys(btns).forEach(k => {
    const el = document.getElementById(btns[k]);
    if (el) {
      if (k === flt) {
        el.className = 'px-2 py-0.5 rounded bg-indigo-600 text-white font-bold';
      } else {
        el.className = 'px-2 py-0.5 rounded text-slate-400 hover:text-white';
      }
    }
  });
  applyFilterAndSort();
  saveV5Ui();
}

function changeSort(val) {
  currentSort = val;
  applyFilterAndSort();
  saveV5Ui();
}

function toggleSort() {
  if (currentSort === 'excess_early') {
    currentSort = 'excess_late';
  } else {
    currentSort = 'excess_early';
  }
  document.getElementById('sort-selector').value = currentSort;
  applyFilterAndSort();
  saveV5Ui();
}

// -------------------------------------------------------------
// 数据拉取与预处理
// -------------------------------------------------------------
function ensureLiveMonth() {
  // 当月进行中切片：异步、只拉一次；拿不到就静默禁用叠加，不影响历史视图
  if (liveMonthPromise) return liveMonthPromise;
  liveMonthPromise = fetch('/static/live_month.json', { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      if (j && j.schema === 'v5_live_month.v1' && j.stocks) liveMonth = j;
      return !!liveMonth;
    })
    .catch(() => { liveMonthPromise = null; return false; });
  return liveMonthPromise;
}

// 切片冻结口径：页头要标出，避免把「没有数据」误读成「没有规律」
let v5Meta = null;
let v5MetaPromise = null;
let v5EmptyReason = '';

function ensureV5Meta() {
  if (v5Meta) return Promise.resolve(v5Meta);
  if (!v5MetaPromise) {
    v5MetaPromise = fetch('/api/v5/status')
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (j && j.available) v5Meta = j; return v5Meta; })
      .catch(() => null);
  }
  return v5MetaPromise;
}

function cutoffSuffix() {
  if (!v5Meta) return '';
  return ` · 数据截止 ${v5Meta.data_cutoff} · 最后完整流月 ${v5Meta.last_formal_month}`;
}

// 结论里的 ✅/⚠️ 是同一个展示门槛，鼠标悬停时把规则说清楚
const GATE_TIP = '本页集中度提示：最低点落在月初、最高点落在月末的次数占比 ≥60% 标 ✅，不到 60% 标 ⚠️（「分散」指时间不集中，不是数据出错）。60% 只是展示门槛，不是统计显著性。';

let monthPathsSym = '';
let monthPathsData = null;
let monthPathsPromise = null;

function ensureMonthPaths() {
  // 逐日路径（同原点视图底座）：按股票取一个小 JSON，缓存；ETF 等缺失时回退旧骨架图
  const key6 = String(resolvedSymbol || currentStock || '').split('.')[0];
  if (!key6) return Promise.resolve(null);
  if (monthPathsSym === key6 && monthPathsData) return Promise.resolve(monthPathsData);
  if (monthPathsPromise && monthPathsSym === key6) return monthPathsPromise;
  monthPathsSym = key6;
  monthPathsData = null;
  monthPathsPromise = fetch(`/static/month_paths/${key6}.json`, { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : null))
    .then((j) => {
      if (j && j.months) monthPathsData = j.months;
      return monthPathsData;
    })
    .catch(() => null);
  return monthPathsPromise;
}

async function fetchData() {
  const my = ++v5FetchSeq;
  const stock = currentStock, dim = currentDim;
  const banner = document.getElementById('banner-text');
  if (!stock) {
    banner.innerText = '请先在顶栏选择标的';
    return;
  }
  banner.innerText = `正在读取 ${stock} (${dim}) 数据...`;
  try {
    const res = await fetch(`/api/v5/groups?id=${encodeURIComponent(stock)}&dimension=${dim}`);
    const json = await res.json();
    if (my !== v5FetchSeq) return;   // 切股票/维度很快时，旧响应不能覆盖新视图
    if (json.error) {
      banner.innerHTML = `<span class="text-red-400 font-bold">读取失败: ${json.error}</span>`;
      rawGroupsData = [];
      activeRows = [];
      selectedGroupKey = null;
      v5EmptyReason = '';
      renderTable();
      renderExtreme100Section();
      renderDetailDrawer();
      return;
    }
    rawGroupsData = json.rows || [];
    resolvedSymbol = json.symbol || stock;
    winExpandKey = null;
    winRowsCache = {};
    winRowsLoading = {};
    winSel = null;
    await ensureV5Meta();
    if (my !== v5FetchSeq) return;
    const dimCn = dim === 'P60' ? '60 流月柱' : '12 月支';
    const cutCn = v5Meta ? `（数据截止 ${v5Meta.data_cutoff}，最后完整流月 ${v5Meta.last_formal_month}）` : '';
    v5EmptyReason = rawGroupsData.length ? '' : (json.empty_reason
      || (json.family === 'INDEX'
        ? 'V5 对指数只保留逐月低点事实，没有 12 月支 / 60 流月柱分组统计。'
        : `该标的在 V5 冻结切片里没有正式流月${cutCn}。切片导出后上市的新股/新 ETF 还没有走完一个完整流月，所以没有可拆解的 ${dimCn}分组。`));
    banner.innerHTML = `已接通 <b class="text-white">${json.symbol}</b> · ${json.family} 族系 · ${json.dimension} 分组 · 共 ${rawGroupsData.length} 组有效记录`;
    document.getElementById('banner-stats').innerText =
      `门槛: n≥${json.min_n} · 条件: ${json.condition_label || 'ALL_VALID'}${cutoffSuffix()}`;
    document.getElementById('table-title').innerText = `${json.symbol} · ${dim === 'P60' ? '60流月柱' : '12月支'} 分组实测统计`;

    renderExtreme100Section();

    // 刷新/切回时优先保留已选分组；只有它不在新数据里才回默认
    const keepSelected = selectedGroupKey && rawGroupsData.some((r) => r.group_key === selectedGroupKey);
    if (!keepSelected) {
      const up100 = rawGroupsData.find((r) => r.up_rate === 1.0);
      if (up100 && dim === 'P60') {
        selectedGroupKey = up100.group_key;
      } else if (rawGroupsData.length > 0) {
        selectedGroupKey = rawGroupsData[0].group_key;
      } else {
        selectedGroupKey = null;
      }
    }

    applyFilterAndSort();
    renderLiveAnnotation();
    renderDetailDrawer();
    // 当月进行中数据与逐日路径都是异步加载的：到达后补一次抽屉重绘（保持已选分组）
    Promise.all([ensureLiveMonth(), ensureMonthPaths()]).then(() => {
      if (my !== v5FetchSeq) return;
      if (selectedGroupKey) renderDetailDrawer();
    });
    if (v5UserStateReady) saveV5Ui();
  } catch (err) {
    if (my !== v5FetchSeq) return;
    banner.innerHTML = `<span class="text-red-400 font-bold">接口未接通: ${err.message}</span>`;
  }
}

// -------------------------------------------------------------
// 渲染顶部 100% 极值几率专区（快速通道）
// -------------------------------------------------------------
function renderExtreme100Section() {
  const sec = document.getElementById('extreme-100-section');
  const sumEl = document.getElementById('extreme-100-summary');
  const cntEl = document.getElementById('extreme-100-content');

  if (currentDim !== 'P60') {
    sumEl.innerHTML = '<span class="text-slate-500">当前在月支维度</span>';
    cntEl.innerHTML = `
      <div class="flex items-center justify-between text-xs text-slate-400 py-1">
        <span>💡 <b>提示</b>：12月支为每年重复（历年 20~25 次），上涨占比被均值平滑（通常 40%~60%）；若想直接发掘 <b>100% 几率（全涨 / 全跌）</b> 的周期规律，请切换至【60流月柱】。</span>
        <button onclick="changeDim('P60')" class="px-2.5 py-1 rounded bg-indigo-600 hover:bg-indigo-500 text-white font-medium text-xs whitespace-nowrap">
          立即查看 60流月柱 100% 几率
        </button>
      </div>
    `;
    return;
  }

  const up100 = rawGroupsData.filter(r => r.up_rate === 1.0);
  const down100 = rawGroupsData.filter(r => r.up_rate === 0.0);

  sumEl.innerHTML = `共发现 <b>${up100.length + down100.length}</b> 个 100% 极值流月柱 (🔴 ${up100.length} 全涨 · 🟢 ${down100.length} 全跌)`;

  let upPillsHtml = up100.map(r => {
    const isSel = r.group_key === selectedGroupKey;
    return `
      <button onclick="quickSelectGroup('${r.group_key}')" class="px-2.5 py-1 rounded border text-xs font-mono transition-all flex items-center gap-1.5 ${isSel ? 'bg-red-600 text-white border-red-400 ring-2 ring-red-400/40 shadow-lg' : 'bg-red-950/40 border-red-800 text-red-200 hover:bg-red-900/60 hover:text-white'}">
        <span class="w-2 h-2 rounded-full bg-red-400"></span>
        <b class="text-sm font-sans">${r.group_key}</b>
        <span class="text-[10px] opacity-80">${r.n_windows}次全涨</span>
      </button>
    `;
  }).join('');

  let downPillsHtml = down100.map(r => {
    const isSel = r.group_key === selectedGroupKey;
    return `
      <button onclick="quickSelectGroup('${r.group_key}')" class="px-2.5 py-1 rounded border text-xs font-mono transition-all flex items-center gap-1.5 ${isSel ? 'bg-emerald-600 text-white border-emerald-400 ring-2 ring-emerald-400/40 shadow-lg' : 'bg-emerald-950/40 border-emerald-800 text-emerald-200 hover:bg-emerald-900/60 hover:text-white'}">
        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
        <b class="text-sm font-sans">${r.group_key}</b>
        <span class="text-[10px] opacity-80">${r.n_windows}次全跌</span>
      </button>
    `;
  }).join('');

  cntEl.innerHTML = `
    <div class="grid grid-cols-1 md:grid-cols-2 gap-3 pt-1">
      <div class="p-2.5 rounded bg-red-950/20 border border-red-900/40 space-y-2">
        <div class="flex items-center justify-between text-xs font-bold text-red-400">
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-red-500"></span> 🔴 100% 全涨流月（上涨占比 100%）</span>
          <span class="font-mono text-[11px] text-red-300/80">${up100.length} 组</span>
        </div>
        <div class="flex flex-wrap gap-2">
          ${upPillsHtml || '<span class="text-xs text-slate-500">该标的历史无 100% 全涨流月</span>'}
        </div>
      </div>

      <div class="p-2.5 rounded bg-emerald-950/20 border border-emerald-900/40 space-y-2">
        <div class="flex items-center justify-between text-xs font-bold text-emerald-400">
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> 🟢 100% 全跌流月（下跌占比 100%）</span>
          <span class="font-mono text-[11px] text-emerald-300/80">${down100.length} 组</span>
        </div>
        <div class="flex flex-wrap gap-2">
          ${downPillsHtml || '<span class="text-xs text-slate-500">该标的历史无 100% 全跌流月</span>'}
        </div>
      </div>
    </div>
  `;
}

function quickSelectGroup(key) {
  selectedGroupKey = key;
  // 若当前筛选把该组隐藏了，则重置为全部或100%极值
  const inFiltered = activeRows.some(r => r.group_key === key);
  if (!inFiltered) {
    currentProbFilter = 'all';
    setProbFilter('all');
  } else {
    renderTable();
  }
  renderExtreme100Section();
  renderDetailDrawer();
  saveV5Ui();

  // 平滑滚动至表格行
  const tr = document.getElementById(`row-${key}`);
  if (tr) {
    tr.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

// -------------------------------------------------------------
// 过滤与排序应用
// -------------------------------------------------------------
function applyFilterAndSort() {
  let list = [...rawGroupsData];

  // 1. 过滤
  if (currentDim === 'P60') {
    if (currentProbFilter === '100_all') {
      list = list.filter(r => r.up_rate === 1.0 || r.up_rate === 0.0);
    } else if (currentProbFilter === '100_up') {
      list = list.filter(r => r.up_rate === 1.0);
    } else if (currentProbFilter === '100_down') {
      list = list.filter(r => r.up_rate === 0.0);
    }
  }

  // 2. 排序
  list.sort((a, b) => {
    if (currentSort === 'up_rate_desc') {
      return (b.up_rate - a.up_rate) || (b.n_windows - a.n_windows);
    } else if (currentSort === 'up_rate_asc') {
      return (a.up_rate - b.up_rate) || (b.n_windows - a.n_windows);
    } else if (currentSort === 'excess_early') {
      return a.mean_excess_earliness - b.mean_excess_earliness;
    } else if (currentSort === 'excess_late') {
      return b.mean_excess_earliness - a.mean_excess_earliness;
    } else if (currentSort === 'n_desc') {
      return b.n_windows - a.n_windows;
    } else {
      return 0; // 默认保留接口返回顺序
    }
  });

  activeRows = list;

  // 保证选中的行在当前视图中存在
  if (!activeRows.some(r => r.group_key === selectedGroupKey) && activeRows.length > 0) {
    selectedGroupKey = activeRows[0].group_key;
  }

  renderTable();
  renderDetailDrawer();
}

// -------------------------------------------------------------
// 绘制月内归一化走势形态叠加图 (Normalized Path Overlay)
// -------------------------------------------------------------
// -------------------------------------------------------------
// 同原点涨幅视图：历史每年一根真实逐日线 + 平均线 + 当月进行中
// -------------------------------------------------------------
function interpPct(pts, x) {
  if (!pts || !pts.length) return null;
  if (x <= pts[0][0]) return pts[0][1];
  for (let i = 1; i < pts.length; i++) {
    if (x <= pts[i][0]) {
      const x0 = pts[i - 1][0], y0 = pts[i - 1][1];
      const x1 = pts[i][0], y1 = pts[i][1];
      const t = x1 === x0 ? 0 : (x - x0) / (x1 - x0);
      return y0 + (y1 - y0) * t;
    }
  }
  return pts[pts.length - 1][1];
}

function renderSameOriginOverlay(subOccs, isUp, containerId, live, liveHint, paths) {
  const box = document.getElementById(containerId);
  if (!box) return;
  const W = 360, H = 136;
  const padL = 34, padR = 22, padT = 10, padB = 22;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const series = subOccs.map((o) => {
    const m = paths[o.month_id];
    const pts = [];
    for (let i = 0; i < m.pts.length; i += 2) pts.push([m.pts[i], m.pts[i + 1] / 10000]);
    return { o, pts, e: m.e / 10000, lk: m.lk, hk: m.hk };
  });
  const livePts = [];
  if (live && liveMonth && Array.isArray(live.pc)) {
    livePts.push([0, 0]);
    for (let i = 0; i < live.pc.length; i++) {
      const di = live.p[i * 2];
      const x = (liveMonth.x || [])[di];
      if (typeof x === 'number') livePts.push([x, live.pc[i] / 10000]);
    }
  }

  const ys = [];
  series.forEach((s) => s.pts.forEach((q) => ys.push(q[1])));
  livePts.forEach((q) => ys.push(q[1]));
  ys.push(0);
  let ymin = Math.min.apply(null, ys), ymax = Math.max.apply(null, ys);
  const ypad = (ymax - ymin) * 0.12 || 0.02;
  ymin -= ypad; ymax += ypad;
  const mapX = (x) => (padL + Math.max(0, Math.min(1, x)) * innerW).toFixed(1);
  const mapY = (y) => (padT + (1 - (y - ymin) / (ymax - ymin)) * innerH).toFixed(1);

  const span = ymax - ymin;
  const step = span > 0.5 ? 0.2 : span > 0.26 ? 0.1 : span > 0.13 ? 0.05 : 0.02;
  let grid = '';
  const t0 = Math.ceil(ymin / step) * step;
  for (let v = t0; v <= ymax + 1e-9; v += step) {
    const isZero = Math.abs(v) < 1e-9;
    grid += `<line x1="${padL}" y1="${mapY(v)}" x2="${padL + innerW}" y2="${mapY(v)}" stroke="${isZero ? '#64748b' : '#1e293b'}" stroke-width="1"${isZero ? ' stroke-dasharray="4,3"' : ''}/>`;
    grid += `<text x="${padL - 5}" y="${Number(mapY(v)) + 3}" font-size="8" fill="${isZero ? '#94a3b8' : '#64748b'}" text-anchor="end">${v > 0 ? '+' : ''}${(v * 100).toFixed(0)}%</text>`;
  }

  const lines = series.map((s) => {
    const d = s.pts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
    const tip = `${s.o.month_id} (${s.o.dir} ${(s.e * 100).toFixed(1)}%)\n低点: 第${s.lk}日 · 高点: 第${s.hk}日`;
    const color = isUp ? 'rgba(244,63,94,0.55)' : 'rgba(56,189,248,0.5)';
    return `<path id="path-${containerId}-${s.o.year}" d="${d}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round" class="cursor-pointer transition-all hover:stroke-white hover:stroke-[3]"><title>${tip}</title></path>`;
  }).join('');

  const gridX = [];
  for (let i = 0; i <= 24; i++) gridX.push(i / 24);
  const meanPts = gridX.map((x) => {
    const vals = series.map((s) => interpPct(s.pts, x)).filter((v) => v != null);
    return [x, vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0];
  });
  const meanD = meanPts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
  const meanEnd = meanPts[meanPts.length - 1][1];

  let liveSvg = '', liveLegend = '';
  let devPp = null;
  if (livePts.length >= 2) {
    const ld = livePts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
    const last = livePts[livePts.length - 1];
    const endX = Number(mapX(last[0])), endY = Number(mapY(last[1]));
    const retTxt = live.r == null ? '—' : `${live.r > 0 ? '+' : ''}${(live.r * 100).toFixed(2)}%`;
    const retCol = (live.r || 0) >= 0 ? '#f87171' : '#34d399';
    const anchor = endX > W - 92 ? 'end' : 'start';
    const labelX = anchor === 'end' ? endX - 5 : endX + 5;
    liveSvg = `
      <path d="${ld}" fill="none" stroke="#22d3ee" stroke-width="7" stroke-linejoin="round" stroke-linecap="round" opacity="0.16"/>
      <path d="${ld}" fill="none" stroke="#22d3ee" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="5,2.6"/>
      <circle cx="${endX}" cy="${endY}" r="3.2" fill="#22d3ee" stroke="#082f49" stroke-width="1.2"/>
      <text x="${labelX}" y="${Math.max(padT + 7, endY - 6)}" font-size="8.5" font-weight="bold" fill="${retCol}" text-anchor="${anchor}">当月 ${retTxt}</text>
    `;
    liveLegend = `<span class="flex items-center gap-1" title="当月真实逐日收盘（复权），首日开盘=0%"><span class="inline-block" style="width:11px;border-top:2px dashed #22d3ee"></span> 青蓝虚线 = 本月进行中（截至 ${liveMonth.data_latest} · 已 ${liveMonth.days} 个交易日）</span>`;
    let sum = 0, cnt = 0;
    livePts.forEach((q) => {
      if (q[0] <= 0) return;
      const mv = interpPct(meanPts, q[0]);
      if (mv != null) { sum += Math.abs(q[1] - mv); cnt++; }
    });
    if (cnt) devPp = (sum / cnt) * 100;
  }

  const yearPillsHtml = series.map((s) => {
    const pct = (s.e * 100).toFixed(1);
    const sign = s.e > 0 ? `+${pct}%` : `${pct}%`;
    const pillColor = s.o.dir === '涨' ? 'text-red-300 border-red-900/60 bg-red-950/40' : 'text-emerald-300 border-emerald-900/60 bg-emerald-950/40';
    return `<span onmouseenter="highlightPath('${containerId}', '${s.o.year}', true)" onmouseleave="highlightPath('${containerId}', '${s.o.year}', false)" class="px-1.5 py-0.5 rounded border text-[10px] font-mono cursor-pointer transition hover:bg-slate-800 hover:text-white ${pillColor}" title="${s.o.month_id} · 首日开盘→末日收盘 ${sign} · 低点第${s.lk}日 · 高点第${s.hk}日">${s.o.year}年 (${sign})</span>`;
  }).join('');

  const lks = series.map((s) => s.lk).sort((a, b) => a - b);
  const hks = series.map((s) => s.hk).sort((a, b) => a - b);
  const med = (arr) => (arr.length ? (arr.length % 2 ? arr[Math.floor(arr.length / 2)] : (arr[arr.length / 2 - 1] + arr[arr.length / 2]) / 2) : null);
  const medLowK = med(lks), medHighK = med(hks);
  const allFirst = lks.every((k) => k <= 3);
  const consensusTxt = allFirst
    ? `<span class="text-emerald-400 font-bold">【波形高度一致】</span> 历次全部在开月前 3 天见底`
    : `历史中枢：低点第 <b>${medLowK}</b> 日 · 高点第 <b>${medHighK}</b> 日（${series.length} 次中位）`;

  let liveStats = '';
  if (livePts.length >= 2 && live) {
    const retTxt = live.r == null ? '—' : `${live.r > 0 ? '+' : ''}${(live.r * 100).toFixed(2)}%`;
    const verdict = devPp == null ? '—' : devPp <= 2 ? '<span class="text-emerald-300 font-bold">高度贴合平均线</span>' : devPp <= 5 ? '<span class="text-amber-300 font-bold">大体跟随平均线</span>' : '<span class="text-rose-300 font-bold">明显偏离平均线</span>';
    const lowCmp = (live.lk == null || medLowK == null) ? '—' : (live.lk < medLowK ? `比历史中枢早 ${medLowK - live.lk} 日` : live.lk > medLowK ? `比历史中枢晚 ${live.lk - medLowK} 日` : '与历史中枢同日');
    liveStats = `
      <div class="text-[10px] text-cyan-100/90 bg-cyan-950/30 border border-cyan-900/50 rounded px-1.5 py-1 leading-relaxed space-y-0.5">
        <div>• <b>本月对照</b>（截至 ${liveMonth.data_latest}）：累计 <b>${retTxt}</b>（首日开盘→最新收盘，复权）· 低点第 <b>${live.lk ?? '—'}</b> 日（${lowCmp}）· 高点第 <b>${live.hk ?? '—'}</b> 日</div>
        <div>• <b>与历史平均线</b>：${verdict}${devPp == null ? '' : `（平均偏离 ${devPp.toFixed(1)} 个百分点）`}</div>
        <div class="text-cyan-300/60">口径：所有线都是复权收盘价、各自月内首日开盘=0%；低/高点序号按收盘极值；横轴=月内日历进度；当月线为进行中，月末才落定。</div>
      </div>`;
  }

  box.innerHTML = `
    <div class="relative bg-slate-950 rounded border border-slate-800 p-2 space-y-2">
      <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto overflow-visible font-mono">
        <line x1="${mapX(1 / 3)}" y1="${padT}" x2="${mapX(1 / 3)}" y2="${padT + innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        <line x1="${mapX(2 / 3)}" y1="${padT}" x2="${mapX(2 / 3)}" y2="${padT + innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        ${grid}
        ${lines}
        <path d="${meanD}" fill="none" stroke="#fbbf24" stroke-width="2.6" stroke-linecap="round"/>
        <text x="${Number(mapX(1)) - 2}" y="${Number(mapY(meanEnd)) - 6}" font-size="8.5" fill="#fbbf24" text-anchor="end">平均 ${meanEnd > 0 ? '+' : ''}${(meanEnd * 100).toFixed(1)}%</text>
        ${liveSvg}
        <text x="${mapX(0)}" y="${H - 4}" font-size="9" fill="#64748b" text-anchor="start">月初</text>
        <text x="${mapX(0.5)}" y="${H - 4}" font-size="9" fill="#64748b" text-anchor="middle">月中</text>
        <text x="${mapX(1)}" y="${H - 4}" font-size="9" fill="#64748b" text-anchor="end">月末</text>
      </svg>

      <div class="flex flex-wrap items-center justify-between gap-1 text-[10px] text-slate-400 px-1 border-t border-slate-900 pt-1.5">
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 bg-amber-400 inline-block"></span> 黄线 = 历史平均线</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 inline-block" style="background:${isUp ? 'rgba(244,63,94,0.75)' : 'rgba(56,189,248,0.7)'}"></span> 细线 = 历史各年真实逐日走势</span>
        ${liveLegend}
        ${liveHint ? `<span class="flex items-center gap-1 text-cyan-300/70"><span class="inline-block" style="width:11px;border-top:2px dashed #22d3ee;opacity:.6"></span> ${liveHint}</span>` : ''}
      </div>

      <div class="text-[10px] text-slate-500 px-1 leading-relaxed">纵轴 = 累计涨跌幅%（<b class="text-slate-400">各自月内首日开盘 = 0%</b>，从同一起跑线出发）；${consensusTxt}</div>

      ${liveStats}

      <div class="flex flex-wrap gap-1 px-1">${yearPillsHtml}</div>
    </div>
  `;
}

function renderPathOverlay(subOccs, isUp, containerId, live, liveHint, paths) {
  if (paths && subOccs && subOccs.length
      && subOccs.every((o) => paths[o.month_id])) {
    renderSameOriginOverlay(subOccs, isUp, containerId, live, liveHint, paths);
    return;
  }
  const box = document.getElementById(containerId);
  if (!subOccs || !subOccs.length) {
    box.innerHTML = '<div class="text-xs text-slate-500 py-2">无样本数据</div>';
    return;
  }
  const W = 360, H = 105;
  const padL = 26, padR = 20, padT = 12, padB = 22;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const mapX = (x) => (padL + Number(x || 0) * innerW).toFixed(1);
  const mapY = (y) => (padT + (1 - Number(y || 0)) * innerH).toFixed(1);

  // 历史各年折线
  const lines = subOccs.map(o => {
    const xLow = mapX(o.low_fraction);
    const xHigh = mapX(o.high_fraction);
    const xStart = mapX(0);
    const xEnd = mapX(1);

    const yLow = mapY(0);
    const yHigh = mapY(1);
    const yStart = mapY(o.low_k === 1 ? 0 : o.high_k === 1 ? 1 : (isUp ? 0.35 : 0.65));
    const yEnd = mapY(o.high_k === o.n ? 1 : o.low_k === o.n ? 0 : (isUp ? 0.85 : 0.15));

    let pathD = '';
    if (o.low_k <= o.high_k) {
      pathD = `M ${xStart} ${yStart} L ${xLow} ${yLow} L ${xHigh} ${yHigh} L ${xEnd} ${yEnd}`;
    } else {
      pathD = `M ${xStart} ${yStart} L ${xHigh} ${yHigh} L ${xLow} ${yLow} L ${xEnd} ${yEnd}`;
    }
    const color = isUp ? 'rgba(244,63,94,0.45)' : 'rgba(56,189,248,0.45)';
    const tip = `${o.month_id} (${o.dir} ${((o.ret||0)*100).toFixed(1)}%)\n见底: 第${o.low_k}日 (${o.low_day})\n冲顶: 第${o.high_k}日 (${o.high_day})`;
    return `<path id="path-${containerId}-${o.year}" d="${pathD}" fill="none" stroke="${color}" stroke-width="1.8" class="cursor-pointer transition-all hover:stroke-white hover:stroke-[3]" title="${tip}"><title>${tip}</title></path>`;
  }).join('');

  // 计算中枢形态（Median Backbone）
  const medLowX = subOccs.map(o => o.low_fraction).sort((a,b)=>a-b)[Math.floor(subOccs.length/2)];
  const medHighX = subOccs.map(o => o.high_fraction).sort((a,b)=>a-b)[Math.floor(subOccs.length/2)];
  const medLowK = subOccs.map(o => o.low_k).sort((a,b)=>a-b)[Math.floor(subOccs.length/2)];
  const medHighK = subOccs.map(o => o.high_k).sort((a,b)=>a-b)[Math.floor(subOccs.length/2)];

  let medPathD = '';
  if (medLowK <= medHighK) {
    medPathD = `M ${mapX(0)} ${mapY(isUp?0.35:0.65)} L ${mapX(medLowX)} ${mapY(0)} L ${mapX(medHighX)} ${mapY(1)} L ${mapX(1)} ${mapY(isUp?0.85:0.15)}`;
  } else {
    medPathD = `M ${mapX(0)} ${mapY(isUp?0.35:0.65)} L ${mapX(medHighX)} ${mapY(1)} L ${mapX(medLowX)} ${mapY(0)} L ${mapX(1)} ${mapY(isUp?0.85:0.15)}`;
  }

  // ---------------- 当月进行中曲线（仅当抽屉组 = 当前流月柱时传入 live） ----------------
  let liveSvg = '';
  let liveLegend = '';
  let liveStats = '';
  if (live && liveMonth && Array.isArray(live.p)) {
    const pts = [];
    for (let i = 0; i < live.p.length; i += 2) {
      const xi = (liveMonth.x || [])[live.p[i]];
      const yi = live.p[i + 1];
      if (typeof xi === 'number' && typeof yi === 'number') pts.push([xi, yi]);
    }
    if (pts.length >= 2) {
      const d = pts.map((q, i) => `${i ? 'L' : 'M'} ${mapX(q[0])} ${mapY(q[1])}`).join(' ');
      const last = pts[pts.length - 1];
      const endX = Number(mapX(last[0]));
      const endY = Number(mapY(last[1]));
      const retTxt = live.r == null ? '—' : `${live.r > 0 ? '+' : ''}${(live.r * 100).toFixed(2)}%`;
      const retCol = (live.r || 0) >= 0 ? '#f87171' : '#34d399';
      const anchor = endX > W - 96 ? 'end' : 'start';
      const labelX = anchor === 'end' ? endX - 5 : endX + 5;
      liveSvg = `
        <path d="${d}" fill="none" stroke="#22d3ee" stroke-width="7" stroke-linejoin="round" stroke-linecap="round" opacity="0.16"/>
        <path d="${d}" fill="none" stroke="#22d3ee" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="5,2.6"/>
        <circle cx="${endX}" cy="${endY}" r="3.2" fill="#22d3ee" stroke="#082f49" stroke-width="1.2"/>
        <text x="${labelX}" y="${Math.max(padT + 7, endY - 6)}" font-size="8.5" font-weight="bold" fill="${retCol}" text-anchor="${anchor}">当月 ${retTxt}</text>
      `;
      liveLegend = `<span class="flex items-center gap-1" title="当月真实日线（复权）；横轴=月内日历进度，纵轴=月内收盘价 min-max 归一化"><span class="inline-block" style="width:11px;border-top:2px dashed #22d3ee"></span> 青蓝虚线 = 本月进行中（截至 ${liveMonth.data_latest} · 已 ${liveMonth.days} 个交易日）</span>`;
      const medPts = medLowK <= medHighK
        ? [[0, isUp ? 0.35 : 0.65], [medLowX, 0], [medHighX, 1], [1, isUp ? 0.85 : 0.15]]
        : [[0, isUp ? 0.35 : 0.65], [medHighX, 1], [medLowX, 0], [1, isUp ? 0.85 : 0.15]];
      const medYAt = (xq) => {
        for (let i = 0; i < medPts.length - 1; i++) {
          const x0 = medPts[i][0], y0 = medPts[i][1];
          const x1 = medPts[i + 1][0], y1 = medPts[i + 1][1];
          if (xq >= x0 && xq <= x1) {
            const t = x1 === x0 ? 0 : (xq - x0) / (x1 - x0);
            return y0 + (y1 - y0) * t;
          }
        }
        return null;
      };
      let dev = null;
      {
        let sum = 0, cnt = 0;
        pts.forEach((q) => {
          const my = medYAt(q[0]);
          if (my != null) { sum += Math.abs(q[1] - my); cnt++; }
        });
        if (cnt) dev = sum / cnt;
      }
      const verdict = dev == null ? '—' : dev <= 0.15 ? '<span class="text-emerald-300 font-bold">高度跟随</span>' : dev <= 0.30 ? '<span class="text-amber-300 font-bold">大体跟随</span>' : '<span class="text-rose-300 font-bold">明显偏离</span>';
      const lowCmp = (live.lk == null) ? '—' : (live.lk < medLowK ? `比中枢早 ${medLowK - live.lk} 日` : live.lk > medLowK ? `比中枢晚 ${live.lk - medLowK} 日` : '与中枢同日');
      liveStats = `
        <div class="text-[10px] text-cyan-100/90 bg-cyan-950/30 border border-cyan-900/50 rounded px-1.5 py-1 leading-relaxed space-y-0.5">
          <div>• <b>本月对照</b>（截至 ${liveMonth.data_latest}）：累计 <b>${retTxt}</b>（首开→最新收，复权）· 低点第 <b>${live.lk ?? '—'}</b> 日（${lowCmp}）· 高点第 <b>${live.hk ?? '—'}</b> 日</div>
          <div>• <b>与历史中枢形态</b>：${verdict}${dev == null ? '' : `（平均偏差 ${dev.toFixed(2)}，0=完全贴合，1=上下相反）`}</div>
          <div class="text-cyan-300/60">口径：青蓝线=复权收盘价；低/高点序号按收盘极值；横轴=月内日历进度（0=首交易日）。本线为进行中，月末才落定。</div>
        </div>`;
    }
  }

  // 历史年份联动标签
  const yearPillsHtml = subOccs.map(o => {
    const retPct = ((o.ret||0)*100).toFixed(1);
    const retSign = o.ret > 0 ? `+${retPct}%` : `${retPct}%`;
    const pillColor = o.dir === '涨' ? 'text-red-300 border-red-900/60 bg-red-950/40' : 'text-emerald-300 border-emerald-900/60 bg-emerald-950/40';
    return `
      <span onmouseenter="highlightPath('${containerId}', '${o.year}', true)" onmouseleave="highlightPath('${containerId}', '${o.year}', false)" class="px-1.5 py-0.5 rounded border text-[10px] font-mono cursor-pointer transition hover:bg-slate-800 hover:text-white ${pillColor}" title="见底:第${o.low_k}日 | 冲顶:第${o.high_k}日">
        ${o.year}年 (${retSign})
      </span>
    `;
  }).join('');

  // 形态共性判定
  const allFirst = subOccs.every(o => o.low_k <= 3);
  const consensusTxt = allFirst ?
    `<span class="text-emerald-400 font-bold">【波形高度一致】</span> 历次全部在开月前 3 天见底，随后单边震荡推升` :
    `<span class="text-slate-400">【波形走势共性】</span> 中枢见底第 <b>${medLowK}</b> 日 · 冲顶第 <b>${medHighK}</b> 日`;

  box.innerHTML = `
    <div class="relative bg-slate-950 rounded border border-slate-800 p-2 space-y-2">
      <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto overflow-visible font-mono">
        <line x1="${mapX(0.333)}" y1="${padT}" x2="${mapX(0.333)}" y2="${padT+innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        <line x1="${mapX(0.666)}" y1="${padT}" x2="${mapX(0.666)}" y2="${padT+innerH}" stroke="#334155" stroke-dasharray="2,2" opacity="0.6"/>
        <line x1="${padL}" y1="${mapY(0.5)}" x2="${padL+innerW}" y2="${mapY(0.5)}" stroke="#1e293b" stroke-width="1"/>

        ${lines}

        <path d="${medPathD}" fill="none" stroke="#fbbf24" stroke-width="2.6" stroke-linecap="round"/>

        ${liveSvg}

        <text x="${mapX(0)}" y="${H-4}" font-size="9" fill="#64748b" text-anchor="start">月初</text>
        <text x="${mapX(0.5)}" y="${H-4}" font-size="9" fill="#64748b" text-anchor="middle">月中</text>
        <text x="${mapX(1)}" y="${H-4}" font-size="9" fill="#64748b" text-anchor="end">月末</text>
        <text x="${padL-4}" y="${mapY(1)+3}" font-size="8" fill="#f43f5e" text-anchor="end">最高</text>
        <text x="${padL-4}" y="${mapY(0)+3}" font-size="8" fill="#38bdf8" text-anchor="end">最低</text>
      </svg>

      <div class="flex flex-wrap items-center justify-between gap-1 text-[10px] text-slate-400 px-1 border-t border-slate-900 pt-1.5">
        <span class="flex items-center gap-1"><span class="w-2.5 h-0.5 bg-amber-400 inline-block"></span> 黄线 = 走势中枢波形</span>
        ${liveLegend}
        ${liveHint ? `<span class="flex items-center gap-1 text-cyan-300/70"><span class="inline-block" style="width:11px;border-top:2px dashed #22d3ee;opacity:.6"></span> ${liveHint}</span>` : ''}
        <span class="text-slate-400 text-[10px]">${consensusTxt}</span>
      </div>

      <div class="text-[10px] text-slate-500 px-1 leading-relaxed">
        纵轴口径：每条线都是<b class="text-slate-400">各自月内</b>最高/最低归一（↑最高=该月自身最高点，↓最低=该月自身最低点），
        <b class="text-slate-400">只比形态与时辰，不比幅度</b>；整月幅度见下方年份胶囊，当月幅度见青蓝标签。横轴：月初/月中/月末=月内三等分。
      </div>

      ${liveStats}

      <div class="flex flex-wrap gap-1 px-1">
        ${yearPillsHtml}
      </div>
    </div>
  `;
}

function highlightPath(containerId, year, on) {
  const p = document.getElementById(`path-${containerId}-${year}`);
  if (p) {
    if (on) {
      // 浅色主题下白线看不见，改用深色高亮
      p.setAttribute('stroke', document.documentElement.classList.contains('light') ? '#0f172a' : '#ffffff');
      p.setAttribute('stroke-width', '3.2');
      p.parentNode.appendChild(p); // 置顶
    } else {
      const isUp = containerId.includes('up');
      p.setAttribute('stroke', isUp ? 'rgba(244,63,94,0.45)' : 'rgba(56,189,248,0.45)');
      p.setAttribute('stroke-width', '1.8');
    }
  }
}


// -------------------------------------------------------------
// 渲染主表格与哑铃点阵标尺
// -------------------------------------------------------------
function renderDumbbellRuler(r) {
  const bPct = (r.mean_null_low_fraction * 100).toFixed(1);
  const mPct = (r.mean_low_fraction * 100).toFixed(1);
  const delta = r.mean_excess_earliness;
  const isEarly = delta < -0.015;
  const isLate = delta > 0.015;

  const leftPct = Math.min(bPct, mPct);
  const widthPct = Math.abs(mPct - bPct);
  const bandCls = isEarly ? 'early' : isLate ? 'late' : '';
  const bandStyle = `left:${leftPct}%; width:${widthPct}%;`;

  const dg = r.digest || {};
  const dots = dg.dots || [];

  const slots = {};
  const dotsHtml = dots.map(d => {
    const slotIdx = Math.round((d.x || 0) * 24);
    slots[slotIdx] = (slots[slotIdx] || 0) + 1;
    const stackBottom = (slots[slotIdx] - 1) * 5 + 3;

    let shapeClass = 'dot-unit neutral';
    if (currentEncodingMode === 'shape') {
      if (d.dir === '涨') shapeClass = 'dot-unit up-solid';
      else if (d.dir === '跌') shapeClass = 'dot-unit down-hollow';
      else shapeClass = 'dot-unit flat-dash';
    }

    const tip = `${d.month_id} (${d.pillar || ''}日)
` +
      `月内第 ${d.k}/${d.n} 交易日 (x=${(d.x||0).toFixed(2)})
` +
      `整月涨跌: ${d.dir} (${((d.ret||0)*100).toFixed(1)}%)
` +
      `并列: ${d.tie_n || 1}天 (取最早)`;

    return `<div class="${shapeClass}" style="left:${((d.x||0)*100).toFixed(1)}%; bottom:${stackBottom}px;" title="${tip}"></div>`;
  }).join('');

  return `
    <div class="ruler-track">
      <div class="seg-bg first" title="月初 (k ≤ ⌈n/3⌉)"></div>
      <div class="seg-bg mid" title="月中"></div>
      <div class="seg-bg last" title="月末"></div>

      ${widthPct > 0.5 ? `<div class="delta-band ${bandCls}" style="${bandStyle}"></div>` : ''}

      <div class="baseline-needle" style="left:${bPct}%;">
        <span class="baseline-tag">N1 ${r.mean_null_low_fraction.toFixed(3)}</span>
      </div>

      <div class="mean-pointer ${isEarly ? 'early' : isLate ? 'late' : 'neutral'}" style="left:${mPct}%;" title="实测均值: ${r.mean_low_fraction.toFixed(3)}"></div>

      ${dotsHtml}
    </div>
  `;
}

function renderTable() {
  const tbody = document.getElementById('group-rows');
  if (!activeRows.length) {
    // 区分「数据里就没有」与「筛选后为空」：前者要把原因写出来，不能只说无记录
    if (!rawGroupsData.length && v5EmptyReason) {
      tbody.innerHTML = `<tr><td colspan="6" class="py-6 px-4" style="white-space:normal;word-break:break-word;">
        <div class="text-xs text-amber-300 font-semibold" style="margin-bottom:4px;text-align:center;white-space:normal;">该标的没有可拆解的分组（不是筛选筛掉的）</div>
        <div class="text-[11px] text-slate-400" style="max-width:760px;margin:0 auto;line-height:1.7;text-align:left;white-space:normal;word-break:break-word;">${v5EmptyReason}</div>
      </td></tr>`;
      return;
    }
    tbody.innerHTML = `<tr><td colspan="6" class="py-6 text-center text-slate-500">当前筛选条件下无达标记录</td></tr>`;
    return;
  }

  tbody.innerHTML = activeRows.map(r => {
    const isSelected = r.group_key === selectedGroupKey;
    const delta = r.mean_excess_earliness;
    const isEarly = delta < -0.015;
    const isLate = delta > 0.015;
    const deltaColor = isEarly ? 'text-sky-400' : isLate ? 'text-orange-400' : 'text-slate-400';
    const deltaSign = delta > 0 ? `+${delta.toFixed(3)} 晚` : delta < 0 ? `${delta.toFixed(3)} 早` : '±0.000 随机';

    const n = r.n_windows;
    const is100Up = r.up_rate === 1.0;
    const is100Down = r.up_rate === 0.0;
    const isLivePillar = !!liveMonth && currentDim === 'P60' && r.group_key === liveMonth.pillar;

    let badge = '三段分散';
    let badgeCls = 'border-slate-700 text-slate-400 bg-slate-800/40';
    if (r.low_first_n / n >= 0.6) {
      badge = `月初集中 (${Math.round((r.low_first_n/n)*100)}%)`;
      badgeCls = 'border-sky-500/40 text-sky-300 bg-sky-500/10';
    } else if (r.low_middle_n / n >= 0.6) {
      badge = `月中集中 (${Math.round((r.low_middle_n/n)*100)}%)`;
      badgeCls = 'border-amber-500/40 text-amber-300 bg-amber-500/10';
    } else if (r.low_last_n / n >= 0.6) {
      badge = `月末集中 (${Math.round((r.low_last_n/n)*100)}%)`;
      badgeCls = 'border-orange-500/40 text-orange-300 bg-orange-500/10';
    }

    return `
      <tr id="row-${r.group_key}" class="cursor-pointer transition-colors hover:bg-slate-800/50 ${isSelected ? 'drawer-active' : ''}" onclick="selectGroup('${r.group_key}')">
        <td class="py-2 px-2 font-bold text-slate-100 whitespace-nowrap">
          <div class="flex items-center gap-1">
            <span class="w-1.5 h-1.5 rounded-full ${isSelected ? 'bg-indigo-400' : 'bg-transparent'} shrink-0"></span>
            <span>${r.group_key}</span>
            ${currentDim === 'P60' ? `<button type="button" title="展开 3 旬 / 6 段" onclick="event.stopPropagation();toggleWinRows('${r.group_key}')" class="ml-0.5 px-1 py-0.2 rounded text-[9px] border border-slate-700 text-slate-400 hover:text-indigo-300 hover:border-indigo-500 shrink-0 whitespace-nowrap">${winExpandKey === r.group_key ? '▾' : '▸'} 旬/段</button>` : ''}
            ${is100Up ? '<span class="px-1 py-0.2 rounded text-[10px] font-sans font-bold bg-red-500/30 text-red-300 border border-red-500/40 shrink-0">100%涨</span>' : ''}
            ${is100Down ? '<span class="px-1 py-0.2 rounded text-[10px] font-sans font-bold bg-emerald-500/30 text-emerald-300 border border-emerald-500/40 shrink-0">100%跌</span>' : ''}
            ${isLivePillar ? '<span class="px-1 py-0.2 rounded text-[10px] font-sans font-bold bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 shrink-0" title="当前正在进行中的流月柱：点这把行可在右侧叠加当月真实走势">本月</span>' : ''}
          </div>
        </td>
        <td class="py-2 px-1 text-center whitespace-nowrap text-slate-300 text-[11px]">
          <span>${r.n_windows}次</span>
          ${r.group_reliability === 'INSUFFICIENT' ? '<span class="px-1 py-0.2 rounded text-[9px] bg-slate-600/40 text-slate-300 ml-0.5">样本不足</span>' : ''}
          ${r.low_sample ? '<span class="px-1 py-0.2 rounded text-[9px] bg-amber-500/20 text-amber-300 ml-0.5">低样本</span>' : ''}
        </td>
        <td class="py-2 px-1 text-center text-[11px] whitespace-nowrap">
          <span class="${r.n_up > 0 ? 'text-red-400 font-bold' : 'text-slate-400'}">${r.n_up}涨</span>/<span class="${r.n_down > 0 ? 'text-emerald-400 font-bold' : 'text-slate-400'}">${r.n_down}跌</span>
          <span class="text-slate-500 text-[10px]">(${((r.up_rate||0)*100).toFixed(0)}%)</span>
        </td>
        <td class="py-2 px-2">
          ${renderDumbbellRuler(r)}
        </td>
        <td class="py-2 px-2 text-right font-mono ${deltaColor} font-semibold text-[11px] whitespace-nowrap">
          ${deltaSign}
        </td>
        <td class="py-2 px-2 text-center text-[11px] whitespace-nowrap">
          <span class="px-1.5 py-0.5 rounded border ${badgeCls}">
            ${badge}
          </span>
        </td>
      </tr>
      ${winExpandKey === r.group_key ? winSubRowsHtml(r.group_key) : ''}
    `;
  }).join('');
}

function selectGroup(key) {
  selectedGroupKey = key;
  winSel = null;
  if (winViewSaved) {
    switchUpView(winViewSaved.up);
    switchDownView(winViewSaved.down);
    winViewSaved = null;
  }
  renderTable();
  renderExtreme100Section();
  renderDetailDrawer();
  saveV5Ui();
}

// -------------------------------------------------------------
// 深入剖析抽屉渲染 (大白话极值时序 + 形态叠加折线 + 规律诊断)
// -------------------------------------------------------------
function liveMatchFor(r) {
  // 只有「60流月柱维度 且 抽屉组键 = 当前流月柱」时才叠加当月进行中曲线
  if (!liveMonth || !r || currentDim !== 'P60') return null;
  if (r.group_key !== liveMonth.pillar) return null;
  const key6 = String(resolvedSymbol || currentStock || '').split('.')[0];
  const s = liveMonth.stocks[key6];
  if (!s || !Array.isArray(s.p) || s.p.length < 4) return null;
  return s;
}

// -------------------------------------------------------------
// 左表：流月柱 → 可展开 3 旬 / 6 段 子行；点窗口 → 右侧抽屉换成该窗口全貌
// -------------------------------------------------------------
let winExpandKey = null;      // 当前展开的流月柱
let winRowsCache = {};        // `${pillar}|${layer}` -> [ {win, d} ... ]
let winRowsLoading = {};      // 正在加载的 key
let winSel = null;            // { pillar, layer, win }
let winDrawerSeq = 0;

const WIN_LAYERS = [['xun', '旬', 3], ['week', '段', 6]];
const WIN_GATE_TIP = '同向占比 ≥60% 标 ✅，不到标 ⚠️；60% 是本页展示门槛，不是统计显著性。';

function winLayerMeta(layer) {
  return layer === 'week' ? { cn: '段', n: 6, step: 5 } : { cn: '旬', n: 3, step: 10 };
}

function winCacheKey(pillar, layer) { return `${pillar}|${layer}`; }

// 页面统一口径：窗口只统计「该柱的 V5 正式月」，避免同一屏出现 5 次 / 6 次
function groupMonthSet(pillar) {
  const g = rawGroupsData.find((x) => x.group_key === pillar);
  const occ = (g && g.occurrences) || [];
  return { set: new Set(occ.map((o) => o.month_id)), total: occ.length };
}

function filterHistByGroup(hist, pillar) {
  const { set, total } = groupMonthSet(pillar);
  const keep = (hist || []).filter((h) => set.has(h.month_id));
  return { hist: keep, months: total, dropped: (hist || []).length - keep.length };
}

async function ensureWinRows(pillar, layer) {
  const key = winCacheKey(pillar, layer);
  if (winRowsCache[key] || winRowsLoading[key]) return winRowsCache[key] || null;
  winRowsLoading[key] = true;
  const { n } = winLayerMeta(layer);
  const sym = String(resolvedSymbol || currentStock || '');
  const arr = await Promise.all(Array.from({ length: n }, (_, i) => fetch(
    `/api/window_path?id=${encodeURIComponent(sym)}&pillar=${encodeURIComponent(pillar)}&layer=${layer}&win=${i + 1}`
  ).then((r) => (r.ok ? r.json() : null)).catch(() => null)));
  winRowsLoading[key] = false;
  if (arr.some((d) => d && d.available)) winRowsCache[key] = arr;
  return winRowsCache[key] || null;
}

function toggleWinRows(pillar) {
  if (winExpandKey === pillar) { winExpandKey = null; renderTable(); return; }
  winExpandKey = pillar;
  winSel = null;
  renderTable();
  // 先把两层的行占位渲染出来，再异步补数据
  ['xun', 'week'].forEach((layer) => {
    ensureWinRows(pillar, layer).then(() => {
      if (winExpandKey === pillar) renderTable();
    });
  });
}

function winRowStats(hist, isUp) {
  const rows = (hist || []).filter((h) => ((h.dir === 'UP') === isUp) && h.n > 0 && h.lk >= 1 && h.hk >= 1);
  if (!rows.length) return null;
  const N = rows.length;
  const pc = (a) => Math.round((a / N) * 100);
  const lowFirst = rows.filter((h) => h.lk <= Math.ceil(h.n / 3)).length;
  const highLast = rows.filter((h) => h.hk > Math.ceil((h.n * 2) / 3)).length;
  const ordered = rows.filter((h) => (isUp ? h.lk < h.hk : h.hk < h.lk)).length;
  const meanLow = rows.reduce((a, h) => a + h.lk, 0) / N;
  const meanHigh = rows.reduce((a, h) => a + h.hk, 0) / N;
  return { N, pc, lowFirst, highLast, ordered, meanLow, meanHigh, span: meanHigh - meanLow };
}

function winSubRowsHtml(pillar) {
  const out = [];
  WIN_LAYERS.forEach(([layer, cn, n]) => {
    const arr = winRowsCache[winCacheKey(pillar, layer)];
    for (let i = 1; i <= n; i++) {
      const d = arr ? arr[i - 1] : null;
      const meta = winLayerMeta(layer);
      const tag = layer === 'xun' ? ['上旬', '中旬', '下旬'][i - 1] : `第${i}段`;
      const rng = layer === 'xun'
        ? (i === 3 ? `${(i - 1) * 10}–下节` : `${(i - 1) * 10}–${i * 10} 日`)
        : (i === 6 ? `${(i - 1) * 5}–下节` : `${(i - 1) * 5}–${i * 5} 日`);
      const sel = winSel && winSel.pillar === pillar && winSel.layer === layer && winSel.win === i;
      let cells;
      if (!d) {
        cells = '<td class="py-1 px-2 text-slate-500" colspan="5">' + (arr ? '不可用' : '加载中…') + '</td>';
      } else if (!d.available) {
        cells = '<td class="py-1 px-2 text-slate-500" colspan="5">' + (d.reason || '不可用') + '</td>';
      } else {
        const f = filterHistByGroup(d.hist, pillar);
        const nUp = f.hist.filter((h) => h.dir === 'UP').length;
        const nDn = f.hist.filter((h) => h.dir === 'DOWN').length;
        const nFl = Math.max(0, f.hist.length - nUp - nDn);
        const isUp = (f.hist.length ? nUp / f.hist.length : 0.5) >= 0.5;
        const c = winRowStats(f.hist, isUp);
        const ud = `${nUp}<span class="text-slate-600">/</span>${nDn}<span class="text-slate-600">/</span>${nFl}`;
        const rate = f.hist.length ? `${Math.round((nUp / f.hist.length) * 100)}%` : '—';
        const lowTxt = c ? (c.N === 1 ? `1 次 · 第 ${c.meanLow} 日` : `${c.pc(c.lowFirst) >= 60 ? '✅' : '⚠️'} ${c.pc(c.lowFirst)}% 前1/3 · 第 ${c.meanLow.toFixed(1)} 日`) : '—';
        const highTxt = c ? (c.N === 1 ? '只有 1 次' : `${c.pc(c.highLast) >= 60 ? '✅' : '⚠️'} ${c.pc(c.highLast)}% 末1/3 · 第 ${c.meanHigh.toFixed(1)} 日`) : '—';
        const ordTxt = c ? (c.N === 1 ? '只有 1 次' : `${c.pc(c.ordered)}% · ${c.span >= 0 ? c.span.toFixed(1) + ' 日' : '高比低早 ' + Math.abs(c.span).toFixed(1) + ' 日'}`) : '—';
        cells = `<td class="py-1 px-1 text-center whitespace-nowrap">${c ? c.N : 0}<span class="text-slate-500">/${f.hist.length}</span></td>`
          + `<td class="py-1 px-1 text-center whitespace-nowrap">${ud}</td>`
          + `<td class="py-1 px-2 text-[10.5px] whitespace-nowrap">${lowTxt}</td>`
          + `<td class="py-1 px-2 text-[10.5px] whitespace-nowrap">${highTxt}</td>`
          + `<td class="py-1 px-2 text-[10.5px] whitespace-nowrap">${ordTxt}</td>`;
      }
      out.push(`<tr class="cursor-pointer ${sel ? 'bg-indigo-500/15' : 'hover:bg-slate-800/40'} text-[11px]"
          onclick="selectWinRow('${pillar}','${layer}',${i})">
        <td class="py-1 pl-6 pr-2 text-slate-300 whitespace-nowrap">↳ <span class="text-indigo-300">${tag}</span><span class="text-slate-500"> ${rng}</span></td>
        ${cells}
      </tr>`);
    }
  });
  out.push(`<tr class="text-[10px] text-slate-500"><td class="py-1 pl-6 pr-2" colspan="6">
      分母 = 该柱 ${groupMonthSet(pillar).total} 个 V5 正式月里、这个窗位有成交的个数（停牌窗口不计；非正式月如单点K线不参与）。
    </td></tr>`);
  return out.join('');
}

function selectWinRow(pillar, layer, win) {
  if (selectedGroupKey !== pillar) selectedGroupKey = pillar;
  if (!winViewSaved) winViewSaved = { up: upViewMode, down: downViewMode };
  winSel = { pillar, layer, win };
  renderTable();
  renderDetailDrawer();
  // 窗口默认看形态折线（和月一样有历年线 + 年份标签）
  switchUpView('path');
  switchDownView('path');
}

function winPathSvg(hist, isUp) {
  const rows = (hist || []).filter((h) => ((h.dir === 'UP') === isUp) && h.pts && h.pts.length > 1);
  if (!rows.length) return '<div class="text-[11px] text-slate-500 py-2">没有可画的歷次窗口。</div>';
  const W = 460, H = 180, padL = 34, padR = 14, padT = 12, padB = 24;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const all = rows.flatMap((h) => h.pts.map((q) => q[1] / 10000));
  let min = Math.min(0, ...all), max = Math.max(0, ...all);
  const pad = (max - min) * 0.12 || 0.01;
  min -= pad; max += pad;
  const mx = (x) => padL + Math.max(0, Math.min(1, x)) * innerW;
  const my = (y) => padT + (1 - (y - min) / (max - min)) * innerH;
  const color = isUp ? '#f87171' : '#34d399';
  const paneId = isUp ? 'up' : 'down';
  const lines = rows.map((h) => {
    const d = h.pts.map((q, i) => `${i ? 'L' : 'M'} ${mx(q[0]).toFixed(1)} ${my(q[1] / 10000).toFixed(1)}`).join(' ');
    return `<path id="winpath-${paneId}-${h.year}" data-color="${color}" d="${d}" fill="none" stroke="${color}" stroke-width="1.5" opacity="0.75"/>`;
  }).join('');
  const chips = rows.map((h) => {
    const r = (h.e_bp || 0) / 10000;
    const sign = (r >= 0 ? '+' : '') + (r * 100).toFixed(1) + '%';
    return `<span onmouseenter="highlightWinPath('${paneId}','${h.year}',true)" onmouseleave="highlightWinPath('${paneId}','${h.year}',false)"
      class="px-1.5 py-0.5 rounded border text-[10px] font-mono cursor-pointer transition hover:bg-slate-800 hover:text-white ${isUp ? 'border-rose-500/40 text-rose-300' : 'border-emerald-500/40 text-emerald-300'}"
      title="${h.month_id} · 窗首开盘→窗尾 ${sign} · 低点第${h.lk}日 · 高点第${h.hk}日">${h.year}年 (${sign})</span>`;
  }).join('');
  const avg = [];
  for (let i = 0; i <= 20; i++) {
    const xs = i / 20;
    let sum = 0, k = 0;
    rows.forEach((h) => {
      let prev = h.pts[0];
      for (const q of h.pts) {
        if (q[0] >= xs) {
          const t = (xs - prev[0]) / Math.max(1e-6, q[0] - prev[0]);
          sum += (prev[1] + (q[1] - prev[1]) * Math.max(0, Math.min(1, t))) / 10000;
          k++;
          break;
        }
        prev = q;
      }
    });
    if (k) avg.push([xs, sum / k]);
  }
  const avgD = avg.map((q, i) => `${i ? 'L' : 'M'} ${mx(q[0]).toFixed(1)} ${my(q[1]).toFixed(1)}`).join(' ');
  return `<div class="relative bg-slate-950 rounded-xl border border-slate-800 p-2">
    <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto">
      <line x1="${padL}" y1="${my(0)}" x2="${padL + innerW}" y2="${my(0)}" stroke="#475569" stroke-dasharray="4,3"/>
      ${lines}
      <path d="${avgD}" fill="none" stroke="#fbbf24" stroke-width="2.2"/>
      <text x="${padL}" y="${H - 4}" font-size="9" fill="#64748b">窗首</text>
      <text x="${padL + innerW}" y="${H - 4}" font-size="9" fill="#64748b" text-anchor="end">窗尾</text>
    </svg>
    <div class="flex flex-wrap gap-1 px-1 mt-1">${chips}</div>
    <div class="text-[10px] text-slate-500 px-1 mt-1">黄线=历史平均；细线=各年（${rows.length} 条）· 从窗首开盘算起；鼠标移到年份上高亮该年。</div>
  </div>`;
}

function setScopeLabel(scope) {
  const u = document.getElementById('up-scope');
  const d = document.getElementById('down-scope');
  if (u) u.textContent = scope;
  if (d) d.textContent = scope;
}

function highlightWinPath(pane, year, on) {
  const p = document.getElementById(`winpath-${pane}-${year}`);
  if (!p) return;
  if (on) {
    p.setAttribute('stroke', document.documentElement.classList.contains('light') ? '#0f172a' : '#ffffff');
    p.setAttribute('stroke-width', '3.2');
    p.setAttribute('opacity', '1');
    p.parentNode.appendChild(p);
  } else {
    p.setAttribute('stroke', p.dataset.color);
    p.setAttribute('stroke-width', '1.5');
    p.setAttribute('opacity', '0.75');
  }
}

function renderWindowDrawer(pillar, layer, win) {
  setScopeLabel('窗');
  const key = winCacheKey(pillar, layer);
  const arr = winRowsCache[key];
  const d = arr ? arr[win - 1] : null;
  const meta = winLayerMeta(layer);
  const tag = layer === 'xun' ? ['上旬', '中旬', '下旬'][win - 1] : `第${win}段`;
  const badgeEl = document.getElementById('detail-badge');
  badgeEl.innerText = tag;
  badgeEl.className = 'px-2.5 py-1 rounded text-xs font-bold bg-indigo-600 text-white whitespace-nowrap shrink-0 inline-flex items-center justify-center tracking-wide shadow-sm';
  document.getElementById('detail-title').innerText = `${currentStock} · ${pillar} · ${tag}`;
  if (!d || !d.available) {
    document.getElementById('detail-n').innerText = '—';
    ['up', 'down'].forEach((k) => {
      document.getElementById(`${k}-count`).innerText = '0';
      document.getElementById(`${k}-rate-badge`).innerText = '占比 —';
      document.getElementById(`${k}-track-container`).innerHTML = '';
      document.getElementById(`${k}-path-container`).innerHTML = '';
      document.getElementById(`${k}-pattern-summary`).innerHTML = '<span class="text-slate-500">' + (d && d.reason ? d.reason : '窗口数据不可用。') + '</span>';
    });
    document.getElementById('timing-tag').innerText = '—';
    document.getElementById('timing-diagnosis').innerHTML = '<span class="text-slate-500">这个窗口没有可用的历史样本。</span>';
    return;
  }
  const fil = filterHistByGroup(d.hist, pillar);
  const hist = fil.hist;
  const s2 = d.stats || {};
  const nUp = hist.filter((h) => h.dir === 'UP').length;
  const nDn = hist.filter((h) => h.dir === 'DOWN').length;
  const nFl = hist.filter((h) => h.dir !== 'UP' && h.dir !== 'DOWN').length;
  const upRate = hist.length ? Math.round((nUp / hist.length) * 100) : 0;
  document.getElementById('detail-n').innerText =
    `${pillar} 的 ${fil.months} 个 V5 正式月中，该窗位有成交 ${hist.length} 个 · 涨 ${nUp} / 跌 ${nDn} / 平 ${nFl}`;
  document.getElementById('up-count').innerText = nUp;
  document.getElementById('down-count').innerText = nDn;
  document.getElementById('up-rate-badge').innerText = `占比 ${upRate}%`;
  document.getElementById('down-rate-badge').innerText = `占比 ${hist.length ? 100 - upRate : 0}%`;

  [['up', true], ['down', false]].forEach(([k, isUp]) => {
    const c = winRowStats(hist, isUp);
    const track = document.getElementById(`${k}-track-container`);
    const summary = document.getElementById(`${k}-pattern-summary`);
    const path = document.getElementById(`${k}-path-container`);
    if (!c) {
      track.innerHTML = `<div class="text-xs text-slate-500 py-2">该窗口历史上没有${isUp ? '上涨' : '下跌'}样本。</div>`;
      summary.innerHTML = `无${isUp ? '涨' : '跌'}窗样本。`;
      path.innerHTML = '<div class="text-[11px] text-slate-500 py-2">—</div>';
      return;
    }
    const rows = hist.filter((h) => ((h.dir === 'UP') === isUp) && h.n > 0 && h.lk >= 1 && h.hk >= 1);
    const dot = (o, frac, color, lbl) => `<div class="dot-unit" style="background:${color};border:1px solid #0f172a;left:${(frac * 100).toFixed(1)}%;bottom:2px;" title="${o.month_id}: ${lbl}在第 ${o.lk}/${o.n} 日"></div>`;
    const lowDots = rows.map((o) => dot(o, (o.lk - 1) / Math.max(1, o.n - 1), '#38bdf8', '最低')).join('');
    const highDots = rows.map((o) => `<div style="position:absolute;width:0;height:0;border-left:3.5px solid transparent;border-right:3.5px solid transparent;border-bottom:7px solid #f43f5e;left:${(((o.hk - 1) / Math.max(1, o.n - 1)) * 100).toFixed(1)}%;bottom:2px;margin-left:-3.5px;" title="${o.month_id}: 最高在第 ${o.hk}/${o.n} 日"></div>`).join('');
    track.innerHTML = `
      <div class="space-y-1">
        <div class="flex justify-between items-center text-[10px] text-slate-300">
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-sky-400 inline-block"></span> 最低点分布 (●) · 平均第 <b>${c.meanLow.toFixed(1)}</b> 交易日</span>
          <span class="text-sky-300 font-mono">前 1/3 占 ${c.pc(c.lowFirst)}% (${c.lowFirst}/${c.N})</span>
        </div>
        <div class="ruler-track" style="height:20px;">
          <div class="seg-bg first"></div><div class="seg-bg mid"></div><div class="seg-bg last"></div>
          ${lowDots}
        </div>
      </div>
      <div class="space-y-1 pt-1">
        <div class="flex justify-between items-center text-[10px] text-slate-300">
          <span class="flex items-center gap-1.5"><span class="w-0 h-0 border-l-[3.5px] border-r-[3.5px] border-b-[6px] border-rose-500 inline-block"></span> 最高点冲顶 (▲) · 平均第 <b>${c.meanHigh.toFixed(1)}</b> 交易日</span>
          <span class="text-rose-300 font-mono">末 1/3 占 ${c.pc(c.highLast)}% (${c.highLast}/${c.N})</span>
        </div>
        <div class="ruler-track" style="height:20px;">
          <div class="seg-bg first"></div><div class="seg-bg mid"></div><div class="seg-bg last"></div>
          ${highDots}
        </div>
      </div>`;
    const icon = (ok) => `<span title="${WIN_GATE_TIP}">${ok ? '✅' : '⚠️'}</span>`;
    const lowTxt = c.N === 1 ? '只有 1 次，谈不上分布。'
      : `${c.pc(c.lowFirst)}% 的${isUp ? '上涨' : '下跌'}窗口在窗首 1/3 内打出最低点（平均第 ${c.meanLow.toFixed(1)} 日）。`;
    const highTxt = c.N === 1 ? '' : `${c.pc(c.highLast)}% 在窗尾 1/3 推出最高点（平均第 ${c.meanHigh.toFixed(1)} 日）。`;
    const ordTxt = c.N === 1 ? '' : `${isUp ? '先低后高' : '先高后低'}占 ${c.pc(c.ordered)}%（${c.ordered}/${c.N}），平均跨度 ${c.span >= 0 ? c.span.toFixed(1) + ' 日' : '（高点反而更早 ' + Math.abs(c.span).toFixed(1) + ' 日）'}。`;
    summary.innerHTML = c.N === 1
      ? `<div>• ${lowTxt}</div>`
      : `<div>• <b>最低点</b>：${icon(c.pc(c.lowFirst) >= 60)} ${lowTxt}</div>
         <div>• <b>最高点</b>：${icon(c.pc(c.highLast) >= 60)} ${highTxt}</div>
         <div>• <b>单边性</b>：${ordTxt}</div>
         <div class="text-[10px] text-slate-500 pt-1 border-t border-slate-800/60">✅ / ⚠️：占比 ≥60% 标 ✅，不到标 ⚠️（本页门槛，非显著性）。</div>`;
    path.innerHTML = winPathSvg(hist, isUp);
  });

  const cUp = winRowStats(hist, true);
  const cDn = winRowStats(hist, false);
  document.getElementById('timing-tag').innerHTML = hist.length <= 2 ? '<span class="text-amber-400 font-bold">样本不足</span>'
    : (upRate === 100 ? '<span class="text-red-400 font-bold">全涨窗口</span>'
      : upRate === 0 ? '<span class="text-emerald-400 font-bold">全跌窗口</span>'
        : '<span class="text-slate-400 font-bold">多空混合</span>');
  document.getElementById('timing-diagnosis').innerHTML =
    `<div>1. <b>窗口样本</b>：${d.win_name || tag} 只在 <b>${fil.months}</b> 个 V5 正式月里统计，其中有成交 <b>${hist.length}</b> 个（涨 ${nUp} / 跌 ${nDn} / 平 ${nFl}）${fil.dropped > 0 ? `；另有 ${fil.dropped} 个窗口样本属于非正式月（如单点K线），未计入` : ''}。</div>`
    + (cUp ? `<div>2. <b>涨窗形态</b>：最低点平均第 ${cUp.meanLow.toFixed(1)} 日、最高点平均第 ${cUp.meanHigh.toFixed(1)} 日；${cUp.pc(cUp.ordered)}% 先低后高。</div>` : '')
    + (cDn && nDn ? `<div>3. <b>跌窗形态</b>：最低点平均第 ${cDn.meanLow.toFixed(1)} 日、最高点平均第 ${cDn.meanHigh.toFixed(1)} 日；${cDn.pc(cDn.ordered)}% 先高后低。</div>` : '')
    + (hist.length <= 2 ? '<div class="text-amber-400/90 pt-1 border-t border-slate-800 text-[10px]">⚠️ <b>理性防误读</b>：样本只有 1–2 次，只能当个案看。</div>' : '');
}

function renderDetailDrawer() {
  if (!(winSel && winSel.pillar === selectedGroupKey)) setScopeLabel('月');
  if (winSel && winSel.pillar === selectedGroupKey) {
    renderWindowDrawer(winSel.pillar, winSel.layer, winSel.win);
    return;
  }
  const r = rawGroupsData.find(x => x.group_key === selectedGroupKey);
  if (!r) {
    // 该标的没有可拆解的分组时清空右栏，不能留着上一只票的内容
    const badgeEl = document.getElementById('detail-badge');
    badgeEl.innerText = '—';
    badgeEl.className = 'px-2.5 py-1 rounded text-xs font-bold bg-indigo-600 text-white whitespace-nowrap shrink-0 inline-flex items-center justify-center tracking-wide shadow-sm';
    document.getElementById('detail-title').innerText = currentStock ? `${currentStock} · 暂无可拆解的分组` : '上涨 vs 下跌极值全貌与规律拆解';
    document.getElementById('detail-n').innerText = '—';
    ['up', 'down'].forEach((k) => {
      document.getElementById(`${k}-count`).innerText = '0';
      document.getElementById(`${k}-rate-badge`).innerText = '占比 —';
      document.getElementById(`${k}-track-container`).innerHTML = '';
      document.getElementById(`${k}-path-container`).innerHTML = '';
      document.getElementById(`${k}-pattern-summary`).innerHTML = '无样本。';
    });
    document.getElementById('timing-tag').innerText = '—';
    document.getElementById('timing-diagnosis').innerHTML = v5EmptyReason
      ? `<span class="text-amber-300">${v5EmptyReason}</span>`
      : '<span class="text-slate-500">左表没有达标分组，这里不做拆解。</span>';
    return;
  }

  const is100Up = r.up_rate === 1.0;
  const is100Down = r.up_rate === 0.0;

  const badgeEl = document.getElementById('detail-badge');
  badgeEl.innerText = r.group_key;
  if (is100Up) {
    badgeEl.className = 'px-2.5 py-1 rounded text-xs font-bold bg-red-600 text-white shadow-md whitespace-nowrap shrink-0 inline-flex items-center justify-center tracking-wide';
  } else if (is100Down) {
    badgeEl.className = 'px-2.5 py-1 rounded text-xs font-bold bg-emerald-600 text-white shadow-md whitespace-nowrap shrink-0 inline-flex items-center justify-center tracking-wide';
  } else {
    badgeEl.className = 'px-2.5 py-1 rounded text-xs font-bold bg-indigo-600 text-white whitespace-nowrap shrink-0 inline-flex items-center justify-center tracking-wide shadow-sm';
  }

  document.getElementById('detail-title').innerText = `${currentStock} · ${r.group_key} 流月极值时序与波形拆解`;

  const occs = r.occurrences || [];
  const upOccs = occs.filter(o => o.dir === '涨');
  const downOccs = occs.filter(o => o.dir === '跌');
  // 出现频率按这组自己的历次年算，不写死「约5年一遇」：停牌/缺月会把间隔拉长
  const occYears = occs.map(o => o.year).filter(y => Number.isFinite(y)).sort((a, b) => a - b);
  const gapYears = (occs.length > 1 && occYears.length > 1)
    ? (occYears[occYears.length - 1] - occYears[0]) / (occs.length - 1) : null;
  document.getElementById('detail-n').innerText = `历史共 ${r.n_windows} 次`
    + (gapYears == null ? '' : ` · 平均间隔约 ${gapYears.toFixed(1)} 年`);
  const live = liveMatchFor(r);
  const paths = (monthPathsData && monthPathsSym === String(resolvedSymbol || currentStock || '').split('.')[0])
    ? monthPathsData : null;
  const liveKey6 = String(resolvedSymbol || currentStock || '').split('.')[0];
  const liveHint = (!live && liveMonth && currentDim === 'P60'
    && liveMonth.stocks && liveMonth.stocks[liveKey6])
    ? `当月（${liveMonth.pillar}）进行中曲线在「${liveMonth.pillar}」组显示` : '';

  const upWinRate = Math.round(((r.n_up || 0) / r.n_windows) * 100);
  document.getElementById('up-count').innerText = upOccs.length;
  document.getElementById('up-rate-badge').innerText = `占比 ${upWinRate}%`;

  document.getElementById('down-count').innerText = downOccs.length;
  document.getElementById('down-rate-badge').innerText = `占比 ${100 - upWinRate}%`;

  // 1. 渲染涨月全貌与时序
  const upBox = document.getElementById('up-track-container');
  const upSummary = document.getElementById('up-pattern-summary');
  if (!upOccs.length) {
    upBox.innerHTML = '<div class="text-xs text-slate-500 py-2">历史上该流月未曾上涨过（0次）</div>';
    upSummary.innerHTML = '无涨月样本可供分析。';
  } else {
    const meanLowNum = upOccs.reduce((a, o) => a + o.low_k, 0) / upOccs.length;
    const meanHighNum = upOccs.reduce((a, o) => a + o.high_k, 0) / upOccs.length;
    const meanLowK = meanLowNum.toFixed(1);
    const meanHighK = meanHighNum.toFixed(1);
    const spanNum = meanHighNum - meanLowNum;   // 用未舍入的均值相减，避免二次舍入
    const lowFirst = upOccs.filter(o => o.seg === '月初').length;
    const lowFirstPct = Math.round((lowFirst / upOccs.length) * 100);
    // 「月初」= 每月前 1/3（第 1~⌈交易日数/3⌉ 天），随月长变化，不能写死成第 1~5 日
    const firstEdges = upOccs.map(o => Math.ceil((o.n || 0) / 3));
    const firstMin = Math.min(...firstEdges), firstMax = Math.max(...firstEdges);
    const firstRange = firstMin === firstMax ? `第 1~${firstMax}` : `第 1~${firstMin}（长月到第 ${firstMax}）`;
    const highLast = upOccs.filter(o => o.high_k > Math.ceil(o.n * 2 / 3)).length;
    const highLastPct = Math.round((highLast / upOccs.length) * 100);
    const orderLowFirst = upOccs.filter(o => o.low_k < o.high_k).length;
    const orderPct = Math.round((orderLowFirst / upOccs.length) * 100);

    const lowSlots = {};
    const lowDotsHtml = upOccs.map(o => {
      const s = Math.round(o.low_fraction * 24);
      lowSlots[s] = (lowSlots[s] || 0) + 1;
      return `<div class="dot-unit" style="background:#38bdf8; border:1px solid #0f172a; left:${(o.low_fraction*100).toFixed(1)}%; bottom:${(lowSlots[s]-1)*5+2}px;" title="${o.month_id}: 最低在第 ${o.low_k}/${o.n} 日 (${o.low_day})"></div>`;
    }).join('');

    const highSlots = {};
    const highTrisHtml = upOccs.map(o => {
      const s = Math.round(o.high_fraction * 24);
      highSlots[s] = (highSlots[s] || 0) + 1;
      return `<div style="position:absolute; width:0; height:0; border-left:3.5px solid transparent; border-right:3.5px solid transparent; border-bottom:7px solid #f43f5e; left:${(o.high_fraction*100).toFixed(1)}%; bottom:${(highSlots[s]-1)*5+2}px; margin-left:-3.5px;" title="${o.month_id}: 最高在第 ${o.high_k}/${o.n} 日 (${o.high_day})"></div>`;
    }).join('');

    upBox.innerHTML = `
      <div class="space-y-1">
        <div class="flex justify-between items-center text-[10px] text-slate-300">
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-sky-400 inline-block"></span> 最低点分布 (●) · 平均第 <b>${meanLowK}</b> 交易日</span>
          <span class="text-sky-300 font-mono">月初占 ${lowFirstPct}% (${lowFirst}/${upOccs.length})</span>
        </div>
        <div class="ruler-track" style="height:20px;">
          <div class="seg-bg first"></div><div class="seg-bg mid"></div><div class="seg-bg last"></div>
          ${lowDotsHtml}
        </div>
      </div>
      <div class="space-y-1 pt-1">
        <div class="flex justify-between items-center text-[10px] text-slate-300">
          <span class="flex items-center gap-1.5"><span class="w-0 h-0 border-l-[3.5px] border-r-[3.5px] border-b-[6px] border-rose-500 inline-block"></span> 最高点冲顶 (▲) · 平均第 <b>${meanHighK}</b> 交易日</span>
          <span class="text-rose-300 font-mono">月末占 ${highLastPct}% (${highLast}/${upOccs.length})</span>
        </div>
        <div class="ruler-track" style="height:20px;">
          <div class="seg-bg first"></div><div class="seg-bg mid"></div><div class="seg-bg last"></div>
          ${highTrisHtml}
        </div>
      </div>
    `;

    upSummary.innerHTML = `
      <div>• <b>最低点探底</b>：${lowFirstPct >= 60 ? `<span title="${GATE_TIP}">✅</span> <span class="text-sky-400 font-bold">买点高度收敛</span>：${lowFirstPct}% 的年份在开月${firstRange}个交易日内就打出最低点，平均在第 <b>${meanLowK}</b> 交易日。` : `<span title="${GATE_TIP}">⚠️</span> <span class="text-amber-400 font-bold">买点较为离散</span>：月初段（开月${firstRange}个交易日）只占 ${lowFirstPct}%，最低点中途仍有探底。`}</div>
      <div>• <b>最高点冲顶</b>：${highLastPct >= 60 ? `<span title="${GATE_TIP}">✅</span> <span class="text-rose-400 font-bold">冲顶高度集中</span>：${highLastPct}% 在月末推升至最高点，平均在第 <b>${meanHighK}</b> 交易日。` : `<span title="${GATE_TIP}">⚠️</span> <span class="text-slate-400 font-bold">冲顶时间分散</span>：月末见顶只占 ${highLastPct}% (${highLast}/${upOccs.length})，其余散在月中。`}</div>
      <div>• <b>波段单边性</b>：先低后高占 <b>${orderPct}%</b> (${orderLowFirst}/${upOccs.length})，${spanNum >= 0
        ? `从最低到最高平均拉升跨度约 <b>${spanNum.toFixed(1)}</b> 个交易日`
        : `高点平均落在低点之前 <b>${Math.abs(spanNum).toFixed(1)}</b> 个交易日`}。</div>
      <div class="text-[10px] text-slate-500 pt-1 border-t border-slate-800/60">✅ / ⚠️：占比 <b>≥60%</b> 标 ✅，不到标 ⚠️（本页门槛，非显著性）。</div>
    `;

    renderPathOverlay(upOccs, true, 'up-path-container', live, liveHint, paths);
  }

  // 2. 渲染跌月全貌与时序
  const downBox = document.getElementById('down-track-container');
  const downSummary = document.getElementById('down-pattern-summary');
  if (!downOccs.length) {
    downBox.innerHTML = '<div class="text-xs text-slate-500 py-2">历史上该流月未曾下跌过（0 次下跌，上涨占比 100%）</div>';
    downSummary.innerHTML = '无跌月样本。';
  } else {
    const meanHighK = (downOccs.reduce((a, o) => a + o.high_k, 0) / downOccs.length).toFixed(1);
    const meanLowK = (downOccs.reduce((a, o) => a + o.low_k, 0) / downOccs.length).toFixed(1);
    const highFirst = downOccs.filter(o => o.high_k <= Math.ceil(o.n / 3)).length;
    const highFirstPct = Math.round((highFirst / downOccs.length) * 100);
    const lowLast = downOccs.filter(o => o.seg === '月末').length;
    const lowLastPct = Math.round((lowLast / downOccs.length) * 100);
    const orderHighFirst = downOccs.filter(o => o.high_k < o.low_k).length;
    const orderPct = Math.round((orderHighFirst / downOccs.length) * 100);

    const highSlots = {};
    const highTrisHtml = downOccs.map(o => {
      const s = Math.round(o.high_fraction * 24);
      highSlots[s] = (highSlots[s] || 0) + 1;
      return `<div style="position:absolute; width:0; height:0; border-left:3.5px solid transparent; border-right:3.5px solid transparent; border-bottom:7px solid #f43f5e; left:${(o.high_fraction*100).toFixed(1)}%; bottom:${(highSlots[s]-1)*5+2}px; margin-left:-3.5px;" title="${o.month_id}: 冲高在第 ${o.high_k}/${o.n} 日 (${o.high_day})"></div>`;
    }).join('');

    const lowSlots = {};
    const lowDotsHtml = downOccs.map(o => {
      const s = Math.round(o.low_fraction * 24);
      lowSlots[s] = (lowSlots[s] || 0) + 1;
      return `<div class="dot-unit" style="background:#38bdf8; border:1px solid #0f172a; left:${(o.low_fraction*100).toFixed(1)}%; bottom:${(lowSlots[s]-1)*5+2}px;" title="${o.month_id}: 探底在第 ${o.low_k}/${o.n} 日 (${o.low_day})"></div>`;
    }).join('');

    downBox.innerHTML = `
      <div class="space-y-1">
        <div class="flex justify-between items-center text-[10px] text-slate-300">
          <span class="flex items-center gap-1.5"><span class="w-0 h-0 border-l-[3.5px] border-r-[3.5px] border-b-[6px] border-rose-500 inline-block"></span> 最高点冲高 (▲) · 平均第 <b>${meanHighK}</b> 交易日</span>
          <span class="text-rose-300 font-mono">月初占 ${highFirstPct}% (${highFirst}/${downOccs.length})</span>
        </div>
        <div class="ruler-track" style="height:20px;">
          <div class="seg-bg first"></div><div class="seg-bg mid"></div><div class="seg-bg last"></div>
          ${highTrisHtml}
        </div>
      </div>
      <div class="space-y-1 pt-1">
        <div class="flex justify-between items-center text-[10px] text-slate-300">
          <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-sky-400 inline-block"></span> 最低点探底 (●) · 平均第 <b>${meanLowK}</b> 交易日</span>
          <span class="text-sky-300 font-mono">月末占 ${lowLastPct}% (${lowLast}/${downOccs.length})</span>
        </div>
        <div class="ruler-track" style="height:20px;">
          <div class="seg-bg first"></div><div class="seg-bg mid"></div><div class="seg-bg last"></div>
          ${lowDotsHtml}
        </div>
      </div>
    `;

    downSummary.innerHTML = `
      <div>• <b>跌月走势</b>：先高后低占 <b>${orderPct}%</b> (${orderHighFirst}/${downOccs.length})。开月平均第 <b>${meanHighK}</b> 交易日冲高见顶，随后一路下行至第 <b>${meanLowK}</b> 交易日探底。</div>
      <div>• <b>见顶规律</b>：${highFirstPct >= 60 ? `月初即是全月最高点（占 ${highFirstPct}%），无深度诱多空间。` : `冲高时段分散，跌月中途有反复震荡。`}</div>
    `;

    renderPathOverlay(downOccs, false, 'down-path-container', live, liveHint, paths);
  }

  // 3. 核心大白话解答：上涨到底有没有规律？
  const timingTag = document.getElementById('timing-tag');
  const timingDiag = document.getElementById('timing-diagnosis');

  const lowFirst = upOccs.filter(o => o.seg === '月初').length;
  const lowFirstPct = upOccs.length ? Math.round((lowFirst / upOccs.length) * 100) : 0;

  if (is100Up) {
    timingTag.innerHTML = '<span class="text-red-400 font-bold animate-pulse">【⭐ 100% 全涨极值流月】</span>';
    timingDiag.innerHTML = `
      <div>1. <b>确定性方向优势</b>：历史遭遇该流月柱共 <b>${r.n_windows} 次全部上涨（上涨占比 100%）</b>，从未收绿，属于极罕见的纯多头极值窗口。</div>
      <div>2. <b>买点形态共性</b>：${lowFirstPct >= 60 ? `<span title="${GATE_TIP}">✅</span> <b>开月下蹲即买点</b>：${lowFirstPct}% 的年份在开月前几天即完成探底，随后持续拉升。买点时序高度确定！` : `<span title="${GATE_TIP}">⚠️</span> <b>中途震荡洗盘</b>：虽然最终整月均收红，但历史上有 ${100 - lowFirstPct}% 的年份中途深蹲，抄底点位容易被震出。`}</div>
      <div>3. <b>波段持有建议</b>：由于 100% 全涨且高点多集中在月末，波段持有至月末收益往往最大化。</div>
      <div class="text-amber-400/90 pt-1 border-t border-slate-800 text-[10px]">⚠️ <b>理性防误读</b>：流月约5年循环一次，历史样本 n=${r.n_windows} 仍属小样本。此为事后客观事实，不等于未来绝对保证，实盘仍需注意止损风控。</div>
    `;
  } else if (is100Down) {
    timingTag.innerHTML = '<span class="text-emerald-400 font-bold animate-pulse">【⭐ 100% 全跌极值流月】</span>';
    timingDiag.innerHTML = `
      <div>1. <b>确定性空头压制</b>：历史遭遇该流月柱共 <b>${r.n_windows} 次全部下跌（下跌占比 100%）</b>，历史上从未收红，空头特征极其顽固。</div>
      <div>2. <b>防守操作准则</b>：开月通常即是全月最高点，随后单边阴跌或反复深探。在此流月切忌在月初盲目抄底！</div>
      <div class="text-amber-400/90 pt-1 border-t border-slate-800 text-[10px]">⚠️ <b>理性防误读</b>：虽然历史 100% 下跌，但遇极端大牛市大盘普涨时仍存在逆转可能，统计只作事后风险揭示。</div>
    `;
  } else if (upWinRate >= 70 && lowFirstPct >= 60) {
    timingTag.innerHTML = '<span class="text-emerald-400 font-bold">【时序规律显著】</span>';
    timingDiag.innerHTML = `
      <div>1. <b>上涨占比较高</b>：历史上涨占比 <b>${upWinRate}%</b> (${upOccs.length}涨/${downOccs.length}跌)，历史以多头为主导。</div>
      <div>2. <b>买点高度确定</b>：涨月里有 <b>${lowFirstPct}%</b> 在月初前几天打出全月最低点，进场时间窗口极度收敛。</div>
      <div class="text-amber-400/90 pt-1 border-t border-slate-800 text-[10px]">⚠️ <b>理性防误读</b>：此为历史事后描述，不构成事前确定性买点。</div>
    `;
  } else if (upWinRate >= 60 && lowFirstPct < 60) {
    timingTag.innerHTML = '<span class="text-amber-400 font-bold">【方向涨但买点无序】</span>';
    timingDiag.innerHTML = `
      <div>1. <b>上涨占比略高</b>：历史上涨占比 <b>${upWinRate}%</b>，历史涨多跌少。</div>
      <div>2. <b>买点无规律</b>：<b>虽然整月上涨，但最低点在月初仅占 ${lowFirstPct}%</b>，洗盘时间不可测，极难抄在精准低点。</div>
    `;
  } else if (upWinRate < 50) {
    timingTag.innerHTML = '<span class="text-rose-400 font-bold">【偏空弱势 · 勿抄底】</span>';
    timingDiag.innerHTML = `
      <div>1. <b>上涨占比偏低</b>：历史上涨占比仅 <b>${upWinRate}%</b> (${upOccs.length}涨/${downOccs.length}跌)，历史跌多涨少。</div>
      <div>2. <b>无规律买点</b>：由于下跌月份占多数，月初盲目抄底极易遭遇持续下探。</div>
    `;
  } else {
    timingTag.innerHTML = '<span class="text-slate-400 font-bold">【多空胶着 · 规律离散】</span>';
    timingDiag.innerHTML = `
      <div>1. <b>多空势均力敌</b>：上涨概率为 <b>${upWinRate}%</b> (${upOccs.length}涨/${downOccs.length}跌)，没有统计方向优势。</div>
      <div>2. <b>极值随机发散</b>：最低点与最高点在各旬散落，基本等同于随机波动。</div>
    `;
  }
}

// -------------------------------------------------------------
// 切换视图：落点尺 vs 形态叠加折线
// -------------------------------------------------------------
let upViewMode = 'path', downViewMode = 'path';
let winViewSaved = null;

function switchUpView(v) {
  const isRuler = v === 'ruler';
  upViewMode = isRuler ? 'ruler' : 'path';
  document.getElementById('btn-up-view-ruler').className = isRuler ?
    'px-1.5 py-0.5 rounded bg-indigo-600 text-white font-medium' : 'px-1.5 py-0.5 rounded text-slate-400 hover:text-white';
  document.getElementById('btn-up-view-path').className = !isRuler ?
    'px-1.5 py-0.5 rounded bg-indigo-600 text-white font-medium' : 'px-1.5 py-0.5 rounded text-slate-400 hover:text-white';
  document.getElementById('up-track-container').classList.toggle('hidden', !isRuler);
  document.getElementById('up-path-container').classList.toggle('hidden', isRuler);
}

function switchDownView(v) {
  const isRuler = v === 'ruler';
  downViewMode = isRuler ? 'ruler' : 'path';
  document.getElementById('btn-down-view-ruler').className = isRuler ?
    'px-1.5 py-0.5 rounded bg-indigo-600 text-white font-medium' : 'px-1.5 py-0.5 rounded text-slate-400 hover:text-white';
  document.getElementById('btn-down-view-path').className = !isRuler ?
    'px-1.5 py-0.5 rounded bg-indigo-600 text-white font-medium' : 'px-1.5 py-0.5 rounded text-slate-400 hover:text-white';
  document.getElementById('down-track-container').classList.toggle('hidden', !isRuler);
  document.getElementById('down-path-container').classList.toggle('hidden', isRuler);
}

function renderLiveAnnotation() {
  const box = document.getElementById('live-annotation');
  const up100Count = rawGroupsData.filter(r => r.up_rate === 1.0).length;
  const down100Count = rawGroupsData.filter(r => r.up_rate === 0.0).length;

  box.innerHTML = `
    <div class="flex items-center gap-2 mb-1.5 font-bold text-slate-200">
      <span class="text-indigo-400">●</span> 实时统计数据契约说明
    </div>
    <div class="leading-relaxed space-y-1 text-slate-400">
      <div>• <b>样本口径</b>：当前标的为 <b>${currentStock}</b>，分组维度为 <b>${currentDim === 'P60' ? '60流月柱 (约5年一循环)' : '12月支 (每年立春至小寒)'}</b>，全部源自冻结 DuckDB 切片。</div>
      ${currentDim === 'P60' ? `<div>• <b>100% 极值几率发现</b>：在 60 组流月中，共检索出 <b>${up100Count}</b> 个 100% 全涨流月柱与 <b>${down100Count}</b> 个 100% 全跌流月柱。因 60 柱每 5 年循环一次，单只股票历史上遇同一柱仅约 3~7 次，请始终以小样本视角谨慎参考。</div>` : ''}
      <div>• <b>基线准则</b>：标尺白线 N1 为严格按月内交易日数与固定首末日求得的组合期望，绝不使用 0.5 伪基线；涨道与跌道各自独立计算条件零期望。</div>
    </div>
  `;
}

// 启动执行：先按会话快照把本栏自己的控件恢复，再拉数据
changeDim(currentDim, { noFetch: true });
// 视图默认落在「形态叠加折线」（DOM 初始是落点尺，这里显式同步按钮与面板）
switchUpView('path');
switchDownView('path');
setEncodingMode(currentEncodingMode);
setProbFilter(currentProbFilter);
const sortSelEl = document.getElementById('sort-selector');
if (sortSelEl) sortSelEl.value = currentSort;
v5Restoring = false;
addEventListener('pagehide', saveV5Ui);
fetchData();


    // 观察主站选中的标的卡片变化（顶栏搜索股票或切换 ETF 自动联动）
  window.onV5StockChange = function(sym) {
    v5MainSynced = true;
    if (sym && sym !== currentStock) {
      changeStock(sym);
    }
  };

  window.onV5TabActive = function(sym) {
    // 主站换标的时 onV5StockChange 已经同步；本栏自己查的票只在页签往返时保留，
    // 不再被 onV5TabActive 强行切回主站标的。
    if (!v5MainSynced) {
      const target = sym || (window.S && window.S.id) || currentStock;
      if (target && target !== currentStock) {
        changeStock(target);
        return;
      }
    }
    if (!rawGroupsData || !rawGroupsData.length) fetchData();
  };

  // 初始优先同步全局已选标的
  if (window.S && window.S.id) {
    currentStock = window.S.id;
  }

// 显式挂载到 window，确保 HTML 行内 onclick/onchange 响应正常
  Object.assign(window, {
    toggleWinRows, selectWinRow, highlightWinPath,
    changeStock, submitStock, changeDim, setEncodingMode,
    setProbFilter, changeSort, toggleSort, quickSelectGroup,
    selectGroup, switchUpView, switchDownView, highlightPath
  });

})();
