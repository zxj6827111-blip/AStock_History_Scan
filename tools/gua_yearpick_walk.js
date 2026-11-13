#!/usr/bin/env node
/* 年份标签「悬停高亮」走查：100%段抽屉 + 机会表抽屉各验一次。
   悬停某年 → 该线加粗变亮(3.2/白)，其余线保持不变；移开 → 恢复原样。不点击。
   用法: node tools/gua_yearpick_walk.js [APP_URL] [OUTDIR]（token 由 /tmp/gua_walk_token.txt 或 GUA_TOKEN） */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.argv[2] || 'http://127.0.0.1:8792';
const OUTDIR = process.argv[3] || path.join(__dirname, '../evidence/gua_yearpick_walk');
const PORT = 9456;
const TOKEN = process.env.GUA_TOKEN || fs.readFileSync('/tmp/gua_walk_token.txt', 'utf8').trim();
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
function check(n, c, e) { if (c) console.log(`  PASS: ${n}${e ? ' | ' + e : ''}`); else { failures++; console.log(`  FAIL: ${n}${e ? ' | ' + e : ''}`); } }

async function main() {
  if (!TOKEN) throw new Error('缺 token');
  fs.mkdirSync(OUTDIR, { recursive: true });
  const chrome = spawn(CHROME, ['--headless=new','--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`,'--user-data-dir=/tmp/cdp-profile-gua-yearpick','--window-size=1500,1300',
    '--hide-scrollbars','--force-device-scale-factor=1','--disable-gpu','about:blank'], { stdio: 'ignore' });
  let up = false; for (let i = 0; i < 80; i++) { try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) { up = true; break; } } catch (e) {} await sleep(250); }
  if (!up) { chrome.kill('SIGKILL'); throw new Error('Chrome 超时'); }
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

    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Network.setUserAgentOverride', { userAgent: UA });
    await send('Network.setCookie', { name: 'astock_session', value: TOKEN, domain: '127.0.0.1', path: '/' });

    async function verify(contextName) {
      const info = await evl(`(() => {
        const box=document.querySelector('#drawer-path-box');
        const ps=[...box.querySelectorAll('[data-winpill]')];
        if(!ps.length) return {ok:false};
        const p=ps[0];
        return {ok:true, n:ps.length, cid:p.getAttribute('data-winpill'), year:p.getAttribute('data-year')};
      })()`);
      check(`${contextName}：图表有可悬停年份标签`, info.ok && info.n > 0, info.ok ? `${info.n} 个年份` : '无标签');
      if (!info.ok) return;
      const { cid, year } = info;
      const pillSel = `#drawer-path-box [data-winpill="${cid}"][data-year="${year}"]`;
      const fire = (prop) => evl(`(() => { const el=document.querySelector('${pillSel}'); if(!el || typeof el.${prop}!=='function') return false; el.${prop}(); return true; })()`);
      // 悬停（调用标签自身的 onmouseenter 处理器，即浏览器悬停时触发的那个）
      const wired = await fire('onmouseenter');
      check(`${contextName}：年份标签已绑定悬停处理`, wired === true);
      await sleep(220);
      const on = await evl(`(() => {
        const cid=${JSON.stringify(cid)}, year=${JSON.stringify(year)};
        const tgt=document.getElementById('wpath-'+cid+'-'+year);
        const sibs=[...document.querySelectorAll('#drawer-path-box [id^="wpath-'+cid+'-"]')].filter(p=>p.id!=='wpath-'+cid+'-'+year);
        return {tw: tgt&&tgt.getAttribute('stroke-width'), ts: tgt&&tgt.getAttribute('stroke'),
                sibsUnchanged: sibs.length? sibs.every(p=>(p.getAttribute('stroke-width')===p.getAttribute('data-w')) && (p.getAttribute('opacity')===null||p.getAttribute('opacity')==='1')):null};
      })()`);
      check(`${contextName}：${year}年 悬停→该线加粗变亮`, on.tw === '3.2' && (on.ts === '#ffffff' || on.ts === '#0f172a' || on.ts === 'white'), `w=${on.tw} stroke=${on.ts}`);
      check(`${contextName}：其余线保持不变（不变暗、不变细）`, on.sibsUnchanged !== false, String(on.sibsUnchanged));
      await shot(`${contextName === '100%段' ? 'A_seg' : 'B_opp'}_yearpick_hover`);
      // 移开
      await fire('onmouseleave');
      await sleep(220);
      const off = await evl(`(() => {
        const cid=${JSON.stringify(cid)}, year=${JSON.stringify(year)};
        const tgt=document.getElementById('wpath-'+cid+'-'+year);
        return {tw: tgt&&tgt.getAttribute('stroke-width'), ts: tgt&&tgt.getAttribute('stroke'), color: tgt&&tgt.getAttribute('data-color')};
      })()`);
      check(`${contextName}：移开→该线恢复原样`, (off.tw === '1.6' || off.tw === '1.7') && off.ts === off.color, `w=${off.tw} stroke==color=${off.ts === off.color}`);
    }

    console.log('A. 100%段抽屉 → 年份标签悬停高亮');
    await send('Page.navigate', { url: BASE + '/?seg=1' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length>0`);
    await sleep(600);
    await evl(`document.querySelector('#opptables tbody tr').click()`);
    await wait(`document.querySelector('#drawer-path-box [data-winpill]')`, 30000);
    await sleep(300);
    await verify('100%段');

    console.log('B. 机会表（整月层）抽屉 → 年份标签悬停高亮');
    await send('Page.navigate', { url: BASE + '/' });
    await wait(`document.querySelectorAll('#opptables tbody tr').length>0`);
    await sleep(600);
    await evl(`document.querySelector('#opptables tbody tr').click()`);
    await wait(`document.querySelector('#drawer-path-box [data-winpill]')`, 30000);
    await sleep(300);
    await verify('机会表');

    console.log(failures ? `\n共 ${failures} 项 FAIL` : '\n全部 PASS');
    process.exitCode = failures ? 1 : 0;
  } finally { chrome.kill('SIGKILL'); }
}
main().catch((e) => { console.error('走查异常:', e.message); process.exit(2); });