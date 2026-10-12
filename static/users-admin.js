/* static/users-admin.js — 账号管理 v2（feat/user-admin-v2）
 *
 * 行为：
 *  1) 工具栏即时筛选：搜索/角色/租户/启停/排序变化 → debounce 200ms 后
 *     fetch 带筛选参数的 /users，用返回的整页 HTML 替换 tbody + 分页控件
 *     （不整页导航；地址栏用 replaceState 同步）。
 *     表格标记仍由 Jinja 模板渲染，JS 不重复实现一遍行渲染。
 *  2) 行点击 → 抽屉打开，渲染该用户的「编辑」面板（含重置密码 / 切换
 *     平台管理员 / 切换启停 / 仓库角色 / 删除）。
 *  3) "新建账号" 按钮 → 抽屉打开，渲染「创建」面板（含仓库权限矩阵）。
 *
 * 设计原则：
 *  - 零依赖（不引入 fetch 包装库、不引入 htmx）。
 *  - 与 ajax-form.js 共存：抽屉内表单提交走整页刷新（PRG），列表里
 *    残余的 data-ajax 行内表单继续走 reload-stay。
 *  - 无 emoji（图标都是 inline SVG）。
 *  - 全部用 var/function，不引 ES6 特性，最大兼容旧浏览器（虽然项目
 *    已经放弃了 IE，但保持一致性）。
 */
(function () {
  'use strict';

  var SEARCH_DEBOUNCE_MS = 200;

  // ────────────────────────────────────────────────────────────────
  // 工具栏筛选（debounce + fetch 局部刷新）
  // ────────────────────────────────────────────────────────────────

  var toolbar = document.getElementById('users-toolbar');
  var tbody = document.getElementById('users-tbody');
  var debounceTimer = null;

  function currentFilters() {
    // 以地址栏现有参数为基底，只覆盖工具栏管的字段 —— 这样 page_size 等
    // 不在工具栏里的参数不会在第一次筛选后被静默丢掉。
    var params = new URLSearchParams(window.location.search);
    params.delete('format');  // JSON 开关永不进地址栏 / 请求 URL
    if (!toolbar) return params;  // 只有分页条没有工具栏的页面（防御性）
    var fd = new FormData(toolbar);
    ['q', 'role', 'tenant_id', 'is_active', 'sort'].forEach(function (name) {
      var value = fd.get(name);
      if (value) params.set(name, value);
      else params.delete(name);
    });
    return params;
  }

  // 目标 URL 只带筛选参数，**不带** format=json —— 这个 URL 会被写进地址栏，
  // 用户刷新/分享/后退拿到的必须是 HTML 页。
  function listUrl(page) {
    var params = currentFilters();
    if (page) params.set('page', String(page));
    var qs = params.toString();
    return window.location.pathname + (qs ? '?' + qs : '');
  }

  // 局部刷新：取整页 HTML（服务端模板是行标记 / 分页标记的唯一来源，
  // 避免在 JS 里重写一遍表格），只替换 tbody 与分页控件。
  //
  // 关键：绝不能 window.location.assign(url) —— 早期版本把 format=json 拼进
  // 导航 URL，点一下筛选/翻页整页就被裸 JSON 顶掉了。
  //
  // 只认最后一次请求：change 分支不 debounce，连点两个筛选会有两个请求在飞，
  // 先回来的旧响应不能覆盖后发的结果（连带把地址栏写回旧参数）。
  var refreshSeq = 0;

  function reloadList(page) {
    var url = listUrl(page);
    var seq = ++refreshSeq;
    window.fetch(url, { credentials: 'same-origin' })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.text();
      })
      .then(function (html) {
        if (seq !== refreshSeq) return;  // 已有更新的请求，丢弃本次
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var freshTbody = doc.getElementById('users-tbody');
        if (!tbody || !freshTbody) throw new Error('users-tbody not found');
        tbody.innerHTML = freshTbody.innerHTML;
        swapPagination(doc.querySelector('.users-pagination'));
        if (window.history && window.history.replaceState) {
          window.history.replaceState(null, '', url);
        }
      })
      .catch(function () {
        if (seq !== refreshSeq) return;
        // 退化路径：整页导航仍然可用（服务端渲染会带上同一组筛选参数）。
        window.location.assign(url);
      });
  }

  // 分页控件在结果集变化时可能整块出现/消失，直接换掉节点。
  // 点击事件用 document 级委托（见下），所以换节点不会丢监听。
  function swapPagination(freshNav) {
    var cur = document.querySelector('.users-pagination');
    if (freshNav) {
      if (cur) {
        cur.outerHTML = freshNav.outerHTML;
      } else {
        // 锚点必须是 .users-table-wrap（与 SSR 里分页条的位置一致），
        // **不能**用 tbody.insertAdjacentHTML —— table 上下文解析会触发
        // foster parenting，分页条被塞进表格内部的匿名单元格里。
        var anchor = document.querySelector('.users-table-wrap') || tbody;
        if (anchor) anchor.insertAdjacentHTML('afterend', freshNav.outerHTML);
      }
    } else if (cur && cur.parentNode) {
      cur.parentNode.removeChild(cur);
    }
  }

  function debounceReload() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(function () { reloadList(1); }, SEARCH_DEBOUNCE_MS);
  }

  if (toolbar) {
    toolbar.addEventListener('input', function (ev) {
      if (ev.target.name === 'q') debounceReload();
    });
    toolbar.addEventListener('change', function (ev) {
      // role / tenant_id / is_active / sort → 立即刷新
      if (['role', 'tenant_id', 'is_active', 'sort'].indexOf(ev.target.name) >= 0) {
        reloadList(1);
      }
    });
  }

  // 分页链接（用 data-page 触发）。分页节点会被 swapPagination 整块替换，
  // 所以用 document 级委托，而不是把监听绑在节点本身上。
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest('a.users-page-link');
    if (!a) return;
    ev.preventDefault();
    if (a.classList.contains('is-disabled')) return;
    reloadList(parseInt(a.getAttribute('data-page'), 10) || 1);
  });

  // ────────────────────────────────────────────────────────────────
  // 抽屉组件
  // ────────────────────────────────────────────────────────────────

  var drawer = document.getElementById('users-drawer');
  var drawerBody = document.getElementById('users-drawer-body');
  var drawerTitle = document.getElementById('users-drawer-title');

  function openDrawer() {
    if (!drawer) return;
    drawer.setAttribute('aria-hidden', 'false');
    drawer.classList.add('is-open');
    document.body.style.overflow = 'hidden';
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.setAttribute('aria-hidden', 'true');
    drawer.classList.remove('is-open');
    document.body.style.overflow = '';
    if (drawerBody) drawerBody.innerHTML = '';
  }

  // 关闭按钮 / 背景点击 / ESC
  if (drawer) {
    drawer.addEventListener('click', function (ev) {
      var t = ev.target.closest('[data-action]');
      if (!t) return;
      var act = t.getAttribute('data-action');
      if (act === 'close-drawer') {
        ev.preventDefault();
        closeDrawer();
      }
    });
  }
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && drawer && drawer.classList.contains('is-open')) {
      closeDrawer();
    }
  });

  // ────────────────────────────────────────────────────────────────
  // 渲染：创建账号抽屉内容
  // ────────────────────────────────────────────────────────────────

  function renderCreateDrawer() {
    if (!drawerTitle || !drawerBody) return;
    drawerTitle.textContent = '新建账号';

    // 把当前 warehouses + tenants 列表从 data-attribute 注入（由模板渲染时绑定）。
    var warehousesData = [];
    var tenantsData = [];
    try {
      warehousesData = JSON.parse(
        document.getElementById('warehouses-data').textContent
      );
      tenantsData = JSON.parse(
        document.getElementById('tenants-data').textContent
      );
    } catch (_) {}

    var html = '';
    html += '<form method="post" action="/users/create" class="users-drawer-form">';
    html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
    html += '<label>账号<input name="username" required autocomplete="off" /></label>';
    html += '<label>密码<input name="password" type="password" required autocomplete="new-password" /></label>';
    html += '<label class="users-drawer-checkbox"><input type="checkbox" name="is_admin" value="1" />设为平台管理员</label>';
    html += '<label>租户<select name="tenant_id">';
    tenantsData.forEach(function (t) {
      html += '<option value="' + t.id + '"' +
        (t.id === currentTenantId() ? ' selected' : '') + '>' +
        escapeHtml(t.name) + '（租户 ' + t.id + '）</option>';
    });
    html += '</select></label>';

    if (warehousesData.length > 0) {
      html += '<fieldset class="users-drawer-fieldset"><legend>仓库权限（可选）</legend>';
      html += '<table class="role-matrix"><tr><th>仓库</th><th>无</th><th>staff</th><th>manager</th><th>admin</th></tr>';
      warehousesData.forEach(function (w) {
        html += '<tr><td><strong>' + escapeHtml(w.name) + '</strong> <small class="muted">(' + escapeHtml(w.code) + ')</small></td>';
        // 新账号默认「无权限」（与 v1 矩阵一致）。同一个 radio group 里
        // 只能有一处 checked —— 早先这里还给 manager 也加了 checked，
        // 浏览器取文档序靠后的那个，于是"不动矩阵直接创建"会给新账号
        // 静默授上**每个仓库**的 manager。
        html += '<td><input type="radio" name="wh_' + w.id + '" value="" checked /></td>';
        ['staff', 'manager', 'admin'].forEach(function (role) {
          html += '<td><input type="radio" name="wh_' + w.id + '" value="' + role + '" /></td>';
        });
        html += '</tr>';
      });
      html += '</table></fieldset>';
    }

    html += '<div class="users-drawer-actions">';
    html += '<button type="button" class="btn" data-action="close-drawer">取消</button>';
    html += '<button type="submit" class="btn primary">创建</button>';
    html += '</div>';
    html += '</form>';

    drawerBody.innerHTML = html;
    openDrawer();
  }

  // ────────────────────────────────────────────────────────────────
  // 渲染：编辑账号抽屉内容
  // ────────────────────────────────────────────────────────────────

  function renderEditDrawer(row) {
    if (!drawerTitle || !drawerBody) return;
    var uid = row.getAttribute('data-user-id');
    var username = row.getAttribute('data-username');
    var isAdmin = row.getAttribute('data-is-admin') === '1';
    var isActive = row.getAttribute('data-is-active') === '1';
    var tenantId = row.getAttribute('data-tenant-id');
    var tenantName = row.getAttribute('data-tenant-name') || '';

    drawerTitle.textContent = '账号：' + username;

    var html = '';
    // 顶部信息卡（只读）
    html += '<div class="users-drawer-meta">';
    html += '<div><span class="muted small">租户</span> ' + escapeHtml(tenantName || '—') + '</div>';
    html += '<div><span class="muted small">用户 ID</span> ' + uid + '</div>';
    if (isAdmin) html += '<div><span class="tag admin">平台管理员</span></div>';
    html += '</div>';

    // 重置密码（折叠 details）
    html += '<details class="users-drawer-section" open>';
    html += '<summary>重置密码</summary>';
    html += '<form method="post" action="/users/' + uid + '/reset-password" class="users-drawer-inline-form" data-ajax data-ajax-mode="reload-stay">';
    html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
    html += '<input name="password" type="password" required placeholder="新密码" autocomplete="new-password" />';
    html += '<button type="submit" class="btn-sm btn-edit">保存</button>';
    html += '</form></details>';

    // 仓库角色管理（折叠 details）
    html += '<details class="users-drawer-section">';
    html += '<summary>仓库角色</summary>';
    html += renderBindingsForm(uid);
    html += '</details>';

    // 批量绑定（v2）
    html += '<details class="users-drawer-section">';
    html += '<summary>批量绑定（本用户 → 仓库）</summary>';
    html += renderBulkBindForm(uid);
    html += '</details>';

    // 底部危险操作：切换平台管理员 / 切换启停 / 删除
    html += '<div class="users-drawer-danger">';
    if (parseInt(uid, 10) !== currentUserId()) {
      html += '<form method="post" action="/users/' + uid + '/toggle-admin" style="display:inline" data-ajax data-ajax-mode="reload-stay" data-ajax-confirm="确认切换平台管理员状态?">';
      html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
      html += '<button type="submit" class="btn-sm">' + (isAdmin ? '取消管理员' : '设为管理员') + '</button>';
      html += '</form>';
      html += '<form method="post" action="/users/' + uid + '/toggle-active" style="display:inline" data-ajax data-ajax-mode="reload-stay" data-ajax-confirm="确认' + (isActive ? '停用' : '启用') + '此账号?">';
      html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
      html += '<button type="submit" class="btn-sm">' + (isActive ? '停用账号' : '启用账号') + '</button>';
      html += '</form>';
      html += '<form method="post" action="/users/' + uid + '/delete" style="display:inline" data-ajax data-ajax-confirm="确认永久删除 ' + escapeHtml(username) + '?此操作不可撤销。">';
      html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
      html += '<button type="submit" class="btn-sm btn-delete">删除账号</button>';
      html += '</form>';
    } else {
      html += '<p class="muted small">不能对自己的账号做这些操作</p>';
    }
    html += '</div>';

    drawerBody.innerHTML = html;
    // Re-bind ajax-form.js to the newly inserted forms (it scans on DOMContentLoaded,
    // so newly-injected forms wouldn't be intercepted otherwise).
    if (window.AjaxForm && typeof window.AjaxForm.scan === 'function') {
      window.AjaxForm.scan(drawerBody);
    }
    openDrawer();
  }

  function renderBindingsForm(uid) {
    // Pull from the row's data-* attributes (bindings data was injected
    // into the table server-side). For simplicity we re-query the DOM
    // here — keeps the JS self-contained without a second JSON call.
    var row = document.querySelector('tr.users-row[data-user-id="' + uid + '"]');
    var tags = row ? row.querySelectorAll('.users-roles .tag') : [];
    var bindings = [];
    tags.forEach(function (t) {
      var title = t.getAttribute('title') || '';
      var txt = t.textContent || '';
      // txt 形如 "wh_001:manager"
      var parts = txt.split(':');
      if (parts.length === 2) {
        bindings.push({ code: parts[0], role: parts[1], title: title });
      }
    });
    var warehousesData = [];
    try {
      warehousesData = JSON.parse(
        document.getElementById('warehouses-data').textContent
      );
    } catch (_) {}

    var html = '<div class="users-bindings">';
    warehousesData.forEach(function (w) {
      var current = null;
      bindings.forEach(function (b) {
        if (b.code === w.code) current = b.role;
      });
      html += '<div class="role-row" data-row>';
      html += '<span class="role-row-label"><strong>' + escapeHtml(w.name) + '</strong> <small class="muted">(' + escapeHtml(w.code) + ')</small></span>';
      if (current) {
        html += '<form method="post" action="/users/' + uid + '/change-role" class="role-row-form" data-ajax data-ajax-mode="reload-stay">';
        html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
        html += '<input type="hidden" name="warehouse_id" value="' + w.id + '" />';
        html += '<select name="role">';
        ['staff', 'manager', 'admin'].forEach(function (r) {
          html += '<option value="' + r + '"' + (r === current ? ' selected' : '') + '>' + r + '</option>';
        });
        html += '</select>';
        html += '<button type="submit" class="btn-sm btn-edit">保存</button></form>';
        html += '<form method="post" action="/users/' + uid + '/unbind" class="role-row-form" data-ajax data-ajax-confirm="确认解除 ' + escapeHtml(w.name) + ' 的权限?">';
        html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
        html += '<input type="hidden" name="warehouse_id" value="' + w.id + '" />';
        html += '<button type="submit" class="btn-sm btn-delete">解绑</button></form>';
      } else {
        html += '<form method="post" action="/users/' + uid + '/bind" class="role-row-form" data-ajax data-ajax-mode="reload-stay">';
        html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
        html += '<input type="hidden" name="warehouse_id" value="' + w.id + '" />';
        html += '<select name="role">';
        ['staff', 'manager', 'admin'].forEach(function (r) {
          html += '<option value="' + r + '"' + (r === 'manager' ? ' selected' : '') + '>' + r + '</option>';
        });
        html += '</select>';
        html += '<button type="submit" class="btn-sm btn-edit">绑定</button></form>';
      }
      html += '</div>';
    });
    html += '</div>';
    return html;
  }

  // uid 由 renderEditDrawer 显式传入。早期版本从 drawerBody 的
  // [data-editing-uid] DOM 属性里读，但那段 html 是在属性写入之前拼好的，
  // 结果是首次打开提交空 user_ids、第二次打开还会带上上一个用户的 id。
  function renderBulkBindForm(uid) {
    // 复用单仓库 + 角色选择 — 不在本抽屉内选多用户，多用户从列表里勾选
    // （v2 P1：列表加 checkbox 列后再扩展）。本期先用：单用户+单仓库+单角色。
    var warehousesData = [];
    try {
      warehousesData = JSON.parse(
        document.getElementById('warehouses-data').textContent
      );
    } catch (_) {}

    var html = '<p class="muted small">把当前用户绑定到一个仓库一个角色。如需多用户批量绑定，请用列表勾选（v2 P1）。</p>';
    html += '<form method="post" action="/users/bulk-bind" class="users-drawer-inline-form">';
    html += '<input type="hidden" name="csrf_token" value="' + csrfToken() + '">';
    html += '<input type="hidden" name="user_ids" value="' + escapeHtml(uid) + '" />';
    html += '<select name="warehouse_id" required>';
    html += '<option value="">选择仓库</option>';
    warehousesData.forEach(function (w) {
      html += '<option value="' + w.id + '">' + escapeHtml(w.code) + ' — ' + escapeHtml(w.name) + '</option>';
    });
    html += '</select>';
    html += '<select name="role">';
    ['staff', 'manager', 'admin'].forEach(function (r) {
      html += '<option value="' + r + '"' + (r === 'manager' ? ' selected' : '') + '>' + r + '</option>';
    });
    html += '</select>';
    html += '<button type="submit" class="btn-sm btn-edit">绑定</button>';
    html += '</form>';
    return html;
  }

  // ────────────────────────────────────────────────────────────────
  // 全局事件：行点击 + 新建按钮
  // ────────────────────────────────────────────────────────────────

  function currentUserId() {
    var el = document.getElementById('current-user-id');
    return el ? parseInt(el.textContent, 10) : 0;
  }

  if (tbody) {
    tbody.addEventListener('click', function (ev) {
      // Don't open drawer when clicking an inline form (links, buttons).
      if (ev.target.closest('form, button, a, input, select')) return;
      var row = ev.target.closest('tr.users-row');
      if (row) renderEditDrawer(row);
    });
  }

  document.addEventListener('click', function (ev) {
    var t = ev.target.closest('[data-action="open-create-drawer"]');
    if (t) {
      ev.preventDefault();
      renderCreateDrawer();
    }
  });

  // ────────────────────────────────────────────────────────────────
  // Helpers
  // ────────────────────────────────────────────────────────────────

  function csrfToken() {
    // Flask-WTF injects a global function csrf_token() in templates, but
    // inside inline JS we can't call it. Read from the meta tag we add in
    // base.html (or fall back to scanning the document).
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute('content') || '';
    // Last-resort: scan forms for an existing csrf input (the warehouse
    // create form has one). Cheaper than a round-trip to the server.
    var inp = document.querySelector('input[name="csrf_token"]');
    return inp ? inp.value : '';
  }
  function currentTenantId() {
    var el = document.getElementById('current-tenant-id');
    return el ? parseInt(el.textContent, 10) : 0;
  }
  // Escape a value before interpolating it into an innerHTML string.
  // The entity table MUST contain real entities ("&amp;" etc.) — writing
  // the bare characters here (as the first revision did) makes this a
  // no-op and turns any user/tenant/warehouse name containing a quote or
  // angle bracket into stored HTML injection.
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[c];
    });
  }
})();
