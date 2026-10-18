/* static/feedback-widget.js — 反馈悬浮窗（Issue #108）
 *
 * 为什么：点「反馈」原先是整页跳转，用户填单途中丢当前页状态，于是「懒得提」。
 * 现在点击 → 就地弹层 → 提交后自动收起，人留在原页。
 *
 * 四条纪律（都来自 code review 抓出来的真实缺陷）：
 *   1. 渐进增强 —— 无 JS 时 <a href> 照旧跳 /feedback/new，弹层只是增强。
 *   2. 成功判据必须看 ticket_no —— 全局 after_request 会把 302 改写成
 *      200 {ok:true}（登录过期时的重定向也走这条路），只看 ok 会误报成功。
 *   3. 失败不丢字 —— 校验失败/会话过期留在弹层内显示原因，输入原样保留；
 *      非 JSON 响应（CSRF 过期返回 HTML 400）先存草稿再提示刷新。
 *   4. 不新造服务端约定 —— 走 POST /feedback/api，响应 {ok, message, ticket_no}。
 */
(function () {
  'use strict';

  var overlay = document.getElementById('feedback-modal');
  if (!overlay) return; // 未登录 / 页面未渲染弹层

  var form = document.getElementById('feedback-modal-form');
  var pageInput = form.querySelector('input[name="page_path"]');
  var errorBox = overlay.querySelector('[data-feedback-error]');
  var submitBtn = form.querySelector('button[type="submit"]');
  var titleInput = form.querySelector('input[name="title"]');
  var bodyInput = form.querySelector('textarea[name="body"]');
  var categoryInput = form.querySelector('select[name="category"]');

  var DRAFT_KEY = 'feedback:draft';
  var lastFocus = null;
  var inFlight = false;
  var scrollLockPrev = null;

  function showError(msg) {
    if (!errorBox) return;
    errorBox.textContent = msg || '提交失败，请稍后重试。';
    errorBox.hidden = false;
  }

  function clearError() {
    if (!errorBox) return;
    errorBox.textContent = '';
    errorBox.hidden = true;
  }

  function lockScroll(on) {
    if (on) {
      // 记住原 inline 值，别硬置 ''：同页其他组件也可能在改 overflow
      scrollLockPrev = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
    } else {
      document.body.style.overflow = scrollLockPrev || '';
      scrollLockPrev = null;
    }
  }

  function saveDraft() {
    try {
      sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
        title: titleInput ? titleInput.value : '',
        body: bodyInput ? bodyInput.value : '',
        category: categoryInput ? categoryInput.value : ''
      }));
    } catch (_) { /* 隐私模式 / 配额满：静默，不影响主流程 */ }
  }

  function restoreDraft() {
    var raw;
    try { raw = sessionStorage.getItem(DRAFT_KEY); } catch (_) { return; }
    if (!raw) return;
    try {
      var d = JSON.parse(raw);
      if (d.title && titleInput) titleInput.value = d.title;
      if (d.body && bodyInput) bodyInput.value = d.body;
      if (d.category && categoryInput) categoryInput.value = d.category;
    } catch (_) { /* 坏数据直接忽略 */ }
  }

  function dropDraft() {
    try { sessionStorage.removeItem(DRAFT_KEY); } catch (_) {}
  }

  function open(trigger) {
    lastFocus = trigger || document.activeElement;
    clearError();
    if (submitBtn) submitBtn.disabled = false;
    if (pageInput) {
      // 记录「提交时所在页面」，供派发时做代码定位（feedback_introspect 消费）
      pageInput.value = window.location.pathname + window.location.search;
    }
    restoreDraft();
    overlay.hidden = false;
    lockScroll(true);
    if (titleInput) {
      // 等一帧再 focus，Safari 对 hidden 刚移除的节点直接 focus 会失败
      requestAnimationFrame(function () { titleInput.focus(); });
    }
  }

  function close() {
    overlay.hidden = true;
    lockScroll(false);
    // preventScroll：不传的话 focus() 会把长列表页滚回页首 —— 那正是本单
    // 要消灭的「丢失当前操作现场」。
    if (lastFocus && typeof lastFocus.focus === 'function') {
      lastFocus.focus({ preventScroll: true });
    }
    lastFocus = null;
  }

  function toast(msg) {
    if (!msg) return;
    var bar = document.createElement('div');
    bar.className = 'ajax-flash';
    bar.textContent = msg;
    bar.setAttribute('role', 'status');
    document.body.appendChild(bar);
    requestAnimationFrame(function () { bar.classList.add('is-show'); });
    setTimeout(function () {
      bar.classList.remove('is-show');
      setTimeout(function () { bar.remove(); }, 250);
    }, 2600);
  }

  function submitNative() {
    // 兜底：浏览器原生提交，行为与改造前（PRG 跳转）一致
    HTMLFormElement.prototype.submit.call(form);
  }

  function onSubmit(ev) {
    ev.preventDefault();
    if (inFlight) return;
    inFlight = true;
    if (submitBtn) submitBtn.disabled = true;
    clearError();

    var url = form.getAttribute('data-endpoint') || form.action;
    fetch(url, {
      method: 'POST',
      body: new FormData(form),
      credentials: 'same-origin',
      headers: {
        'X-Requested-With': 'fetch',
        'Accept': 'application/json'
      }
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (payload) {
        return { ok: res.ok, status: res.status, body: payload };
      });
    }).then(function (r) {
      inFlight = false;
      // 判据看 ticket_no：成功必带单号。会话过期时全局 hook 返 401
      // {ok:false, reason:'auth_required', location}（不再是旧的 200 ok:true），
      // 两种情况都没有 ticket_no，绝不会误判成功。
      if (r.body && r.body.ok && r.body.ticket_no) {
        dropDraft();
        form.reset();
        if (pageInput) pageInput.value = '';
        close();
        toast(r.body.message || '已提交，管理员会尽快处理。');
        return;
      }
      if (submitBtn) submitBtn.disabled = false;

      // 登录过期 / 未选仓：hook 现在返 401/409 {ok:false, reason, location}。
      // 按 location 给准话并暂存草稿——用户写的内容绝不能静默丢失。
      if (r.body && r.body.location) {
        var loc = String(r.body.location);
        saveDraft();
        if (loc.indexOf('/login') !== -1) {
          showError('登录已过期，请重新登录后再提交（已暂存你写的内容）。');
          return;
        }
        if (loc.indexOf('select-warehouse') !== -1) {
          showError('请先在右上角选择仓库，再提交反馈。（已暂存你写的内容）');
          return;
        }
        showError('页面已跳转，请刷新后重试（已暂存你写的内容）。');
        return;
      }

      if (r.body && r.body.message) {
        // 400 校验失败 / 401 登录过期 / 409 未选仓：都留在弹层里，输入不清空
        if (r.status === 401) saveDraft();
        showError(r.body.message);
        return;
      }

      // 非 JSON（CSRF 过期返回 HTML 400、网关错误页）：存草稿再提示，
      // 绝不静默清空用户写的东西。
      saveDraft();
      showError('页面可能已过期，请刷新后重试（已暂存你写的内容）。');
    }).catch(function () {
      // 只有网络层失败（fetch 直接 reject）才回退原生提交：
      // 服务端若已落库，再 POST 一次会建第二张单 —— 所以 4xx/5xx 不走这里。
      inFlight = false;
      if (submitBtn) submitBtn.disabled = false;
      submitNative();
    });
  }

  // 打开：ctx-bar 的「反馈」链接（以及任何显式带 data-feedback-open 的元素）
  document.addEventListener('click', function (ev) {
    // 修饰键 / 中键：尊重用户「新标签打开」的意图，不拦
    if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var target = ev.target;
    if (!target || typeof target.closest !== 'function') return;
    var trigger = target.closest('[data-feedback-open]');
    if (!trigger) return;
    ev.preventDefault();
    open(trigger);
  });

  // 关闭：× / 取消 / 点遮罩空白处
  overlay.addEventListener('click', function (ev) {
    var target = ev.target;
    var hitClose = target && typeof target.closest === 'function' &&
      target.closest('[data-feedback-close]');
    if (target === overlay || hitClose) close();
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !overlay.hidden) close();
  });

  form.addEventListener('submit', onSubmit);
})();
