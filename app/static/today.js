(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const params = new URLSearchParams(location.search);
  const PREVIEW = params.has('preview');
  const S = { asof: '', list: 'L1', tier: 'all', latest: null, backtest: null, ledger: null, dates: [] };
  let previewData = null;

  const pct = (v, d = 1) => (v == null || Number.isNaN(Number(v)) ? '—' : `${(Number(v) * 100).toFixed(d)}%`);
  const signed = (v, d = 2, unit = '%') => {
    if (v == null || Number.isNaN(Number(v))) return '—';
    const n = Number(v);
    return `${n > 0 ? '+' : ''}${n.toFixed(d)}${unit}`;
  };
  const fmtEnd = (iso, kind) => {
    const s = String(iso || '');
    if (s.length < 16) return '—';
    const md = s.slice(5, 10);
    return kind === 'week' ? `${md} ${s.slice(11, 16)}` : md;
  };

  function applyTheme(light) {
    document.documentElement.classList.toggle('light', light);
    document.documentElement.classList.toggle('dark', !light);
    document.body.classList.toggle('theme-light', light);
    const btn = $('#theme-toggle');
    if (btn) btn.textContent = light ? '🌙' : '☀️';
  }

  async function loadPreview() {
    if (previewData) return previewData;
    const res = await fetch('preview_data.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('预览数据缺失');
    previewData = await res.json();
    return previewData;
  }

  async function api(path, query) {
    if (PREVIEW) {
      const data = await loadPreview();
      if (path === '/api/today/latest') {
        const list = (query && query.list) || 'L1';
        const tier = (query && query.tier) || 'all';
        const base = data.latest[list];
        if (!base) throw new Error('预览缺少名单');
        const rows = tier === 'all' ? base.rows : base.rows.filter((r) => tierOf(r) === tier);
        return Object.assign({}, base, { tier, rows, count: rows.length, list_total: base.rows.length });
      }
      if (path === '/api/today/backtest') return data.backtest;
      if (path === '/api/today/ledger') return data.ledger;
      if (path === '/api/today/dates') return data.dates;
      throw new Error('预览不支持的接口');
    }
    const qs = new URLSearchParams(query || {});
    const res = await fetch(`${path}?${qs.toString()}`, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      const msg = (body && body.error) || `请求失败（HTTP ${res.status}）`;
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function showStatus(message, isError) {
    const el = $('#status');
    el.textContent = message || '';
    el.classList.toggle('show', !!message);
    el.classList.toggle('err', !!isError);
  }

  function renderHeader(data) {
    const cal = data.calendar || {};
    const left = cal.month_end_at ? ` · 流月至 ${String(cal.month_end_at).slice(5, 10)}` : '';
    $('#kv-asof').textContent = `${data.asof || '—'}${data.backfill ? '（补跑）' : ''}`;
    $('#kv-entry').textContent = `${data.entry || '下一交易日开盘'}${data.entry_date ? `（${data.entry_date}）` : ''}`;
    $('#kv-cal').textContent = cal.month_id
      ? `${cal.pillar || ''} · ${cal.month_id}${cal.month_term ? `（${cal.month_term}月）` : ''} / 旬${cal.xun_no || '—'} / 段${cal.week_no || '—'}${left}`
      : '—';
    $('#kv-release').textContent = data.release_id || '—';
    const meta = data.stale
      ? `非最新名单（数据日期 ${data.asof}，距今 ${data.stale_days} 天）`
      : `${data.asof || ''} · 研究用途，不构成投资建议`;
    $('#head-meta').textContent = meta;
    $('#head-meta').classList.toggle('today-stale', !!data.stale);
  }

  function s1Fact() {
    const bt = S.backtest || {};
    const lists = bt.lists || [];
    const l1 = lists.find((r) => Number(r.horizon) === 5 && r.list === 'L1');
    const c3 = lists.find((r) => Number(r.horizon) === 5 && r.list === 'C3');
    if (!l1 || !c3) return '回测对照数据缺失';
    const x = Number(l1.b) * 100;
    const y = Number(c3.b) * 100;
    const d = Number(l1.d_C3_pp);
    const tb = Number(l1.t_C3);
    const r = Number(l1.meanR_vs_C3_bp);
    const tr = Number(l1.t_meanR_vs_C3);
    let word = '持平';
    if (Number.isFinite(tr)) {
      if (tr >= 2) word = '高于';
      else if (tr <= -2) word = '低于';
    }
    const fmt = (v, digits = 1) => (Number.isFinite(v) ? `${v >= 0 ? '+' : ''}${v.toFixed(digits)}` : '—');
    const fmtP = (v) => (Number.isFinite(v) ? `${v.toFixed(1)}%` : '—');
    const fmtT = (v) => (Number.isFinite(v) ? v.toFixed(2) : '—');
    return `回测 2018–2026 样本外：本榜 5 日跑赢率 ${fmtP(x)}，同档随机 ${fmtP(y)}`
      + `（${fmt(d)}pp，t=${fmtT(tb)}）；平均 5 日收益与同档随机${word}（${fmt(r)}bp，t=${fmtT(tr)}）`;
  }

  function renderCards(data) {
    const labels = data.labels || {};
    $('#s1-label').textContent = labels.S1 || '—';
    $('#s1-fact').textContent = s1Fact();
    const recent = (labels.S4_recent || [])[0];
    $('#s1-extra').textContent = recent
      ? `2025 年起 L1−C3 ${Number(recent.d_vs_C3_pp) >= 0 ? '+' : ''}${Number(recent.d_vs_C3_pp).toFixed(1)}pp（t=${Number(recent.t).toFixed(2)}）`
      : '';
    $('#s2-label').textContent = labels.S2 || '—';
    const bt = S.backtest || {};
    const s2 = (bt.details && bt.details.S2) || {};
    if (s2.ci95 && s2.ci95.length === 2) {
      $('#s2-detail').textContent =
        `ΔLL 均值 ${Number(s2.mean_dll).toFixed(6)}，95% 区间 [${Number(s2.ci95[0]).toFixed(6)}, ${Number(s2.ci95[1]).toFixed(6)}]`;
    } else {
      $('#s2-detail').textContent = '命理特征未检出增量（详见回测报告）';
    }
    const ledger = S.ledger || {};
    const summary = ledger.summary || {};
    const forward = summary.forward || {};
    const block = (forward.last_20 || {})['5'] || {};
    const l1 = block.L1 || {};
    const c3 = block.C3 || {};
    const days = (forward.days || []).length;
    if (days < 5 || !l1.n) {
      $('#ledger-label').textContent = '台账积累中';
      $('#ledger-detail').textContent =
        `前瞻 forward ${days} 天；补跑已计分 ${(summary.backfill && summary.backfill.days || []).length} 天单独统计，不计入前瞻成绩。`;
    } else {
      $('#ledger-label').textContent =
        `近20日：L1 ${pct(l1.beat_rate)} vs C3 ${pct(c3.beat_rate)}（n=${l1.n}）`;
      const all = ((forward.all || {})['5'] || {});
      $('#ledger-detail').textContent =
        `全部 forward ${days} 天：L1 ${pct((all.L1 || {}).beat_rate)} vs C3 ${pct((all.C3 || {}).beat_rate)}；VOID ${((all.L1 || {}).void ?? '—')}。`;
    }
  }

  const tierOf = (r) => r.cap_band || r.tier || '';

  function tierClass(tier) {
    if (tier === '1000亿以上' || tier === '500-1000亿') return 't-super';
    if (tier === '300-500亿' || tier === '100-300亿') return 't-mid';
    return 't-micro';
  }

  /* 档位下拉的选项来自接口（市值扩展库读不到时后端会回落到名单里冻结的档位），
     前端不另存一份清单，避免两边漂移。 */
  function syncTierOptions(options) {
    const sel = $('#tier');
    if (!sel || !options || !options.length) return;
    if (S.tier !== 'all' && !options.includes(S.tier)) S.tier = 'all';
    if (sel.dataset.built === options.join('|')) { sel.value = S.tier; return; }
    sel.dataset.built = options.join('|');
    sel.innerHTML = '';
    const add = (v, t) => {
      const o = document.createElement('option');
      o.value = v; o.textContent = t; o.selected = S.tier === v;
      sel.appendChild(o);
    };
    add('all', '全部市值档');
    options.forEach((b) => add(b, b));
  }

  function stopTitle() {
    const stops = ((S.backtest || {}).stop_stats || []);
    const row = stops.find((r) => r.scope === 'L1_h5');
    if (!row || row.break_rate == null) return '回测触发比例见回测报告';
    return `回测中 L1 收盘跌破失效线约 ${(Number(row.break_rate) * 100).toFixed(1)}%`;
  }

  function renderTable(data) {
    const rows = data.rows || [];
    const tbody = $('#rows');
    tbody.innerHTML = '';
    const empty = $('#table-empty');
    const total = Number(data.list_total ?? rows.length);
    const count = Number(data.count ?? rows.length);
    let note;
    if (total < 20) note = `本次 ${total} 只（命理池不足 20）`;
    else if (S.tier !== 'all') note = `筛选 ${count}/${total} 只`;
    else note = `共 ${total} 只`;
    if (data.n_universe != null) note += ` · 可交易 ${data.n_universe} 只 · 命理池 ${data.n_pool} 只`;
    if (data.tier_asof_month) note += ` · 市值档按 ${data.tier_asof_month} 月末快照`;
    $('#count-note').textContent = note;

    if (!rows.length) {
      empty.style.display = '';
      empty.textContent = total ? '当前市值档筛选下没有股票' : '今日名单为空';
      return;
    }
    empty.style.display = 'none';
    const title = stopTitle();
    rows.forEach((r) => {
      const m = r.m || [0, 0], x = r.x || [0, 0], w = r.w || [0, 0];
      const r5 = Number(r.r5);
      const r5cls = r5 > 0 ? 'today-up' : (r5 < 0 ? 'today-down' : 'today-flat');
      const link = `/?id=${encodeURIComponent(r.code)}&tab=stock&from=board`;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${r.rank}</td>
        <td><a class="today-code" href="${link}">${esc(r.code)}</a><span class="today-name">${esc(r.name)}</span></td>
        <td><span class="today-badge ${tierClass(tierOf(r))}" title="按 ${esc(data.tier_asof_month || '所选流月')} 月末自由流通市值快照分档">${esc(tierOf(r))}</span></td>
        <td>${pct(r.p5_cal)}</td>
        <td>${pct(r.p10_cal)}</td>
        <td>${r.res_up}</td>
        <td class="today-hist" title="历史描述，不是概率">月 ${m[0]}/${m[1]} · 旬 ${x[0]}/${x[1]} · 段 ${w[0]}/${w[1]}</td>
        <td class="${r5cls}">${signed(r5 * 100, 2)}</td>
        <td>${pct(r.vol20, 2)}</td>
        <td>±${(Number(r.band5) * 100).toFixed(1)}%</td>
        <td title="${esc(title)}">${Number(r.stop_price).toFixed(2)}</td>
        <td>5–10 个交易日<br><span class="today-muted">本段至 ${fmtEnd(data.calendar && data.calendar.week_end_at, 'week')} · 本旬至 ${fmtEnd(data.calendar && data.calendar.xun_end_at, 'xun')}</span></td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function loadLatest() {
    showStatus('读取名单中…', false);
    try {
      const query = { list: S.list, tier: S.tier };
      if (S.asof) query.asof = S.asof;
      const data = await api('/api/today/latest', query);
      S.latest = data;
      syncTierOptions(data.tier_options || [...new Set((data.rows || []).map(tierOf).filter(Boolean))]);
      showStatus('', false);
      renderHeader(data);
      renderCards(data);
      renderTable(data);
    } catch (e) {
      showStatus(`今日关注暂不可用：${e.message}`, true);
      ['kv-asof', 'kv-entry', 'kv-cal', 'kv-release'].forEach((id) => {
        const el = $('#' + id);
        if (el) el.textContent = '—';
      });
      $('#head-meta').textContent = '—';
      $('#ledger-label').textContent = '—';
      $('#ledger-detail').textContent = '—';
      $('#rows').innerHTML = '';
      $('#table-empty').style.display = '';
      $('#table-empty').textContent = '暂无名单';
      $('#count-note').textContent = '—';
    }
  }

  async function loadStatic() {
    const results = await Promise.allSettled([
      api('/api/today/backtest'), api('/api/today/ledger', { days: '20' }), api('/api/today/dates'),
    ]);
    if (results[0].status === 'fulfilled') S.backtest = results[0].value;
    if (results[1].status === 'fulfilled') S.ledger = results[1].value;
    if (results[2].status === 'fulfilled') {
      S.dates = (results[2].value && results[2].value.dates) || [];
      const sel = $('#asof');
      sel.innerHTML = '<option value="">最新名单</option>' +
        S.dates.map((d) => `<option value="${esc(d.asof)}">${esc(d.asof)}${d.backfill ? ' · 补跑' : ''}</option>`).join('');
    }
  }

  function bind() {
    $('#list-tabs').addEventListener('click', (ev) => {
      const btn = ev.target.closest('button[data-list]');
      if (!btn) return;
      S.list = btn.dataset.list;
      document.querySelectorAll('#list-tabs button').forEach((b) => b.classList.toggle('on', b === btn));
      loadLatest();
    });
    $('#tier').addEventListener('change', (ev) => { S.tier = ev.target.value; loadLatest(); });
    $('#asof').addEventListener('change', (ev) => { S.asof = ev.target.value; loadLatest(); });
    $('#export').addEventListener('click', () => {
      if (PREVIEW) return;
      const q = new URLSearchParams({ list: S.list, tier: S.tier });
      if (S.asof) q.set('asof', S.asof);
      window.location.href = `/api/today/export?${q.toString()}`;
    });
    $('#theme-toggle').addEventListener('click', () => {
      const light = !document.documentElement.classList.contains('light');
      localStorage.setItem('bazi_theme', light ? 'light' : 'dark');
      applyTheme(light);
    });
  }

  async function init() {
    const theme = params.get('theme');
    const stored = localStorage.getItem('bazi_theme');
    applyTheme(theme ? theme === 'light' : stored === 'light');
    bind();
    $('#export').style.display = PREVIEW ? 'none' : '';
    await loadStatic();
    await loadLatest();
  }

  init().catch((e) => showStatus(`今日关注加载失败：${e.message}`, true));
})();
