(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const params = new URLSearchParams(location.search);
  const S = {
    code: params.get('code') || '600519.SH',
    offset: Number(params.get('offset') || 0),
    layer: ['month', 'xun', 'week'].includes(params.get('layer')) ? params.get('layer') : 'month',
    meta: null,
  };
  let seq = 0;  // 请求序号：丢弃过期响应，防止连改筛选时旧结果盖回新结果

  const LAYER_NOTE = {
    month: '月卦 = 所选流月的<b>上一流月</b>整月K线定卦；价格 raw 不复权。',
    xun: '旬卦 = 所选流月内 上旬(第1–10自然日) / 中旬(11–20) / 下旬(21–月末) 各铸一卦；进行中为动态卦（未定）。',
    week: '段卦 = 所选流月内 6 段（第1–5 / 6–10 / 11–15 / 16–20 / 21–25 / 26–月末自然日）各铸一卦；进行中为动态卦（未定）。',
  };

  function applyTheme(light) {
    document.documentElement.classList.toggle('light', light);
    document.documentElement.classList.toggle('dark', !light);
    document.body.classList.toggle('theme-light', light);
    const btn = $('#theme-toggle');
    if (btn) btn.textContent = light ? '🌙' : '☀️';
  }

  async function api(path, query) {
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

  function buildMonthSelect() {
    const sel = $('#month');
    sel.innerHTML = '';
    (S.meta.months || []).forEach((m) => {
      const o = document.createElement('option');
      o.value = String(m.offset);
      o.textContent = `${m.month_id} ${m.month_pillar}（${m.label}）`;
      o.selected = m.offset === S.offset;
      sel.appendChild(o);
    });
    if (!sel.value) { S.offset = 0; sel.value = '0'; }
  }

  function renderSources() {
    const src = (S.meta && S.meta.sources) || {};
    const parts = [];
    if (src.stock_daily) parts.push(`个股历史日线止于 ${src.stock_daily}`);
    if (src.live) parts.push(`当月实时日线止于 ${src.live}`);
    if (src.index_mirror) parts.push(`指数日线镜像止于 ${src.index_mirror}`);
    parts.push(src.index_ext ? `指数补拉止于 ${src.index_ext}` : '指数补拉未完成（补拉前指数窗口按数据不足处理）');
    $('#foot-src').innerHTML = `<b>数据源</b>：${esc(parts.join('；'))}。本页全部只读，不写任何数据库。`;
  }

  // 卡片渲染器 guaCardHTML / guaSideBadge 由 /static/gua_card.js 提供（与 100%段抽屉共用）。

  function renderCast(d) {
    $('#head-meta').textContent = `${d.name} ${d.code} · 日线止于 ${d.data_asof || '—'} · raw 不复权`;
    const cards = $('#cards');
    cards.classList.toggle('single', d.layer === 'month');
    cards.innerHTML = (d.windows || []).map(guaCardHTML).join('');
    const anchor = d.anchor_month;
    if (d.layer === 'month' && anchor) {
      $('#layer-note').innerHTML = `所选 ${esc(d.month.month_id)}（${esc(d.month.month_pillar)}）的月卦，锚定上一流月 <b>${esc(anchor.month_id)} ${esc(anchor.month_pillar)}</b> 整月K线。`;
    } else {
      $('#layer-note').innerHTML = LAYER_NOTE[d.layer] || LAYER_NOTE.month;
    }
  }

  async function loadCast() {
    const my = ++seq;
    showStatus('起卦中…', false);
    try {
      const d = await api('/api/gua/cast', { code: S.code, layer: S.layer, offset: String(S.offset) });
      if (my !== seq) return;
      showStatus('', false);
      renderCast(d);
    } catch (e) {
      if (my !== seq) return;
      if (e.status === 401) {
        showStatus('登录已失效，请回首页重新登录后再打开本页。', true);
      } else {
        showStatus(`起卦失败：${e.message}`, true);
      }
      $('#cards').innerHTML = '<div class="gua-empty">无结果</div>';
      $('#head-meta').textContent = '—';
    }
  }

  function bind() {
    $('#go').onclick = () => { S.code = $('#code').value.trim() || S.code; loadCast(); };
    $('#code').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') $('#go').click(); });
    document.querySelectorAll('.gua-chip').forEach((b) => {
      b.onclick = () => { S.code = b.dataset.code; $('#code').value = S.code; loadCast(); };
    });
    $('#month').addEventListener('change', (ev) => { S.offset = Number(ev.target.value); loadCast(); });
    $('#layer-tabs').addEventListener('click', (ev) => {
      const btn = ev.target.closest('button[data-layer]');
      if (!btn) return;
      S.layer = btn.dataset.layer;
      document.querySelectorAll('#layer-tabs button').forEach((b) => b.classList.toggle('on', b === btn));
      loadCast();
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
    $('#code').value = S.code;
    document.querySelectorAll('#layer-tabs button').forEach((b) => b.classList.toggle('on', b.dataset.layer === S.layer));
    bind();
    try {
      S.meta = await api('/api/gua/meta');
      $('#disclaimer').innerHTML = `<b>文化展示 · 非选股依据</b>：${esc(S.meta.disclaimer)}`;
      buildMonthSelect();
      renderSources();
    } catch (e) {
      showStatus(`页面初始化失败：${e.message}`, true);
      return;
    }
    await loadCast();
  }

  init().catch((e) => showStatus(`流月卦加载失败：${e.message}`, true));
})();