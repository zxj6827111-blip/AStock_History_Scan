#!/usr/bin/env node
/* 十神升级开发版 UI 走查：真实无头 Chrome + CDP，按用户路径点击并截图。
   仅使用 Node 内置模块（fetch + global WebSocket）。
   用法：APP_URL=http://127.0.0.1:8848/ OUTDIR=evidence/screenshots node tools/ui_walk_tengod.js */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = process.argv[2] || 'evidence/screenshots';
const PORT = 9444;
const BASE = process.env.APP_URL || 'http://127.0.0.1:8848/';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  const prof = path.join('/tmp', 'cdp-profile-tengod-dev');
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
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
      if (Date.now() - t0 > (ms || 12000)) throw new Error('timeout waiting for ' + expr);
      await sleep(150);
    }
  };
  const shot = async (name) => {
    const { data } = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(OUTDIR, name + '.png'), Buffer.from(data, 'base64'));
    console.log('shot', name);
  };
  const scrollTo = async (sel) => {
    await evl(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); if(e) e.scrollIntoView({block:'start'}); return !!e})()`);
    await sleep(400);
  };

  await send('Page.enable'); await send('Runtime.enable'); await send('Log.enable');
  await send('Page.navigate', { url: BASE });
  await wait(`document.querySelector('#stockcard') && document.querySelector('#stockcard').innerText.length>30`, 20000);
  // 首页是机会表时个股卡在后台先渲染完，导航就绪（__appReady）之前的点击会被忽略，先等它
  await wait(`window.__appReady === true`, 40000);
  await sleep(500);
  // 改版后首页是流月机会表；本走查看的是个股档案
  await evl(`document.querySelector('#back-stock').click()`);
  await sleep(500);

  const report = [];
  const rec = (step, value) => { report.push({ step, value }); console.log(step, ':', value); };

  // ---------- ETF 510050 ----------
  await evl(`document.querySelector('#ds button[data-ds=etf]').click()`);
  await wait(`document.querySelector('#stockcard').innerText.includes('510050')`, 20000);
  await sleep(400);
  rec('dataset_etf', await evl(`document.body.dataset.dataset || 'etf'`));
  rec('stock_card', await evl(`document.querySelector('#stockcard').innerText.replace(/\\s+/g,' ').slice(0,300)`));
  rec('now_card', await evl(`(document.querySelector('#tengodnow')||{innerText:''}).innerText.replace(/\\s+/g,' ')`));
  await shot('01_ETF_510050_档案与当前下一流月');

  // ---------- tab 3: 60 pillars ----------
  await evl(`activateTab('pillar')`);
  await wait(`document.querySelectorAll('#pillartable tbody tr').length===60`, 20000);
  rec('pillar_rows', await evl(`document.querySelectorAll('#pillartable tbody tr').length`));
  rec('pillar_headers', await evl(`[...document.querySelectorAll('#pillartable thead th')].map(t=>t.innerText.trim()).join('|')`));
  rec('pillar_jihai_row', await evl(`(()=>{const rows=[...document.querySelectorAll('#pillartable tbody tr')];
    const r=rows.find(x=>x.children[0].innerText.trim()==='己亥');
    return r ? [...r.children].map(c=>c.innerText.replace(/\\s+/g,' ').trim()).join(' | ') : 'MISSING';})()`));
  await shot('02_60柱主表_含干十神与藏干');

  // 十神筛选（客户端，默认不限）
  await evl(`(()=>{const s=document.querySelector('#p-tg'); s.value='劫财';
    s.dispatchEvent(new Event('change')); return true})()`);
  await wait(`document.querySelectorAll('#pillartable tbody tr').length===6`, 15000);
  rec('pillar_tg_filter_rows', await evl(`document.querySelectorAll('#pillartable tbody tr').length`));
  rec('pillar_tg_filter_note', await evl(`document.querySelector('#p-count').innerText`));
  await shot('02b_60柱_按流月干十神筛选');
  await evl(`(()=>{const s=document.querySelector('#p-tg'); s.value='';
    s.dispatchEvent(new Event('change')); return true})()`);
  await wait(`document.querySelectorAll('#pillartable tbody tr').length===60`, 15000);
  await scrollTo('#pillardetail');

  await evl(`(()=>{const rows=[...document.querySelectorAll('#pillartable tbody tr')];
    const r=rows.find(x=>x.children[0].innerText.trim()==='己亥'); if(r) r.click(); return !!r;})()`);
  await wait(`document.querySelector('#pillardetail h3') && document.querySelector('#pillardetail h3').innerText.includes('己亥')`, 15000);
  rec('pillar_detail_title', await evl(`document.querySelector('#pillardetail h3').innerText`));
  rec('pillar_detail_headers', await evl(`[...document.querySelectorAll('#pillardetail thead th')].map(t=>t.innerText.trim()).join('|')`));
  rec('pillar_detail_rows', await evl(`(()=>{const hs=[...document.querySelectorAll('#pillardetail thead th')].map(t=>t.innerText.trim());
    return [...document.querySelectorAll('#pillardetail tbody tr')].map(r=>{
      const c=[...r.children].map(x=>x.innerText.replace(/\\s+/g,' ').trim());
      const o={}; hs.forEach((h,i)=>o[h]=c[i]);
      return c[0]+' 干十神='+o['流月干十神']+' 藏干='+o['流月支藏干十神']+' 方向='+o['涨跌']+' 涨跌='+o['涨跌幅']+' 超额='+o['超额'];
    }).join(' || ');})()`));
  await shot('03_60柱明细_己亥四次_十神与收益字段');

  // ---------- tab 5: ten-god stats ----------
  await evl(`document.querySelector('.tabs button[data-tab=tengod]').click()`);
  await wait(`document.querySelectorAll('#tengodtable tbody tr').length===10`, 20000);
  rec('tengod_summary', await evl(`document.querySelector('#tengodsummary').innerText.replace(/\\s+/g,' ').slice(0,400)`));
  rec('tengod_headers', await evl(`[...document.querySelectorAll('#tengodtable thead th')].map(t=>t.innerText.trim()).join('|')`));
  rec('tengod_rows', await evl(`[...document.querySelectorAll('#tengodtable tbody tr')].map(r=>
    [...r.children].map(c=>c.innerText.replace(/\\s+/g,' ').trim()).join(' | ')).join('\\n')`));
  rec('tengod_conservation', await evl(`(()=>{const rows=[...document.querySelectorAll('#tengodtable tbody tr')];
    return rows.reduce((s,r)=>s+Number(r.children[1].innerText.replace(/[^0-9]/g,'')),0);})()`));
  await shot('04_十神历史涨跌_10类总表');
  await scrollTo('#tengoddetail');

  await evl(`(()=>{const rows=[...document.querySelectorAll('#tengodtable tbody tr')];
    const r=rows.find(x=>x.children[0].innerText.trim()==='劫财'); if(r) r.click(); return !!r;})()`);
  await wait(`document.querySelector('#tengoddetail h3') && document.querySelector('#tengoddetail h3').innerText.includes('劫财')`, 15000);
  rec('tengod_detail_title', await evl(`document.querySelector('#tengoddetail h3').innerText`));
  rec('tengod_detail_count', await evl(`document.querySelectorAll('#tengoddetail tbody tr').length`));
  rec('tengod_detail_first', await evl(`(()=>{const r=document.querySelector('#tengoddetail tbody tr');
    return r ? [...r.children].map(c=>c.innerText.replace(/\\s+/g,' ').trim()).join(' | ') : 'MISSING';})()`));
  rec('tengod_jihai_rows', await evl(`[...document.querySelectorAll('#tengoddetail tbody tr')].filter(r=>r.innerText.includes('己亥')).map(r=>
    [...r.children].map(c=>c.innerText.replace(/\\s+/g,' ').trim()).join(' | ')).join(' || ')`));
  await shot('05_十神涨跌_劫财逐月明细');

  // 月详情弹窗（含藏干明细）
  await evl(`document.querySelector('#tengoddetail tbody tr').click()`);
  await wait(`!document.querySelector('#modal').classList.contains('hidden')`, 10000);
  rec('month_modal_ten_god', await evl(`(()=>{const rows=[...document.querySelectorAll('#mbody .kv-i')];
    return rows.filter(r=>r.innerText.includes('十神')||r.innerText.includes('藏干')).map(r=>r.innerText.replace(/\\s+/g,' ').trim()).join(' || ');})()`));
  await shot('06_历史月详情_藏干明细');
  await evl(`document.querySelector('#mclose').click()`);

  // ---------- tab 2: branch detail ----------
  await evl(`activateTab('branch')`);
  await wait(`document.querySelectorAll('#branchtable tbody tr').length===12`, 20000);
  await evl(`(()=>{const rows=[...document.querySelectorAll('#branchtable tbody tr')];
    const r=rows.find(x=>x.children[0].innerText.trim().startsWith('亥')); if(r) r.click(); return !!r;})()`);
  await wait(`document.querySelector('#branchdetail h3') && document.querySelector('#branchdetail h3').innerText.includes('亥')`, 15000);
  rec('branch_headers', await evl(`[...document.querySelectorAll('#branchdetail thead th')].map(t=>t.innerText.trim()).join('|')`));
  rec('branch_jihai', await evl(`(()=>{const rows=[...document.querySelectorAll('#branchdetail tbody tr')];
    return rows.filter(r=>r.innerText.includes('己亥')).map(r=>[...r.children].slice(0,6).map(c=>c.innerText.replace(/\\s+/g,' ').trim()).join(' | ')).join(' || ');})()`));
  await scrollTo('#branchdetail');
  await shot('07_12支明细_亥支含十神');

  // ---------- tab 4: relation months ----------
  await evl(`document.querySelector('.tabs button[data-tab=relation]').click()`);
  await wait(`document.querySelectorAll('#relationtable tbody tr').length>0`, 20000);
  await evl(`(()=>{const rows=[...document.querySelectorAll('#relationtable tbody tr')];
    const r=rows.find(x=>x.children[0].innerText.includes('六合')) || rows[0]; if(r) r.click(); return !!r;})()`);
  await wait(`document.querySelectorAll('#relationpillars tbody tr').length>0`, 15000);
  await evl(`document.querySelector('#relationpillars tbody tr').click()`);
  await wait(`document.querySelector('#relationmonths h3')`, 15000);
  rec('relation_headers', await evl(`[...document.querySelectorAll('#relationmonths thead th')].map(t=>t.innerText.trim()).join('|')`));
  rec('relation_first_row', await evl(`(()=>{const r=document.querySelector('#relationmonths tbody tr');
    return r ? [...r.children].slice(0,8).map(c=>c.innerText.replace(/\\s+/g,' ').trim()).join(' | ') : 'MISSING';})()`));
  await scrollTo('#relationmonths');
  await shot('08_冲合关系月明细_含十神');
  await evl(`document.querySelector('#ds button[data-ds=ashare]').click()`);
  await wait(`document.querySelector('#stockcard').innerText.includes('000001')`, 20000);
  await evl(`document.querySelector('.tabs button[data-tab=tengod]').click()`);
  await wait(`document.querySelectorAll('#tengodtable tbody tr').length===10`, 20000);
  rec('ashare_summary', await evl(`document.querySelector('#tengodsummary').innerText.replace(/\\s+/g,' ').slice(0,300)`));
  rec('ashare_sum', await evl(`[...document.querySelectorAll('#tengodtable tbody tr')]
    .reduce((s,r)=>s+Number(r.children[1].innerText.replace(/[^0-9]/g,'')),0)`));
  await shot('09_A股_十神统计');

  // ---------- A 股：缺档案身份 ----------
  await evl(`(async()=>{await loadStock('000003.SZ'); return true})()`);
  await wait(`document.querySelector('#stockcard').innerText.includes('000003')`, 20000);
  await wait(`document.querySelector('#tengodsummary').innerText.includes('暂无十神数据')`, 20000);
  await sleep(400);
  rec('noprofile_now', await evl(`(document.querySelector('#tengodnow')||{innerText:''}).innerText.replace(/\\s+/g,' ')`));
  rec('noprofile_stats', await evl(`document.querySelector('#tengodsummary').innerText.replace(/\\s+/g,' ').slice(0,300)`));
  await shot('10_A股_缺档案身份_不猜十神');

  // ---------- 18.52% vs 4.16% 字段归属 ----------
  await evl(`document.querySelector('#ds button[data-ds=etf]').click()`);
  await wait(`document.querySelector('#stockcard').innerText.includes('510050')`, 20000);
  await evl(`activateTab('pillar')`);
  await wait(`document.querySelectorAll('#pillartable tbody tr').length===60`, 20000);
  await evl(`(()=>{const rows=[...document.querySelectorAll('#pillartable tbody tr')];
    const r=rows.find(x=>x.children[0].innerText.trim()==='己亥'); if(r) r.click(); return !!r;})()`);
  await wait(`document.querySelector('#pillardetail h3') && document.querySelector('#pillardetail h3').innerText.includes('己亥')`, 15000);
  rec('field_18_52', await evl(`(()=>{const hs=[...document.querySelectorAll('#pillardetail thead th')].map(t=>t.innerText.trim());
    const r=[...document.querySelectorAll('#pillardetail tbody tr')].find(x=>x.innerText.includes('2006-M10'));
    const cs=[...r.children].map(c=>c.innerText.replace(/\\s+/g,' ').trim());
    return hs.map((h,i)=>h+'='+cs[i]).join(' | ');})()`));
  await scrollTo('#pillardetail');
  await shot('11_2006M10_收益与超额字段对照');

  // ---------- 浅色主题回归 ----------
  await evl(`document.querySelector('.tabs button[data-tab=tengod]').click()`);
  await wait(`document.querySelectorAll('#tengodtable tbody tr').length===10`, 20000);
  await evl(`document.querySelector('#theme-toggle').click()`);
  await sleep(700);
  await evl(`window.scrollTo(0,0)`);
  await shot('12_浅色主题_十神统计');
  await evl(`document.querySelector('#theme-toggle').click()`);
  await sleep(500);

  rec('page_errors', JSON.stringify(pageErrors));
  fs.writeFileSync(path.join(OUTDIR, 'ui_walk_report.json'),
    JSON.stringify({ base: BASE, at: new Date().toISOString(), report, pageErrors }, null, 2));

  ws.close();
  chrome.kill('SIGKILL');
  console.log('DONE', OUTDIR);
}

main().catch((e) => { console.error('FAILED', e); process.exit(1); });
