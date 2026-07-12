# 弹窗式出入库录入 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将出库/入库/盘点 session 页面的内联输入改为点击卡片弹出浮窗输入，后端零改动。

**Architecture:** 每个品项卡片移除 `.er-input` 内联输入区，改为可点击的纯信息卡。点击后弹出居中浮层（modal），内含单位选择 pill、数量输入、换算提示。确认后写入隐藏 input，卡片变淡绿色背景标记已填。最终「提交并出库」按钮一次性 POST 提交。

**Tech Stack:** Jinja2 + 原生 CSS + 原生 JS；pytest（仅回归，无新后端逻辑）。

---
## Global Constraints

- Python 3.10+ target，行长度 100，Ruff lint（E/F/W/I/B/UP，忽略 E501/B008）
- 不修改任何后端蓝图文件（`blueprints/*.py`）
- 不修改 `tests/`、`db/`、`blueprints/_helpers.py`
- 弹窗换算提示用 JS `Math.round(x*100)/100` 精度，仅展示用
- 三个 session 模板共享同一套 modal 结构，仅 name 前缀不同（`outbound_` / `restock_` / `actual_`）

---

## File Structure

修改：
- `static/style.css` — 追加 modal、card 状态、pill 样式（末尾追加，约 40 行）
- `templates/outbound_session.html` — 卡片去输入 + 弹窗 HTML + JS
- `templates/restock_session.html` — 同上（name 前缀 `restock_`）
- `templates/stocktake_session.html` — 同上（name 前缀 `actual_`）

---

### Task 1: CSS 追加 modal / card / pill 样式

**Files:**
- Modify: `static/style.css` 末尾追加样式

**Interfaces:**
- 提供 `.modal-overlay`、`.modal`、`.pill-group`、`.pill`、`.pill--active`、`.card--filled`、`.card-summary` 样式给三个 session 模板共用

- [ ] **Step 1: 追加 CSS**

在 `static/style.css` 末尾追加：

```css
/* Modal overlay + dialog */
.modal-overlay {
  position: fixed; inset: 0; z-index: 999;
  background: rgba(0,0,0,0.35);
  display: flex; align-items: center; justify-content: center;
}
.modal {
  background: #fff; border-radius: 16px;
  width: 360px; max-width: 90vw;
  padding: 20px; box-shadow: 0 8px 32px rgba(0,0,0,0.18);
  position: relative;
}
.modal h3 { margin: 0 0 4px; font-size: 16px; }
.modal .modal-stock { font-size: 13px; color: var(--muted); margin-bottom: 14px; }
.modal-close {
  position: absolute; top: 12px; right: 16px;
  background: none; border: none; font-size: 18px;
  color: var(--muted); cursor: pointer; padding: 4px;
}

/* Pill group — separate rounded pills */
.pill-group {
  display: flex; gap: 12px; margin-bottom: 14px;
}
.pill {
  border: 1.5px solid var(--border); border-radius: 999px;
  background: transparent; color: var(--muted);
  font-size: 13px; padding: 6px 18px; cursor: pointer;
}
.pill--active {
  border-color: var(--brand); background: var(--brand); color: #fff;
}

/* Filled card state */
.card--filled { background: #ecfdf5 !important; }
.card-summary {
  font-size: 11px; color: #0f766e; text-align: right;
  margin-top: 4px;
}
```

- [ ] **Step 2: 提交**

```bash
git add static/style.css
git commit -m "style(modal-input): 追加 modal / pill / card--filled 样式"
```

---

### Task 2: 三个 session 模板改为弹窗输入

**Files:**
- Modify: `templates/outbound_session.html`
- Modify: `templates/restock_session.html`
- Modify: `templates/stocktake_session.html`

**Interfaces:**
- 卡片 HTML 移除 `.er-input` 区段（含 seg + input + hidden + calc-hint），改为纯信息卡 + `onclick` 弹窗
- 弹窗 HTML 插在 `{% endblock %}` 前，每个页面一个
- JS 用 `dataset.prefix` 区分三个页面的 name 前缀
- 已填数据写入隐藏 input，和当前提交路径一致

- [ ] **Step 1: 修改 outbound_session.html**

把 `.entry-row` 内的 `.er-input` 区段（第 37-58 行）替换为：

```html
        <div class="er-input" style="display:none">
          <input type="hidden" name="outbound_{{ i.id }}_unit" value="base" />
        </div>
```

把 `.er-meta` + `.er-stock` 行的外层 `<div class="entry-row"` 加上 `onclick` 和 `data-*` 属性：

```html
      <div class="entry-row" data-cat="{{ i.category_name }}"
           onclick="openModal(this)"
           data-id="{{ i.id }}"
           data-name="{{ i.name }}"
           data-unit="{{ i.unit }}"
           data-stock="{{ i.quantity | fmt_qty }}"
           data-aux-unit="{{ i.aux_unit or '' }}"
           data-aux-rate="{{ i.aux_rate }}">
```

在 `{% endblock %}` 前追加弹窗 HTML：

```html
<!-- Modal -->
<div id="modal-overlay" class="modal-overlay" hidden onclick="closeModal(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <h3 id="modal-name"></h3>
    <p class="modal-stock" id="modal-stock"></p>

    <div class="pill-group" id="modal-pills">
      <button class="pill pill--active" data-u="base" id="pill-base"></button>
      <button class="pill" data-u="aux" id="pill-aux"></button>
    </div>

    <label style="font-size:13px;color:var(--muted);display:block;margin-bottom:4px">数量</label>
    <input id="modal-qty" type="number" step="0.01" min="0" inputmode="decimal"
           style="width:100%;padding:10px 12px;font-size:16px;border:1.5px solid var(--border);border-radius:10px;box-sizing:border-box" />
    <div id="modal-hint" class="calc-hint" hidden style="margin:8px 0 0"></div>

    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="btn-cancel" style="flex:1" onclick="closeModal()">取消</button>
      <button class="btn" style="flex:1" onclick="confirmModal()">确认</button>
    </div>
  </div>
</div>
```

把现有的两段 JS（cat-bar segment + unit segment）整体替换为一段 JS：

```html
<script>
  /* ---- category filter (unchanged) ---- */
  (function () {
    var bar = document.querySelector('[data-cat-bar]');
    if (!bar) return;
    var chips = document.querySelectorAll('.cat-chip');
    var rows = bar.querySelectorAll('.entry-row');
    function apply(cat) {
      for (var i = 0; i < chips.length; i++) {
        var on = chips[i].getAttribute('data-cat') === cat;
        chips[i].classList.toggle('is-active', on);
        chips[i].setAttribute('aria-selected', on ? 'true' : 'false');
      }
      var first = null;
      for (var j = 0; j < rows.length; j++) {
        var row = rows[j];
        var match = (cat === '__all') || (row.getAttribute('data-cat') === cat);
        row.classList.toggle('is-hidden', !match);
        if (match && first === null) first = row;
      }
      if (cat !== '__all' && first) first.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    for (var k = 0; k < chips.length; k++) {
      chips[k].addEventListener('click', (function (c) { return function () { apply(c); }; })(chips[k].getAttribute('data-cat')));
    }
  })();

  /* ---- modal input ---- */
  var PREFIX = 'outbound_';
  var modalOverlay = document.getElementById('modal-overlay');
  var modalName = document.getElementById('modal-name');
  var modalStock = document.getElementById('modal-stock');
  var pillBase = document.getElementById('pill-base');
  var pillAux = document.getElementById('pill-aux');
  var modalQty = document.getElementById('modal-qty');
  var modalHint = document.getElementById('modal-hint');

  var _currentId = null, _currentUnit = null, _currentAuxUnit = null, _currentAuxRate = 0;

  function openModal(el) {
    _currentId = el.getAttribute('data-id');
    var name = el.getAttribute('data-name');
    var unit = el.getAttribute('data-unit');
    var stock = el.getAttribute('data-stock');
    var auxUnit = el.getAttribute('data-aux-unit');
    var auxRate = parseFloat(el.getAttribute('data-aux-rate') || '0');

    _currentUnit = unit;
    _currentAuxUnit = auxUnit;
    _currentAuxRate = auxRate;

    modalName.textContent = name;
    modalStock.textContent = '当前库存: ' + stock + ' ' + unit;

    pillBase.textContent = unit;
    if (auxUnit && auxRate > 0) {
      pillAux.textContent = auxUnit;
      pillAux.style.display = '';
    } else {
      pillAux.style.display = 'none';
    }

    // restore from hidden input
    var hidden = el.querySelector('input[type=hidden]');
    var savedUnit = hidden ? hidden.value : 'base';
    setActivePill(savedUnit);

    var savedQty = el.getAttribute('data-saved-qty') || '';
    modalQty.value = savedQty;
    updateModalHint();

    modalOverlay.removeAttribute('hidden');
  }

  function closeModal(e) {
    if (e && e.target !== modalOverlay) return;
    modalOverlay.setAttribute('hidden', '');
  }

  function setActivePill(u) {
    var btns = document.querySelectorAll('#modal-pills .pill');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('pill--active', btns[i].getAttribute('data-u') === u);
    }
  }

  document.getElementById('modal-pills').addEventListener('click', function (e) {
    var btn = e.target.closest('.pill');
    if (!btn || btn.style.display === 'none') return;
    setActivePill(btn.getAttribute('data-u'));
    updateModalHint();
  });

  modalQty.addEventListener('input', updateModalHint);

  function updateModalHint() {
    var v = parseFloat(modalQty.value || '0');
    var activePill = document.querySelector('#modal-pills .pill--active');
    if (!activePill) return;
    var u = activePill.getAttribute('data-u');

    if (isNaN(v) || v === 0) { modalHint.setAttribute('hidden', ''); return; }

    if (u === 'aux' && _currentAuxRate > 0) {
      var base = Math.round((v / _currentAuxRate) * 100) / 100;
      modalHint.textContent = v + ' ' + _currentAuxUnit + ' \u2248 ' + base + ' ' + _currentUnit;
      modalHint.removeAttribute('hidden');
    } else {
      modalHint.setAttribute('hidden', '');
    }
  }

  function confirmModal() {
    var v = modalQty.value.trim();
    var activePill = document.querySelector('#modal-pills .pill--active');
    if (!activePill) return;
    var u = activePill.getAttribute('data-u');

    // Find the row
    var row = document.querySelector('.entry-row[data-id="' + _currentId + '"]');
    if (!row) return;

    // Hidden input
    var hidden = row.querySelector('input[type=hidden]');
    if (!hidden) {
      hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = PREFIX + _currentId + '_unit';
      row.querySelector('.er-input').appendChild(hidden);
    }
    hidden.value = u;

    // Store the qty
    var qtyInput = row.querySelector('input[name="' + PREFIX + _currentId + '"]');
    if (!qtyInput) {
      qtyInput = document.createElement('input');
      qtyInput.type = 'hidden';
      qtyInput.name = PREFIX + _currentId;
      row.querySelector('.er-input').appendChild(qtyInput);
    }
    qtyInput.value = v;

    row.setAttribute('data-saved-qty', v);

    // Update card visual
    var summary = row.querySelector('.card-summary');
    if (v && parseFloat(v) > 0) {
      row.classList.add('card--filled');
      if (!summary) {
        summary = document.createElement('div');
        summary.className = 'card-summary';
        row.appendChild(summary);
      }
      summary.textContent = '已填 ' + v + ' ' + (u === 'aux' ? _currentAuxUnit : _currentUnit);
    } else {
      row.classList.remove('card--filled');
      if (summary) summary.textContent = '';
    }

    closeModal();
  }
</script>
```

- [ ] **Step 2: 同步修改 restock_session.html**

outbound_session.html 的修改逐项套用到 restock_session.html：
- `PREFIX = 'restock_'`
- hidden input name: `restock_{{ i.id }}_unit`
- form action: `restock_submit`
- openModal onclick + data-* 属性同上

- [ ] **Step 3: 同步修改 stocktake_session.html**

套用同样修改，但：
- `PREFIX = 'actual_'`
- hidden input name: `actual_{{ i.id }}_unit`
- form action: `stocktake_submit`

注：盘点无理由字段，表单只有 `actual_{{id}}` + `actual_{{id}}_unit` + `note`

- [ ] **Step 4: 手动目测验证**

```bash
# 服务已运行在 8080
# 1. 浏览器打开 /outbound/session — 卡片无输入框，可点击弹出弹窗
# 2. 选择辅单位 pill，输入数量，确认 — 卡片变绿，显示摘要
# 3. 再次点击卡片可修改
# 4. 重复 2-3 步多个品项，点「提交并出库」验证提交成功
# 5. 分别检查 /restock/session 和 /stocktake/session
```

- [ ] **Step 5: Lint 检查**

```bash
ruff check static/style.css
```

Expected: 无新增错误（仅有预存的解析报错）

- [ ] **Step 6: 提交**

```bash
git add templates/outbound_session.html templates/restock_session.html templates/stocktake_session.html
git commit -m "feat(modal-input): 出入库/盘点卡片改为点击弹窗输入"
```
