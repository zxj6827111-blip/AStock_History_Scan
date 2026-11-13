/* 流月卦卡片共享渲染器：/gua 独立页与 100%段抽屉共用，保证同一表现。
   暴露 window.guaCardHTML(w) / window.guaSideBadge(side) / window.GUA_STATUS_TXT。
   w 形如 gua_api._cast_window 的输出：{name,status,start,end,n_bars,ohlc,digit_sums,gua,note,missing_days}。 */
(function () {
  'use strict';
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
  const STATUS_TXT = { fixed: '定卦', dynamic: '未定', none: '未开始', insufficient: '数据不足' };

  function sideBadge(side) {
    const cls = side === '好' ? 'good' : (side === '差' ? 'bad' : 'mid');
    return `<span class="gua-side ${cls}">${esc(side || '中')}</span>`;
  }

  // 共识徽章：与参考系统同词表（▲双好 / ▼双差 / 分歧 / 空→一般），A股习惯 好=红、差=绿
  function consBadge(cons) {
    const c = String(cons || '').trim();
    const cls = c.indexOf('双好') >= 0 ? 'good' : c.indexOf('双差') >= 0 ? 'bad'
      : c.indexOf('分歧') >= 0 ? 'conflict' : 'neutral';
    const label = c || '一般';
    const tip = c.indexOf('双好') >= 0 ? '卦象操作信号与高岛断语都看好'
      : c.indexOf('双差') >= 0 ? '卦象操作信号与高岛断语都看差'
      : c.indexOf('分歧') >= 0 ? '卦象与高岛方向对立，交回人工判断'
      : '两套解读未形成一致结论（一方中性或语气不明）';
    return `<span class="gua-cons ${cls}" title="${esc(tip)}（仅供解读，不参与选股）">${esc(label)}</span>`;
  }

  function windowCard(w) {
    const st = w.status || 'none';
    const badge = `<span class="gua-badge st-${esc(st)}">${esc(STATUS_TXT[st] || st)}</span>`;
    const head = `<div class="gua-card-head">
        <span class="gua-win-name">${esc(w.name)}</span>${badge}
        <span class="gua-win-range">${esc(w.start)} → ${esc(w.end)} · ${w.n_bars} 根K线</span>
      </div>`;
    if (st === 'none') {
      return `<div class="gua-card dim">${head}<div class="gua-empty">窗口未开始，无卦</div></div>`;
    }
    if (st === 'insufficient') {
      const miss = (w.missing_days || []).length
        ? `<div class="gua-note">缺日（前 ${Math.min(w.missing_days.length, 10)} 个）：${esc(w.missing_days.slice(0, 10).join('、'))}${w.missing_days.length > 10 ? ' …' : ''}</div>` : '';
      return `<div class="gua-card dim">${head}<div class="gua-empty">${esc(w.note || '数据不足，不Cast')}</div>${miss}</div>`;
    }
    const g = w.gua || {};
    const gt = g.gaodao_text || '';
    const o = w.ohlc || {}, ds = w.digit_sums || {};
    const cell = (k, v, s) => `<div class="cell"><span class="k">${k}</span><span class="v">${esc(v)}</span><span class="s">数字和 ${esc(s)}</span></div>`;
    return `<div class="gua-card">
      ${head}
      <div class="flex items-center gap-3 mt-2">
        <span class="gua-symbol">${esc(g.gua_symbol)}</span>
        <div>
          <div class="gua-name">${esc(g.gua_name)} <span class="gua-yao">动爻 · ${esc(g.yao_name)}</span></div>
          <div class="gua-sub">上${esc(g.upper_name)}${esc(g.upper_symbol)} 下${esc(g.lower_name)}${esc(g.lower_symbol)} · 变卦 ${esc(g.changed_hexagram_name)} · 状态 ${esc(g.state_id)}</div>
        </div>
      </div>
      <div class="gua-ohlc">
        ${cell('开', o.open, ds.open)}${cell('高', o.high, ds.high)}${cell('低', o.low, ds.low)}${cell('收', o.close, ds.close)}
      </div>
      <div class="gua-text"><b>卦辞</b>：${esc(g.gua_ci)}</div>
      <div class="gua-text"><b>爻辞</b>：${esc(g.yao_ci)}</div>
      <div class="gua-text"><b>市场解读</b>：${esc(g.market_judgement)} <span class="gua-sub">操作信号：${esc(g.action_signal)}</span></div>
      ${gt
        ? `<div class="gua-text gua-gaodao"><b>高岛易断</b>：${esc(gt)}${sideBadge(g.gaodao_side)}</div>`
        : `<div class="gua-text gua-gaodao"><b>高岛易断</b>：<span class="gua-sub">此书该爻无营商断语（以市场解读为准）</span></div>`}
      <div class="gua-consensus">共识 ${consBadge(g.consensus)}<span class="gua-sub">卦象 ${sideBadge(g.gua_side)} · 高岛 ${sideBadge(g.gaodao_side)}</span></div>
      <div class="gua-note">${esc(w.note)}</div>
    </div>`;
  }

  window.guaCardHTML = windowCard;
  window.guaSideBadge = sideBadge;
  window.GUA_STATUS_TXT = STATUS_TXT;
})();