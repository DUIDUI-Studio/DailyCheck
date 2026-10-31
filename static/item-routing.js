/* Drafts deliberately live only in this page's memory; saved filters never use draft values. */
(() => {
  'use strict';
  const root = document.getElementById('routing-editor');
  const dataNode = document.getElementById('routing-data');
  if (!root || !dataNode) return;
  const data = JSON.parse(dataNode.textContent);
  const items = data.items;
  const byId = new Map(items.map(item => [item.id, item]));
  const drafts = new Map();
  const selected = new Set();
  const errors = new Map();
  const $ = id => document.getElementById(id);
  const fields = {q: $('filter-q'), category: $('filter-category'), warehouse: $('filter-warehouse'), status: $('filter-status'), dirty: $('filter-dirty')};
  const dialog = $('routing-preview');
  let page = 1, saving = false, errorsOnly = false, preview = [];
  const PAGE_SIZE = 50;
  const dcName = code => code === null ? '未配置' : `${data.warehouses.find(w => w.code === code)?.name || code}（${code}）`;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (text, handler, className) => {
    const element = node('button', text, className);
    element.type = 'button';
    element.addEventListener('click', handler);
    return element;
  };
  const routeState = item => item.saved.dc_code !== null ? 'configured' : item.diagnostic && !item.diagnostic.exclusive_only ? 'pending' : 'other';
  const stateLabel = item => ({configured: '已配置', pending: '待配置 · 尚未显式配置', other: '其他 / 不参与自动播种'})[routeState(item)];
  function notify(message) {
    $('routing-message').textContent = message;
    $('routing-message').hidden = !message;
  }
  function dcOptions(select, blankText, nullable = false) {
    select.append(new Option(blankText, ''));
    data.warehouses.forEach(w => select.append(new Option(dcName(w.code), w.code)));
    if (nullable) select.options[0].textContent = '未配置 / 清除路由';
  }
  data.categories.forEach(category => fields.category.append(new Option(category.name, category.code)));
  data.warehouses.forEach(w => fields.warehouse.append(new Option(dcName(w.code), w.code)));
  dcOptions($('bulk-dc'), '选择共同配送中心');
  function matches(item, omitStatus = false) {
    const q = fields.q.value.trim().toLocaleLowerCase();
    return (!q || `${item.name} ${item.canonical_sku}`.toLocaleLowerCase().includes(q))
      && (!fields.category.value || item.category_code === fields.category.value)
      && (!fields.warehouse.value || (fields.warehouse.value === '__none' ? item.saved.dc_code === null : item.saved.dc_code === fields.warehouse.value))
      && (omitStatus || !fields.status.value || routeState(item) === fields.status.value)
      && (!fields.dirty.checked || drafts.has(item.id))
      && (!errorsOnly || errors.has(item.id));
  }
  function filtered() { return items.filter(item => matches(item)); }
  function visible() { return filtered().slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE); }
  function setDraft(id, code) {
    const item = byId.get(id);
    if (code === item.saved.dc_code) {
      drafts.delete(id); errors.delete(id);
    } else {
      if (!drafts.has(id) && drafts.size >= 200) {
        notify('每批最多暂存 200 项，请先保存当前草稿。品项列表未被截断。');
        return false;
      }
      drafts.set(id, code);
      // Keep a server error until this item is undone or its baseline is explicitly accepted.
    }
    return true;
  }
  function desired(item) { return drafts.has(item.id) ? drafts.get(item.id) : item.saved.dc_code; }
  function updateSelection() {
    const current = visible();
    const checked = current.filter(item => selected.has(item.id)).length;
    $('select-page').checked = current.length > 0 && checked === current.length;
    $('select-page').indeterminate = checked > 0 && checked < current.length;
    $('select-page').disabled = current.length === 0 || saving;
    $('selection-count').textContent = `已选 ${selected.size} 项`;
    $('bulk-apply').disabled = selected.size === 0 || !$('bulk-dc').value || saving;
  }
  function renderItem(item) {
    const dirty = drafts.has(item.id), error = errors.get(item.id);
    const row = node('article', undefined, `routing-row${dirty ? ' is-dirty' : ''}${error ? ' has-error' : ''}`);
    row.dataset.id = item.id;
    row.setAttribute('aria-labelledby', `item-name-${item.id}`);
    const identity = node('div', undefined, 'routing-identity');
    const checkboxLabel = node('label');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(item.id);
    checkbox.setAttribute('aria-label', `选择 ${item.name}`);
    checkbox.addEventListener('change', () => { if (checkbox.checked) selected.add(item.id); else selected.delete(item.id); updateSelection(); });
    checkboxLabel.append(checkbox); identity.append(checkboxLabel);
    const description = node('div');
    const name = node('strong', item.name, 'routing-item-name'); name.id = `item-name-${item.id}`;
    description.append(name, node('small', item.canonical_sku, 'routing-sku'), node('small', item.category_name || item.category_code || '未分类', 'routing-category'));
    identity.append(description);
    const status = node('div');
    status.append(node('span', stateLabel(item), `routing-pill ${routeState(item) === 'pending' ? 'pending' : ''}`));
    if (item.status !== 'active') status.append(node('span', '品项已停用', 'routing-pill inactive'));
    const diagnostic = item.diagnostic;
    let diagnosticText = '候选诊断：停用品项未扫描';
    if (diagnostic) diagnosticText = diagnostic.exclusive_only ? '候选诊断：仅有门店专属绑定' : diagnostic.codes.length ? `候选诊断：${diagnostic.codes.length > 1 ? '多仓候选' : '唯一候选'} · ${diagnostic.codes.join(' / ')}` : '候选诊断：未检出合格 DC 绑定';
    status.append(node('small', diagnosticText, 'routing-diagnostic'));
    status.append(node('small', item.saved.updated_at ? `已保存于 ${item.saved.updated_at}` : '尚无显式路由', 'routing-time'));
    const editor = node('div');
    const label = node('label', '履约配送中心', 'routing-editor-label'); label.htmlFor = `dc-${item.id}`;
    const select = node('select'); select.id = `dc-${item.id}`; select.dataset.dcId = item.id;
    select.setAttribute('aria-label', `${item.name}的履约配送中心`);
    dcOptions(select, '', true); select.value = desired(item) || '';
    select.title = dcName(desired(item));
    select.addEventListener('change', () => {
      setDraft(item.id, select.value || null);
      render();
      document.getElementById(`dc-${item.id}`)?.focus({preventScroll: true});
    });
    editor.append(label, select);
    // Native option popups can truncate long text, so the selected value is always fully readable here.
    editor.append(node('small', `当前选择：${dcName(desired(item))}`, 'routing-diagnostic'));
    if (dirty) editor.append(node('p', `未保存：${dcName(item.saved.dc_code)} → ${drafts.get(item.id) === null ? '清除路由' : dcName(drafts.get(item.id))}`, 'routing-diff'));
    const actions = node('div', undefined, 'routing-row-actions');
    if (dirty) actions.append(button('撤销此项', () => { drafts.delete(item.id); errors.delete(item.id); render(); }));
    if (desired(item) !== null) actions.append(button('暂存清除', () => { setDraft(item.id, null); render(); }));
    if (error) {
      editor.append(node('p', error.message, 'routing-row-error'));
      if (error.current) {
        editor.append(node('small', `服务器最新值：${dcName(error.current.dc_code)} · ${error.current.updated_at || '无路由'}`, 'routing-row-error'));
        actions.append(button('确认最新基线，保留草稿', () => {
          if (!window.confirm(`已核对 ${item.name} 的最新值：${dcName(error.current.dc_code)}。保留当前草稿，下一次保存将以此为基线？`)) return;
          const target = desired(item);
          item.saved = {...error.current}; errors.delete(item.id);
          setDraft(item.id, target); render();
        }));
      }
    }
    editor.append(actions); row.append(identity, status, editor);
    return row;
  }
  function render() {
    const results = filtered();
    const pages = Math.max(1, Math.ceil(results.length / PAGE_SIZE));
    page = Math.min(page, pages);
    const current = visible();
    $('routing-list').replaceChildren(...current.map(renderItem));
    $('routing-empty').hidden = results.length > 0;
    $('routing-list').hidden = results.length === 0;
    $('routing-result').textContent = `${results.length} 项符合 · 全部 ${items.length} 项（含停用品项）`;
    $('page-label').textContent = `${page} / ${pages}`;
    $('page-prev').disabled = page === 1 || saving; $('page-next').disabled = page === pages || saving;
    const other = items.filter(item => matches(item, true));
    $('count-all').textContent = other.length;
    $('count-pending').textContent = other.filter(item => routeState(item) === 'pending').length;
    $('count-configured').textContent = other.filter(item => routeState(item) === 'configured').length;
    root.querySelectorAll('[data-status]').forEach(b => { b.classList.toggle('is-current', b.dataset.status === fields.status.value); b.setAttribute('aria-pressed', String(b.dataset.status === fields.status.value)); });
    const shown = new Set(current.map(item => item.id));
    const hidden = [...drafts.keys()].filter(id => !shown.has(id)).length;
    $('draft-count').textContent = `已修改 ${drafts.size} 项`;
    $('hidden-count').textContent = `当前页外 ${hidden} 项 · 每批最多 200 项`;
    $('save-count').textContent = drafts.size;
    $('draft-discard').disabled = !drafts.size || saving;
    $('draft-preview').disabled = !drafts.size || saving;
    $('filter-errors').hidden = errors.size === 0;
    $('filter-errors').textContent = errorsOnly ? '显示全部品项' : `查看错误项 ${errors.size}`;
    updateSelection();
  }
  function filterChanged() { page = 1; selected.clear(); render(); }
  function clearFilters() {
    Object.entries(fields).forEach(([key, field]) => { if (key === 'dirty') field.checked = false; else field.value = ''; });
    errorsOnly = false; filterChanged();
  }
  Object.entries(fields).forEach(([key, field]) => field.addEventListener(key === 'q' ? 'input' : 'change', filterChanged));
  root.querySelectorAll('[data-status]').forEach(b => b.addEventListener('click', () => { fields.status.value = b.dataset.status; filterChanged(); }));
  $('filter-clear').addEventListener('click', clearFilters); $('empty-clear').addEventListener('click', clearFilters);
  $('filter-errors').addEventListener('click', () => { errorsOnly = !errorsOnly; fields.q.value = ''; fields.category.value = ''; fields.warehouse.value = ''; fields.status.value = ''; fields.dirty.checked = false; filterChanged(); });
  $('select-page').addEventListener('change', event => { visible().forEach(item => { if (event.target.checked) selected.add(item.id); else selected.delete(item.id); }); render(); });
  $('bulk-dc').addEventListener('change', updateSelection);
  $('bulk-apply').addEventListener('click', () => {
    const code = $('bulk-dc').value;
    const planned = new Map(drafts);
    selected.forEach(id => { if (byId.get(id).saved.dc_code === code) planned.delete(id); else planned.set(id, code); });
    if (planned.size > 200) { notify('本次统一暂存会超过 200 项上限，请先保存或缩小选择。'); return; }
    selected.forEach(id => { if (byId.get(id).saved.dc_code === code) { drafts.delete(id); errors.delete(id); } });
    selected.forEach(id => setDraft(id, code)); render();
  });
  for (const [id, delta] of [['page-prev', -1], ['page-next', 1]]) $(id).addEventListener('click', () => { page += delta; selected.clear(); render(); $('routing-list').scrollIntoView({block: 'start'}); });
  $('draft-discard').addEventListener('click', () => { if (window.confirm(`放弃全部 ${drafts.size} 项未保存修改？`)) { drafts.clear(); errors.clear(); selected.clear(); notify('全部草稿已放弃。'); render(); } });
  $('draft-preview').addEventListener('click', () => {
    preview = [...drafts].map(([id, dc]) => ({canonical_id: id, operation: dc === null ? 'delete' : 'set', dc_code: dc, baseline: {...byId.get(id).saved}}));
    $('preview-count').textContent = `共 ${preview.length} 项，包括当前页外草稿。确认后作为一个批次保存。`;
    $('preview-warning').hidden = !preview.some(change => change.operation === 'delete');
    $('preview-list').replaceChildren(...preview.map(change => {
      const item = byId.get(change.canonical_id), li = node('li');
      li.append(node('strong', item.name), node('small', item.canonical_sku), node('div', `${dcName(change.baseline.dc_code)} → ${change.dc_code === null ? '清除路由' : dcName(change.dc_code)}`)); return li;
    }));
    dialog.showModal();
  });
  $('preview-cancel').addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  function verified(body, submitted) {
    if (!body || body.ok !== true || !Array.isArray(body.items) || body.items.length !== submitted.length || typeof body.batch_id !== 'string') return false;
    const result = new Map(body.items.map(item => [item.canonical_id, item]));
    return result.size === submitted.length && submitted.every(change => {
      const saved = result.get(change.canonical_id);
      return saved && saved.dc_code === change.dc_code && (saved.dc_code === null ? saved.updated_at === null : typeof saved.updated_at === 'string' && saved.updated_at.length > 0);
    });
  }
  $('preview-save').addEventListener('click', async () => {
    if (saving || !preview.length) return;
    const submitted = preview.map(change => ({...change, baseline: {...change.baseline}}));
    saving = true; $('routing-controls').disabled = true;
    $('preview-save').disabled = true; $('preview-cancel').disabled = true;
    $('preview-save').textContent = '正在保存…'; render();
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(root.dataset.batchUrl, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: {'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': $('routing-csrf').value},
        body: JSON.stringify({changes: submitted})
      });
      if (response.redirected || response.status === 401 || response.status === 403) {
        notify('登录或权限已失效，未确认保存结果。草稿已保留；请在其他标签页重新登录后安全重试。'); return;
      }
      if (!response.headers.get('Content-Type')?.includes('application/json')) {
        notify('服务器未返回可验证的保存结果（可能为会话或 CSRF 失效）。草稿保留，请核对会话后安全重试。'); return;
      }
      const body = await response.json();
      if (!response.ok || body.ok !== true) {
        Object.entries(body.errors || {}).forEach(([id, error]) => { if (byId.has(Number(id))) errors.set(Number(id), error); });
        notify(`${body.message || '保存未成功，请核对会话或服务器状态。'} 所有草稿保留。`); return;
      }
      if (!verified(body, submitted)) {
        notify('收到的结果无法逐项核验，保存结果未知。草稿保留，可安全重试：已达目标值的项目会跳过，其他变化会进行冲突检查。'); return;
      }
      body.items.forEach(saved => { byId.get(saved.canonical_id).saved = {dc_code: saved.dc_code, updated_at: saved.updated_at}; drafts.delete(saved.canonical_id); errors.delete(saved.canonical_id); });
      selected.clear();
      notify(`已确认保存 ${submitted.length} 项（实际更新 ${body.updated_count} 项）。${body.audit_recorded ? '' : `仓内审计未完成，路由已提交；追踪批次 ${body.batch_id}。`}`);
    } catch (error) {
      notify('网络中断或请求超时，保存结果未知，草稿已保留。可安全重试同一批次：已达目标值的项目会跳过，其他变化会进行冲突检查。');
    } finally {
      clearTimeout(timeout); saving = false; $('routing-controls').disabled = false;
      $('preview-save').disabled = false; $('preview-cancel').disabled = false;
      $('preview-save').textContent = '确认统一保存'; dialog.close(); preview = []; render();
    }
  });
  window.addEventListener('beforeunload', event => { if (drafts.size) { event.preventDefault(); event.returnValue = ''; } });
  const nav = document.querySelector('.mobile-nav');
  function navHeight() { root.style.setProperty('--routing-nav-height', `${nav && getComputedStyle(nav).display !== 'none' ? nav.getBoundingClientRect().height : 0}px`); }
  if (nav && window.ResizeObserver) new ResizeObserver(navHeight).observe(nav);
  window.addEventListener('resize', navHeight); navHeight(); render();
})();
