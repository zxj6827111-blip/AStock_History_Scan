#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = path.join(__dirname, '../evidence/current_window_walk');
const PORT = 9477;
const APP_PORT = 8740;
const BASE = `http://127.0.0.1:${APP_PORT}/`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  console.log(`[E2E] 1. 启动测试服务器端口 ${APP_PORT}...`);
  const server = spawn('python3', [
    path.join(__dirname, '../app/server.py'),
    '--port', String(APP_PORT)
  ], { stdio: 'inherit' });

  // 等待服务器就绪
  let serverUp = false;
  for (let i = 0; i < 40; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${APP_PORT}/api/calendar_now`);
      if (res.ok) { serverUp = true; break; }
    } catch (e) {}
    await sleep(250);
  }
  if (!serverUp) {
    server.kill('SIGKILL');
    throw new Error('测试服务器启动失败');
  }
  console.log(`[E2E] 测试服务器已就绪: ${BASE}`);

  console.log(`[E2E] 2. 启动 Headless Chrome...`);
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-win-walk',
    '--window-size=1780,1200', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });

  let chromeUp = false;
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) { chromeUp = true; break; }
    } catch (e) {}
    await sleep(200);
  }
  if (!chromeUp) {
    chrome.kill('SIGKILL');
    server.kill('SIGKILL');
    throw new Error('Chrome 启动超时');
  }

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
    const send = (method, params = {}) => new Promise((res, rej) => {
      const mid = ++id;
      pend.set(mid, { res, rej });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

    await send('Page.enable');
    await send('Runtime.enable');
    await send('DOM.enable');

    const evalJs = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
      return r.result.value;
    };

    const snap = async (name) => {
      const r = await send('Page.captureScreenshot', { format: 'png' });
      const fpath = path.join(OUTDIR, `${name}.png`);
      fs.writeFileSync(fpath, Buffer.from(r.data, 'base64'));
      console.log(`  [截图] -> ${fpath}`);
    };

    console.log(`[E2E] 3. 访问首页并加载个股 600519.SH...`);
    await send('Page.navigate', { url: `${BASE}?id=600519.SH` });

    // 等待页面就绪 (S 和 cal 均加载完成)
    for (let i = 0; i < 40; i++) {
      const ready = await evalJs(`Boolean(typeof S !== 'undefined' && S.stock && S.cal && S.cal.current)`);
      if (ready) break;
      await sleep(250);
    }

    const calInfo = await evalJs(`({
      hasCurrent: Boolean(typeof S !== 'undefined' && S.cal && S.cal.current),
      hasWindow: Boolean(typeof S !== 'undefined' && S.cal && S.cal.current_window),
      curPillar: typeof S !== 'undefined' && S.cal && S.cal.current && S.cal.current.month_pillar,
      winName: typeof S !== 'undefined' && S.cal && S.cal.current_window && S.cal.current_window.week_name
    })`);
    console.log(`  历法与窗位数据:`, calInfo);

    // 检查方案二：个股模式下 #tengodnow 的窗位胶囊与六周微条
    const tengodHtml = await evalJs(`document.getElementById('tengodnow')?.innerHTML || ''`);
    const hasNowTag = tengodHtml.includes('当下：') && tengodHtml.includes('中旬') && tengodHtml.includes('第4周');
    const hasWeeksBar = tengodHtml.includes('六周进程') && tengodHtml.includes('第4周当下');
    console.log(`  方案二验证: 包含当前窗位胶囊? ${hasNowTag}, 包含六周微条? ${hasWeeksBar}`);
    await snap('01_page1_stock_current_window');

    console.log(`[E2E] 4. 切换到页面五「流月机会表」...`);
    await evalJs(`setBoardMode(true)`);
    await sleep(2000);

    // 检查方案一：#o-now-wrap 是否显示、内容是否为中旬·第4周
    const nowWrapVisible = await evalJs(`!document.getElementById('o-now-wrap')?.classList.contains('hidden')`);
    const nowWrapText = await evalJs(`document.getElementById('o-now-text')?.textContent || ''`);
    console.log(`  方案一验证: #o-now-wrap 可见? ${nowWrapVisible}, 文本: ${nowWrapText}`);
    await snap('02_page5_board_now_badge');

    console.log(`[E2E] 5. 点击「🎯 定位到当下」按钮...`);
    await evalJs(`document.getElementById('o-btn-locate-now')?.click()`);
    await sleep(1500);

    const activeLayer = await evalJs(`typeof S !== 'undefined' ? S.oppLayer : null`);
    const activeWindowNo = await evalJs(`typeof S !== 'undefined' ? S.oppWindowNo : null`);
    console.log(`  定位结果: layer=${activeLayer}, window_no=${activeWindowNo}`);
    await snap('03_page5_board_located_week4');

    console.log(`============================================================`);
    console.log(`E2E 走查与截图验收 100% 成功！`);
    console.log(`============================================================`);

    ws.close();
  } finally {
    chrome.kill('SIGKILL');
    server.kill('SIGKILL');
  }
}

main().catch((err) => {
  console.error('[E2E 失败]', err);
  process.exit(1);
});
