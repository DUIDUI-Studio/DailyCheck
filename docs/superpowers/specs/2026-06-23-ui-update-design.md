# UI Update — 2026-06-23

5 项独立 UI 调整,纯前端,后端零改动。

## 范围

| # | 改动 | 涉及文件 |
|---|---|---|
| A | 产品配方卡底部显示备注 | `templates/production/session.html`, `static/style.css` |
| B | 生产提交按钮高度加倍 | `static/style.css` |
| C | 日期筛选按钮样式 → qty-preset | `static/style.css` |
| D | 出/入/盘点品项卡 → 3 排垂直堆叠 | `static/style.css`(全局 `.entry-row`) |
| E | 库存查阅页 chip 改客户端筛选 | `templates/inventory.html`, `static/style.css` |

## 设计

### A. 产品配方卡底部显示备注

`templates/production/session.html` 在 `.recipe-pane` 的 `<ul class="recipe-list">…</ul>` 之后追加:

```html
{% if chosen['note'] %}
  <div class="recipe-note">
    <span class="recipe-note-label">备注</span>
    <p>{{ chosen['note'] }}</p>
  </div>
{% endif %}
```

`static/style.css` 新增:

```css
.recipe-note {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed var(--border);
}
.recipe-note-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.recipe-note p {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
```

- 数据源:`chosen['note']`(产品表 `note` 字段,已在表单中保存)
- 无备注的产品不显示该区块
- `white-space: pre-wrap` 保留换行

### B. 生产提交按钮高度加倍

基线 `button` 高度 ≈ 42px(padding 11px + 字号 13px + 边框)。加倍 ≈ 84px。

`static/style.css` 新增精准规则,只命中 `#submit-btn`:

```css
.session-form #submit-btn {
  padding: 22px 24px;
  min-height: 84px;
  font-size: 16px;
}
```

### C. 日期筛选按钮样式 → qty-preset

修改 `static/style.css` 中的 `.filter-chip` 块,与 `.qty-preset` 视觉一致:

- padding: `6px 12px`
- font-size: `14px`(原 13)
- font-weight: `600`
- border-radius: `8px`(原 999)
- hover:border-color → `var(--brand-ink, #0f766e)`

激活态 `.filter-chip.is-active` 保持原配色不变(背景 `var(--brand-ink, #0f766e)`,文字 `#fff`)。

`.filter-chip input[type=radio]` 和 `.filter-chip input[type=date]` 在内部保持透明,与 `.qty-preset` 内嵌输入控件的视觉一致。

### D. 出/入/盘点品项卡 → 3 排垂直堆叠

修改 `static/style.css` 中的 `.entry-row`,从横向 grid `1fr auto auto` 改成纵向 grid `auto auto auto`:

```css
.entry-row {
  display: grid;
  grid-template-columns: 1fr;
  grid-template-rows: auto auto auto;
  align-items: stretch;
  gap: 8px 0;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: #fafbfd;
  min-height: 0;
}
```

并新增/调整:

```css
.entry-row .er-stock {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  font-size: 13px;
  color: var(--muted);
}
.entry-row .er-input {
  display: flex;
  align-items: center;
  gap: 8px;
}
.entry-row .er-input input { max-width: 200px; }
```

- 3 排垂直:品项名(`er-meta`) → 库存/安全库存(`er-stock`)→ 输入框(`er-input`)
- 卡片高度按内容自适应(原固定 3 栏栅格被打破,卡片不再强制等高)
- 影响范围:`outbound_session.html`、`restock_session.html`、`stocktake_session.html`、`adjustment_session.html` 全部使用 `.entry-row`(用户已确认 adjustment 顺势一起变)

### E. 库存查阅页 → 客户端筛选 + 不隐藏其他 chip

`templates/inventory.html` 改动:

1. `.cat-bar` 内每个 `<a class="cat-chip">` 改成 `<button type="button" class="cat-chip">`,加 `data-cat` 属性,不再有 `href`
2. 每个 `.inv-card` 加 `data-cat="{{ i.category_name }}"`
3. 模板底部新增 JS:

```js
(function() {
  var bar = document.querySelector('.cat-bar');
  if (!bar) return;
  var chips = bar.querySelectorAll('.cat-chip');
  var cards = document.querySelectorAll('.inv-card');
  function apply(cat) {
    chips.forEach(function(c) {
      var on = c.getAttribute('data-cat') === cat;
      c.classList.toggle('is-active', on);
      c.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    cards.forEach(function(card) {
      var match = (cat === '__all') || (card.getAttribute('data-cat') === cat);
      card.classList.toggle('is-hidden', !match);
    });
    if (cat !== '__all') {
      var first = document.querySelector('.inv-card[data-cat="' + cat + '"]');
      if (first) first.scrollIntoView({block:'start', behavior:'smooth'});
    }
  }
  chips.forEach(function(c) {
    c.addEventListener('click', function() { apply(c.getAttribute('data-cat')); });
  });
})();
```

`static/style.css` 新增:

```css
.inv-card.is-hidden { display: none; }
```

- chip 永远可见,只是 `is-active` 高亮跟随切换
- 点过的 chip 高亮保持;再点 `全部` 才还原
- `?cat=xxx` URL 参数:后端仍能识别(模板顶部的 `cat` 变量照旧解析),但页面不再生成这种 URL(只是兼容性兜底,实际不依赖)

## 范围外

- 不动后端、不动 SQL、不动权限
- 不动其他页面(仅 5 项明确范围)
- 不动 `adjustment_session.html` 的字段(只随 CSS 改动布局)

## 测试

后端无改动,前端纯样式 + 一个小脚本。手测:

1. `/production/session/<product_id>` 创建带备注的产品 → 配方卡底部出现备注;不填则不显示
2. `/production/session/<product_id>` → 提交按钮明显变高,视觉对比其他按钮
3. `/production/runs` → 日期筛选 chip 视觉与产出量 chip 一致(尺寸/圆角/激活态)
4. `/outbound`、`/restock`、`/stocktake` → 每张卡片名/库存行/输入行三排,卡片高度按内容;切换品类 chip 只显示对应卡片
5. `/inventory` → 点击品类 chip 不再跳转;切换 chip 只前端隐藏/显示卡片,其他 chip 始终可见

## 风险

- `.entry-row` 是全局类,D 项的 CSS 改动同时影响 `adjustment_session.html`(已与用户确认顺势改)。如该页有特殊依赖旧栅格的脚本/样式需在实施时复检一次。
- E 项把 chip 从 `<a>` 改成 `<button>`,搜索引擎或浏览器后退行为不再受影响(本来就是 session 内部导航)。