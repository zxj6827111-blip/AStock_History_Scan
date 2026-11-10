(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const state = { meta: null, code: '', dir: 'up', offset: 0, limit: 100, rows: [], hasMore: false, seq: 0 };

  const fmtInt = (n) => (n == null || Number.isNaN(Number(n)) ? '—' : Number(n).toLocaleString('zh-CN'));
  const fmtPct = (v) => (v == null || Number.isNaN(Number(v)) ? '—' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2) + '%');
  const signCls = (v) => (Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'down' : ''));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const numVal = (sel, dflt) => { const v = $(sel).value.trim(); return v === '' ? dflt : Number(v); };

  const PRESETS = {
    short_high:   { dir: 'up',   n: '5',     lmin: 1, lmax: 5, mean: 5, worst: 0, sort: 'score' },
    short_high_3: { dir: 'up',   n: '3',     lmin: 1, lmax: 5, mean: 5, worst: 0, sort: 'score' },
    d3:           { dir: 'up',   n: '5',     lmin: 3, lmax: 3, mean: 0, worst: 0, sort: 'mean' },
    d5:           { dir: 'up',   n: '5',     lmin: 5, lmax: 5, mean: 0, worst: 0, sort: 'mean' },
    only3:        { dir: 'up',   n: 'only3', lmin: 1, lmax: 5, mean: 5, worst: 0, sort: 'mean' },
    down:         { dir: 'down', n: '3',     lmin: 1, lmax: 5, mean: '', worst: '', sort: 'mean' },
    reset:        { dir: 'up',   n: '5',     lmin: 1, lmax: 5, mean: 5, worst: 0, sort: 'score' }
  };

  function setDir(dir) {
    state.dir = dir;
    $$('#dir-seg [data-dir]').forEach((b) => {
      const on = b.dataset.dir === dir;
      b.style.background = on ? 'var(--accent-indigo)' : 'transparent';
      b.style.color = on ? '#fff' : 'var(--text-sub)';
      b.style.fontWeight = on ? '600' : '400';
    });
    $('#wrap-mean').classList.toggle('hidden', dir !== 'up');
    $('#wrap-worst').classList.toggle('hidden', dir !== 'up');
  }

  function applyPreset(name) {
    const p = PRESETS[name];
    if (!p) return;
    setDir(p.dir);
    $('#f-n').value = p.n;
    $('#f-len-min').value = p.lmin;
    $('#f-len-max').value = p.lmax;
    $('#f-mean').value = p.mean;
    $('#f-worst').value = p.worst;
    $('#f-sort').value = p.sort;
    $('#f-pillar').value = '';
    state.code = '';
    $('#f-code').value = '';
    hideSuggest();
    $$('#presets .rv-btn').forEach((b) => b.classList.toggle('rv-btn-pri', b.dataset.preset === name));
    run(true);
  }

  function buildParams(includePage) {
    const dir = state.dir;
    const nv = $('#f-n').value;
    let nMin = 5, nMax = 0;
    if (nv === 'only3') { nMin = 3; nMax = 3; } else { nMin = parseInt(nv, 10) || 5; }
    const sort = $('#f-sort').value;
    let order = 'desc';
    if (sort === 'mean' || sort === 'median' || sort === 'min') order = dir === 'down' ? 'asc' : 'desc';
    const p = {
      direction: dir, n_min: nMin,
      len_min: numVal('#f-len-min', 1), len_max: numVal('#f-len-max', 5),
      all_sign: 1, sort: sort, order: order
    };
    if (nMax) p.n_max = nMax;
    if (state.code) p.code = state.code;
    const pillar = $('#f-pillar').value;
    if (pillar) p.pillar = pillar;
    if (dir === 'up') {
      if ($('#f-mean').value.trim() !== '') p.mean_min = Number($('#f-mean').value);
      if ($('#f-worst').value.trim() !== '') p.min_min = Number($('#f-worst').value);
    }
    if (includePage) { p.limit = state.limit; p.offset = state.offset; }
    return p;
  }

  function writeUrl(p) {
    const u = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => { if (v !== '' && v != null) u.set(k, v); });
    history.replaceState(null, '', location.pathname + '?' + u.toString());
  }

  function readUrl() {
    const q = new URLSearchParams(location.search);
    if (!Array.from(q.keys()).length) return false;
    setDir(q.get('direction') === 'down' ? 'down' : 'up');
    const nMin = Number(q.get('n_min')) || 5;
    const nMax = Number(q.get('n_max')) || 0;
    $('#f-n').value = nMax === 3 ? 'only3' : String(Math.min(6, Math.max(3, nMin)));
    $('#f-len-min').value = q.get('len_min') || 1;
    $('#f-len-max').value = q.get('len_max') || 5;
    $('#f-mean').value = q.has('mean_min') ? q.get('mean_min') : (q.get('direction') === 'down' ? '' : 5);
    $('#f-worst').value = q.has('min_min') ? q.get('min_min') : (q.get('direction') === 'down' ? '' : 0);
    $('#f-sort').value = q.get('sort') || 'score';
    if (q.get('pillar')) $('#f-pillar').value = q.get('pillar');
    if (q.get('code')) { state.code = q.get('code'); $('#f-code').value = state.code; }
    return true;
  }

  function setLoading(on) {
    $('#loading').classList.toggle('hidden', !on);
    $('#btn-query').disabled = !!on;
    if (on) $('#empty').classList.add('hidden');
  }

  function showErr(msg) {
    const el = $('#err');
    el.textContent = msg;
    el.style.display = 'block';
  }
  function hideErr() { const el = $('#err'); el.style.display = 'none'; el.textContent = ''; }

  async function resolveCode() {
    const raw = $('#f-code').value.trim();
    if (!raw) { state.code = ''; return true; }
    if (/^\d{6}\.(SH|SZ)$/i.test(raw)) { state.code = raw.toUpperCase(); return true; }
    if (state.code) return true;
    const m = raw.match(/\d{6}/);
    if (!m) { showErr('请从下拉建议里选一只股票，或输入 000859.SZ 这样的代码'); return false; }
    try {
      const r = await fetch('/api/search?q=' + encodeURIComponent(m[0]) + '&limit=8', { credentials: 'same-origin' });
      const arr = await r.json();
      const exact = (Array.isArray(arr) ? arr : []).find((x) =>
        String(x.identity_code || '').toUpperCase() === m[0].toUpperCase() ||
        String(x.security_code || '') === m[0]);
      if (!exact) { showErr('没找到这只票，请从下拉建议里选'); return false; }
      state.code = exact.identity_code;
      $('#f-code').value = state.code + ' ' + (exact.name || '');
      return true;
    } catch (e) {
      showErr('股票搜索失败：' + e.message);
      return false;
    }
  }

  async function run(forceReset) {
    if (forceReset) state.offset = 0;
    hideErr();
    if (!(await resolveCode())) return;
    const my = ++state.seq;
    setLoading(true);
    try {
      const p = buildParams(true);
      const r = await fetch('/api/seg/query?' + new URLSearchParams(p), { credentials: 'same-origin' });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
      if (my !== state.seq) return;
      state.rows = j.rows || [];
      state.hasMore = !!j.has_more;
      render();
      $('#elapsed').textContent = '查询 ' + j.elapsed_ms + ' ms';
      writeUrl(buildParams(false));
      loadCount(buildParams(false), my);
    } catch (e) {
      if (my === state.seq) showErr(e.message || String(e));
    } finally {
      if (my === state.seq) setLoading(false);
    }
  }

  async function loadCount(p, my) {
    $('#count-note').textContent = '命中统计中…';
    try {
      const r = await fetch('/api/seg/count?' + new URLSearchParams(p), { credentials: 'same-origin' });
      const j = await r.json();
      if (my !== state.seq) return;
      if (r.ok) $('#count-note').textContent = '命中 ' + fmtInt(j.count) + ' 条';
      else $('#count-note').textContent = '命中统计失败';
    } catch (e) {
      if (my === state.seq) $('#count-note').textContent = '命中统计失败';
    }
  }

  function badgeClass(n) { return n >= 5 ? 'n5' : (n === 4 ? 'n4' : 'n3'); }

  function render() {
    const tb = $('#tbl-body');
    $('#empty').classList.toggle('hidden', state.rows.length > 0);
    tb.innerHTML = state.rows.map((r, i) => {
      const dirTxt = r.direction === 'up' ? '100%涨' : '100%跌';
      const dirCls = r.direction === 'up' ? 'up' : 'down';
      return '<tr data-i="' + i + '">' +
        '<td><div class="seg-code">' + esc(r.code) + '</div><div class="seg-name">' + esc(r.name || '') + '</div></td>' +
        '<td>' + esc(r.pillar) + '</td>' +
        '<td class="' + dirCls + '">' + dirTxt + '</td>' +
        '<td><span class="seg-badge ' + badgeClass(r.n_valid) + '" title="命中 ' + r.n_valid + ' 次 / 有行情 ' + r.n_seen + ' 次 / 全柱 ' + r.n_months + ' 次">' + r.n_valid + '/' + r.n_seen + '</span></td>' +
        '<td>' + Number(r.length_days).toFixed(2) + ' 天</td>' +
        '<td class="seg-mono">第' + r.start_day + '日 ' + esc(r.start_time) + ' → 第' + r.end_day + '日 ' + esc(r.end_time) + '</td>' +
        '<td class="' + signCls(r.mean_pct) + '">' + fmtPct(r.mean_pct) + '</td>' +
        '<td class="' + signCls(r.median_pct) + '">' + fmtPct(r.median_pct) + '</td>' +
        '<td class="' + signCls(r.min_pct) + '">' + fmtPct(r.min_pct) + '</td>' +
        '<td class="' + signCls(r.max_pct) + '">' + fmtPct(r.max_pct) + '</td>' +
        '<td>' + r.year_min + '–' + r.year_max + '</td>' +
        '<td><button type="button" class="rv-btn rv-btn-sm" data-detail="' + i + '">明细</button></td>' +
        '</tr>';
    }).join('');
    const n = state.rows.length;
    $('#page-note').textContent = n ? '显示第 ' + fmtInt(state.offset + 1) + '–' + fmtInt(state.offset + n) + ' 条' : '';
    $('#prev').disabled = state.offset <= 0;
    $('#next').disabled = !state.hasMore || state.offset + state.limit >= 5000;
    $('#page-info').textContent = '第 ' + (Math.floor(state.offset / state.limit) + 1) + ' 页';
  }

  function openDrawer(r) {
    const head = document.querySelector('header');
    const hh = head ? Math.round(head.getBoundingClientRect().height) : 52;
    document.documentElement.style.setProperty('--seg-head-h', hh + 'px');
    $('#drawer-title').textContent = r.code + ' ' + (r.name || '');
    const ids = String(r.month_ids || '').split(';').filter(Boolean);
    const rets = String(r.month_rets || '').split(';').filter(Boolean).map(Number);
    const maxAbs = Math.max(0.0001, ...rets.map((v) => Math.abs(v)));
    const bars = ids.map((m, i) => {
      const v = rets[i];
      const pct = v * 100;
      const w = Math.min(100, Math.abs(v) / maxAbs * 100);
      const bg = v >= 0 ? 'var(--stock-up)' : 'var(--stock-down)';
      return '<div class="seg-bar-row"><span class="seg-mono">' + esc(m) + '</span>' +
        '<div class="seg-bar-track"><div class="seg-bar-fill" style="width:' + w + '%;background:' + bg + '"></div></div>' +
        '<b class="' + signCls(pct) + '" style="text-align:right">' + fmtPct(pct) + '</b></div>';
    }).join('');
    const dirTxt = r.direction === 'up' ? '100% 涨' : '100% 跌';
    $('#drawer-body').innerHTML =
      '<div class="seg-kv">' +
        '<span class="k">流月柱</span><span>' + esc(r.pillar) + '</span>' +
        '<span class="k">方向</span><span class="' + (r.direction === 'up' ? 'up' : 'down') + '">' + dirTxt + '</span>' +
        '<span class="k">次数</span><span>' + r.n_valid + ' / ' + r.n_seen + '（全柱出现 ' + r.n_months + ' 次，可计算 ' + r.n_avail + ' 次）</span>' +
        '<span class="k">区间</span><span>第' + r.start_day + '个交易日 ' + esc(r.start_time) + ' → 第' + r.end_day + '个交易日 ' + esc(r.end_time) + '（' + Number(r.length_days).toFixed(2) + ' 天）</span>' +
        '<span class="k">年份</span><span>' + r.year_min + '–' + r.year_max + '</span>' +
      '</div>' +
      '<div class="seg-stat mt-3">' +
        '<div><span class="rv-note">平均</span><b class="' + signCls(r.mean_pct) + '">' + fmtPct(r.mean_pct) + '</b></div>' +
        '<div><span class="rv-note">中位</span><b class="' + signCls(r.median_pct) + '">' + fmtPct(r.median_pct) + '</b></div>' +
        '<div><span class="rv-note">最小</span><b class="' + signCls(r.min_pct) + '">' + fmtPct(r.min_pct) + '</b></div>' +
        '<div><span class="rv-note">最大</span><b class="' + signCls(r.max_pct) + '">' + fmtPct(r.max_pct) + '</b></div>' +
      '</div>' +
      '<div class="mt-4"><div class="rv-note" style="margin-bottom:6px">这 ' + r.n_valid + ' 次分别涨跌多少</div>' + bars + '</div>' +
      '<div class="mt-4 flex flex-wrap gap-2">' +
        '<a class="rv-btn rv-btn-pri" href="/?id=' + encodeURIComponent(r.code) + '&tab=overview">进入个股档案</a>' +
        '<button type="button" class="rv-btn" id="copy-code">复制代码</button>' +
      '</div>' +
      '<p class="rv-note mt-3">这里只是这 ' + r.n_valid + ' 次的历史；不是未来保证。3/3 尤其要看中位和最差一次。</p>';
    $('#drawer').classList.add('open');
    $('#overlay').classList.add('open');
    const btn = $('#copy-code');
    if (btn) btn.onclick = () => {
      const done = () => { btn.textContent = '已复制'; setTimeout(() => { btn.textContent = '复制代码'; }, 1200); };
      if (navigator.clipboard) navigator.clipboard.writeText(r.code).then(done).catch(done);
      else done();
    };
  }

  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#overlay').classList.remove('open');
  }

  let sugTimer = null;
  function hideSuggest() { $('#code-suggest').classList.add('hidden'); $('#code-suggest').innerHTML = ''; }

  async function suggest(q) {
    try {
      const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&limit=8', { credentials: 'same-origin' });
      const arr = await r.json();
      const list = (Array.isArray(arr) ? arr : []).filter((x) => !x.security_type || x.security_type === 'A_SHARE');
      if (!list.length) { hideSuggest(); return; }
      const box = $('#code-suggest');
      box.innerHTML = list.map((x) =>
        '<button type="button" data-code="' + esc(x.identity_code) + '" data-name="' + esc(x.name || '') + '">' +
        '<span class="seg-mono">' + esc(x.identity_code) + '</span><span class="seg-name">' + esc(x.name || '') + '</span></button>'
      ).join('');
      box.classList.remove('hidden');
    } catch (e) {
      hideSuggest();
    }
  }

  function bind() {
    $$('#presets [data-preset]').forEach((b) => { b.onclick = () => applyPreset(b.dataset.preset); });
    $$('#dir-seg [data-dir]').forEach((b) => { b.onclick = () => { setDir(b.dataset.dir); }; });
    $('#btn-query').onclick = () => run(true);
    $('#btn-reset').onclick = () => applyPreset('short_high');
    $('#btn-export').onclick = () => {
      location.href = '/api/seg/export?' + new URLSearchParams(buildParams(false)).toString();
    };
    $('#prev').onclick = () => { if (state.offset > 0) { state.offset = Math.max(0, state.offset - state.limit); run(false); } };
    $('#next').onclick = () => { if (state.hasMore && state.offset + state.limit < 5000) { state.offset += state.limit; run(false); } };
    $('#f-code-clear').onclick = () => { state.code = ''; $('#f-code').value = ''; hideSuggest(); };
    $('#f-code').addEventListener('input', () => {
      state.code = '';
      const q = $('#f-code').value.trim();
      clearTimeout(sugTimer);
      if (!q) { hideSuggest(); return; }
      sugTimer = setTimeout(() => suggest(q), 180);
    });
    $('#f-code').addEventListener('keydown', (e) => { if (e.key === 'Enter') { hideSuggest(); run(true); } });
    $('#code-suggest').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-code]');
      if (!b) return;
      state.code = b.dataset.code;
      $('#f-code').value = b.dataset.code + ' ' + (b.dataset.name || '');
      hideSuggest();
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#f-code') && !e.target.closest('#code-suggest')) hideSuggest();
    });
    $('#tbl-body').addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-i]');
      if (tr) openDrawer(state.rows[Number(tr.dataset.i)]);
    });
    $('#drawer-close').onclick = closeDrawer;
    $('#overlay').onclick = closeDrawer;
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  }

  async function init() {
    try {
      if (localStorage.getItem('bazi_theme') === 'light') {
        document.documentElement.classList.remove('dark');
        document.documentElement.classList.add('light');
        document.body.classList.add('theme-light');
      }
    } catch (e) {}
    bind();
    try {
      const r = await fetch('/api/seg/meta', { credentials: 'same-origin' });
      const m = await r.json();
      if (!r.ok) throw new Error(m.error || ('HTTP ' + r.status));
      state.meta = m;
      $('#f-pillar').innerHTML = '<option value="">全部</option>' +
        (m.pillars || []).map((p) => '<option>' + esc(p) + '</option>').join('');
      $('#meta-note').textContent = 'A股 · ' + m.months + ' · ' + fmtInt(m.n_segments) + ' 条 · ' + fmtInt(m.n_stocks) + ' 只';
    } catch (e) {
      showErr('数据信息读取失败：' + e.message);
    }
    if (!readUrl()) applyPreset('short_high');
    else run(true);
  }

  init();
})();
