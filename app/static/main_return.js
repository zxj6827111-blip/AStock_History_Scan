/* 独立栏目共用：从主系统进入时，返回优先恢复历史页面，避免整页重建。
   主系统按钮写 bazi_main_return；referrer 作为主证据，会话标记用于 BFCache 后丢 referrer 的场景。 */
(() => {
  "use strict";

  window.bindMainReturn = function bindMainReturn() {
    const link = document.getElementById("back-main");
    if (!link) return;
    link.addEventListener("click", (ev) => {
      if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey
          || ev.shiftKey || ev.altKey) return;
      let fromMain = false;
      let marked = false;
      try {
        marked = sessionStorage.getItem("bazi_main_return") === "1";
        sessionStorage.removeItem("bazi_main_return");
      } catch (_) { /* 会话存储不可用时仍看 referrer */ }
      try {
        const ref = new URL(document.referrer);
        fromMain = ref.origin === location.origin
          && (ref.pathname === "/" || ref.pathname === "/index.html");
      } catch (_) { /* 无 referrer 时看会话标记 */ }
      // 已知来自外站时不误回上一页；直接打开独立栏目仍按原 href 回主系统。
      fromMain = fromMain || (marked && !document.referrer);
      if (!fromMain || history.length < 2) return;
      ev.preventDefault();
      history.back();
    });
  };
})();
