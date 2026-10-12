/* static/users-admin.js — 账号管理 v2（feat/user-admin-v2）
 *
 * 行为：
 *  1) 工具栏即时筛选：搜索/角色/租户/启停/排序变化 → debounce 200ms 后
 *     以 query string 重新拉取 /users（带 ?format=json），把 tbody 替换。
 *     滚动位置通过 sessionStorage 保活（ajax-form.js 已在 pageshow 里还原）。
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
  // 工具栏筛选（debounce + JSON 拉取）
  // ────────────────────────────────────────────────────────────────

  var toolbar = document.getElementById('users-toolbar');
  var tbody = document.getElementById('users-tbody');
  var paginationEl = document.querySelector('.users-pagination');
  var debounceTimer = null;

  function currentFilters() {
    var fd = new FormData(toolbar);
    var params = new URLSearchParams();
    params.set('format', 'json');
    if (fd.get('q')) params.set('q', fd.get('q'));
    if (fd.get('role')) params.set('role', fd.get('role'));
    if (fd.get('tenant_id')) params.set('tenant_id', fd.get('tenant_id'));
    if (fd.get('is_active')) params.set('is_active', fd.get('is_active'));
    if (fd.get('sort')) params.set('sort', fd.get('sort'));
    return params;
  }

  function reloadList(page) {
    var params = currentFilters();
    if (page) params.set('page', String(page));
    // Save scroll position so the AJAX reload doesn't snap to top.
    try {
      sessionStorage.setItem(
        'ajax:scrollY:' + window.location.pathname + window.location.search,
        String(window.scrollY)
      );
    } catch (_) {}
    window.location.assign(window.location.pathname + '?' + params.toString());
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

  // 分页链接（用 data-page 触发）
  if (paginationEl) {
    paginationEl.addEventListener('click', function (ev) {
      var a = ev.target.closest('a.users-page-link');
      if (!a || a.classList.contains('is-disabled')) {
        ev.preventDefault();
        return;
      }
      ev.preventDefault();
      reloadList(parseInt(a.getAttribute('data-page'), 10) || 1);
    });
  }

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
        t.name + '（租户 ' + t.id + '）</option>';
    });
    html += '</select></label>';

    if (warehousesData.length > 0) {
      html += '<fieldset class="users-drawer-fieldset"><legend>仓库权限（可选）</legend>';
      html += '<table class="role-matrix"><tr><th>仓库</th><th>无</th><th>staff</th><th>manager</th><th>admin</th></tr>';
      warehousesData.forEach(function (w) {
        html += '<tr><td><strong>' + w.name + '</strong> <small class="muted">(' + w.code + ')</small></td>';
        html += '<td><input type="radio" name="wh_' + w.id + '" value="" checked /></td>';
        ['staff', 'manager', 'admin'].forEach(function (role) {
          html += '<td><input type="radio" name="wh_' + w.id + '" value="' + role + '"' +
            (role === 'manager' ? ' checked' : '') + ' /></td>';
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
    html += renderBulkBindForm();
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
      html += '<span class="role-row-label"><strong>' + w.name + '</strong> <small class="muted">(' + w.code + ')</small></span>';
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

  function renderBulkBindForm() {
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
    html += '<input type="hidden" name="user_ids" value="' + currentEditingUserId() + '" />';
    html += '<select name="warehouse_id" required>';
    html += '<option value="">选择仓库</option>';
    warehousesData.forEach(function (w) {
      html += '<option value="' + w.id + '">' + w.code + ' — ' + w.name + '</option>';
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

  var currentEditingUserId = function () {
    var row = drawerBody && drawerBody.querySelector('[data-editing-uid]');
    return row ? row.getAttribute('data-editing-uid') : '';
  };
  // 简单暴露给上面的 renderBulkBindForm 用
  // (ES5 闭包传值麻烦，直接读 attribute)
  function currentUserId() {
    var el = document.getElementById('current-user-id');
    return el ? parseInt(el.textContent, 10) : 0;
  }
  function currentEditingUid() {
    return drawerBody ? drawerBody.getAttribute('data-editing-uid') || '' : '';
  }
  // Stash editing uid on the body element so renderBulkBindForm can find it.
  var _origRenderEditDrawer = renderEditDrawer;
  renderEditDrawer = function (row) {
    _origRenderEditDrawer(row);
    if (drawerBody) drawerBody.setAttribute('data-editing-uid', row.getAttribute('data-user-id'));
  };

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
  function currentActorId() {
    var el = document.getElementById('current-user-id');
    return el ? parseInt(el.textContent, 10) : 0;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&', '<': '<', '>': '>', '"': '"', "'": '&#39;' }[c];
    });
  }
})();
