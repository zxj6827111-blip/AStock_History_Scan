#!/usr/bin/env node
/* 页面五流月机会看板（旬层/周层）自动化端到端走查脚本
   模拟真实用户操作，验证切层、窗位筛选、抽屉明细与窗位小表。
   保存关键帧截图到 evidence/opp_window_walk */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = process.argv[2] || path.join(__dirname, '../evidence/opp_window_walk');
const PORT = 9460;
const BASE = process.env.APP_URL || 'http://127.0.0.1:8739/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  console.log(`启动 Chrome 走查 Page 5：${BASE} -> 目标目录: ${OUTDIR}`);

  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-opp-walk',
    '--window-size=1780,1200', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });

  let up = false;
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) { up = true; break; }
    } catch (e) {}
    await sleep(250);
  }
  if (!up) { chrome.kill('SIGKILL'); throw new Error('Chrome 调试端口启动超时'); }

  try {
    const tgt = await (await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })).json();
    const ws = new WebSocket(tgt.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

    let id = 0;
    const pend = new Map();
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pend.has(m.id)) {
        const p = pend.get(m.id); pend.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
      }
    };

    const send = (method, params) => new Promise((res, rej) => {
      const i = ++id; pend.set(i, { res, rej });
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });

    const evl = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) {
        const d = r.exceptionDetails;
        throw new Error('JS Error: ' + (d.exception && (d.exception.description || d.exception.value) || d.text));
      }
      return r.result.value;
    };

    const wait = async (expr, ms = 25000) => {
      const t0 = Date.now();
      for (;;) {
        let ok = false;
        try { ok = await evl(expr); } catch (e) { ok = false; }
        if (ok) return true;
        if (Date.now() - t0 > ms) throw new Error('超时等待: ' + expr);
        await sleep(150);
      }
    };

    const shot = async (name) => {
      const { data } = await send('Page.captureScreenshot', { format: 'png' });
      fs.writeFileSync(path.join(OUTDIR, name + '.png'), Buffer.from(data, 'base64'));
      console.log(`  [截图] ${name}.png 已保存`);
    };

    await send('Page.enable');
    await send('Runtime.enable');

    console.log('1. 打开页面五 流月机会看板...');
    await send('Page.navigate', { url: BASE + '#board' });
    await wait(`document.querySelector('#opptables tbody tr')`, 30000);
    await sleep(1000);

    const monthCount = await evl(`document.querySelectorAll('#opptables tbody tr').length`);
    console.log(`  PASS: 月层机会榜已渲染，行数: ${monthCount}`);
    await shot('01_page5_month_initial');

    console.log('2. 切换至 旬层（60柱·旬）...');
    await evl(`document.querySelector('#o-layer-seg button[data-v="xun"]').click()`);
    await wait(`document.querySelector('#o-xun-pills') && !document.querySelector('#o-xun-pills').classList.contains('hidden')`, 10000);
    await wait(`document.querySelector('#opptables tbody tr')`, 15000);
    await sleep(1000);

    const xunCount = await evl(`document.querySelectorAll('#opptables tbody tr').length`);
    const xunHeader = await evl(`Array.from(document.querySelectorAll('#opptables thead th')).map(h => h.innerText.trim()).join(' | ')`);
    console.log(`  PASS: 旬层全部机会榜已渲染，行数: ${xunCount}，表头: ${xunHeader}`);
    await shot('02_page5_xun_all');

    console.log('3. 切换旬位子筛选器：查看【中旬 (10–20日)】...');
    await evl(`document.querySelector('#o-xun-pills button[data-w="2"]').click()`);
    await sleep(2000);
    await wait(`document.querySelectorAll('#opptables tbody tr').length > 0`, 15000);

    const midCount = await evl(`document.querySelectorAll('#opptables tbody tr').length`);
    const sampleTags = await evl(`Array.from(document.querySelectorAll('#opptables tbody tr')).slice(0, 3).map(r => r.querySelector('td:nth-child(2)').innerText.trim())`);
    console.log(`  PASS: 中旬机会榜已渲染，行数: ${midCount}，抽查窗位标签: ${JSON.stringify(sampleTags)}`);
    await shot('03_page5_xun_mid');

    console.log('4. 切换至 周层（60柱·周）...');
    await evl(`document.querySelector('#o-layer-seg button[data-v="week"]').click()`);
    await wait(`document.querySelector('#o-week-pills') && !document.querySelector('#o-week-pills').classList.contains('hidden')`, 10000);
    await wait(`document.querySelector('#opptables tbody tr')`, 15000);
    await sleep(1200);

    const weekCount = await evl(`document.querySelectorAll('#opptables tbody tr').length`);
    const weekHeader = await evl(`Array.from(document.querySelectorAll('#opptables thead th')).map(h => h.innerText.trim()).join(' | ')`);
    console.log(`  PASS: 周层全部机会榜已渲染，行数: ${weekCount}，表头: ${weekHeader}`);
    await shot('04_page5_week_all');

    console.log('5. 点击首行打开右侧抽屉，验证月内窗位小表...');
    await evl(`document.querySelector('#opptables tbody tr').click()`);
    await wait(`!document.querySelector('#slide-drawer').classList.contains('hidden')`, 10000);
    await wait(`document.querySelector('#drawer-windows-box') && document.querySelector('#dw-cards')`, 10000);
    await sleep(800);

    const drawerTitle = await evl(`document.querySelector('#drawer-title-name').innerText`);
    const drawerPillar = await evl(`document.querySelector('#drawer-title-pillar').innerText`);
    console.log(`  PASS: 抽屉打开成功: ${drawerTitle} ${drawerPillar}`);
    await shot('05_page5_drawer_window_table');

    console.log('全部 5 项测试全部通过！');
    ws.close();
    chrome.kill('SIGKILL');
  } catch (err) {
    chrome.kill('SIGKILL');
    throw err;
  }
}

main().catch((err) => {
  console.error('测试异常:', err);
  process.exit(1);
});
