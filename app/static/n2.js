(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const PAGE = 100;
  const CLASH = new Set(['六冲', '天干相冲', '天克地冲', '反吟', '相刑', '三刑', '自刑', '相害', '六破']);
  const HARMONY = new Set(['六合', '三合', '半合', '天干五合', '天合地合', '三会']);

  const STEMS = '甲乙丙丁戊己庚辛壬癸';
  const BRANCHES = '子丑寅卯辰巳午未申酉戌亥';
  const PILLARS = Array.from({ length: 60 }, (_, i) => STEMS[i % 10] + BRANCHES[i % 12]);
  const REQ = { stock: 0, day: 0, rel: 0 };  // 丢弃过期响应：连续改筛选时后发的请求必须赢
  const S = {
    meta: null, panel: 'stock',
    stock: { id: '600519.SH', offset: 0, total: 0 },
    day: { date: '', dir: '', offset: 0, total: 0 },
    rel: { date: '', rel: '六冲', offset: 0, total: 0 },
    stats: { dim: 'pillar', caliber: 'MAIN' },
    month: { all: null, baseline: null },
  };

  const nf = (v, d = 0) => (v == null || Number.isNaN(Number(v)) ? '—'
    : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v, d = 3) => (v == null || Number.isNaN(Number(v)) ? '—'
    : `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(d)}%`);
  const pp = (v, d = 3) => (v == null || Number.isNaN(Number(v)) ? '—'
    : `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(d)}pp`);
  const cls = (v) => (v == null ? '' : (Number(v) > 0 ? 'n2-up' : (Number(v) < 0 ? 'n2-down' : 'n2-flat')));
  const DIR = { UP: '涨', DOWN: '跌', FLAT: '平' };
  const dirWord = (d) => DIR[d] || esc(d || '—');
  const shortTime = (s) => (s ? String(s).replace('T', ' ').slice(0, 16) : '—');
  const fmtShare = (v) => (v == null || Number.isNaN(Number(v)) ? '—' : `${Number(v).toFixed(1)}%`);

  function status(message, isError) {
    const el = $('#status');
    el.textContent = message || '';
    el.classList.toggle('show', !!message);
    el.classList.toggle('err', !!isError);
  }

  async function api(path, query) {
    const qs = new URLSearchParams(query || {});
    const res = await fetch(`${path}?${qs.toString()}`, {
      headers: { Accept: 'application/json' }, cache: 'no-store',
    });
    if (res.status === 401) {
      location.href = '/login?redirect=' + encodeURIComponent('/n2');
      throw new Error('未登录');
    }
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok) {
      const err = new Error((body && (body.error || body.message)) || `请求失败（HTTP ${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function fillSelect(el, values) {
    if (!el) return;
    const head = el.querySelector('option');
    el.innerHTML = (head ? head.outerHTML : '<option value="">全部</option>')
      + values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  }

  function relTags(list) {
    if (!list || !list.length) return '<span class="n2-rel">无</span>';
    return list.map((r) => {
      const k = CLASH.has(r) ? 'clash' : (HARMONY.has(r) ? 'harmony' : '');
      return `<span class="n2-rel ${k}">${esc(r)}</span>`;
    }).join('');
  }

  function quality(q, valid) {
    if (Number(valid) === 1) return `<span class="n2-pill">${esc(q || 'OK')}</span>`;
    return `<span class="n2-pill bad" title="异常收益：不计入均值分母">${esc(q || '异常')} · 不计入</span>`;
  }

  function disputeTag(on) {
    return on ? '<span class="n2-pill dispute" title="边界争议身份：主口径保留，EXCL_DISPUTED 口径剔除">边界争议</span>' : '';
  }

  function kv(k, v) {
    return `<div class="n2-kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
  }

  // ---------- meta ----------
  async function boot() {
    const m = await api('/api/n2/meta');
    S.meta = m;
    const cov = m.coverage || {};
    $('#chip-cutoff').innerHTML = `数据截止 <b>${esc(cov.last_a_share || '—')}</b>`;
    $('#chip-rule').innerHTML = `<b>${esc(m.calculation_mode || '—')}</b> · ${esc(m.rule_version || '')}`;
    $('#chip-facts').innerHTML = `日事实 <b>${nf(m.counts.daily_facts)}</b> 行`;
    $('#chip-stocks').innerHTML = `A股 <b>${nf(m.counts.a_share)}</b> 只 / 争议 <b>${nf(m.disputed_identities)}</b> 只`;
    $('#chip-tpl').innerHTML = `模板 <b>${nf(m.counts.templates)}</b> · 关系事件 <b>${nf(m.counts.template_events)}</b>`;
    $('#chip-built').innerHTML = `构建 <b>${esc(shortTime(m.built_at_bjt))}</b>`;
    $('#chip-stats').innerHTML = `统计文件 <b>${esc(m.stats_file_time || "—")}</b>`;
    if (m.build_status !== 'COMPLETE') {
      status(`影子库状态 ${m.build_status || '未知'}，不是已发布库，页面数据仅供排查`, true);
    }

    for (const id of ['#day-date', '#rel-date']) {
      const el = $(id);
      el.min = cov.first || '';
      el.max = cov.last_a_share || '';
      el.value = cov.last_a_share || '';
    }
    S.day.date = cov.last_a_share || '';
    S.rel.date = cov.last_a_share || '';

    const sel = $('#stats-caliber');
    sel.innerHTML = (m.calibers || []).map((c) => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join('');
    fillSelect($('#stock-pillar'), PILLARS);
    fillSelect($('#day-pillar'), PILLARS);
    fillSelect($('#stock-rel'), m.relation_types || []);
    fillSelect($('#day-rel'), m.relation_types || []);
    fillSelect($('#stock-tg'), m.ten_gods || []);
    fillSelect($('#day-tg'), m.ten_gods || []);
    fillSelect($('#rel-tg'), m.ten_gods || []);
    const relsel = $('#rel-type');
    relsel.innerHTML = (m.relation_types || []).map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
    if ((m.relation_types || []).includes('六冲')) relsel.value = '六冲';
  }

  // ---------- 1 个股流日 ----------
  async function loadStock() {
    const st = S.stock;
    const my = ++REQ.stock;
    status('');
    $('#stock-go').disabled = true;
    try {
      const out = await api('/api/n2/stock', Object.assign({ id: st.id, limit: PAGE, offset: st.offset }, stockFilters()));
      if (my !== REQ.stock) return;
      const id = out.identity;
      st.total = out.n_rows;
      const sm = out.summary;
      const act = filterLabels(stockFilters());
      const cards = [
        kv('代码', `<span class="n2-code">${esc(id.code)}</span> <span class="n2-name">${esc(id.name)}</span> ${disputeTag(id.disputed)}`),
        kv('本命三柱', `<span style="font-family:var(--font-mono)">${esc(id.pillars)}</span>`),
        kv('全部行 / 命中', `${nf(out.n_total)} / <b>${nf(sm.n_rows)}</b>`),
        kv('涨 / 跌 / 平', `${nf(sm.n_up)} / ${nf(sm.n_down)} / ${nf(sm.n_flat)}`),
        kv('均值', `<span class="${cls(sm.mean_pct)}">${pct(sm.mean_pct)}</span>`),
      ];
      if (act.length) cards.push(kv('当前筛选', esc(act.join(' · '))));
      $('#stock-card').innerHTML = cards.join('');
      const body = $('#stock-table tbody');
      if (!out.rows.length) {
        body.innerHTML = '<tr><td colspan="10"><div class="n2-empty">此区间没有数据</div></td></tr>';
      } else {
        body.innerHTML = out.rows.map((r) => `<tr>
          <td>${esc(r.date)}</td>
          <td><span style="font-family:var(--font-mono)">${esc(r.pillar)}</span></td>
          <td class="num">${nf(r.open, 2)}</td>
          <td class="num">${nf(r.close, 2)}</td>
          <td class="num ${cls(r.ret_pct)}">${pct(r.ret_pct)}</td>
          <td>${dirWord(r.direction)}</td>
          <td>${esc(r.ten_god || '—')}</td>
          <td>${relTags(r.relations)}</td>
          <td class="num">${nf(r.n_events)}</td>
          <td>${quality(r.quality, r.valid)}</td>
        </tr>`).join('');
      }
      const from = st.total ? st.offset + 1 : 0;
      const to = Math.min(st.offset + PAGE, st.total);
      $('#stock-page').textContent = `第 ${nf(from)}–${nf(to)} 行 / 共 ${nf(st.total)} 行`;
      $('#stock-prev').disabled = st.offset <= 0;
      $('#stock-next').disabled = to >= st.total;
    } catch (e) {
      if (my !== REQ.stock) return;
      status(e.message, true);
      $('#stock-card').innerHTML = '';
      $('#stock-table tbody').innerHTML = '';
      $('#stock-page').textContent = '';
    } finally {
      if (my === REQ.stock) $('#stock-go').disabled = false;
    }
  }

  function stockFilters() {
    return {
      from: $('#stock-from').value, to: $('#stock-to').value,
      pillar: $('#stock-pillar').value, rel: $('#stock-rel').value,
      dir: $('#stock-dir').value, ten_god: $('#stock-tg').value,
    };
  }

  function dayFilters() {
    return {
      dir: $('#day-dir').value, pillar: $('#day-pillar').value,
      rel: $('#day-rel').value, ten_god: $('#day-tg').value,
    };
  }

  function relFilters() {
    return { dir: $('#rel-dir').value, ten_god: $('#rel-tg').value };
  }

  function filterLabels(f) {
    const out = [];
    if (f.from || f.to) out.push(`${f.from || '起始'}～${f.to || '最新'}`);
    if (f.pillar) out.push(`流日柱 ${f.pillar}`);
    if (f.rel) out.push(`含 ${f.rel}`);
    if (f.dir && f.dir !== 'ALL') out.push(DIR[f.dir] || f.dir);
    if (f.ten_god) out.push(`十神 ${f.ten_god}`);
    return out;
  }

  // ---------- 2 单日全市场 / 3 单日×关系 ----------
  function dayRows(rows) {
    if (!rows.length) return '<tr><td colspan="8"><div class="n2-empty">这一天没有符合条件的行</div></td></tr>';
    return rows.map((r) => `<tr>
      <td><span class="n2-code">${esc(r.code)}</span><span class="n2-name">${esc(r.name)}</span> ${disputeTag(r.disputed)}</td>
      <td><span style="font-family:var(--font-mono)">${esc(r.pillar)}</span></td>
      <td class="num ${cls(r.ret_pct)}">${pct(r.ret_pct)}</td>
      <td>${dirWord(r.direction)}</td>
      <td>${esc(r.ten_god || '—')}</td>
      <td>${relTags(r.relations)}</td>
      <td class="num">${nf(r.n_events)}</td>
      <td>${quality(r.quality, r.valid)}</td>
    </tr>`).join('');
  }

  function dayCards(key, out, extras) {
    const sm = out.summary || {};
    const cards = [
      kv('交易日', esc(out.date)),
      kv('命中行数', `<b>${nf(out.n_rows)}</b>`),
      kv('涨 / 跌 / 平', `${nf(sm.n_up)} / ${nf(sm.n_down)} / ${nf(sm.n_flat)}`),
      kv('有效样本', nf(sm.n_valid)),
      kv('当日均值', `<span class="${cls(sm.mean_pct)}">${pct(sm.mean_pct)}</span>`),
    ];
    (extras || []).forEach((x) => { if (x) cards.push(x); });
    $('#' + key + '-card').innerHTML = cards.join('');
  }

  async function loadDay() {
    const st = S.day;
    const my = ++REQ.day;
    status('');
    $('#day-go').disabled = true;
    try {
      const out = await api('/api/n2/day', Object.assign({
        date: st.date, limit: PAGE, offset: st.offset,
      }, dayFilters()));
      if (my !== REQ.day) return;
      st.total = out.n_rows;
      dayCards('day', out, [kv('当前筛选', esc(filterLabels(dayFilters()).join(' · ') || '无'))]);
      $('#day-table tbody').innerHTML = dayRows(out.rows);
      pageInfo('day', st.total);
    } catch (e) {
      if (my !== REQ.day) return;
      status(e.message, true);
    } finally {
      if (my === REQ.day) $('#day-go').disabled = false;
    }
  }

  async function loadRel() {
    const st = S.rel;
    const my = ++REQ.rel;
    status('');
    $('#rel-go').disabled = true;
    try {
      const out = await api('/api/n2/day_relation', Object.assign({
        date: st.date, rel: st.rel, limit: PAGE, offset: st.offset,
      }, relFilters()));
      if (my !== REQ.rel) return;
      st.total = out.n_rows;
      dayCards('rel', out, [kv('关系类型', esc(st.rel)),
                            kv('当前筛选', esc(filterLabels(relFilters()).join(' · ') || '无'))]);
      $('#rel-table tbody').innerHTML = dayRows(out.rows);
      pageInfo('rel', st.total);
    } catch (e) {
      if (my !== REQ.rel) return;
      status(e.message, true);
    } finally {
      if (my === REQ.rel) $('#rel-go').disabled = false;
    }
  }

  function pageInfo(key, total) {
    const st = S[key];
    const from = total ? st.offset + 1 : 0;
    const to = Math.min(st.offset + PAGE, total);
    $('#' + key + '-page').textContent = `第 ${nf(from)}–${nf(to)} 行 / 共 ${nf(total)} 行`;
    $('#' + key + '-prev').disabled = st.offset <= 0;
    $('#' + key + '-next').disabled = to >= total;
  }

  // ---------- 4 统计看板 ----------
  async function loadStats() {
    const st = S.stats;
    status('');
    try {
      const out = await api('/api/n2/stats', { dim: st.dim, caliber: st.caliber });
      const base = out.baseline || {};
      $('#stats-base').innerHTML = base.mean_pct == null ? ''
        : `全市场基准：均值 <b class="${cls(base.mean_pct)}">${pct(base.mean_pct)}</b> · `
          + `中位 ${pct(base.median_pct)} · 有效样本 ${nf(base.n_return_valid)}`;
      const maxAbs = Math.max(0.001, ...out.rows.map((r) => Math.abs(r.mean_pct || 0)));
      const body = $('#stats-table tbody');
      if (!out.rows.length) {
        body.innerHTML = '<tr><td colspan="11"><div class="n2-empty">该维度在此口径下没有行</div></td></tr>';
      } else {
        body.innerHTML = out.rows.map((r) => {
          const v = r.mean_pct;
          const w = v == null ? 0 : Math.min(50, Math.abs(v) / maxAbs * 50);
          const bar = `<span class="n2-bar"><u></u><i class="${v >= 0 ? 'pos' : 'neg'}" style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%"></i></span>`;
          return `<tr>
            <td><span style="font-family:var(--font-mono)">${esc(r.key || r.relation_type || r.asset_class || '')}</span></td>
            <td class="num">${nf(r.n_identities)}</td>
            <td class="num">${nf(r.n_stock_days)}</td>
            <td class="num">${nf(r.n_up)}</td>
            <td class="num">${nf(r.n_down)}</td>
            <td class="num">${nf(r.n_flat)}</td>
            <td class="num">${nf(r.n_return_valid)}</td>
            <td class="num ${cls(r.mean_pct)}">${pct(r.mean_pct)}</td>
            <td>${bar} <span class="${cls(r.delta_pp)}" style="font-size:11px">${pp(r.delta_pp)}</span></td>
            <td class="num">${pct(r.median_pct)}</td>
            <td class="num">${r.n_dates == null ? '—' : nf(r.n_dates)}</td>
          </tr>`;
        }).join('');
      }
      $('#stats-note').innerHTML = statsNote(st.caliber);
    } catch (e) {
      status(e.message, true);
    }
  }

  function statsNote(caliber) {
    const common = '<b>股票日</b>是独立（股票,交易日）行数；一个事实命中多个关系类型时会在多行重复计入，各类型之间不可相加。'
      + '<b>股票数</b>是该键出现过的不重复身份数。<b>相对全市场</b>=该键均值 − 全市场均值，单位 pp。';
    if (caliber === 'MAIN_EXCL_ANOM') {
      return common + ' 主口径本身已把异常收益排除在分子分母之外，因此「主口径(排异常)」与「主口径」数值完全相同，列出只为对照。';
    }
    if (caliber === 'INCL_SUSPECT') {
      return common + ' 敏感性C把 return_valid=0 的可疑收益计入分子与分母，只作敏感性参考，不是推荐口径。';
    }
    if (caliber === 'EXCL_DISPUTED') {
      return common + ' 敏感性A剔除 73 只边界争议身份（保留原始档案，不替换三柱）。';
    }
    return common + ' 主口径：return_valid=1 的 5,131 只 A 股全部。指数对照行不计入任何 A 股分母。';
  }

  // ---------- 4b 流月聚合 ----------
  async function ensureMonths() {
    if (S.month.all) return;
    const out = await api('/api/n2/months', { limit: 0 });
    S.month.all = out.rows || [];
    S.month.baseline = out.baseline || {};
    const years = Array.from(new Set(S.month.all.map((r) => r.month_id.slice(0, 4)))).sort();
    fillYearSelect($('#month-year-from'), years, '最早');
    fillYearSelect($('#month-year-to'), years, '最新');
    fillSelect($('#month-pillar'),
      PILLARS.filter((p) => S.month.all.some((r) => r.month_pillar === p)));
  }

  function fillYearSelect(el, years, allLabel) {
    if (!el) return;
    el.innerHTML = `<option value="">${allLabel}</option>`
      + years.map((y) => `<option value="${esc(y)}">${esc(y)}</option>`).join('');
  }

  function monthFilters() {
    return {
      pillar: $('#month-pillar').value,
      year_from: $('#month-year-from').value,
      year_to: $('#month-year-to').value,
      result: $('#month-result').value,
    };
  }

  function filteredMonths() {
    const f = monthFilters();
    return (S.month.all || []).filter((r) => {
      if (f.pillar && r.month_pillar !== f.pillar) return false;
      const y = r.month_id.slice(0, 4);
      if (f.year_from && y < f.year_from) return false;
      if (f.year_to && y > f.year_to) return false;
      if (f.result === 'up' && !(r.median_month_pct > 0)) return false;
      if (f.result === 'down' && !(r.median_month_pct < 0)) return false;
      return true;
    });
  }

  async function loadMonths() {
    status('');
    try {
      await ensureMonths();
      const b = S.month.baseline || {};
      const all = S.month.all || [];
      const rows = filteredMonths();
      const base = b.median_month_pct;
      const meds = rows.map((r) => r.median_month_pct).filter((v) => v != null);
      const avgMed = meds.length ? meds.reduce((a, c) => a + c, 0) / meds.length : null;
      const posMonths = rows.filter((r) => r.median_month_pct > 0).length;
      const filtered = rows.length !== all.length;
      const showPos = !monthFilters().result;
      $('#month-hit').innerHTML = filtered
        ? `<b>命中 ${nf(rows.length)} / ${nf(all.length)} 个流月</b> · `
          + `中位（按月等权）${pct(avgMed)}`
          + (showPos ? ` · 中位为正 ${nf(posMonths)} 个（${fmtShare(rows.length ? posMonths / rows.length * 100 : null)}）` : '')
        : `共 ${nf(all.length)} 个流月`;
      $('#month-base').innerHTML = b.n_months
        ? `全历史基准（不随筛选变化）：${nf(b.n_months)} 个流月 · ${nf(b.n_month_valid)} 个股月 · `
          + `均值 <b class="${cls(b.mean_month_pct)}">${pct(b.mean_month_pct)}</b> · `
          + `中位 ${pct(b.median_month_pct)} · 上涨占比 ${fmtShare(b.up_share_month)}`
        : '';
      const body = $('#month-table tbody');
      if (!rows.length) {
        body.innerHTML = '<tr><td colspan="10"><div class="n2-empty">该筛选下没有流月</div></td></tr>';
        $('#month-note').innerHTML = monthNote();
        return;
      }
      // 用 90 分位做刻度，避免 1990 年那种 4 只股票的极端月把小月压成一条线
      const sortedAbs = rows.map((r) => Math.abs(r.median_month_pct || 0)).sort((x, y) => x - y);
      const scale = Math.max(0.001, sortedAbs[Math.floor(0.9 * (sortedAbs.length - 1))] || 0.001);
      body.innerHTML = rows.map((r) => {
        const v = r.median_month_pct;
        const w = v == null ? 0 : Math.min(50, Math.abs(v) / scale * 50);
        const bar = `<span class="n2-bar"><u></u><i class="${v >= 0 ? 'pos' : 'neg'}"`
          + ` style="left:${v >= 0 ? 50 : 50 - w}%;width:${w}%"></i></span>`;
        const delta = (v != null && base != null) ? v - base : null;
        return `<tr class="n2-clickable" data-date="${esc(r.last_date)}" title="点击查看 ${esc(r.last_date)} 的全市场">
          <td><span style="font-family:var(--font-mono)">${esc(r.month_id)}</span>
              <span class="n2-name">${esc(r.month_pillar)}</span></td>
          <td class="num">${nf(r.n_dates)}</td>
          <td class="num">${nf(r.n_identities)}</td>
          <td class="num">${nf(r.n_stock_days)}</td>
          <td class="num">${nf(r.n_up)} / ${nf(r.n_down)} / ${nf(r.n_flat)}</td>
          <td class="num ${cls(r.mean_day_pct)}">${pct(r.mean_day_pct)}</td>
          <td class="num ${cls(r.mean_month_pct)}">${pct(r.mean_month_pct)}</td>
          <td class="num ${cls(v)}">${pct(v)}</td>
          <td>${bar} <span class="${cls(delta)}" style="font-size:11px">${pp(delta)}</span></td>
          <td class="num">${fmtShare(r.up_share_month)}</td>
        </tr>`;
      }).join('');
      $('#month-note').innerHTML = monthNote();
    } catch (e) {
      status(e.message, true);
    }
  }

  function monthNote() {
    return '<b>这是按流月聚合的日级数据，不是流月关系。</b>'
      + '日级涨跌 = 开→收；月收益（个股）= 该股当月首个交易日开盘 → 最后一个交易日收盘（前复权），'
      + '再对当月所有股票取均值/中位；含异常收益日的个股月份不计入月收益（仍计入股票日）。'
      + '流月柱取自主库月级表，属日历量。「相对全历史」始终对比全部 430 个流月的中位，不随筛选变化。'
      + '与「流月机会表」（月级库）不是同一份复权基准，个股级可能差几个 bp，市场汇总不受影响。'
      + '全历史均值 +1.51% 高于中位 −0.12%：少数大涨月把均值拉高，典型月份其实是微跌。'
      + '1990-M11 是数据起点所在月，不完整。'
      + '当月还没走完的月份不纳入本表（避免用半个月对整月），它的日级数据在上面的流日视图里可以查到。'
      + '点击任意一行可跳到该月最后一个交易日的全市场。';
  }

  // ---------- 5 沪深同日对照 ----------
  async function loadBench() {
    status('');
    try {
      const out = await api('/api/n2/benchmark');
      const idx = (out.index || [])[0];
      const rows = out.rows || [];
      const ex = { SH: '上证（对照 000001.SH）', SZ: '深证（对照 399106.SZ）' };
      const body = $('#bench-table tbody');
      body.innerHTML = rows.length ? rows.map((r) => `<tr>
        <td>${esc(ex[r.exchange] || r.exchange)}</td>
        <td>${r.relation_type === '__ALL__' ? '<b>全部（不分关系）</b>' : esc(r.relation_type)}</td>
        <td class="num">${nf(r.n_stock_days)}</td>
        <td class="num">${nf(r.n_benchmark_matched)}</td>
        <td class="num">${nf(r.n_benchmark_missing)}</td>
        <td class="num ${cls(r.mean_pct)}">${pct(r.mean_pct)}</td>
        <td class="num ${cls(r.mean_benchmark_pct)}">${pct(r.mean_benchmark_pct)}</td>
        <td class="num ${cls(r.mean_excess_pct)}">${pp(r.mean_excess_pct)}</td>
        <td class="num ${cls(r.median_excess_pct)}">${pp(r.median_excess_pct)}</td>
      </tr>`).join('') : '<tr><td colspan="9"><div class="n2-empty">没有对照行</div></td></tr>';
      const idxLine = idx
        ? `指数关系实验（单列，不并入 A 股分母）：000001.SH ${nf(idx.n_stock_days)} 行，`
          + `均值 <b class="${cls(idx.mean_pct)}">${pct(idx.mean_pct)}</b>，中位 ${pct(idx.median_pct)}。`
        : '';
      const cutoff = (S.meta && S.meta.index_benchmark_cutoff) || '2026-10-08';
      $('#bench-note').innerHTML = `<b>本表只覆盖到 ${esc(cutoff)}</b>：本轮只刷新了 A 股日线，`
        + `指数日线在本机还没有 ${esc(cutoff)} 之后的数据，因此不参与这之后的对照。<br>${idxLine}<br>`
        + '<b>只配同日价格</b>：不做前向填充、不用其它指数替代；指数缺失只计「缺失」列，不补数。'
        + '「均值超额为正、中位超额为负」= 少数大涨日把均值拉高，典型交易日的个股反而略低于同日指数（右偏），两者要一起看。';
    } catch (e) {
      status(e.message, true);
    }
  }

  // ---------- 交互 ----------
  function switchPanel(name) {
    S.panel = name;
    $$('#n2-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.panel === name));
    $$('.n2-panel').forEach((p) => p.classList.toggle('on', p.id === 'panel-' + name));
    history.replaceState(null, '', '#' + name);
    if (name === 'stock' && !$('#stock-table tbody').children.length) loadStock();
    if (name === 'day' && !$('#day-table tbody').children.length) loadDay();
    if (name === 'rel' && !$('#rel-table tbody').children.length) loadRel();
    if (name === 'stats') loadStats();
    if (name === 'month') loadMonths();
    if (name === 'bench') loadBench();
  }

  function bind() {
    $$('#n2-tabs button').forEach((b) => { b.onclick = () => switchPanel(b.dataset.panel); });

    $('#stock-go').onclick = () => { S.stock.id = ($('#stock-id').value || '').trim().toUpperCase(); S.stock.offset = 0; loadStock(); };
    $('#stock-id').onkeydown = (e) => { if (e.key === 'Enter') $('#stock-go').click(); };
    $$('.n2-quick').forEach((el) => {
      el.onclick = () => { $('#stock-id').value = el.dataset.code; S.stock.id = el.dataset.code; S.stock.offset = 0; switchPanel('stock'); loadStock(); };
    });
    ['#stock-from', '#stock-to', '#stock-pillar', '#stock-rel', '#stock-dir', '#stock-tg'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('change', () => { S.stock.offset = 0; loadStock(); });
    });
    $('#stock-reset').onclick = () => {
      ['#stock-from', '#stock-to', '#stock-pillar', '#stock-rel', '#stock-dir', '#stock-tg']
        .forEach((id) => { const el = $(id); if (el) el.value = ''; });
      S.stock.offset = 0; loadStock();
    };
    $('#stock-prev').onclick = () => { S.stock.offset = Math.max(0, S.stock.offset - PAGE); loadStock(); };
    $('#stock-next').onclick = () => { S.stock.offset += PAGE; loadStock(); };

    $('#day-go').onclick = () => { S.day.date = $('#day-date').value; S.day.offset = 0; loadDay(); };
    ['#day-dir', '#day-pillar', '#day-rel', '#day-tg'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('change', () => { S.day.offset = 0; loadDay(); });
    });
    $('#day-reset').onclick = () => {
      ['#day-dir', '#day-pillar', '#day-rel', '#day-tg'].forEach((id) => { const el = $(id); if (el) el.value = ''; });
      S.day.offset = 0; loadDay();
    };
    $('#day-date').onchange = () => $('#day-go').click();
    $('#day-prev').onclick = () => { S.day.offset = Math.max(0, S.day.offset - PAGE); loadDay(); };
    $('#day-next').onclick = () => { S.day.offset += PAGE; loadDay(); };

    $('#rel-go').onclick = () => { S.rel.date = $('#rel-date').value; S.rel.rel = $('#rel-type').value; S.rel.offset = 0; loadRel(); };
    $('#rel-type').onchange = () => $('#rel-go').click();
    ['#rel-dir', '#rel-tg'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('change', () => { S.rel.offset = 0; loadRel(); });
    });
    $('#rel-reset').onclick = () => {
      ['#rel-dir', '#rel-tg'].forEach((id) => { const el = $(id); if (el) el.value = ''; });
      S.rel.offset = 0; loadRel();
    };
    $('#rel-date').onchange = () => $('#rel-go').click();
    $('#rel-prev').onclick = () => { S.rel.offset = Math.max(0, S.rel.offset - PAGE); loadRel(); };
    $('#rel-next').onclick = () => { S.rel.offset += PAGE; loadRel(); };

    $('#stats-dim').onchange = () => { S.stats.dim = $('#stats-dim').value; loadStats(); };
    $('#stats-caliber').onchange = () => { S.stats.caliber = $('#stats-caliber').value; loadStats(); };
    $('#stats-go').onclick = () => loadStats();

    ['#month-pillar', '#month-year-from', '#month-year-to', '#month-result'].forEach((id) => {
      const el = $(id);
      if (el) el.addEventListener('change', () => loadMonths());
    });
    $('#month-reset').onclick = () => {
      ['#month-pillar', '#month-year-from', '#month-year-to', '#month-result']
        .forEach((id) => { const el = $(id); if (el) el.value = ''; });
      loadMonths();
    };
    $('#month-table tbody').addEventListener('click', (e) => {
      const tr = e.target.closest('tr[data-date]');
      if (!tr || !tr.dataset.date) return;
      S.day.date = tr.dataset.date;
      S.day.dir = '';
      S.day.offset = 0;
      $('#day-date').value = S.day.date;
      $('#day-dir').value = '';
      switchPanel('day');
      loadDay();
    });
  }

  (async function start() {
    bind();
    try {
      await boot();
    } catch (e) {
      status(e.message, true);
      return;
    }
    const hash = (location.hash || '').replace('#', '');
    const known = ['stock', 'day', 'rel', 'stats', 'month', 'bench'];
    switchPanel(known.includes(hash) ? hash : 'stock');
  })();
})();
