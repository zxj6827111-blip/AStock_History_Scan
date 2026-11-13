#!/usr/bin/env node
/* 流月机会表抽屉卦象走查：月/旬/段三层，各真实点行 → 读 #drawer-gua-box →
   与 /api/gua/cast 按窗口起止日期匹配交叉对照 → 截图。
   用法: node tools/gua_opp_walk.js [APP_URL] [OUTDIR]  （token 由 /tmp/gua_walk_token.txt 或 GUA_TOKEN）
   默认打预览 8792（BAZI_NO_JOBS=1），不动 8737/8791。 */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.argv[2] || 'http://127.0.0.1:8792';
const OUTDIR = process.argv[3] || path.join(__dirname, '../evidence/gua_opp_walk');
const PORT = 9454;
const TOKEN = process.env.GUA_TOKEN || fs.readFileSync('/tmp/gua_walk_token.txt', 'utf8').trim();
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(name, cond, extra) { if (cond) console.log(`  PASS: ${name}${extra ? ' | ' + extra : ''}`); else { failures++; console.log(`  FAIL: ${name}${extra ? ' | ' + extra : ''}`); } }

async function main() {
  if (!TOKEN) throw new Error('缺 token');
  fs.mkdirSync(OUTDIR, { recursive: true });
  const chrome = spawn(CHROME, ['--headless=new','--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`,'--user-data-dir=/tmp/cdp-profile-gua-opp','--window-size=1500,1300',
    '--hide-scrollbars','--force-device-scale-factor=1','--disable-gpu','about:blank'], { stdio: 'ignore' });
  let up = false; for (let i = 0; i < 80; i++) { try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) { up = true; break; } } catch (e) {} await sleep(250); }
  if (!up) { chrome.kill('SIGKILL'); throw new Error('Chrome 启动超时'); }
  try {
    const tgt = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })).json();
    const ws = new WebSocket(tgt.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    let id = 0; const pend = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); } };
    const send = (method, params) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params: params || {} })); });
    const evl = async (expr) => { const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true }); if (r.exceptionDetails) { const d = r.exceptionDetails; throw new Error('JS Error: ' + (d.exception && (d.exception.description || d.exception.value) || d.text)); } return r.result.value; };
    const wait = async (expr, ms = 30000) => { const t0 = Date.now(); for (;;) { let ok = false; try { ok = await evl(expr); } catch (e) { ok = false; } if (ok) return true; if (Date.now() - t0 > ms) throw new Error('超时: ' + expr); await sleep(150); } };
    const shot = async (name) => { const { data } = await send('Page.captureScreenshot', { format: 'png' }); fs.writeFileSync(path.join(OUTDIR, name + '.png'), Buffer.from(data, 'base64')); console.log(`  [截图] ${name}.png`); };
    const click = async (sel) => { await evl(`(()=>{const el=document.querySelector(${JSON.stringify(sel)}); if(!el) throw new Error('无元素 '+${JSON.stringify(sel)}); el.click(); return true;})()`); };

    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Network.setUserAgentOverride', { userAgent: UA });
    await send('Network.setCookie', { name: 'astock_session', value: TOKEN, domain: '127.0.0.1', path: '/' });

    await send('Page.navigate', { url: BASE + '/' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length > 0`);
    await sleep(600);

    // 打开首行抽屉并校验卦象，返回 {code}
    async function openAndCheck(layer) {
      await click('#opptables tbody tr');
      await wait(`document.querySelector('#drawer-gua-box') && !document.querySelector('#drawer-gua-box').classList.contains('hidden') && document.querySelector('#drawer-gua-box .gua-card, #drawer-gua-box .gua-empty, #drawer-gua-box .text-slate-500')`);
      await sleep(500);
      const dom = await evl(`(() => {
        const box = document.querySelector('#drawer-gua-box');
        const c = box.querySelector('.gua-card');
        const title = document.querySelector('#drawer-title-name').textContent.trim();
        return {
          visible: !box.classList.contains('hidden'),
          title,
          header: (box.querySelector('.gua-drawer-title')||{}).textContent || '',
          hasCard: !!c,
          gua: c ? (c.querySelector('.gua-name')||{}).textContent.trim() : '',
          range: c ? (c.querySelector('.gua-win-range')||{}).textContent.trim() : '',
          ohlc: c ? [...c.querySelectorAll('.gua-ohlc .v')].map(x=>x.textContent) : [],
          disclaimer: (box.querySelector('.gua-disclaimer')||{}).textContent || '',
          foldTitle: (box.querySelector('.gua-fold .gua-fold-sum b')||{}).textContent || '',
          foldOpen: !!(box.querySelector('.gua-fold') && box.querySelector('.gua-fold').open),
          badgePos: (() => { const b = box.querySelector('.gua-badge'); return b ? getComputedStyle(b).position : ''; })(),
        };
      })()`);
      const code = (dom.title || '').split(' ')[0];
      let expect = null;
      if (dom.hasCard && code) {
        expect = await evl(`(async()=>{const r=await fetch('/api/gua/cast?code=${encodeURIComponent(code)}&layer=${layer}&offset=0',{headers:{Accept:'application/json'}}); if(!r.ok) return {err:r.status}; return await r.json();})()`);
      }
      return { dom, code, expect };
    }
    function crossCheck(layer, r) {
      const { dom, expect } = r;
      check('卦象段可见', dom.visible);
      check('#4 抽屉已无免责声明横幅', !dom.disclaimer, dom.disclaimer ? dom.disclaimer.slice(0, 20) : '已删除');
      check('#3 定卦徽章非 fixed 定位', dom.badgePos !== 'fixed', dom.badgePos || '(无badge)');
      if (layer === 'month') check('月层头注「锚定上一流月」', dom.header.includes('锚定上一流月'), dom.header);
      if (layer === 'month') {
        check('#2 月层有「历次流月」折叠区', dom.foldTitle.includes('历次流月'), dom.foldTitle);
        check('#2 历次流月默认折叠', dom.foldOpen === false, String(dom.foldOpen));
      }
      if (layer === 'xun') check('旬层头注「三旬」', dom.header.includes('三旬'), dom.header);
      if (layer === 'week') check('段层头注「六段」', dom.header.includes('六段'), dom.header);
      if (!dom.hasCard) { check('出卦卡', false, '无卡：' + dom.header); return; }
      if (!expect || expect.err) { check('API 可对照', false, 'expect err=' + (expect && expect.err)); return; }
      // 按起止日期在 cast 结果里定位同一窗口
      const rng = (dom.range || '').split('→');
      const ds = (rng[0] || '').trim(), de = (rng[1] || '').split('·')[0].trim();
      const w = (expect.windows || []).find((x) => x.start === ds && x.end === de);
      check('卦卡窗口能在 cast 结果中定位', !!w, `DOM ${ds}→${de}`);
      if (!w) return;
      const g = w.gua || {};
      check('卦名与 cast 一致', dom.gua.includes(g.gua_name || ''), `DOM「${dom.gua}」 cast「${g.gua_name}·${g.yao_name}」`);
      const o = w.ohlc || {};
      check('OHLC 与 cast 一致', JSON.stringify(dom.ohlc) === JSON.stringify([o.open, o.high, o.low, o.close]), dom.ohlc.join('/'));
    }
    async function closeDrawer() {
      const open = await evl(`!!document.querySelector('#slide-drawer') && !document.querySelector('#slide-drawer').classList.contains('hidden')`);
      if (open) { await click('#drawer-close-btn'); await wait(`document.querySelector('#slide-drawer').classList.contains('hidden')`); }
      await sleep(200);
    }
    async function switchLayer(layer) {
      const marker = layer === 'xun' ? '旬' : layer === 'week' ? '段' : '';
      const before = await evl(`(document.querySelector('#opptables tbody tr')||{}).textContent||''`);
      await click(`#o-layer-seg button[data-v="${layer}"]`);
      await wait(`(document.querySelector('#o-layer-note')||{}).textContent.includes(${JSON.stringify(layer === 'xun' ? '旬层' : layer === 'week' ? '段层' : '月层')})`);
      // 等板表重载完成：首行文本变化 + （旬/段）首行含窗位标记
      const realCond = `(()=>{const t=(document.querySelector('#opptables tbody tr')||{}).textContent||''; return t.length>0 && t!==${JSON.stringify(before)}${marker ? ` && t.indexOf(${JSON.stringify(marker)})>=0` : ''};})()`;
      await wait(realCond, 40000);
      await sleep(300);
    }

    console.log('1. 月层（默认）点首行 → 抽屉卦象');
    let r = await openAndCheck('month');
    console.log('   点行：', r.dom.title);
    crossCheck('month', r);
    // #2 展开历次流月，核对卡片数=pillar.n 且首卡卦名对齐 /api/gua/pillar
    {
      const pil = (await evl(`document.querySelector('#drawer-title-pillar').textContent`)).replace('×', '').trim();
      await evl(`document.querySelector('#drawer-gua-box .gua-fold summary').click()`);
      await sleep(250);
      const fi = await evl(`(() => { const f=document.querySelector('#drawer-gua-box .gua-fold'); return {open:!!f.open, n:f.querySelectorAll('.gua-card').length, first:(f.querySelector('.gua-name')||{}).textContent||''}; })()`);
      const pf = await evl(`(async()=>{const rr=await fetch('/api/gua/pillar?code=${encodeURIComponent(r.code)}&pillar=${encodeURIComponent(pil)}',{headers:{Accept:'application/json'}});return await rr.json();})()`);
      check('#2 历次流月可点开展开', fi.open);
      check('#2 历次流月卡片数=pillar.n', fi.n === pf.n, `DOM=${fi.n} API=${pf.n}`);
      const pf0 = (pf.guas && pf.guas[0] && pf.guas[0].gua) || {};
      check('#2 历次流月首卡卦名对齐', fi.first.includes(pf0.gua_name || ''), `DOM「${fi.first.trim().split(' ')[0]}」 API「${pf0.gua_name}」`);
    }
    await shot('01_opp_month_gua_expanded');
    await closeDrawer();

    console.log('2. 切旬层 → 点首行 → 抽屉卦象');
    await switchLayer('xun');
    r = await openAndCheck('xun');
    console.log('   点行：', r.dom.title);
    crossCheck('xun', r);
    await shot('02_opp_xun_gua');
    await closeDrawer();

    console.log('3. 切段层 → 点首行 → 抽屉卦象');
    await switchLayer('week');
    r = await openAndCheck('week');
    console.log('   点行：', r.dom.title);
    crossCheck('week', r);
    await shot('03_opp_week_gua');
    await closeDrawer();

    console.log(failures ? `\n共 ${failures} 项 FAIL` : '\n全部 PASS');
    process.exitCode = failures ? 1 : 0;
  } finally { chrome.kill('SIGKILL'); }
}
main().catch((e) => { console.error('走查异常:', e.message); process.exit(2); });