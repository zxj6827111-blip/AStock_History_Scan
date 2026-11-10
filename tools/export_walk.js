#!/usr/bin/env node
/* 页面五「流月机会表 → 导出 Excel」真实点击走查。
   只用 Node 内置模块（fetch + global WebSocket），无头 Chrome + CDP，下载落到磁盘后由
   tools/verify_opp_export.py 独立复算校验。
   用法：APP_URL=http://127.0.0.1:8737/ node tools/export_walk.js evidence/export_walk */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = process.argv[2] || 'evidence/export_walk';
const DLDIR = path.join(OUTDIR, 'downloads');
const PORT = 9446;
const BASE = process.env.APP_URL || 'http://127.0.0.1:8737/';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(DLDIR, { recursive: true });
  /* 预检：端口上已经有 Chrome 就是上轮没清干净的旧实例，连它会走查错对象（宁可不跑） */
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
        if (r.ok) { console.error(`端口 ${PORT} 已有调试实例在跑，先杀掉它再重跑`); process.exit(2); } }
  catch (e) { /* 没人监听才对 */ }
  const prof = path.join('/tmp', 'cdp-profile-opp-export');
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${prof}`,
    '--window-size=1780,1300', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });

  let up = false;
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
          if (r.ok) { up = true; break; } } catch (e) {}
    await sleep(250);
  }
  if (!up) { console.error('chrome devtools port never came up'); chrome.kill('SIGKILL'); process.exit(1); }

  const tgt = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`,
    { method: 'PUT' })).json();
  const ws = new WebSocket(tgt.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  let id = 0; const pend = new Map(); const pageErrors = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Runtime.exceptionThrown') {
      pageErrors.push(m.params.exceptionDetails.exception
        ? m.params.exceptionDetails.exception.description : m.params.exceptionDetails.text);
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error')
      pageErrors.push(m.params.entry.text);
    if (m.id && pend.has(m.id)) {
      const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
    }
  };
  const send = (method, params) => new Promise((res, rej) => {
    const i = ++id; pend.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params: params || {} }));
  });
  const evl = async (expr) => {
    const r = await send('Runtime.evaluate',
      { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error('JS: ' + (d.exception && (d.exception.description || d.exception.value) || d.text));
    }
    return r.result.value;
  };
  const wait = async (expr, ms) => {
    const t0 = Date.now();
    for (;;) {
      let ok = false;
      try { ok = await evl(expr); } catch (e) { ok = false; }
      if (ok) return true;
      if (Date.now() - t0 > (ms || 15000)) throw new Error('timeout waiting for ' + expr);
      await sleep(150);
    }
  };
  const shot = async (name) => {
    const { data } = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(OUTDIR, name + '.png'), Buffer.from(data, 'base64'));
    console.log('shot', name);
  };

  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Browser.setDownloadBehavior',
    { behavior: 'allow', downloadPath: DLDIR, eventsEnabled: true });

  const dlFiles = () => fs.readdirSync(DLDIR).filter((f) => f.endsWith('.xlsx'));
  const waitDownload = async (before, ms) => {
    const t0 = Date.now();
    for (;;) {
      const nu = dlFiles().filter((f) => !before.includes(f) && !f.endsWith('.crdownload'));
      if (nu.length) {
        const p = path.join(DLDIR, nu[0]);
        let last = -1, same = 0;
        while (same < 3) {
          const s = fs.existsSync(p) ? fs.statSync(p).size : 0;
          same = (s > 0 && s === last) ? same + 1 : 0;
          last = s; await sleep(200);
        }
        return nu[0];
      }
      if (Date.now() - t0 > (ms || 20000)) throw new Error('no download appeared');
      await sleep(200);
    }
  };

  await send('Page.navigate', { url: BASE + '#board' });
  await wait(`document.querySelectorAll('#opptables table tbody tr').length>0`, 40000);
  await sleep(600);

  const report = [];
  const rec = (step, value) => { report.push({ step, value }); console.log(step, ':', value); };

  /* 改筛选后必须等到「服务端回来的 params 已经是新值」才算渲染完，
     只看行数会把上一屏的表当成这一屏导出去。 */
  const settled = (key, want) => `S.opp && S.opp.params && `
    + `String(S.opp.params.${key})===${JSON.stringify(String(want))}`;
  const setVal = async (sel, v, key, want) => {
    await evl(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); e.value=${v};
      e.dispatchEvent(new Event('change',{bubbles:true})); return e.value})()`);
    await wait(settled(key, want), 40000);
    await sleep(900);
  };
  const setChk = async (sel, on, key, want) => {
    await evl(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); e.checked=${on ? 'true' : 'false'};
      e.dispatchEvent(new Event('change',{bubbles:true})); return e.checked})()`);
    await wait(settled(key, want), 40000);
    await sleep(900);
  };
  const tableState = async () => JSON.parse(await evl(`JSON.stringify({
    th: [...document.querySelectorAll('#opptables thead th')].map(t=>t.innerText.trim()),
    domRows: document.querySelectorAll('#opptables tbody tr').length,
    cells: [...document.querySelectorAll('#opptables tbody tr')].slice(0,3)
      .map(tr=>[...tr.children].map(td=>td.innerText.replace(/\\s+/g,' ').trim())),
    specLen: (typeof S!=='undefined'&&S.oppExport) ? S.oppExport.spec.length : null,
    specHeaders: (typeof S!=='undefined'&&S.oppExport) ? S.oppExport.spec.map(c=>c.h) : null,
    params: (typeof S!=='undefined'&&S.opp) ? S.opp.params : null,
    dataset: (typeof S!=='undefined'&&S.opp) ? S.opp.dataset : null,
    month: (typeof S!=='undefined'&&S.oppExport) ? {month_id:S.oppExport.m.month_id,
      pillar:S.oppExport.m.pillar, ordinal:S.oppExport.m.ordinal,
      market_rows_in_month:S.oppExport.m.market_rows_in_month,
      in_scope:S.oppExport.m.totals.in_scope, returned:S.oppExport.m.totals.returned,
      caveats:(S.opp.caveats||[]).length,
      actual_summary:S.oppExport.m.actual_summary} : null,
    btnDisabled: document.querySelector('#o-export').disabled,
    btnTitle: document.querySelector('#o-export').title,
  })`));
  const doExport = async (tag) => {
    const before = dlFiles();
    await evl(`document.querySelector('#o-export').click()`);
    const f = await waitDownload(before, 30000);
    const msg = await evl(`document.querySelector('#o-export-msg').innerText`);
    const st = await tableState();
    st.exportedFile = f;
    fs.writeFileSync(path.join(OUTDIR, tag + '.json'), JSON.stringify(st, null, 2));
    rec(tag + '_file', f);
    rec(tag + '_msg', msg);
    rec(tag + '_shape', st.specLen + ' 导出列 / ' + st.th.length + ' 表头 / ' + st.domRows + ' 行');
    return { file: f, msg, state: st };
  };

  // ---------- A 本月（该月行情未入库） ----------
  await setVal('#o-month', "'0'", 'offset', '0');
  rec('A_month_txt', await evl(`document.querySelector('#o-month-txt').innerText`));
  rec('A_rows', await evl(`document.querySelectorAll('#opptables tbody tr').length`));
  const A = await doExport('A_current_month');
  await shot('A_本月导出');

  // ---------- B 历史月：点开月份格子，选最后一个有行情的格子（多两列） ----------
  await evl(`document.querySelector('#o-month-btn').click()`);
  await sleep(400);
  const pick = await evl(`(()=>{const b=[...document.querySelectorAll('#mp-grid .mp-cell.has')];
    if(!b.length) return null; b[b.length-1].click(); return b[b.length-1].innerText.replace(/\\s+/g,' ')})()`);
  rec('B_picked_cell', pick);
  await wait(`S.opp && String(S.opp.params.offset)===document.querySelector('#o-month').value`
    + `&& document.querySelectorAll('#opptables tbody tr').length>0`, 40000);
  await sleep(900);
  rec('B_month_txt', await evl(`document.querySelector('#o-month-txt').innerText`));
  rec('B_extra_headers', await evl(`[...document.querySelectorAll('#opptables thead th')].map(t=>t.innerText.trim()).join('|')`));
  const B = await doExport('B_past_month');
  await shot('B_历史月导出');

  // ---------- C 方向轴切下跌（表头文案要跟着翻） ----------
  await setVal('#o-dir', "'down'", 'dir', 'down');
  const C = await doExport('C_dir_down');
  rec('C_headers', C.state.th.join('|'));
  await shot('C_下跌方向导出');
  await setVal('#o-dir', "'up'", 'dir', 'up');

  // ---------- D 连续两月 ----------
  await setChk('#o-both-months', true, 'both_months', 'true');
  const D = await doExport('D_both_months');
  rec('D_headers', D.state.th.join('|'));
  await shot('D_连续两月导出');
  await setChk('#o-both-months', false, 'both_months', 'false');

  // ---------- E 排除单交易日月（涨/跌/平与占比换分母） ----------
  await setChk('#o-es', true, 'policy', 'EXCLUDE_SINGLE');
  const E = await doExport('E_exclude_single');
  await shot('E_排除单交易日月导出');
  await setChk('#o-es', false, 'policy', 'ALL');

  // ---------- F 空表：按钮必须禁用并给原因 ----------
  await setVal('#o-min', "'6'", 'min_n', '6');
  await setVal('#o-grade', "'1-2'", 'grade', '1-2');
  await sleep(1200);
  rec('F_dom_rows', await evl(`document.querySelectorAll('#opptables tbody tr').length`));
  rec('F_btn_disabled', await evl(`document.querySelector('#o-export').disabled`));
  rec('F_btn_title', await evl(`document.querySelector('#o-export').title`));
  rec('F_note', await evl(`(document.querySelector('#opptables .noprof')||{innerText:''}).innerText.slice(0,140)`));
  await shot('F_空表按钮禁用');
  await setVal('#o-grade', "''", 'grade', '');
  await setVal('#o-min', "'3'", 'min_n', '3');

  // ---------- G ETF 数据集 ----------
  await evl(`document.querySelector('#ds button[data-ds=etf]').click()`);
  await wait(`S.opp && S.opp.dataset==='etf'`, 40000);
  await sleep(900);
  rec('G_dom_rows', await evl(`document.querySelectorAll('#opptables tbody tr').length`));
  const G = await doExport('G_etf');
  await shot('G_ETF导出');
  await evl(`document.querySelector('#ds button[data-ds=ashare]').click()`);
  await sleep(1500);
  await wait(`S.opp && S.opp.dataset==='ashare'`, 40000);

  // ---------- H 组件缺失时的降级说明（临时把 XLSX 藏起来再点，不静默失败） ----------
  await evl(`window.__x=XLSX; XLSX=undefined;`);
  const beforeH = dlFiles().length;
  await evl(`document.querySelector('#o-export').click()`);
  await sleep(500);
  rec('H_missing_lib_msg', await evl(`document.querySelector('#o-export-msg').innerText`));
  rec('H_no_new_file', dlFiles().length === beforeH);
  await evl(`XLSX=window.__x;`);
  rec('H_after_restore', await evl(`typeof XLSX`));

  rec('downloads_total', dlFiles().length);
  rec('page_errors', JSON.stringify(pageErrors));
  fs.writeFileSync(path.join(OUTDIR, 'export_walk_report.json'),
    JSON.stringify({ base: BASE, at: new Date().toISOString(), report,
      scenarios: { A, B, C, D, E, G }, pageErrors }, null, 2));

  ws.close();
  chrome.kill('SIGKILL');
  console.log('DONE', OUTDIR, 'files=', dlFiles().length);
}

main().catch((e) => { console.error('FAILED', e); process.exit(1); });
