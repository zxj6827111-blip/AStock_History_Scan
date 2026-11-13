#!/usr/bin/env node
/* 流月卦页端到端走查：真实点击 + DOM 断言 + 截图。
   用法: GUA_TOKEN=<session> node tools/gua_walk.js [APP_URL] [OUTDIR]
   默认打预览实例 8791（BAZI_NO_JOBS=1），不动 8737。 */
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE = process.argv[2] || 'http://127.0.0.1:8791';
const OUTDIR = process.argv[3] || path.join(__dirname, '../evidence/gua_walk');
const PORT = 9451;
const TOKEN = process.env.GUA_TOKEN || '';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;

function check(name, cond, extra) {
  if (cond) console.log(`  PASS: ${name}${extra ? ' | ' + extra : ''}`);
  else { failures++; console.log(`  FAIL: ${name}${extra ? ' | ' + extra : ''}`); }
}

async function main() {
  if (!TOKEN) throw new Error('缺 GUA_TOKEN 环境变量');
  fs.mkdirSync(OUTDIR, { recursive: true });
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
    `--remote-debugging-port=${PORT}`, '--user-data-dir=/tmp/cdp-profile-gua',
    '--window-size=1500,1100', '--hide-scrollbars', '--force-device-scale-factor=1',
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
      console.log(`  [截图] ${name}.png`);
    };
    const click = async (sel) => {
      await evl(`(() => { const el = document.querySelector('${sel}'); if (!el) throw new Error('无元素 ${sel}'); el.click(); return true; })()`);
    };

    await send('Page.enable');
    await send('Runtime.enable');
    await send('Network.enable');
    await send('Network.setCookie', { name: 'astock_session', value: TOKEN, domain: '127.0.0.1', path: '/' });

    console.log('1. 打开 /gua（默认 600519.SH 本月 流月卦）');
    await send('Page.navigate', { url: BASE + '/gua' });
    await wait(`document.querySelector('.gua-card .gua-name') && document.querySelector('.gua-card .gua-name').textContent.includes('火风鼎')`);
    await sleep(500);
    const gold = await evl(`(() => {
      const c = document.querySelector('.gua-card');
      return {
        name: c.querySelector('.gua-name').textContent,
        yao: c.querySelector('.gua-yao').textContent,
        badge: c.querySelector('.gua-badge').textContent,
        ohlc: [...c.querySelectorAll('.gua-ohlc .v')].map((x) => x.textContent),
        sums: [...c.querySelectorAll('.gua-ohlc .s')].map((x) => x.textContent),
        range: c.querySelector('.gua-win-range').textContent,
        disclaimer: document.querySelector('#disclaimer').textContent,
        head: document.querySelector('#head-meta').textContent,
      };
    })()`);
    check('月卦卦名=火风鼎', gold.name.includes('火风鼎'), gold.name.trim());
    check('动爻=九二', gold.yao.includes('九二'), gold.yao.trim());
    check('状态=定卦', gold.badge.includes('定卦'), gold.badge);
    check('OHLC=1255.42/1480.60/1255.42/1453.00',
      JSON.stringify(gold.ohlc) === JSON.stringify(['1255.42', '1480.60', '1255.42', '1453.00']), gold.ohlc.join('/'));
    check('数字和=19/19/19/13',
      JSON.stringify(gold.sums) === JSON.stringify(['数字和 19', '数字和 19', '数字和 19', '数字和 13']), gold.sums.join('/'));
    check('窗口=2026-10-09→2026-11-07 · 21根', gold.range.includes('2026-10-09') && gold.range.includes('2026-11-07') && gold.range.includes('21'), gold.range);
    check('免责声明在页首', gold.disclaimer.includes('非选股依据') && gold.disclaimer.includes('文化展示'));
    check('页头显示标的与数据截止', gold.head.includes('600519') && gold.head.includes('raw'), gold.head);
    await shot('01_month_golden');

    console.log('2. 点「三旬」tab（当前月：上旬未定，中下旬未开始）');
    await click('#layer-tabs button[data-layer="xun"]');
    await wait(`document.querySelectorAll('.gua-card').length === 3 && document.querySelector('.gua-badge.st-dynamic')`);
    const xun = await evl(`[...document.querySelectorAll('.gua-card')].map((c) => ({
      name: c.querySelector('.gua-win-name').textContent,
      badge: c.querySelector('.gua-badge').textContent,
      note: (c.querySelector('.gua-note') || {}).textContent || '',
    }))`);
    check('三旬=3张卡', xun.length === 3);
    check('上旬=未定', xun[0].badge.includes('未定'), xun[0].badge);
    check('未定卡带「卦未定」说明', xun[0].note.includes('未定'), xun[0].note);
    check('中旬/下旬=未开始', xun[1].badge.includes('未开始') && xun[2].badge.includes('未开始'));
    await shot('02_xun_current');

    console.log('3. 点「六段」tab + 月份切到上月（M09 全定卦）');
    await click('#layer-tabs button[data-layer="week"]');
    await wait(`document.querySelectorAll('.gua-card').length === 6`);
    await evl(`(() => { const sel = document.querySelector('#month'); sel.value = '-1'; sel.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
    await wait(`document.querySelectorAll('.gua-card').length === 6 && [...document.querySelectorAll('.gua-badge')].every((b) => b.textContent.includes('定卦'))`);
    const wk = await evl(`[...document.querySelectorAll('.gua-card')].map((c) => ({
      name: c.querySelector('.gua-win-name').textContent,
      gua: (c.querySelector('.gua-name') || {}).textContent || '',
      n: c.querySelector('.gua-win-range').textContent,
    }))`);
    check('六段=6张卡全定卦', wk.length === 6, wk.map((w) => w.gua.trim().split(' ')[0]).join('/'));
    check('第1段=火山旅（与API一致）', wk[0].gua.includes('火山旅'), wk[0].gua.trim());
    const nSum = wk.reduce((a, w) => a + Number((w.n.match(/· (\d+) 根/) || [])[1] || 0), 0);
    check('6段K线数合计=21（与整月一致）', nSum === 21, `nSum=${nSum}`);
    await shot('03_week_m09');

    console.log('4. 点「上证指数」chip + 本月 + 流月卦层（锚=M09，补拉前：数据不足不Cast）');
    await click('.gua-chip[data-code="000001.SH"]');
    await evl(`(() => { const sel = document.querySelector('#month'); sel.value = '0'; sel.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
    await click('#layer-tabs button[data-layer="month"]');
    await wait(`document.querySelectorAll('.gua-card').length === 1 && document.querySelector('.gua-badge.st-insufficient')`);
    const idx = await evl(`(() => {
      const c = document.querySelector('.gua-card');
      return { badge: c.querySelector('.gua-badge').textContent, text: c.textContent, hasGua: !!c.querySelector('.gua-name') };
    })()`);
    check('指数=数据不足', idx.badge.includes('数据不足'), idx.badge);
    check('不足时不出卦', !idx.hasGua);
    check('缺日提示可见', idx.text.includes('缺') && idx.text.includes('数据不足'));
    await shot('04_index_insufficient');

    console.log('4b. 指数切到上月（锚=M08，镜像已覆盖：应定卦）——正对照');
    await evl(`(() => { const sel = document.querySelector('#month'); sel.value = '-1'; sel.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
    await wait(`document.querySelector('.gua-badge.st-fixed') && document.querySelector('.gua-card .gua-name')`);
    const idx8 = await evl(`(() => {
      const c = document.querySelector('.gua-card');
      return { gua: c.querySelector('.gua-name').textContent, range: c.querySelector('.gua-win-range').textContent };
    })()`);
    check('指数 M08 定卦出卦名', idx8.gua.trim().length > 0, idx8.gua.trim() + ' | ' + idx8.range);
    await shot('04b_index_m08_fixed');

    console.log('5. 空态：输入不存在的代码 999999');
    await evl(`(() => { const inp = document.querySelector('#code'); inp.value = '999999'; inp.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
    await click('#go');
    await wait(`document.querySelector('#status').classList.contains('err')`);
    const errMsg = await evl(`document.querySelector('#status').textContent`);
    check('错误提示含「未找到标的」', errMsg.includes('未找到标的'), errMsg);
    await shot('05_bad_code');

    console.log('6. 首页导航按钮「流月卦」真实点击跳转');
    await send('Page.navigate', { url: BASE + '/' });
    // 等首页 init 完成（板表行出现）再点，否则 #open-gua 的 onclick 尚未绑定会空点
    await wait(`document.querySelector('#open-gua') && document.querySelectorAll('#opptables tbody tr').length > 0`);
    await sleep(400);
    await click('#open-gua');
    await wait(`location.pathname === '/gua' && document.querySelector('.gua-card')`, 25000);
    check('首页按钮跳到 /gua 并渲染', await evl(`location.pathname === '/gua'`));
    await shot('06_nav_from_index');

    console.log(failures ? `\n共 ${failures} 项 FAIL` : '\n全部 PASS');
    process.exitCode = failures ? 1 : 0;
  } finally {
    chrome.kill('SIGKILL');
  }
}

main().catch((e) => { console.error('走查异常:', e.message); process.exit(2); });