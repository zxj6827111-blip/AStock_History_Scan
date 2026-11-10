/* V1.2 男女命 · 大运页面（用户裁定：阳=男/阴=女/平=中性；A股 + ETF；只读本地 API） */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const S = {
  id: null, dataset: 'ashare', profileData: null, metaData: null,
  step: 'ALL', policy: 'ALL', status: 'ALL',
  returnTab: 'stock', fromBoard: false, restoring: false,
  monthsData: null, statsData: null,
};

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
const fmtPct = (v) => (v === null || v === undefined || Number.isNaN(Number(v)))
  ? '—' : `${(Number(v) * 100).toFixed(2)}%`;
const fmtNum = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v)))
  ? '—' : Number(v).toFixed(d);
const dateOnly = (s) => (s ? String(s).slice(0, 10) : '—');

const dirText = (d) => (d === 'UP' ? '涨' : d === 'DOWN' ? '跌' : d === 'FLAT' ? '平' : '—');
const dirHtml = (d) => {
  const cls = d === 'UP' ? 'c-up' : d === 'DOWN' ? 'c-down' : d === 'FLAT' ? 'c-flat' : 'na';
  return `<span class="${cls}">${dirText(d)}</span>`;
};

const GENDER_TAG = { MALE: 'ok', FEMALE: 'warn', NEUTRAL: 'warn', UNKNOWN: 'blocked', INVALID: 'blocked' };
const TIMELINE_TAG = {
  AVAILABLE: 'ok', ANCHOR_PILLAR_CONFLICT: 'warn', BLOCKED_NEUTRAL: 'warn',
  BLOCKED_GENDER: 'blocked', NO_PROFILE: 'blocked', UNAVAILABLE: 'blocked',
  BIRTH_DATE_MISSING: 'blocked', ALGORITHM_UNAVAILABLE: 'blocked',
};
const BINDING_TAG = {
  SINGLE_DAYUN: 'ok', CROSSES_DAYUN_BOUNDARY: 'warn', BEFORE_FIRST_DAYUN: 'indigo',
  BLOCKED_NEUTRAL: 'warn', BLOCKED_GENDER: 'blocked', NO_PROFILE: 'blocked',
  TIMELINE_UNAVAILABLE: 'warn', UNAVAILABLE_DIRECTION: 'blocked',
  MISSING_PRICE_DATES: 'warn', AFTER_LAST_DAYUN: 'warn', OUTSIDE_TIMELINE: 'warn',
};
const THRESHOLD_TAG = {
  MEETS_80_PERCENT: ['ok', '达标 ≥80%'],
  BELOW_80_PERCENT: ['warn', '未达 80%'],
  INSUFFICIENT_SAMPLE: ['', '样本不足'],
  NO_SAMPLE: ['', '无样本'],
};
const GRADE_TEXT = {
  NO_HISTORY: '无历史', SINGLE_OBSERVATION: '单次', VERY_FEW: '2–3 次',
  FEW: '4–5 次', RELATIVELY_MORE: '≥6 次',
};

const tag = (text, cls = '') => `<span class="tag ${cls}">${esc(text)}</span>`;
const kv = (label, value, extra = '') => `
  <div class="dy-kv"><label>${esc(label)}</label><div ${extra}>${value}</div></div>`;
const apiUrl = (path, params = {}) => {
  const q = new URLSearchParams({ dataset: S.dataset, ...params });
  return `${path}?${q.toString()}`;
};

const DAYUN_STATUSES = new Set([
  'ALL', 'SINGLE_DAYUN', 'CROSSES_DAYUN_BOUNDARY', 'BEFORE_FIRST_DAYUN',
  'BLOCKED_NEUTRAL', 'BLOCKED_GENDER', 'TIMELINE_UNAVAILABLE', 'NO_PROFILE',
]);
const MAIN_TABS = new Set(['stock', 'branch', 'pillar', 'relation', 'tengod', 'meta', 'lowpoint']);

function readDayunUrl() {
  const p = new URLSearchParams(location.search);
  return {
    dataset: p.get('dataset') === 'etf' ? 'etf' : 'ashare',
    id: p.get('id') || '',
    step: p.get('step') || 'ALL',
    policy: p.get('policy') === 'EXCLUDE_SINGLE' ? 'EXCLUDE_SINGLE' : 'ALL',
    status: DAYUN_STATUSES.has(p.get('status')) ? p.get('status') : 'ALL',
    tab: MAIN_TABS.has(p.get('tab')) ? p.get('tab') : 'stock',
    fromBoard: p.get('from') === 'board',
    // embed=1：嵌在主站「个股档案 › 大运」子页里，自己的顶栏和搜索框不显示，主题跟主站走
    embed: p.get('embed') === '1',
  };
}

function dayunUrl() {
  const p = new URLSearchParams();
  p.set('dataset', S.dataset);
  if (S.id) p.set('id', S.id);
  if (S.step !== 'ALL') p.set('step', String(S.step));
  if (S.policy !== 'ALL') p.set('policy', S.policy);
  if (S.status !== 'ALL') p.set('status', S.status);
  p.set('tab', S.returnTab || 'stock');
  if (S.fromBoard) p.set('from', 'board');
  if (S.embed) p.set('embed', '1');
  return location.pathname + '?' + p.toString();
}

function syncDayunUrl(mode) {
  if (!S.restoring) {
    const url = dayunUrl();
    const cur = location.pathname + location.search;
    if (mode === 'push' && url !== cur) history.pushState({ dayun: true }, '', url);
    else history.replaceState({ dayun: true }, '', url);
  }
  syncBackLink();
}

function syncBackLink() {
  const back = $('#back-main');
  if (!back) return;
  const p = new URLSearchParams();
  p.set('dataset', S.dataset);
  if (S.id) p.set('id', S.id);
  p.set('tab', S.returnTab || 'stock');
  if (S.fromBoard) p.set('from', 'board');
  back.href = '/?' + p.toString();
}

function toast(msg, isError = true) {
  const el = document.createElement('div');
  el.className = 'toast';
  if (isError) el.style.borderColor = 'var(--stock-up-border)';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 6500);
}

async function fetchJSON(url) {
  const resp = await fetch(url, { cache: 'no-store' });
  if (resp.status === 401) {
    window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname + window.location.search);
    return new Promise(() => {});
  }
  let body;
  try { body = await resp.json(); } catch (e) { body = { message: String(e) }; }
  if (!resp.ok) throw new Error(body.error || body.message || `HTTP ${resp.status}`);
  return body;
}

function setStep(step) {
  S.step = String(step);
  syncDayunUrl('replace');
  renderFilters(S.profileData);
  loadMonths();
  loadStats();
}
function setPolicy(policy) {
  S.policy = policy;
  syncDayunUrl('replace');
  renderFilters(S.profileData);
  loadMonths();
  loadStats();
}
function setStatus(status) {
  S.status = status;
  syncDayunUrl('replace');
  renderFilters(S.profileData);
  loadMonths();
}

function applyTheme() {
  const embed = new URLSearchParams(location.search).get('embed') === '1';
  if (embed) document.documentElement.classList.add('dy-embed');
  const saved = localStorage.getItem(embed ? 'bazi_theme' : 'dayun-theme');
  if (saved === 'light') {
    document.documentElement.classList.remove('dark');
    document.documentElement.classList.add('light');
    $('#theme-toggle').textContent = '🌙';
  }
}

function syncDatasetButtons() {
  $$('#dy-ds [data-ds]').forEach((b) => {
    b.classList.toggle('on', b.dataset.ds === S.dataset);
  });
  const input = $('#q');
  if (S.dataset === 'etf') {
    input.placeholder = '输入 ETF 代码/名称，如 510050.SH / 上证50ETF';
  } else {
    input.placeholder = '输入 A 股代码/名称，如 000690.SZ / 宝新能源';
  }
}

async function setDataset(ds, opts = {}) {
  if (ds === S.dataset) return;
  S.dataset = ds;
  S.step = 'ALL'; S.policy = 'ALL'; S.status = 'ALL';
  syncDatasetButtons();
  await loadMeta();
  const defaultId = ds === 'etf' ? '510050.SH' : '000690.SZ';
  $('#q').value = defaultId;
  await loadStock(defaultId, { push: opts.push !== false });
  syncBackLink();
}

async function loadMeta() {
  try {
    S.metaData = await fetchJSON(apiUrl('/api/dayun/meta'));
    renderInfo(S.metaData);
    renderSyncBanner(S.metaData);
  } catch (e) {
    $('#info-body').innerHTML = `<div class="banner blocked">扩展元信息读取失败：${esc(e.message)}</div>`;
  }
}

function renderSyncBanner(m) {
  const box = $('#sync-banner');
  if (!box) return;
  const f = (m && m.main_db_freshness) || null;
  if (!f || f.status === 'FRESH') { box.innerHTML = ''; return; }
  const reg = f.registered || {};
  box.innerHTML = `<div class="banner ${f.status === 'STALE' ? 'blocked' : 'warn'}">
    <b>${f.status === 'STALE' ? '大运扩展库落后于主库。' : '无法判定扩展库是否与主库同版。'}</b>
    ${esc(f.message || '')}<br>
    <span class="small muted">登记主库哈希 ${esc((reg.sha256 || '—').slice(0, 16))}…
      · 登记于 ${esc(reg.path || '—')}</span></div>`;
}

function renderUnavailable(data) {
  S.profileData = null;
  const msg = data.message || '扩展暂不可用';
  const notFound = data.extension_status === 'UNKNOWN_IDENTITY';
  $('#ext-banner').innerHTML = `<div class="banner blocked"><b>${notFound ? '未找到该证券身份。' : '扩展暂不可用。'}</b> ${esc(msg)}<br>
    <span class="muted">${notFound ? '请检查代码/名称后重新搜索。' : '主 V1 查询不受影响；请按 RUNBOOK 构建或恢复扩展库。'}</span></div>`;
  $('#profile-card').innerHTML = '';
  $('#steps-card').innerHTML = '';
  $('#filters').innerHTML = '';
  $('#month-summary').innerHTML = '';
  $('#months-table thead').innerHTML = '';
  $('#months-table tbody').innerHTML = '';
  $('#stats-body').innerHTML = '<div class="muted">扩展不可用，无法显示大运统计。</div>';
}

function renderProfile(data) {
  const p = data.profile;
  const sec = data.identity;
  const isAvail = p.timeline_status === 'AVAILABLE';
  const genderCls = GENDER_TAG[p.gender] || '';
  const directionCls = p.direction === 'FORWARD' ? 'ok' : p.direction === 'REVERSE' ? 'warn' : 'blocked';
  const timelineCls = TIMELINE_TAG[p.timeline_status] || '';

  let banner = '';
  if (!p.profile_present) {
    banner = `<div class="banner blocked"><b>无命盘。</b> 该身份没有可核验的本命三柱，无法排大运；原行情与 V1 关系统计仍可在主页面查看。</div>`;
  } else if (p.gender === 'NEUTRAL') {
    banner = `<div class="banner warn"><b>用户裁定：平盘→中性。</b> 中性没有男/女顺逆，四象限无法裁定，因而不能给出确定大运或历史月归属。
      下方只列出顺/逆两套候选柱序（不含起运时间），它们都是研究候选，不是“两套都已成立”。</div>`;
  } else if (p.timeline_status === 'ANCHOR_PILLAR_CONFLICT') {
    banner = `<div class="banner warn"><b>方向已裁定，但时间轴待核。</b> 代理命别 ${esc(p.proxy_polarity || '—')} → ${esc(p.gender_label)}，方向 ${esc(p.direction_label)}；
      09:30 复算三柱与档案三柱冲突，按锚点策略不使用 23:59:59 回退，因此不生成起运日期。
      下方候选柱序中，有效方向已标记。</div>`;
  } else if (isAvail) {
    banner = `<div class="banner ok"><b>用户裁定研究口径：阳=男、阴=女、平=中性。</b>
      当前代理命别 ${esc(p.proxy_polarity || '—')} → ${esc(p.gender_label)}；方向 ${esc(p.direction_label)}。
      ${esc(p.proxy_warning || '这是研究口径，不是证券的真实性别。')}</div>`;
  } else {
    banner = `<div class="banner warn"><b>代理阴阳缺失，方向待核。</b> 不能默认男命；本页只展示历史流月与原有 V1 关系，不提供确定大运或历史月归属。</div>`;
  }
  $('#ext-banner').innerHTML = banner;

  const datasetLabel = data.dataset === 'etf' ? 'ETF' : 'A股';
  const anchorLabel = data.dataset === 'etf' ? '出生锚点（库内首根K线）' : '出生锚点（上市日）';
  const birth = p.birth_date || sec.listed_date;
  const archiveOriginal = [p.archive_gender_raw, p.archive_polarity_raw]
    .filter((x) => x !== null && x !== undefined && x !== '').join(' / ') || '—';

  $('#profile-card').innerHTML = `
    <section class="card">
      <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-start">
        <div>
          <h2 style="margin-bottom:2px">${esc(sec.name || sec.identity_code)}
            <span class="muted small" style="font-weight:400">${esc(sec.identity_code)} · ${esc(datasetLabel)} · ${esc(sec.security_type || '')}</span></h2>
          <div class="muted small">上市：${esc(sec.listed_date || '—')} · 正式流月：${esc(sec.formal_month_count ?? '—')}
            · 档案版本：${esc(p.profile_version || '—')}</div>
        </div>
        <div class="badges" style="margin:0">
          ${tag('性别（用户裁定）：' + (p.gender_label || '未知'), genderCls)}
          ${tag('代理命别：' + (p.proxy_polarity || '—'), genderCls)}
          ${tag('方向：' + (p.direction_label || '不可计算'), directionCls)}
          ${tag('时间轴：' + (p.timeline_status_label || p.timeline_status), timelineCls)}
        </div>
      </div>
      <div class="dy-kv-grid" style="margin-top:12px">
        ${kv('本命年柱', esc(p.natal_year_pillar || '—'))}
        ${kv('本命月柱（排运基准）', esc(p.natal_month_pillar || '—'))}
        ${kv('本命日柱', esc(p.natal_day_pillar || '—'))}
        ${kv('日主', esc((p.day_master_stem || '—') + (p.day_master_element ? '（' + p.day_master_element + '）' : '')))}
        ${kv('本命年干阴阳', esc(p.year_yinyang || '—'))}
        ${kv('性别（用户裁定）', esc(p.gender_label || '—'))}
        ${kv('代理命别', esc(p.proxy_polarity || '—'), 'title="阳=男、阴=女、平=中性；研究口径，不是证券真实性别"')}
        ${kv('旧档案原字段', esc(archiveOriginal), 'title="A股旧表男女列实为阳/阴；ETF 男/女来自首根K线方向。不是本轮代理命别的替代"')}
        ${kv('代理来源', esc(p.proxy_source || '—'))}
        ${kv('性别口径', esc(p.gender_source_label || p.gender_source || '—'))}
        ${kv(anchorLabel, esc(birth || '—'))}
        ${kv('起运时间', isAvail ? esc(dateOnly(p.start_at) + '（09:30 锚点）') : '待核')}
        ${kv('起运精度 / 时区', isAvail ? esc(p.start_precision + ' / ' + p.timezone) : '—')}
        ${kv('出生后年数 / 虚岁', isAvail ? esc(fmtNum(p.start_age_years) + ' 年 / ' + (p.start_age_lunar ?? '—')) : '—')}
        ${kv('起运方法', esc(p.method_id || '—'))}
        ${kv('锚点对齐', esc(p.anchor_alignment || '—'))}
      </div>
      <div class="footnote" style="margin-top:10px">
        ${esc(p.proxy_warning || '')}
        ${p.boundary_conflict && p.boundary_conflict !== 'NONE' ? ' 交节争议：' + esc(p.boundary_conflict) + '（与“跨大运”是独立质量维度）。' : ''}
        ${p.error_code ? ' 原因码：' + esc(p.error_code) + (p.error_label ? '（' + esc(p.error_label) + '）' : '') + '。' : ''}
      </div>
    </section>`;
}

function renderSteps(data) {
  const p = data.profile;
  const steps = data.steps || [];
  if (p.timeline_status === 'AVAILABLE' && steps.length) {
    $('#steps-card').innerHTML = `
      <section class="card">
        <h2>十步大运</h2>
        <div class="table-wrap">
          <table class="dy-table">
            <thead><tr>
              <th class="left">步</th><th class="left">大运</th><th class="left">大运干十神</th>
              <th>起</th><th>止</th><th>出生后年数</th><th>虚岁</th><th>单运月</th>
            </tr></thead>
            <tbody>
              ${steps.map((s) => `<tr class="no-hover">
                <td class="left">${s.step_no}</td>
                <td class="left"><b>${esc(s.ganzhi)}</b></td>
                <td class="left">${esc(s.dayun_stem_tengod || '—')}</td>
                <td>${esc(dateOnly(s.start_at))}</td>
                <td>${esc(dateOnly(s.end_at))}</td>
                <td>${esc(fmtNum(s.age_years_at_start))}</td>
                <td>${esc(s.start_age_lunar ?? '—')}</td>
                <td>${esc(s.single_month_count ?? 0)}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
        <div class="footnote" style="margin-top:8px">
          第一步不是本命月柱；每一步按六十甲子整体进退。起运时间是 DAY 精度，边界按 09:30 处理；
          虚岁来自 lunar-python 的运段字段，与“出生后年数”并列保留，避免混用。
        </div>
      </section>`;
    return;
  }

  if (!p.profile_present) {
    $('#steps-card').innerHTML = `<section class="card"><h2>十步大运</h2>
      <div class="banner blocked">无命盘，无法排大运。</div></section>`;
    return;
  }

  const cand = data.candidates || { FORWARD: [], REVERSE: [] };
  const effectiveDir = p.direction_status === 'AVAILABLE' ? p.direction : null;
  const banner = p.gender === 'NEUTRAL'
    ? '用户裁定：平盘→中性。中性没有男/女顺逆；下面两套柱序都是研究候选，不含起运时间。'
    : effectiveDir
      ? `方向已裁定（${esc(p.direction_label)}），但 09:30 复算三柱与档案冲突；有效方向的柱序已标记，时间轴待核。`
      : '代理阴阳缺失，不能确定顺逆；下面两套柱序都是研究候选，不含起运时间。';

  const table = (rows, label, isEffective) => `
    <div>
      <div class="candidate-head">${esc(label)}
        ${isEffective ? tag('已裁定方向', 'ok') : (effectiveDir ? tag('反向对照', '') : tag('未裁定', 'warn'))}
      </div>
      <div class="table-wrap">
        <table class="dy-table">
          <thead><tr><th class="left">步</th><th class="left">大运</th><th class="left">大运干十神</th></tr></thead>
          <tbody>${rows.map((r) => `<tr class="no-hover">
            <td class="left">${r.step_no}</td><td class="left"><b>${esc(r.ganzhi)}</b></td>
            <td class="left">${esc(r.dayun_stem_tengod || '—')}</td></tr>`).join('')}</tbody>
        </table>
      </div>
    </div>`;
  const fwdLabel = '顺行' + (effectiveDir === 'FORWARD' ? '（已裁定方向）' : effectiveDir ? '（对照）' : '候选');
  const revLabel = '逆行' + (effectiveDir === 'REVERSE' ? '（已裁定方向）' : effectiveDir ? '（对照）' : '候选');
  $('#steps-card').innerHTML = `
    <section class="card">
      <h2>十步柱序候选</h2>
      <div class="banner ${p.gender === 'NEUTRAL' ? 'warn' : effectiveDir ? 'warn' : 'blocked'}" style="margin-bottom:10px">${banner}</div>
      <div class="candidate-grid">
        ${table(cand.FORWARD || [], fwdLabel, effectiveDir === 'FORWARD')}
        ${table(cand.REVERSE || [], revLabel, effectiveDir === 'REVERSE')}
      </div>
    </section>`;
}

function renderFilters(data) {
  const p = data.profile;
  const available = p.timeline_status === 'AVAILABLE';
  const steps = data.steps || [];
  const stepPills = [`<button class="pill ${S.step === 'ALL' ? 'on' : ''}" data-step="ALL" ${available ? '' : 'disabled'}>全部单运</button>`]
    .concat(steps.map((s) => `<button class="pill ${String(S.step) === String(s.step_no) ? 'on' : ''}" data-step="${s.step_no}" ${available ? '' : 'disabled'}>
      ${esc(s.ganzhi)}<span class="sub">第${s.step_no}步 · ${s.single_month_count ?? 0}月</span></button>`))
    .join('');
  const policyPills = ['ALL', 'EXCLUDE_SINGLE'].map((v) => `
    <button class="pill ${S.policy === v ? 'on' : ''}" data-policy="${v}">
      ${v === 'ALL' ? '含单交易日月' : '不含单交易日月'}
    </button>`).join('');
  const statusOptions = [
    ['ALL', '全部状态'], ['SINGLE_DAYUN', '单一运'], ['CROSSES_DAYUN_BOUNDARY', '跨换运'],
    ['BEFORE_FIRST_DAYUN', '起运前'], ['BLOCKED_NEUTRAL', '中性'],
    ['BLOCKED_GENDER', '代理缺失'], ['TIMELINE_UNAVAILABLE', '时间轴待核'], ['NO_PROFILE', '无命盘'],
  ].map(([v, t]) => `<option value="${v}" ${S.status === v ? 'selected' : ''}>${t}</option>`).join('');

  $('#filters').innerHTML = `
    <div class="filter-line">
      <span class="label">大运</span>
      <div class="pill-row">${stepPills}</div>
    </div>
    <div class="filter-line">
      <span class="label">样本口径</span>
      <div class="pill-row">${policyPills}</div>
      <span class="label" style="margin-left:12px">月状态</span>
      <select id="status-filter" class="dy-select">${statusOptions}</select>
      ${available ? '' : '<span class="small muted">当前时间轴不可用：步选择已禁用，只做历史月展示。</span>'}
    </div>`;
  $$('#filters [data-step]').forEach((b) => b.addEventListener('click', () => {
    if (!b.disabled) setStep(b.dataset.step);
  }));
  $$('#filters [data-policy]').forEach((b) => b.addEventListener('click', () => setPolicy(b.dataset.policy)));
  $('#status-filter').addEventListener('change', (e) => setStatus(e.target.value));
}

function renderMonthSummary(data) {
  const summary = data.summary || {};
  const labels = (data.labels && data.labels.binding) || {};
  const order = ['SINGLE_DAYUN', 'CROSSES_DAYUN_BOUNDARY', 'BEFORE_FIRST_DAYUN',
    'BLOCKED_NEUTRAL', 'BLOCKED_GENDER', 'TIMELINE_UNAVAILABLE', 'NO_PROFILE'];
  const chips = order.filter((k) => summary[k]).map((k) =>
    `<span class="tag ${BINDING_TAG[k] || ''}">${esc(labels[k] || k)} ${summary[k]}</span>`).join('');
  $('#month-summary').innerHTML = `<div class="badges" style="margin:0">${chips}</div>`;
}

function relText(raw) {
  if (!raw) return '—';
  try {
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr) || !arr.length) return '—';
    const shown = arr.slice(0, 5).map(esc).join('、');
    return arr.length > 5 ? shown + '…' : shown;
  } catch (e) { return esc(raw); }
}

function renderMonths(data) {
  renderMonthSummary(data);
  const head = `<tr>
    <th class="left">月</th><th class="left">流月柱</th><th>涨跌</th><th>收益</th>
    <th class="left">大运</th><th class="left">大运十神</th><th class="left">绑定</th>
    <th class="left">跨运</th><th class="left">主关系</th>
  </tr>`;
  $('#months-table thead').innerHTML = head;
  const rows = data.months || [];
  if (!rows.length) {
    $('#months-table tbody').innerHTML = `<tr class="no-hover"><td colspan="9" class="left muted">无符合条件的月份。</td></tr>`;
    return;
  }
  $('#months-table tbody').innerHTML = rows.map((r) => {
    const cross = r.crosses_boundary
      ? (r.prev_step_no === 0
          ? `起运前→第${r.next_step_no}步`
          : `第${r.prev_step_no}→第${r.next_step_no}步`)
      : '—';
    return `<tr data-month="${esc(r.month_id)}" ${r.binding_status !== 'SINGLE_DAYUN' ? 'title="' + esc(r.note || r.binding_label) + '"' : ''}>
      <td class="left nowrap">${esc(r.month_id)}</td>
      <td class="left nowrap"><b>${esc(r.flow_pillar)}</b></td>
      <td>${dirHtml(r.direction)}</td>
      <td class="${r.direction === 'UP' ? 'c-up' : r.direction === 'DOWN' ? 'c-down' : 'c-flat'}">${esc(fmtPct(r.body_return))}</td>
      <td class="left nowrap">${esc(r.dayun_ganzhi || '—')}</td>
      <td class="left">${esc(r.dayun_stem_tengod || '—')}</td>
      <td class="left">${tag(r.binding_label || r.binding_status, BINDING_TAG[r.binding_status] || '')}</td>
      <td class="left nowrap">${esc(cross)}</td>
      <td class="left">${relText(r.relation_type_list)}</td>
    </tr>`;
  }).join('');
  $$('#months-table tbody tr[data-month]').forEach((tr) =>
    tr.addEventListener('click', () => openMonth(tr.dataset.month)));
}

async function loadMonths() {
  if (!S.id) return;
  try {
    S.monthsData = await fetchJSON(apiUrl('/api/dayun/months', {
      id: S.id, step: S.step, policy: S.policy, status: S.status,
    }));
    if (S.monthsData.extension_status !== 'AVAILABLE') {
      $('#month-summary').innerHTML = `<div class="banner blocked">${esc(S.monthsData.message || '扩展不可用')}</div>`;
      $('#months-table thead').innerHTML = '';
      $('#months-table tbody').innerHTML = '';
      return;
    }
    renderMonths(S.monthsData);
  } catch (e) { toast('月份读取失败：' + e.message); }
}

function thresholdTag(status) {
  const [cls, text] = THRESHOLD_TAG[status] || ['', status || '—'];
  return tag(text, cls);
}

function statTable(rows, firstLabel, withElement) {
  if (!rows || !rows.length) return '<div class="muted">无数据。</div>';
  return `<div class="table-wrap" style="max-height:430px;overflow:auto">
    <table class="dy-table">
      <thead><tr>
        <th class="left">${esc(firstLabel)}</th>${withElement ? '<th class="left">五行</th>' : ''}
        <th>n</th><th>涨</th><th>跌</th><th>平</th><th>上涨占比</th><th>下跌占比</th>
        <th>均值</th><th>中位</th><th class="left">样本</th><th class="left">阈值</th>
      </tr></thead>
      <tbody>${rows.map((r) => `<tr class="no-hover ${r.n ? '' : 'zero'}">
        <td class="left"><b>${esc(r.key)}</b></td>
        ${withElement ? `<td class="left">${esc(r.element || '—')}</td>` : ''}
        <td>${r.n}</td><td class="c-up">${r.up}</td><td class="c-down">${r.down}</td><td class="c-flat">${r.flat}</td>
        <td>${esc(fmtPct(r.up_rate))}</td><td>${esc(fmtPct(r.down_rate))}</td>
        <td class="${r.mean_return > 0 ? 'c-up' : r.mean_return < 0 ? 'c-down' : ''}">${esc(fmtPct(r.mean_return))}</td>
        <td>${esc(fmtPct(r.median_return))}</td>
        <td class="left">${esc(GRADE_TEXT[r.sample_grade] || r.sample_grade || '—')}</td>
        <td class="left">${thresholdTag(r.threshold_status)}</td>
      </tr>`).join('')}</tbody>
    </table>
  </div>`;
}

function renderStats(data) {
  if (!data || data.status !== 'OK') {
    $('#stats-body').innerHTML = `<div class="banner warn">${esc(data && data.message ? data.message : '该身份不能做大运分组统计。')}
      ${data && data.direction_label ? '<br><span class="muted">方向：' + esc(data.direction_label) + '；性别：' + esc(data.gender_label || '—') + '</span>' : ''}
      <br><span class="muted">原 V1 的 12 支/60 柱统计仍可在主页面查看；跨运/起运前/中性/时间轴待核月不进入大运分组。</span></div>`;
    return;
  }
  const t = data.totals || {};
  const ex = data.excluded || {};
  const stepText = data.step_info
    ? `第${data.step_info.step_no}步 ${data.step_info.ganzhi}（${data.step_info.dayun_stem_tengod || '—'}）`
    : '全部单运月';
  const crossList = (ex.crosses || []).map((c) => esc(c.month_id)).join('、') || '无';
  $('#stats-body').innerHTML = `
    <div class="banner" style="margin-bottom:10px">
      当前：<b>${esc(stepText)}</b> · 口径 <b>${esc(data.policy)}</b>。
      ${ex.note ? esc(ex.note) : ''}
      跨运月 ${esc(ex.crosses_n ?? 0)} 个（${crossList}）；起运前 ${esc(ex.before_n ?? 0)} 个。
    </div>
    <div class="stat-big">
      <div class="box"><b>${t.n ?? 0}</b><span>样本月 n</span></div>
      <div class="box"><b class="c-up">${t.up ?? 0}</b><span>涨</span></div>
      <div class="box"><b class="c-down">${t.down ?? 0}</b><span>跌</span></div>
      <div class="box"><b class="c-flat">${t.flat ?? 0}</b><span>平</span></div>
      <div class="box"><b>${(t.n === (t.up + t.down + t.flat)) ? '守恒 ✓' : '不守恒 ✗'}</b><span>涨+跌+平=n</span></div>
    </div>
    <div class="footnote" style="margin-bottom:10px">
      门槛沿用既有口径：12 支 n≥${data.thresholds.branch_min_n}；60 柱 n≥${data.thresholds.pillar_min_n}
      （3–5 为低样本）；同向占比 ≥${Math.round(data.thresholds.dominant_rate * 100)}%。不按大运重调阈值。
      分母是去重的股票×历史月；平盘计入分母；零样本显示 NULL。
    </div>
    <div class="cols-2">
      <div><h3>12 月支</h3>${statTable(data.branch12, '支', true)}</div>
      <div><h3>60 流月柱</h3>${statTable(data.pillar60, '柱', false)}</div>
    </div>`;
}

async function loadStats() {
  if (!S.id) return;
  try {
    S.statsData = await fetchJSON(apiUrl('/api/dayun/stats', {
      id: S.id, step: S.step, policy: S.policy,
    }));
    renderStats(S.statsData);
  } catch (e) {
    $('#stats-body').innerHTML = `<div class="banner blocked">统计读取失败：${esc(e.message)}</div>`;
  }
}

function renderInfo(data) {
  if (!data || data.extension_status !== 'AVAILABLE') {
    $('#info-body').innerHTML = `<div class="banner blocked">${esc(data && data.message ? data.message : '扩展元信息不可用')}</div>`;
    return;
  }
  const m = data.meta || {};
  const c = data.coverage || {};
  const datasetLabel = data.dataset === 'etf' ? 'ETF' : 'A股';
  const sources = (data.sources || []).map((s) => `<tr class="no-hover">
    <td class="left">${esc(s.source_key)}</td><td class="left">${esc(s.version || '—')}</td>
    <td class="left"><span class="small">${esc((s.sha256 || '').slice(0, 12))}…</span></td>
    <td class="left">${esc(s.role)}</td><td class="left">${esc(s.status)}</td>
  </tr>`).join('');
  const interp = data.gender_interpretation;
  $('#info-body').innerHTML = `
    <div class="dy-kv-grid">
      ${kv('数据集 / 构建版本', esc(datasetLabel + ' / ' + (m.build_version || '—')))}
      ${kv('扩展库 SHA256', `<span class="small" style="word-break:break-all">${esc(data.extension_sha256 || '—')}</span>`)}
      ${kv('构建时间', esc(m.build_time || '—'))}
      ${kv('行情截止 / 末完整月', esc((m.as_of || '—') + ' / ' + (m.market_data_last_month || '—')))}
      ${kv('主库 SHA256', `<span class="small" style="word-break:break-all">${esc(m.main_db_sha256 || '—')}</span>`)}
      ${kv('方法 / 规则版本', esc((m.method_id || '—') + ' / ' + (m.rule_version || '—')))}
      ${kv('十神规则', esc(m.ten_god_rule_version || '—'))}
      ${kv('起运精度 / 时区', esc((m.start_precision || '—') + ' / ' + (m.timezone || '—')))}
      ${kv('性别口径', esc(m.gender_policy || '—'))}
      ${kv('性别口径说明', esc(m.gender_policy_note || '—'))}
      ${kv('代理来源', esc(m.proxy_source || '—'))}
      ${kv('锚点策略', esc(m.anchor_policy || '—'))}
      ${kv('大运-流月派生关系', esc(m.dayun_flow_interactions || '—'))}
    </div>
    <div class="banner warn" style="margin-top:10px">${esc(m.proxy_warning || '')}</div>
    <h3>覆盖摘要</h3>
    <div class="badges">
      ${tag('身份 ' + (c.identity ?? '—'), 'indigo')}
      ${tag('有命盘 ' + (c.profile ?? '—'), 'indigo')}
      ${tag('男 ' + (c.male ?? 0), 'ok')}
      ${tag('女 ' + (c.female ?? 0), 'warn')}
      ${tag('中性 ' + (c.neutral ?? 0), 'warn')}
      ${tag('代理缺失 ' + (c.unknown ?? 0), 'blocked')}
      ${tag('方向可算 ' + (c.direction_available ?? 0), 'ok')}
      ${tag('时间轴可算 ' + (c.timeline_available ?? 0), 'ok')}
      ${tag('锚点冲突 ' + (c.timeline_anchor_conflict ?? 0), 'warn')}
      ${tag('单运月 ' + (c.binding_SINGLE_DAYUN ?? 0), 'ok')}
      ${tag('跨运月 ' + (c.binding_CROSSES_DAYUN_BOUNDARY ?? 0), 'warn')}
      ${tag('中性月 ' + (c.binding_BLOCKED_NEUTRAL ?? 0), 'warn')}
      ${tag('无命盘 ' + (c.no_profile ?? 0), 'blocked')}
    </div>
    <h3>传统男女解释</h3>
    <div class="banner warn" style="margin:0">
      状态：<b>${esc(interp ? interp.status : 'NOT_REVIEWED')}</b>。本轮不生成“男命财星必吉/女命官星必吉”等解释；
      十神名称与五行关系不因性别改变。
    </div>
    <h3>来源</h3>
    <div class="table-wrap" style="max-height:280px;overflow:auto">
      <table class="dy-table">
        <thead><tr><th class="left">source_key</th><th class="left">版本</th><th class="left">SHA</th>
          <th class="left">角色</th><th class="left">状态</th></tr></thead>
        <tbody>${sources}</tbody>
      </table>
    </div>
    <div class="footnote" style="margin-top:10px">
      本页是历史描述性研究，不是预测，不构成吉凶评级或买卖建议。性别是用户裁定的研究口径，不是证券的现实属性；
      平/中性没有男/女顺逆，不被默认成男命，也不由涨跌倒推。跨换运月不拆分、不重复计入。
    </div>`;
}

async function openMonth(monthId) {
  try {
    const detail = await fetchJSON(apiUrl('/api/month_detail', { id: S.id, month: monthId }));
    if (!detail) { toast('未找到该月事实'); return; }
    const tg = detail.ten_god || {};
    const stemGod = tg.stem && tg.stem.ten_god ? tg.stem.ten_god : (detail.stem_ten_god || '—');
    const hidden = (tg.hidden_stems || []).map((h) =>
      `${esc(h.stem || h.hidden_stem || '')}${h.ten_god ? '·' + esc(h.ten_god) : ''}`).join('、') || '—';
    const events = detail.events || [];
    const eventRows = events.length ? events.map((e) => `<tr class="no-hover">
      <td class="left">${esc(e.record_kind)}</td><td class="left">${esc(e.relation_type)}</td>
      <td class="left">${esc(e.natal_position || '—')}</td>
      <td class="left">${esc((e.flow_stem || '') + (e.flow_branch || ''))}</td>
      <td class="left">${esc((e.natal_element || '') + '/' + (e.flow_element || ''))}</td>
      <td class="left">${esc(e.svu || '—')}</td>
      <td class="left">${e.undisputed ? '无争议' : '有争议'}</td>
    </tr>`).join('') : `<tr class="no-hover"><td colspan="7" class="left muted">该月无 V1 关系事件。</td></tr>`;
    $('#modal-title').textContent = `${detail.month_id} · ${detail.flow_pillar} · ${detail.identity_code}`;
    $('#modal-body').innerHTML = `
      <div class="badges">
        ${tag('涨跌：' + dirText(detail.direction), detail.direction === 'UP' ? 'ok' : detail.direction === 'DOWN' ? 'blocked' : 'warn')}
        ${tag('收益：' + fmtPct(detail.body_return), 'indigo')}
        ${tag(detail.research_sample_status, detail.boundary_disputed ? 'warn' : 'ok')}
        ${tag(detail.fact_status || '', '')}
      </div>
      <div class="dy-kv-grid">
        ${kv('节气区间', esc((detail.start_at || '—') + ' → ' + (detail.end_at || '—')))}
        ${kv('首/末交易日', esc((detail.first_date || '—') + ' → ' + (detail.last_date || '—')))}
        ${kv('交易日数', esc(detail.trading_days ?? '—'))}
        ${kv('开/收（原始）', esc((detail.raw_open ?? '—') + ' / ' + (detail.raw_close ?? '—')))}
        ${kv('基准 / 超额', esc((detail.benchmark_code || '—') + ' / ' + fmtPct(detail.excess_return)))}
        ${kv('流月干十神', esc(stemGod))}
        ${kv('藏干十神', esc(hidden))}
        ${kv('交节争议', detail.boundary_disputed ? '是' : '否')}
      </div>
      <h3>V1 原局-流月关系事件（${events.length}）</h3>
      <div class="table-wrap" style="max-height:320px;overflow:auto">
        <table class="dy-table">
          <thead><tr><th class="left">类型</th><th class="left">关系</th><th class="left">原局位</th>
            <th class="left">流月干支</th><th class="left">五行</th><th class="left">SVU</th><th class="left">争议</th></tr></thead>
          <tbody>${eventRows}</tbody>
        </table>
      </div>
      <div class="footnote" style="margin-top:10px">
        这里只显示 V1 已冻结的原局-流月关系；不包含本轮未实现的大运-流月派生关系。
      </div>`;
    $('#modal').classList.remove('hidden');
    $('#modal').setAttribute('aria-hidden', 'false');
  } catch (e) { toast('月详情读取失败：' + e.message); }
}

function closeModal() {
  $('#modal').classList.add('hidden');
  $('#modal').setAttribute('aria-hidden', 'true');
}

async function loadStock(id, opts = {}) {
  id = String(id || '').trim();
  if (!id) return;
  S.id = id;
  $('#q').value = id;
  $('#ext-banner').innerHTML = '<div class="banner">加载中…</div>';
  try {
    const data = await fetchJSON(apiUrl('/api/dayun/profile', { id }));
    S.profileData = data;
    if (data.extension_status !== 'AVAILABLE') {
      renderUnavailable(data);
      syncDayunUrl(opts.push ? 'push' : 'replace');
      return;
    }
    const steps = data.steps || [];
    if (S.step !== 'ALL' && !steps.some((s) => String(s.step_no) === String(S.step))) {
      S.step = 'ALL';
    }
    renderProfile(data);
    renderSteps(data);
    renderFilters(data);
    syncDayunUrl(opts.push ? 'push' : 'replace');
    await Promise.all([loadMonths(), loadStats()]);
  } catch (e) {
    $('#ext-banner').innerHTML = `<div class="banner blocked">读取失败：${esc(e.message)}</div>`;
    toast('读取失败：' + e.message);
  }
}

async function doSearch(q) {
  q = (q || '').trim();
  const box = $('#suggest');
  if (!q) { box.classList.add('hidden'); return; }
  try {
    const rows = await fetchJSON(apiUrl('/api/search', { q, limit: 20 }));
    if (!Array.isArray(rows) || !rows.length) {
      box.innerHTML = '<div style="padding:10px" class="muted">无匹配</div>';
    } else {
      box.innerHTML = rows.map((r) => `<button data-id="${esc(r.identity_code)}">
        <b>${esc(r.identity_code)}</b> ${esc(r.name || '')}
        <span class="muted small">${esc(r.security_type || '')}${r.has_natal_profile ? '' : ' · 无命盘'}</span>
      </button>`).join('');
      $$('#suggest [data-id]').forEach((b) => b.addEventListener('click', () => {
        box.classList.add('hidden');
        $('#q').value = b.dataset.id;
        loadStock(b.dataset.id, { push: true });
      }));
    }
    box.classList.remove('hidden');
  } catch (e) { toast('搜索失败：' + e.message); }
}

function bindEvents() {
  $('#theme-toggle').addEventListener('click', () => {
    const html = document.documentElement;
    const light = html.classList.toggle('light');
    html.classList.toggle('dark', !light);
    localStorage.setItem('dayun-theme', light ? 'light' : 'dark');
    $('#theme-toggle').textContent = light ? '🌙' : '☀️';
  });
  $$('#dy-ds [data-ds]').forEach((b) =>
    b.addEventListener('click', () => setDataset(b.dataset.ds)));
  $('#search-btn').addEventListener('click', () => doSearch($('#q').value));
  let timer = null;
  $('#q').addEventListener('input', (e) => {
    clearTimeout(timer);
    const q = e.target.value.trim();
    timer = setTimeout(() => doSearch(q), 220);
  });
  $('#q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doSearch($('#q').value); }
  });
  $('#modal-close').addEventListener('click', closeModal);
  $('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#stocksearch, #suggest, #q, #search-btn')) {
      $('#suggest').classList.add('hidden');
    }
  });
}

(async function init() {
  applyTheme();
  bindEvents();
  const st = readDayunUrl();
  S.dataset = st.dataset;
  S.step = st.step; S.policy = st.policy; S.status = st.status;
  S.returnTab = st.tab; S.fromBoard = st.fromBoard; S.embed = st.embed;
  syncDatasetButtons();
  await loadMeta();
  const id = st.id || (S.dataset === 'etf' ? '510050.SH' : '000690.SZ');
  $('#q').value = id;
  await loadStock(id);
  addEventListener('popstate', async () => {
    const back = readDayunUrl();
    S.restoring = true;
    try {
      S.returnTab = back.tab;
      S.fromBoard = back.fromBoard;
      if (back.dataset !== S.dataset) {
        S.dataset = back.dataset;
        syncDatasetButtons();
        await loadMeta();
      }
      S.step = back.step; S.policy = back.policy; S.status = back.status;
      const target = back.id || (S.dataset === 'etf' ? '510050.SH' : '000690.SZ');
      $('#q').value = target;
      await loadStock(target);
    } finally {
      S.restoring = false;
    }
  });
})();
