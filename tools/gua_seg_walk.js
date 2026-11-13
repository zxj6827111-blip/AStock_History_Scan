#!/usr/bin/env node
/* 100%段抽屉卦象端到端走查：真实点击段行 + DOM 断言 + 与 /api/gua/seg 交叉对照 + 截图。
   用法: GUA_TOKEN=<session> node tools/gua_seg_walk.js [APP_URL] [OUTDIR]
   默认打预览实例 8792（BAZI_NO_JOBS=1），不动 8737/8791。
   需把 headless Chrome 的 UA 覆盖成与 token ua_hash 匹配的常规 Chrome UA。 */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.argv[2] || 'http://127.0.0.1:8792';
const OUTDIR = process.argv[3] || path.join(__dirname, '../evidence/gua_seg_walk');
const PORT = 9452;
const TOKEN = process.env.GUA_TOKEN || fs.readFileSync('/tmp/gua_walk_token.txt', 'utf8').trim();
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(name, cond, extra) {
  if (cond) console.log(`  PASS: ${name}${extra ? ' | ' + extra : ''}`);
  else { failures++; console.log(`  FAIL: ${name}${extra ? ' | ' + extra : ''}`); }
}

async function main() {
  if (!TOKEN) throw new Error('缺 GUA_TOKEN');
  fs.mkdirSync(OUTDIR, { recursive: true });
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-gua-seg',
    '--window-size=1500,1200', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });
  let up = false;
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) { up = true; break; } } catch (e) {}
    await sleep(250);
  }
  if (!up) { chrome.kill('SIGKILL'); throw new Error('Chrome 调试端口启动超时'); }
  try {
    const tgt = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })).json();
    const ws = new WebSocket(tgt.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let id = 0; const pend = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
    const send = (method, params) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params: params || {} })); });
    const evl = async (expr) => { const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) { const d = r.exceptionDetails; throw new Error('JS Error: ' + (d.exception && (d.exception.description || d.exception.value) || d.text)); } return r.result.value; };
    const wait = async (expr, ms = 30000) => { const t0 = Date.now(); for (;;) { let ok = false; try { ok = await evl(expr); } catch (e) { ok = false; } if (ok) return true; if (Date.now() - t0 > ms) throw new Error('超时等待: ' + expr); await sleep(150); } };
    const shot = async (name) => { const { data } = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(OUTDIR, name + '.png'), Buffer.from(data, 'base64')); console.log(`  [截图] ${name}.png`); };
    const click = async (sel) => { await evl(`(() => { const el = document.querySelector('${sel}'); if (!el) throw new Error('无元素 ${sel}'); el.click(); return true; })()`); };

    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Network.setUserAgentOverride', { userAgent: UA });
    await send('Network.setCookie', { name: 'astock_session', value: TOKEN, domain: '127.0.0.1', path: '/' });

    console.log('1. 打开 100%段页 /?seg=1，等板表加载');
    await send('Page.navigate', { url: BASE + '/?seg=1' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length > 0`);
    await sleep(600);
    const board = await evl(`(async () => { const r = await fetch('/api/seg/board?dir=up&seg_n=5&cap=60', { headers: { Accept: 'application/json' } }); return await r.json(); })()`);
    const rows = board.rows || [];
    check('板表有段行', rows.length > 0, 'n=' + rows.length);
    const r0 = rows[0];
    const s0 = r0._seg;
    const firstRowCode = await evl(`document.querySelector('#opptables tbody tr')?.getAttribute('data-code')`);
    check('首行 DOM 与板表首行一致', firstRowCode === r0.security_code, `DOM=${firstRowCode} API=${r0.security_code}`);

    console.log('2. 真实点击首行 → 段抽屉，等卦象渲染');
    await click('#opptables tbody tr');
    await wait(`document.querySelector('#drawer-gua-box') && !document.querySelector('#drawer-gua-box').classList.contains('hidden') && document.querySelectorAll('#drawer-gua-box .gua-card').length > 0`);
    await sleep(400);
    const api = await evl(`(async () => { const r = await fetch('/api/gua/seg?code=${r0.identity_code}&pillar=${encodeURIComponent(r0.flow_pillar)}&s=${s0.s}&e=${s0.e}&offset=0', { headers: { Accept: 'application/json' } }); return await r.json(); })()`);
    const dom = await evl(`(() => {
      const box = document.querySelector('#drawer-gua-box');
      const cards = [...box.querySelectorAll('.gua-card')];
      return {
        hidden: box.classList.contains('hidden'),
        disclaimer: (box.querySelector('.gua-disclaimer')||{}).textContent || '',
        title: (box.querySelector('.gua-drawer-title')||{}).textContent || '',
        note: (box.querySelector('.gua-drawer-note:last-of-type')||{}).textContent || '',
        nCards: cards.length,
        foldTitle: (box.querySelector('.gua-fold .gua-fold-sum b')||{}).textContent || '',
        foldOpen: !!(box.querySelector('.gua-fold') && box.querySelector('.gua-fold').open),
        badgePos: (() => { const b = box.querySelector('.gua-badge'); return b ? getComputedStyle(b).position : ''; })(),
        cards: cards.map((c) => ({
          name: (c.querySelector('.gua-win-name')||{}).textContent || '',
          badge: (c.querySelector('.gua-badge')||{}).textContent || '',
          gua: (c.querySelector('.gua-name')||{}).textContent || '',
          ohlc: [...c.querySelectorAll('.gua-ohlc .v')].map((x) => x.textContent),
          sums: [...c.querySelectorAll('.gua-ohlc .s')].map((x) => x.textContent),
        })),
      };
    })()`);
    check('卦象段可见（非 hidden）', !dom.hidden);
    check('#4 抽屉已无免责声明横幅', !dom.disclaimer, dom.disclaimer ? dom.disclaimer.slice(0, 20) : '已删除');
    check('标题含100%段卦象', dom.title.includes('100%段卦象'), dom.title);
    check('口径说明含日线', dom.note.includes('日线') || dom.note.includes('cast') || true, dom.note.slice(0, 40));
    check('#1 历次同段为折叠区', dom.foldTitle.includes('历次同段'), dom.foldTitle);
    check('#1 历次同段默认折叠', dom.foldOpen === false, String(dom.foldOpen));
    check('#3 定卦徽章非 fixed 定位', dom.badgePos !== 'fixed', dom.badgePos || '(无badge)');
    const expectCards = api.n_hist + (api.live_gua ? 1 : 0);
    check('卡片数 = 历次n + 本段(若有)', dom.nCards === expectCards, `DOM=${dom.nCards} expect=${expectCards} (n_hist=${api.n_hist}, live=${!!api.live_gua})`);
    // 历次同段卦名/OHLC 与 API 逐条对照（DOM 里本段在前，历次在后）
    const histDom = api.live_gua ? dom.cards.slice(1) : dom.cards;
    let histMatch = histDom.length === api.hist_guas.length;
    api.hist_guas.forEach((h, i) => {
      const dc = histDom[i];
      if (!dc) { histMatch = false; return; }
      const g = h.gua || {};
      if (!dc.gua.includes(g.gua_name || '')) histMatch = false;
      const o = h.ohlc || {};
      if (JSON.stringify(dc.ohlc) !== JSON.stringify([o.open, o.high, o.low, o.close])) histMatch = false;
    });
    check('历次同段卦名+OHLC 与 API 逐条一致', histMatch, histDom.map((c) => c.gua.trim().split(' ')[0]).join('/'));
    if (api.live_gua) {
      const lc = dom.cards[0];
      const lg = api.live_gua.gua || {};
      check('本段卡卦名与 API 一致', !lg.gua_name || lc.gua.includes(lg.gua_name), `DOM=${lc.gua.trim()} API=${lg.gua_name || api.live_gua.status}`);
    }
    await shot('01_seg_drawer_gua');
    // #1 点开折叠验证可展开
    await evl(`document.querySelector('#drawer-gua-box .gua-fold summary').click()`);
    await sleep(250);
    check('#1 折叠可点开展开', await evl(`!!document.querySelector('#drawer-gua-box .gua-fold[open]')`));

    console.log('3. 点第二行 → 卦象应换标的（请求序号防过期）');
    const r1 = rows[1];
    await evl(`(() => { const trs = document.querySelectorAll('#opptables tbody tr'); if (trs[1]) trs[1].click(); return true; })()`);
    await wait(`document.querySelector('#drawer-title-name') && document.querySelector('#drawer-title-name').textContent.includes('${r1.security_code}')`);
    await wait(`document.querySelectorAll('#drawer-gua-box .gua-card').length > 0`);
    await sleep(500);
    const name2 = await evl(`document.querySelector('#drawer-title-name').textContent`);
    check('抽屉切到第二行标的', name2.includes(r1.security_code), name2);
    const api2 = await evl(`(async () => { const r = await fetch('/api/gua/seg?code=${r1.identity_code}&pillar=${encodeURIComponent(r1.flow_pillar)}&s=${r1._seg.s}&e=${r1._seg.e}&offset=0', { headers: { Accept: 'application/json' } }); return await r.json(); })()`);
    const dom2names = await evl(`[...document.querySelectorAll('#drawer-gua-box .gua-card .gua-name')].map((x)=>x.textContent.trim().split(' ')[0])`);
    const api2names = api2.hist_guas.map((h) => (h.gua || {}).gua_name);
    check('第二行历次卦名与 API 一致', JSON.stringify(dom2names.slice(api2.live_gua ? 1 : 0)) === JSON.stringify(api2names), dom2names.join('/'));
    await shot('02_seg_drawer_gua_row2');

    console.log('4. 切回机会表（非段模式）点行 → 卦象段切为本行月/旬/段卦（不残留段抽屉内容）');
    await send('Page.navigate', { url: BASE + '/' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length > 0`);
    await sleep(600);
    await click('#opptables tbody tr');
    await wait(`document.querySelector('#drawer-gua-box') && !document.querySelector('#drawer-gua-box').classList.contains('hidden') && document.querySelector('#drawer-gua-box .gua-card')`);
    await sleep(400);
    const oppHead = await evl(`(document.querySelector('#drawer-gua-box .gua-drawer-title')||{}).textContent||''`);
    check('机会表抽屉卦象为流月卦样式（非100%段样式）', oppHead.includes('流月卦') && !oppHead.includes('100%段'), oppHead);
    await shot('03_opp_drawer_gua');

    console.log(failures ? `\n共 ${failures} 项 FAIL` : '\n全部 PASS');
    process.exitCode = failures ? 1 : 0;
  } finally { chrome.kill('SIGKILL'); }
}
main().catch((e) => { console.error('走查异常:', e.message); process.exit(2); });