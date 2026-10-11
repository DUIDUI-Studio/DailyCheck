/* static/ajax-form.js — 行内表单 AJAX 拦截器
 *
 * 目标：列表页里"删除 / 回退 / 启用停用"这类行内小按钮，点完不应该把整页
 * 重画、把滚动位置弹回顶部。改成 fetch + 局部 DOM 更新，浏览器不重渲染，
 * 滚动位置天然保留。
 *
 * 用法（模板侧）：
 *   - 给行内 <form> 加 data-ajax 属性
 *   - 给那一行（<tr> / <div class="card"> / 自定义节点）加 data-row 属性
 *   - 可选：data-ajax-confirm="提示文案"  — 提交前 confirm 弹窗
 *   - 可选：data-ajax-mode="remove|reload|hide"（默认 remove）
 *         remove — 移除最近的 [data-row] 祖先
 *         reload — location.reload()
 *         hide   — 隐藏（display:none），不删
 *   - 可选：data-ajax-success="window.foo"   自定义成功回调（接收 envelope）
 *
 * 服务端约定：
 *   - 响应必须带 X-Requested-With: fetch 头触发
 *   - 成功：服务端 after_request hook 把 302 redirect 转成
 *           {"ok": true, "location": "...", "message": "..."} 的 200 JSON
 *   - 失败：HTTP 4xx/5xx 时，JS 走 .catch 弹 alert
 *
 * 不影响：未带 data-ajax 的 form（含所有"新建 / 跳转"型大表单），
 * 一律按浏览器默认行为走。
 */
(function () {
  'use strict';

  // 重复绑定防御：把已处理的 form 标记一下，避免重复挂监听。
  function alreadyBound(form) {
    return form.__ajaxFormBound === true;
  }
  function markBound(form) {
    form.__ajaxFormBound = true;
  }

  // 找最近的 [data-row] 容器
  function findRow(form) {
    var el = form;
    while (el && el !== document.body) {
      if (el.hasAttribute && el.hasAttribute('data-row')) return el;
      el = el.parentElement;
    }
    return null;
  }

  // 显示成功提示。优先用服务端带回来的 message；fallback 到 form 自带的属性。
  function showSuccess(form, envelope) {
    var msg = (envelope && envelope.message) ||
              form.getAttribute('data-ajax-success-msg') || '';
    if (!msg) return;
    // 简单 top-of-page 浮窗（不引第三方依赖）
    var bar = document.createElement('div');
    bar.className = 'ajax-flash';
    bar.textContent = msg;
    bar.setAttribute('role', 'status');
    document.body.appendChild(bar);
    // 触发动画
    requestAnimationFrame(function () { bar.classList.add('is-show'); });
    setTimeout(function () {
      bar.classList.remove('is-show');
      setTimeout(function () { bar.remove(); }, 250);
    }, 2400);
  }

  // 自定义回调名解析：data-ajax-success="window.foo" → window.foo(envelope, form)
  function runCustom(name, envelope) {
    if (!name) return false;
    // 支持 "window.foo" 或 "foo"（默认挂 window 上）
    var fn;
    try {
      fn = (0, eval)(name);
    } catch (_) {
      return false;
    }
    if (typeof fn === 'function') {
      try { fn(envelope); return true; } catch (_) { return false; }
    }
    return false;
  }

  function applyLocalSideEffect(form, envelope) {
    // 自定义优先
    var custom = form.getAttribute('data-ajax-success');
    if (custom && runCustom(custom, envelope)) return;

    var mode = form.getAttribute('data-ajax-mode');
    var row = findRow(form);

    // 没标记 [data-row]：默认 reload-stay（保底，不会让用户错觉"成功但没变化"）
    if (!row) {
      reloadStay();
      return;
    }

    if (mode === 'reload' || mode === 'reload-stay') {
      reloadStay();
      return;
    }
    if (mode === 'hide') {
      row.style.display = 'none';
      return;
    }
    // 默认 remove
    row.remove();
  }

  // reload 但保持滚动位置：reload 前存 scrollY 到 sessionStorage，
  // pageshow 时还原一次后清掉。这是处理"启用/停用"等需要重新渲染
  // 状态文字的 toggle 类操作的体验（删除/回退类用 remove 即可，
  // 不需要 reload）。
  function reloadStay() {
    var key = 'ajax:scrollY:' + window.location.pathname + window.location.search;
    try { sessionStorage.setItem(key, String(window.scrollY)); } catch (_) {}
    window.location.reload();
  }

  function submitAjax(form) {
    // 构造 FormData：包含 csrf_token + 表单字段
    var fd = new FormData(form);
    var url = form.action || window.location.href;

    // 提交期间禁用按钮，避免重复点击
    var btns = form.querySelectorAll('button, input[type=submit]');
    btns.forEach(function (b) { b.disabled = true; });

    return fetch(url, {
      method: (form.method || 'POST').toUpperCase(),
      body: fd,
      credentials: 'same-origin',
      headers: {
        'X-Requested-With': 'fetch',
        'Accept': 'application/json'
      },
      redirect: 'follow'
    }).then(function (res) {
      // 服务端 after_request hook 会把 3xx 转成 200 JSON。
      // 但个别路由若 abort/raise 返回 HTML 错误页 → res.ok === false
      if (!res.ok) {
        throw new Error('HTTP ' + res.status);
      }
      return res.json().catch(function () {
        // 兜底：服务端万一返回非 JSON（例如仍是 redirect HTML），
        // 视为成功 + reload，避免给用户错觉"失败"。
        return { ok: true, _fallback: true };
      });
    }).then(function (envelope) {
      if (envelope && envelope.ok) {
        showSuccess(form, envelope);
        applyLocalSideEffect(form, envelope);
      } else {
        throw new Error((envelope && envelope.message) || '操作失败');
      }
    }).catch(function (err) {
      // 失败：恢复按钮 + 弹窗
      btns.forEach(function (b) { b.disabled = false; });
      window.alert(
        '操作失败：' + (err && err.message ? err.message : '请重试') +
        '\n（页面将刷新以保证数据一致）'
      );
      window.location.reload();
    });
  }

  function bind(form) {
    if (alreadyBound(form)) return;
    markBound(form);

    form.addEventListener('submit', function (ev) {
      // 已是 AJAX 提交：直接返回
      ev.preventDefault();

      // 已有 confirm（模板里用 onsubmit 的 confirm）— 不重复弹
      var confirmMsg = form.getAttribute('data-ajax-confirm');
      if (confirmMsg && !window.confirm(confirmMsg)) {
        return;
      }

      submitAjax(form);
    });
  }

  function scan(root) {
    var forms = (root || document).querySelectorAll('form[data-ajax]');
    for (var i = 0; i < forms.length; i++) bind(forms[i]);
  }

  // DOM ready 之后扫描
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { scan(); });
  } else {
    scan();
  }

  // 暴露给其他脚本（比如动态插入的 form / htmx 之类）
  window.AjaxForm = { scan: scan, bind: bind, submit: submitAjax };

  // pageshow：处理 reload-stay 还原滚动位置。
  // pageshow 在 BFCache 命中时也会触发，cache 状态下不需要还原（y=0）。
  window.addEventListener('pageshow', function (ev) {
    var key = 'ajax:scrollY:' + window.location.pathname + window.location.search;
    try {
      var y = sessionStorage.getItem(key);
      if (y && !ev.persisted) {
        // 延迟一帧再还原，确保新页面 DOM 已稳定
        requestAnimationFrame(function () {
          window.scrollTo(0, parseInt(y, 10) || 0);
        });
        sessionStorage.removeItem(key);
      }
    } catch (_) {}
  });
})();