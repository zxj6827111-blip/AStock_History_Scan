#!/usr/bin/env node
/* 高岛易断 + 双好/双差 渲染走查：100%段 / 机会表 / /gua 三个面。
   断言每张卦卡都有「高岛易断」行与「共识」徽章；并点一只已知双好/双差的票截图实证。
   用法: node tools/gua_gaodao_walk.js [APP_URL] [OUTDIR]  （token 由 /tmp/gua_walk_token.txt 或 GUA_TOKEN） */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.argv[2] || 'http://127.0.0.1:8792';
const OUTDIR = process.argv[3] || path.join(__dirname, '../evidence/gua_gaodao_walk');
const PORT = 9455;
const TOKEN = process.env.GUA_TOKEN || fs.readFileSync('/tmp/gua_walk_token.txt', 'utf8').trim();
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(name, cond, extra) { if (cond) console.log(`  PASS: ${name}${extra ? ' | ' + extra : ''}`); else { failures++; console.log(`  FAIL: ${name}${extra ? ' | ' + extra : ''}`); } }

async function main() {
  if (!TOKEN) throw new Error('缺 token');
  fs.mkdirSync(OUTDIR, { recursive: true });
  const chrome = spawn(CHROME, ['--headless=new','--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`,'--user-data-dir=/tmp/cdp-profile-gua-gaodao','--window-size=1500,1400',
    '--hide-scrollbars','--force-device-scale-factor=1','--disable-gpu','about:blank'], { stdio: 'ignore' });
  let up = false; for (let i = 0; i < 80; i++) { try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) { up = true; break; } } catch (e) {} await sleep(250); }
  if (!up) { chrome.kill('SIGKILL'); throw new Error('Chrome 启动超时'); }
  try {
    const tgt = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })).json();
    const ws = new WebSocket(tgt.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let id = 0; const pend = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
    const send = (m, p) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method: m, params: p || {} })); });
    const evl = async (e) => { const r = await send('Runtime.evaluate', { expression: e, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; };
    const wait = async (e, ms = 30000) => { const t0 = Date.now(); for (;;) { let ok = false; try { ok = await evl(e); } catch (x) { ok = false; } if (ok) return true; if (Date.now() - t0 > ms) throw new Error('超时: ' + e); await sleep(150); } };
    const shot = async (n) => { const { data } = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(OUTDIR, n + '.png'), Buffer.from(data, 'base64')); console.log(`  [截图] ${n}.png`); };
    const click = async (s) => { await evl(`(()=>{const el=document.querySelector(${JSON.stringify(s)}); if(!el) throw new Error('无 '+${JSON.stringify(s)}); el.click(); return true;})()`); };

    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Network.setUserAgentOverride', { userAgent: UA });
    await send('Network.setCookie', { name: 'astock_session', value: TOKEN, domain: '127.0.0.1', path: '/' });

    const readCards = () => evl(`(() => {
      const box = document.querySelector('#drawer-gua-box');
      return [...box.querySelectorAll('.gua-card')].map((c) => ({
        name: (c.querySelector('.gua-win-name')||{}).textContent||'',
        gua: (c.querySelector('.gua-name')||{}).textContent||'',
        gaodao: (c.querySelector('.gua-gaodao')||{}).textContent||'',
        gdSide: (c.querySelector('.gua-gaodao .gua-side')||{}).textContent||'',
        cons: (c.querySelector('.gua-cons')||{}).textContent||'',
        consCls: (c.querySelector('.gua-cons')||{}).className||'',
      }));
    })()`);

    // A. 100%段抽屉：点一只已知双好/双差的票
    console.log('A. 100%段抽屉 → 点双好/双差票');
    await send('Page.navigate', { url: BASE + '/?seg=1' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length>0`);
    await sleep(600);
    const board = await evl(`(async()=>{const r=await fetch('/api/seg/board?dir=up&seg_n=5&cap=60',{headers:{Accept:'application/json'}});return await r.json();})()`);
    const rows = board.rows || [];
    const demo = ['000002.SZ','600326.SH','000928.SZ','600057.SH','600761.SH']
      .map((c) => rows.find((r) => r.security_code === c)).find(Boolean);
    const demoCode = demo ? demo.security_code : (rows[0] || {}).security_code;
    const idx = await evl(`[...document.querySelectorAll('#opptables tbody tr')].findIndex(tr=>tr.getAttribute('data-code')===${JSON.stringify(demoCode)})`);
    await evl(`document.querySelectorAll('#opptables tbody tr')[${idx < 0 ? 0 : idx}].click()`);
    await wait(`document.querySelectorAll('#drawer-gua-box .gua-card').length>0`);
    await sleep(600);
    const segCards = await readCards();
    check('段抽屉每卡都有高岛易断行', segCards.every((c) => c.gaodao.includes('高岛易断')), `${segCards.length} 卡`);
    check('段抽屉每卡都有共识徽章', segCards.every((c) => c.cons), segCards.map((c) => c.cons || '·').join('/'));
    const consDist = segCards.map((c) => c.cons || '一般');
    console.log('   共识分布:', consDist.join(' / '));
    const hasDD = segCards.some((c) => /双好|双差/.test(c.cons));
    check('演示票出现双好/双差', hasDD, consDist.filter((x) => /双好|双差/.test(x)).join(',') || '本票无双好/双差');
    await shot('A_seg_gaodao_consensus');
    // 关抽屉
    await evl(`document.querySelector('#drawer-close-btn')?.click()`);
    await wait(`document.querySelector('#slide-drawer').classList.contains('hidden')`);

    // B. 机会表抽屉（月层）
    console.log('B. 机会表抽屉 → 月层卦卡');
    await send('Page.navigate', { url: BASE + '/' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length>0`);
    await sleep(600);
    await click('#opptables tbody tr');
    await wait(`document.querySelectorAll('#drawer-gua-box .gua-card').length>0`);
    await sleep(500);
    const oppCards = await readCards();
    check('机会表卦卡有高岛易断行', oppCards[0].gaodao.includes('高岛易断'), oppCards[0].gaodao.slice(0, 30));
    check('机会表卦卡有共识徽章', !!oppCards[0].cons, oppCards[0].cons || '一般');
    await shot('B_opp_gaodao_consensus');

    // C. /gua 页
    console.log('C. /gua 页 → 卦卡');
    await send('Page.navigate', { url: BASE + '/gua' });
    await wait(`document.querySelector('.gua-cards .gua-card')`);
    await sleep(800);
    const guaCards = await evl(`(() => [...document.querySelectorAll('.gua-cards .gua-card')].map((c)=>({
      gaodao: (c.querySelector('.gua-gaodao')||{}).textContent||'',
      cons: (c.querySelector('.gua-cons')||{}).textContent||'',
    })))()`);
    check('/gua 每卡都有高岛易断行', guaCards.every((c) => c.gaodao.includes('高岛易断')), `${guaCards.length} 卡`);
    check('/gua 每卡都有共识徽章', guaCards.every((c) => c.cons), guaCards.map((c) => c.cons || '一般').join('/'));
    await shot('C_gua_page_gaodao');

    console.log(failures ? `\n共 ${failures} 项 FAIL` : '\n全部 PASS');
    process.exitCode = failures ? 1 : 0;
  } finally { chrome.kill('SIGKILL'); }
}
main().catch((e) => { console.error('走查异常:', e.message); process.exit(2); });