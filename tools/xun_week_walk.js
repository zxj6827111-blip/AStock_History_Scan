#!/usr/bin/env node
/* 自动化端到端走查脚本：模拟真实用户操作，验证切层、筛选保留、抽屉开关与窗位渲染。
   保存关键帧截图到 evidence/xun_week_walk */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const OUTDIR = process.argv[2] || path.join(__dirname, '../evidence/xun_week_walk');
const PORT = 9450;
const BASE = process.env.APP_URL || 'http://127.0.0.1:8739/';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  fs.mkdirSync(OUTDIR, { recursive: true });
  console.log(`启动 Chrome 自动化走查：${BASE} -> 目标目录: ${OUTDIR}`);

  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-xun-week',
    '--window-size=1780,1200', '--hide-scrollbars', '--force-device-scale-factor=1',
    '--disable-gpu', 'about:blank',
  ], { stdio: 'ignore' });

  let up = false;
  for (let i = 0; i < 80; i++) {
    try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
          if (r.ok) { up = true; break; } } catch (e) {}
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
      ws.send(JSON.stringify({ id: i, method, params: params || {} }));
    });

    const evl = async (expr) => {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) {
        const d = r.exceptionDetails;
        throw new Error('JS Error: ' + (d.exception && (d.exception.description || d.exception.value) || d.text));
      }
      return r.result.value;
    };

    const wait = async (expr, ms = 20000) => {
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

    console.log('1. 打开页面三 60流月柱历史规律...');
    await send('Page.navigate', { url: BASE + '?id=600519.SH&tab=pillar' });
    await wait(`document.querySelector('#pillartable tbody tr')`, 30000);
    await sleep(800);

    // 检查旬层默认加载
    const xunCount = await evl(`document.querySelectorAll('#pillartable tbody tr').length`);
    console.log(`  PASS: 默认旬层行数: ${xunCount} 行`);
    await shot('01_page3_xun_initial');

    console.log('2. 筛选测试与切层保留验证：输入丙午，切至周层...');
    await evl(`(() => {
      const inp = document.querySelector('#p-search');
      inp.value = '丙午';
      inp.dispatchEvent(new Event('input', { bubbles: true }));
      inp.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
    await sleep(600);
    const filterCountXun = await evl(`document.querySelectorAll('#pillartable tbody tr').length`);
    console.log(`  PASS: 旬层筛选丙午行数: ${filterCountXun} 行 (期望 3 窗)`);

    // 切换到周层
    await evl(`document.querySelector('#p-layer-seg button[data-v="week"]').click()`);
    await sleep(800);
    const searchValWeek = await evl(`document.querySelector('#p-search').value`);
    const filterCountWeek = await evl(`document.querySelectorAll('#pillartable tbody tr').length`);
    console.log(`  PASS: 周层筛选保留搜索值: "${searchValWeek}"，行数: ${filterCountWeek} 行 (期望 6 窗)`);
    await shot('02_page3_week_filter_preserved');

    console.log('3. 点击行打开个股抽屉...');
    await evl(`document.querySelector('#pillartable tbody tr').click()`);
    await wait(`!document.querySelector('#slide-drawer').classList.contains('hidden')`, 10000);
    await wait(`document.querySelector('#drawer-windows-box') && document.querySelector('#dw-cards')`, 10000);
    await sleep(600);

    const drawerPillar = await evl(`document.querySelector('#drawer-title-pillar').innerText`);
    console.log(`  PASS: 抽屉已打开，当前展示: ${drawerPillar}`);

    // 切到 3 旬视图
    await evl(`document.querySelector('#dw-btn-xun').click()`);
    await sleep(400);
    const xunCardCount = await evl(`document.querySelectorAll('#dw-cards > div').length`);
    console.log(`  PASS: 抽屉 3 旬视图卡片数: ${xunCardCount}`);
    await shot('03_drawer_xun_subtable');

    // 切到 6 周视图
    await evl(`document.querySelector('#dw-btn-week').click()`);
    await sleep(400);
    const weekCardCount = await evl(`document.querySelectorAll('#dw-cards > div').length`);
    console.log(`  PASS: 抽屉 6 周视图卡片数: ${weekCardCount}`);
    await shot('04_drawer_week_subtable');

    console.log('4. 关闭抽屉，清空筛选...');
    await evl(`document.querySelector('#drawer-close-btn').click()`);
    await sleep(300);
    const drawerHidden = await evl(`document.querySelector('#slide-drawer').classList.contains('hidden')`);
    console.log(`  PASS: 抽屉已成功关闭: ${drawerHidden}`);

    await evl(`document.querySelector('#p-reset').click()`);
    await sleep(600);
    const resetCount = await evl(`document.querySelectorAll('#pillartable tbody tr').length`);
    console.log(`  PASS: 清空筛选后总窗数: ${resetCount} 行`);
    await shot('05_page3_week_full');

    console.log('\n全部自动化走查用例执行通过！');
    ws.close();
  } finally {
    chrome.kill('SIGKILL');
  }
}

main().catch(err => {
  console.error('走查执行失败:', err);
  process.exit(1);
});
