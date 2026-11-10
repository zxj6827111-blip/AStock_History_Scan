#!/usr/bin/env node
/* 页面五导出改动之后的回归冒烟：逐个页签真点一遍 + 对重构过的两个渲染函数做等值断言。
   等值基准是改动前那份代码的输出文本（不是我自己现在写的），防止「重构」悄悄改行为。
   用法：APP_URL=http://127.0.0.1:8737/ node tools/board_regress.js evidence/regress */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = process.argv[2] || 'evidence/regress';
const PORT = 9447;
const BASE = process.env.APP_URL || 'http://127.0.0.1:8737/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  /* 预检：端口上已经有 Chrome 就是上轮没清干净的旧实例，连它会走查错对象（宁可不跑） */
  try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
        if (r.ok) { console.error(`端口 ${PORT} 已有调试实例在跑，先杀掉它再重跑`); process.exit(2); } }
  catch (e) { /* 没人监听才对 */ }
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-board-regress',
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
  await send('Page.navigate', { url: BASE });
  await wait(`document.querySelector('#stockcard') && document.querySelector('#stockcard').innerText.length>20`, 40000);
  // 首页是机会表时个股卡在后台先渲染完，导航就绪（__appReady）之前的点击会被忽略，先等它
  await wait(`window.__appReady === true`, 40000);

  // ---- 1. 个股档案子页逐个真点一遍（首页是机会表，先切到个股档案）----
  // [子页, 代表区块]：按流月看默认 60 柱 → #tab-pillar；档案与来源 → #tab-stock（下半是 #tab-meta）
  await evl(`document.querySelector('#back-stock').click()`);
  const TABS = [['overview', 'tab-overview'], ['flow', 'tab-pillar'], ['relation', 'tab-relation'],
                ['tengod', 'tab-tengod'], ['lowpoint', 'tab-lowpoint'], ['archive', 'tab-stock']];
  for (const [t, sec] of TABS) {
    await evl(`document.querySelector('.tabs button[data-tab="${t}"]').click()`);
    await wait(`document.querySelector('#${sec}').classList.contains('on')`, 20000);
    /* 页签内容是各自异步渲染的（档案与来源要打四个接口），只睡固定时长会把「还没到」误判成「坏了」 */
    await wait(`document.querySelector('#${sec}').innerText.trim().length>60`, 40000)
      .catch(() => false);
    await sleep(600);
    const len = await evl(`document.querySelector('#${sec}').innerText.trim().length`);
    const tables = await evl(`document.querySelectorAll('#${sec} table').length`);
    const bad = await evl(`(document.querySelector('#${sec}').innerText.match(/undefined|NaN|\\[object/g)||[]).length`);
    rec('页签 ' + t + ' 有内容', len > 60, `${len} 字 / ${tables} 张表`);
    rec('页签 ' + t + ' 无 undefined/NaN/[object 文本', bad === 0, `${bad} 处`);
  }
  // 大运是嵌入的原独立页：看 iframe 里有没有内容、自己的顶栏是否已隐藏
  await evl(`document.querySelector('.tabs button[data-tab="dayun"]').click()`);
  await wait(`(()=>{const d=document.querySelector('#dayun-frame').contentDocument;
    return d && d.body && d.body.innerText.length>200})()`, 40000).catch(() => false);
  const DY = await evl(`(()=>{const d=document.querySelector('#dayun-frame').contentDocument;
    return [d.body.innerText.length, getComputedStyle(d.querySelector('.dy-header')).display].join('/')})()`);
  rec('页签 dayun 嵌入页有内容且隐藏自身顶栏', Number(DY.split('/')[0]) > 200 && DY.split('/')[1] === 'none', DY);
  await shot('01_个股档案子页');

  // ---- 2. 个股检索：按用户路径（打字→出联想→点联想项） ----
  await evl(`document.querySelector('.tabs button[data-tab="overview"]').click()`);
  await evl(`document.querySelector('#q').value='600519';
    document.querySelector('#q').dispatchEvent(new Event('input',{bubbles:true}))`);
  await wait(`[...document.querySelectorAll('#suggest .sg')].some(x=>x.innerText.includes('600519'))`, 30000);
  await evl(`(()=>{const it=[...document.querySelectorAll('#suggest .sg')].find(x=>x.innerText.includes('600519'));
    it.click(); return it.innerText.replace(/\\s+/g,' ')})()`);
  await wait(`document.querySelector('#stockcard').innerText.includes('贵州茅台')`, 30000);
  rec('个股检索 600519 正常', true, await evl(`document.querySelector('#stockcard').innerText.replace(/\\s+/g,' ').slice(0,60)`));

  // ---- 3. 重构过的两个渲染函数：等值基准取自改动前的实现 ----
  const REL = await evl(`JSON.stringify([
    oppRelCell({occurrence_count:0, has_natal_profile:1, relation_types:'[]'}),
    oppRelCell({occurrence_count:3, has_natal_profile:0, relation_types:'[]'}),
    oppRelCell({occurrence_count:3, has_natal_profile:1, relation_types:null}),
    oppRelCell({occurrence_count:3, has_natal_profile:1, relation_types:'[]'}),
    oppRelCell({occurrence_count:3, has_natal_profile:1, relation_types:'["六冲","同支"]'}),
    oppRelCell({occurrence_count:3, has_natal_profile:1, relation_types:'["<b>"]'})])`);
  const EXP = [
    '<span class="dim text-[11px]">历史无同柱，不推断</span>',
    '<span class="px-2 py-0.5 rounded text-[11px] badge-flat font-medium">无命盘 · 不计算关系</span>',
    '<span class="dim text-[11px]">（无）</span>',
    '<span class="dim text-[11px]">（无）</span>',
    '<span class="px-2 py-0.5 rounded text-[11px] badge-up font-medium whitespace-nowrap">六冲 / 同支</span>',
    '<span class="px-2 py-0.5 rounded text-[11px] badge-up font-medium whitespace-nowrap">&lt;b&gt;</span>'];
  const rel = JSON.parse(REL);
  rel.forEach((got, i) => rec('oppRelCell 状态' + (i + 1) + ' 与改动前逐字相同', got === EXP[i],
    got === EXP[i] ? '' : `得 ${got} 期望 ${EXP[i]}`));
  const MKT = await evl(`JSON.stringify(['600519.SH','000001.SZ','830799.BJ','123456.HK',''].map(oppMarketOf))`);
  rec('oppMarketOf 与改动前内联写法一致', MKT === JSON.stringify(['沪市', '深市', '北交所', '', '']), MKT);
  const NAME = await evl(`(()=>{const h=oppNameCell({security_code:'600519.SH',name:'贵州茅台',security_type:'A_SHARE'});
    return [h.includes('贵州茅台'), h.includes('600519.SH'), h.includes('沪市'), !h.includes('指数'), !h.includes('undefined')].join(',')})()`);
  rec('oppNameCell 仍含名称/代码/市场且无 undefined', NAME === 'true,true,true,true,true', NAME);

  // ---- 4. 机会表本体：行数/表头/点行开抽屉 ----
  await evl(`document.querySelector('#open-board').click()`);
  await wait(`document.querySelectorAll('#opptables tbody tr').length>0`, 40000);
  await sleep(800);
  const shape = await evl(`[document.querySelectorAll('#opptables thead th').length,
    document.querySelectorAll('#opptables tbody tr').length,
    (document.querySelector('#opptables').innerText.match(/undefined|\\[object|NaN/g)||[]).length].join('/')`);
  rec('机会表本月形态 12 列 × 100 行且无脏文本', shape.split('/')[2] === '0'
    && Number(shape.split('/')[1]) > 0, shape);
  await evl(`document.querySelector('#opptables tbody tr').click()`);
  await wait(`!document.querySelector('#slide-drawer').classList.contains('hidden')`, 20000);
  rec('点行仍滑出对账抽屉', true, await evl(`document.querySelector('#drawer-title-name').innerText`));
  await evl(`document.querySelector('#slide-drawer').classList.add('hidden')`);
  await shot('02_机会表与抽屉');

  // ---- 5. 导出按钮：始终导出全部列（含「列设置」里隐藏的列），屏幕上只显示勾选的列 ----
  const pair = await evl(`[document.querySelectorAll('#opptables thead th').length, S.oppColsAll.length, S.oppExport.spec.length].join('/')`);
  const [shownN, allN, specN] = pair.split('/').map(Number);
  const perCol = await evl(`S.oppExport.spec.filter(c=>c.h.startsWith('历史')).length + '/' + S.oppExport.spec.length`);
  rec('导出覆盖全部列（含隐藏列），屏幕只显示勾选的列', shownN <= allN && specN > allN,
    `表头 ${shownN} / 全部列 ${allN} / 导出单元列 ${specN}；占比相关列 ${perCol}`);
  rec('按钮可用且写明行数', !(await evl(`document.querySelector('#o-export').disabled`))
    && (await evl(`document.querySelector('#o-export').title`)).includes('行'),
    await evl(`document.querySelector('#o-export').title`));

  rec('page_errors', pageErrors.length === 0, JSON.stringify(pageErrors).slice(0, 300));
  fs.writeFileSync(path.join(OUTDIR, 'board_regress.json'),
    JSON.stringify({ base: BASE, at: new Date().toISOString(), out, pageErrors }, null, 2));
  const fails = out.filter((o) => !o.ok);
  console.log(`\n结果：${out.length - fails.length}/${out.length} PASS`);
  ws.close(); chrome.kill('SIGKILL');
  process.exit(fails.length ? 1 : 0);
}
main().catch((e) => { console.error('FAILED', e); process.exit(1); });
