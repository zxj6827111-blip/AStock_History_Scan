#!/usr/bin/env node
/* 市值档接入的实机走查：个股页字段 + 页面五筛选 + 导出列 + 控制台错误。
   用法：APP_URL=http://127.0.0.1:8855/ node tools/cap_tier_walk.js evidence/cap_tier_walk */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = process.argv[2] || 'evidence/cap_tier_walk';
const PORT = 9449;
const BASE = process.env.APP_URL || 'http://127.0.0.1:8855/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
        if (r.ok) { console.error(`端口 ${PORT} 已有调试实例`); process.exit(2); } } catch (e) {}
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-cap-tier',
    '--window-size=1780,1300', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });
  let up = false;
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
          if (r.ok) { up = true; break; } } catch (e) {}
    await sleep(250);
  }
  if (!up) { chrome.kill('SIGKILL'); throw new Error('chrome devtools port never came up'); }

  const tgt = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`,
    { method: 'PUT' })).json();
  const ws = new WebSocket(tgt.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0; const pend = new Map(); const pageErrors = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Runtime.exceptionThrown') pageErrors.push(m.params.exceptionDetails.exception
      ? m.params.exceptionDetails.exception.description : m.params.exceptionDetails.text);
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') pageErrors.push(m.params.entry.text);
    if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id);
      m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
  const send = (method, params) => new Promise((res, rej) => {
    const i = ++id; pend.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params: params || {} })); });
  const evl = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) { const d = r.exceptionDetails;
      throw new Error('JS: ' + (d.exception && (d.exception.description || d.exception.value) || d.text)); }
    return r.result.value; };
  const wait = async (expr, ms) => { const t0 = Date.now();
    for (;;) { let ok = false; try { ok = await evl(expr); } catch (e) { ok = false; }
      if (ok) return true;
      if (Date.now() - t0 > (ms || 20000)) throw new Error('timeout: ' + expr);
      await sleep(150); } };
  const shot = async (name) => { const { data } = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(OUTDIR, name + '.png'), Buffer.from(data, 'base64')); };

  const out = [];
  const rec = (n, ok, d) => { out.push({ n, ok, d }); console.log((ok ? 'PASS ' : 'FAIL ') + n + (d ? '  | ' + d : '')); };

  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Page.navigate', { url: BASE + '?id=601398.SH&tab=stock' });
  await wait(`document.querySelector('#stockcard') && document.querySelector('#stockcard').innerText.includes('自由流通市值')`, 40000);
  const card = await evl(`document.querySelector('#stockcard').innerText`);
  rec('个股页-自由流通市值', /自由流通市值/.test(card), card.match(/自由流通市值[^\n]*/)?.[0] || '');
  rec('个股页-市值档', /市值档/.test(card), card.match(/市值档[^\n]*/)?.[0] || '');
  rec('个股页-20日均额', /20日均额/.test(card), card.match(/20日均额[^\n]*/)?.[0] || '');
  await shot('01_stock_601398');

  await evl(`document.querySelector('#open-board').click()`);
  await wait(`typeof S !== 'undefined' && S.opp && S.opp.params && S.opp.rows && S.opp.rows.length > 0`, 40000);
  await evl(`(() => { const m = document.querySelector('#o-month'); m.value = '-2'; m.dispatchEvent(new Event('change', {bubbles:true})); })()`);
  await wait(`S.opp && S.opp.params && String(S.opp.params.offset) === '-2' && S.opp.rows.length > 0`, 40000);
  const before = await evl(`(() => { const trs = [...document.querySelectorAll('#opptables tbody tr')];
    const tiers = trs.map((tr) => tr.lastElementChild.innerText.trim()).filter(Boolean);
    return { rows: trs.length, tiers: [...new Set(tiers)], asof: S.opp.params.cap_asof_month }; })()`);
  rec('机会表-默认混合档', new Set(before.tiers).size >= 3, `${before.rows} 行 / ${JSON.stringify(before.tiers)} / asof ${before.asof}`);
  await shot('02_board_all');

  const apiCount = await (async () => {
    const r = await fetch(BASE + 'api/opportunities?months=1&offset=-2&min_n=3&dir=up&rate_min=80&cap=200&cap_tier=' + encodeURIComponent('超大盘'));
    const d = await r.json(); return d.rows.length; })();

  await evl(`(() => { const s = document.querySelector('#o-cap-tier'); s.value = '超大盘';
    s.dispatchEvent(new Event('change', {bubbles:true})); })()`);
  await wait(`S.opp && S.opp.params && S.opp.params.cap_tier === '超大盘'`, 40000);
  const after = await evl(`(() => { const trs = [...document.querySelectorAll('#opptables tbody tr')];
    const tiers = trs.map((tr) => tr.lastElementChild.innerText.trim()).filter(Boolean);
    return { rows: trs.length, tiers: [...new Set(tiers)], note: document.querySelector('#o-note').innerText,
             exportCols: (S.oppExport && S.oppExport.spec || []).map((c) => c.h) }; })()`);
  rec('机会表-筛选超大盘', after.rows === apiCount && after.tiers.length === 1 && after.tiers[0] === '超大盘',
      `${after.rows} 行（API ${apiCount}）/ ${JSON.stringify(after.tiers)}`);
  rec('机会表-口径行显示市值档', /市值档\s*超大盘/.test(after.note), after.note.match(/市值档[^·]*/)?.[0] || '');
  rec('导出含市值档列', after.exportCols.includes('市值档') && after.exportCols.includes('自由流通市值(亿)'),
      after.exportCols.filter((x) => /市值/.test(x)).join(' / '));
  const exportDisabled = await evl(`document.querySelector('#o-export').disabled`);
  rec('导出按钮可用', exportDisabled === false, String(exportDisabled));
  await shot('03_board_supercap');

  await evl(`(() => { const s = document.querySelector('#o-cap-tier'); s.value = '';
    s.dispatchEvent(new Event('change', {bubbles:true})); })()`);
  await wait(`S.opp && S.opp.params && S.opp.params.cap_tier === ''`, 40000);
  const reset = await evl(`(() => { const trs = [...document.querySelectorAll('#opptables tbody tr')];
    return [...new Set(trs.map((tr) => tr.lastElementChild.innerText.trim()).filter(Boolean))]; })()`);
  rec('机会表-清空筛选恢复混合档', new Set(reset).size >= 3, JSON.stringify(reset));

  await evl(`document.querySelector('#ds button[data-ds="etf"]').click()`);
  await wait(`typeof S !== 'undefined' && S.dataset === 'etf'`, 30000);
  await sleep(900);
  const etfHidden = await evl(`document.querySelector('#o-cap-tier-wrap').classList.contains('hidden')`);
  rec('ETF-隐藏市值档筛选', etfHidden === true, String(etfHidden));
  await evl(`document.querySelector('#ds button[data-ds="ashare"]').click()`);
  await sleep(500);

  rec('无控制台错误', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  fs.writeFileSync(path.join(OUTDIR, 'report.json'),
    JSON.stringify({ base: BASE, results: out, pageErrors }, null, 2));
  ws.close(); chrome.kill('SIGKILL');
  const failed = out.filter((x) => !x.ok).length;
  console.log(`\n${out.length - failed}/${out.length} PASS，截图与 report.json 在 ${OUTDIR}`);
  process.exit(failed ? 1 : 0);
}
main().catch((e) => { console.error(e); process.exit(1); });
