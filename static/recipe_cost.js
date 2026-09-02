(function() {
  'use strict';

  function round2(n) { return Math.round(n * 100) / 100; }
  function fmtPct(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return (x * 100).toFixed(1) + '%';
  }
  function fmtMoney(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return Number(x).toFixed(2);
  }

  function getVariant() {
    var tbody = document.getElementById('bom-body');
    if (!tbody) return 'recipe';
    var tbl = tbody.closest('table');
    var v = tbl && tbl.getAttribute('data-bom-variant');
    return v === 'ic' ? 'ic' : 'recipe';
  }

  function getSalePriceEl() {
    return document.getElementById('sale-price') || document.getElementById('bom-sale-price');
  }

  // 原料销售单价试算：ic 页面每行新增 .bom-sp-adj 输入，允许覆盖品项原销售单价；
  // recipe 页面沿用 .bom-temp-price 输入和 use-temp-prices 开关。
  // 返回 { purchase, selling(调整后), sellingOrig(原值), grams, uc, sp, unit }
  function lineItemCost(row, useTemp) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var itemSel = row.querySelector('.bom-item-sel');
    if (!itemSel) return {purchase: 0, selling: 0, sellingOrig: 0, grams: 0, uc: 0, sp: 0, unit: ''};
    var opt = itemSel.options[itemSel.selectedIndex];
    var uc = parseFloat(opt.getAttribute('data-uc') || '0');
    var sp = parseFloat(opt.getAttribute('data-sp') || '0');
    var gpu = parseFloat(opt.getAttribute('data-gram') || '0');
    var unit = opt.getAttribute('data-unit') || '';
    var qtyStock = gpu > 0 ? qty / gpu : qty;
    var costOrig = round2(qtyStock * sp);

    // 优先取页面上的调整值，无调整则回退到品项原销售单价
    var adjInput = row.querySelector('.bom-sp-adj');
    var adjSp = adjInput ? (parseFloat(adjInput.value) || sp) : sp;

    // recipe 页面的临时售价（优先级低于 ic 调整）——仅当 useTemp 开关打开时生效
    if (useTemp) {
      var tp = row.querySelector('.bom-temp-price');
      if (tp) adjSp = parseFloat(tp.value) || sp;
    }

    var costAdj = round2(qtyStock * adjSp);
    return {
      purchase: round2(qtyStock * uc),
      selling: costAdj,
      sellingOrig: costOrig,
      grams: qty,
      uc: uc, sp: sp, unit: unit,
    };
  }

  function lineIcCost(row, useTemp, icRecipes) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var sel = row.querySelector('.bom-ic-sel');
    if (!sel) return {purchase: 0, selling: 0, sellingOrig: 0, grams: 0};
    var icId = parseInt(sel.value);
    var ic = icRecipes[icId];
    if (!ic) return {purchase: 0, selling: 0, sellingOrig: 0, grams: 0};
    var grams = qty * (parseFloat(ic.output_qty) || 0);
    var cost = round2(qty * ic.cost_selling_per_unit);
    return {
      purchase: round2(qty * ic.cost_purchase_per_unit),
      selling: cost,
      sellingOrig: cost,
      grams: grams,
    };
  }

  function recomputeAll() {
    var toggle = document.getElementById('use-temp-prices');
    var useTemp = toggle ? toggle.checked : false;
    var variant = getVariant();
    var rows = document.querySelectorAll('#bom-body .bom-row');
    var totalP = 0, totalS = 0, totalSOrig = 0, totalGrams = 0;
    rows.forEach(function(row) {
      var delCb = row.querySelector('input[name="bom_delete"]');
      if (delCb && delCb.checked) {
        row.querySelector('.bom-cost-purchase').textContent = '0.00';
        row.querySelector('.bom-cost-selling').textContent = '0.00';
        return;
      }
      var st = row.getAttribute('data-source-type') || 'item';
      var cost;
      if (st === 'ic_recipe') {
        cost = lineIcCost(row, useTemp, window.__icRecipes || {});
      } else {
        cost = lineItemCost(row, useTemp);
      }
      totalP += cost.purchase;
      totalS += cost.selling;
      totalSOrig += cost.sellingOrig;
      totalGrams += cost.grams;

      row.querySelector('.bom-cost-purchase').textContent = cost.purchase.toFixed(2);
      row.querySelector('.bom-cost-selling').textContent = cost.selling.toFixed(2);

      if (variant === 'ic' && st === 'item') {
        var ucEl = row.querySelector('.bom-uc');
        var spEl = row.querySelector('.bom-sp');
        var unitEl = row.querySelector('.bom-unit-label');
        if (ucEl) ucEl.textContent = (cost.uc || 0).toFixed(3);
        if (spEl) spEl.textContent = (cost.sp || 0).toFixed(3);
        if (unitEl) unitEl.textContent = cost.grams > 0 ? '克' : (cost.unit || '—');
        // 同步调整后销售单价的高亮：与品项原值不同时标注
        var adjInp = row.querySelector('.bom-sp-adj');
        if (adjInp) {
          var origSp = parseFloat(adjInp.getAttribute('data-original-sp') || '0');
          adjInp.classList.toggle('is-adjusted', Math.abs(parseFloat(adjInp.value || 0) - origSp) > 0.005);
        }
      }

      var tpInput = row.querySelector('.bom-temp-price');
      if (tpInput) {
        if (useTemp && st === 'item') {
          tpInput.disabled = false;
        } else {
          tpInput.disabled = true;
          if (st === 'item') {
            var itemSel = row.querySelector('.bom-item-sel');
            if (itemSel) {
              var sp = parseFloat(itemSel.options[itemSel.selectedIndex].getAttribute('data-sp') || '0');
              tpInput.value = sp;
            }
          } else { tpInput.value = 0; }
        }
      }
    });

    var salePriceEl = getSalePriceEl();
    var salePrice = salePriceEl ? parseFloat(salePriceEl.value || '0') : 0;

    // --- 采购毛利（不变：售价−采购成本）---
    var purchaseMarginAmt = salePrice > 0 ? (salePrice - totalP) : null;
    var purchaseMarginPct = salePrice > 0 ? (salePrice - totalP) / salePrice : null;

    // --- 配方原值（使用品项默认销售单价）---
    var sellingMarginOrigAmt = salePrice > 0 ? (salePrice - totalSOrig) : null;
    var sellingMarginOrigPct = salePrice > 0 ? (salePrice - totalSOrig) / salePrice : null;
    var profitOrig = totalSOrig - totalP;
    var profitMarginOrig = totalSOrig > 0 ? profitOrig / totalSOrig : null;

    // --- 调整后（原料销售单价覆盖后）---
    var sellingMarginAmt = salePrice > 0 ? (salePrice - totalS) : null;
    var sellingMarginPct = salePrice > 0 ? (salePrice - totalS) / salePrice : null;
    var profit = totalS - totalP;
    var profitMargin = totalS > 0 ? profit / totalS : null;

    // 每份输出量（自动）
    var outQty = document.getElementById('output-qty');
    if (outQty) outQty.value = round2(totalGrams).toFixed(2);

    // --- footer ---
    document.getElementById('total-purchase').textContent = totalP.toFixed(2);
    document.getElementById('total-selling').textContent = totalS.toFixed(2);

    var fsp = document.getElementById('footer-sale-price');
    if (fsp) fsp.textContent = salePrice.toFixed(2);

    // 采购毛利（不受原料售价影响，固定）
    var fmpa = document.getElementById('footer-margin-purchase-amt');
    if (fmpa) fmpa.textContent = fmtMoney(purchaseMarginAmt);
    var fmpp = document.getElementById('footer-margin-purchase-pct');
    if (fmpp) fmpp.textContent = '毛利率：' + fmtPct(purchaseMarginPct) + '（不变）';

    // 销售毛利：对原值 + 调整后发生变化的项，展示对比
    var hasDelta = Math.abs(totalS - totalSOrig) > 0.005;
    var deltaS = round2(totalS - totalSOrig);

    var fmsa = document.getElementById('footer-margin-selling-amt');
    if (fmsa) {
      var smOrigLabel = hasDelta ? ' · 原 ¥' + fmtMoney(sellingMarginOrigAmt) : '';
      fmsa.textContent = fmtMoney(sellingMarginAmt) + smOrigLabel;
    }
    var fmsp = document.getElementById('footer-margin-selling-pct');
    if (fmsp) {
      var adjPct = fmtPct(sellingMarginPct);
      var origPct = hasDelta ? '（原 ' + fmtPct(sellingMarginOrigPct) + '）' : '';
      fmsp.textContent = '毛利率：' + adjPct + origPct;
    }

    // 净利润：同样展示原值对比
    var fp = document.getElementById('footer-profit');
    if (fp) {
      var profitOrigLabel = hasDelta ? ' · 原 ¥' + fmtMoney(profitOrig) : '';
      fp.textContent = profit.toFixed(2) + profitOrigLabel;
      fp.classList.toggle('is-neg', profit < 0);
    }
    var fpm = document.getElementById('footer-profit-margin');
    if (fpm) {
      var apm = fmtPct(profitMargin);
      var opm = hasDelta ? '（原 ' + fmtPct(profitMarginOrig) + '）' : '';
      fpm.textContent = apm + opm;
    }

    // 负利润提示
    var warn = document.getElementById('margin-warning');
    if (warn) warn.hidden = !(profit < 0);

    // 动态标注行内影响
    if (variant === 'ic' && hasDelta) {
      var dEl = document.getElementById('bom-s-delta');
      if (!dEl) {
        dEl = document.createElement('div');
        dEl.id = 'bom-s-delta';
        dEl.className = 'bom-delta-note';
        var tbl = document.querySelector('.bom-table');
        if (tbl && tbl.parentNode) tbl.parentNode.insertBefore(dEl, tbl.nextSibling);
      }
      dEl.textContent = '▸ 调整后销售价值 ¥' + totalS.toFixed(2)
        + '，较原值 ¥' + totalSOrig.toFixed(2)
        + ' 变动 ' + (deltaS > 0 ? '+' : '') + deltaS.toFixed(2);
      dEl.hidden = false;
    } else {
      var dEl2 = document.getElementById('bom-s-delta');
      if (dEl2) dEl2.hidden = true;
    }
  }

  // --- event bindings ---

  function bindItemSelChange(row) {
    var sel = row.querySelector('.bom-item-sel');
    if (!sel) return;
    sel.addEventListener('change', function() {
      // 品项切换时：把调整售价重置为新品项的默认销售单价
      var opt = sel.options[sel.selectedIndex];
      var sp = parseFloat(opt.getAttribute('data-sp') || '0');
      var adjInp = row.querySelector('.bom-sp-adj');
      if (adjInp) {
        adjInp.value = sp;
        adjInp.setAttribute('data-original-sp', String(sp));
      }
      // 也更新隐藏的 data-selling-price
      row.setAttribute('data-selling-price', String(sp));
      recomputeAll();
    });
  }

  function bindSpAdjInput(row) {
    var inp = row.querySelector('.bom-sp-adj');
    if (!inp) return;
    inp.addEventListener('input', recomputeAll);
  }

  function bindTempPriceInput(row) {
    var tpInput = row.querySelector('.bom-temp-price');
    if (!tpInput) return;
    tpInput.addEventListener('input', recomputeAll);
  }

  function bindSourceTypeSwitches() {
    document.querySelectorAll('.bom-source-type').forEach(function(sel) {
      sel.addEventListener('change', function() {
        var row = sel.closest('.bom-row');
        var cell = row.querySelector('.bom-target-cell');
        var st = sel.value;
        cell.innerHTML = st === 'ic_recipe' ? buildIcRecipeSelect() : buildItemSelect();
        bindItemSelChange(row);
        bindTempPriceInput(row);
        bindSpAdjInput(row);
        recomputeAll();
      });
    });
  }

  function buildItemSelect() {
    var items = window.__items || [];
    return '<select name="bom_item_id" class="bom-item-sel">'
      + items.map(function(it) {
        return '<option value="' + it.id
          + '" data-gram="' + (it.gram_per_unit || 0)
          + '" data-unit="' + (it.unit || '')
          + '" data-uc="' + (it.unit_cost || 0)
          + '" data-sp="' + (it.selling_price || 0)
          + '">' + it.label + '</option>';
      }).join('') + '</select>';
  }

  function buildIcRecipeSelect() {
    var ics = window.__icRecipes || {};
    return '<select name="bom_ic_recipe_id" class="bom-ic-sel">'
      + Object.keys(ics).map(function(id) {
        var ic = ics[id];
        return '<option value="' + id + '">' + ic.name + '</option>';
      }).join('') + '</select>';
  }

  function bindAddRow() {
    var btn = document.getElementById('bom-add');
    if (!btn) return;
    btn.addEventListener('click', function() {
      var variant = getVariant();
      var tbody = document.getElementById('bom-body');
      var tr = document.createElement('tr');
      tr.className = 'bom-row';
      tr.setAttribute('data-source-type', 'item');

      if (variant === 'ic') {
        tr.innerHTML = [
          '<td>',
            '<input type="hidden" name="bom_row_id" value="" />',
            buildItemSelect(),
          '</td>',
          '<td><input type="number" step="0.01" min="0" name="bom_qty" class="bom-qty" value="0" /></td>',
          '<td><span class="bom-unit-label">—</span></td>',
          '<td><span class="bom-uc">0.000</span></td>',
          '<td><span class="bom-cost-purchase">0.00</span></td>',
          '<td><span class="bom-sp">0.000</span></td>',
          '<td class="sp-adj-col"><input type="number" class="bom-sp-adj" name="bom_sp_adj" min="0" step="0.01" value="0" data-original-sp="0" /></td>',
          '<td><span class="bom-cost-selling">0.00</span></td>',
          '<td><label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label></td>',
        ].join('');
      } else {
        tr.innerHTML = [
          '<td>',
            '<input type="hidden" name="bom_row_id" value="" />',
            '<select name="bom_source_type" class="bom-source-type">',
              '<option value="item" selected>品项</option>',
              '<option value="ic_recipe">冰激凌配方</option>',
            '</select>',
          '</td>',
          '<td class="bom-target-cell">', buildItemSelect(), '</td>',
          '<td><input type="number" step="0.01" min="0" name="bom_qty" class="bom-qty" value="0" /></td>',
          '<td><span class="bom-unit-label">—</span></td>',
          '<td><span class="bom-cost-purchase">0.00</span></td>',
          '<td><span class="bom-cost-selling">0.00</span></td>',
          '<td><input type="number" class="bom-temp-price" min="0" step="0.01" value="0" disabled /></td>',
          '<td><label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> 删除</label></td>',
        ].join('');
      }

      tbody.appendChild(tr);
      bindSourceTypeSwitches();
      bindItemSelChange(tr);
      bindTempPriceInput(tr);
      bindSpAdjInput(tr);
      recomputeAll();
    });
  }

  function bindAll() {
    var toggle = document.getElementById('use-temp-prices');
    if (toggle) toggle.addEventListener('change', recomputeAll);

    var salePriceEl = getSalePriceEl();
    if (salePriceEl) salePriceEl.addEventListener('input', function() {
      var hid = document.getElementById('bom-sale-price');
      if (hid && hid !== salePriceEl) hid.value = salePriceEl.value;
      recomputeAll();
    });

    document.querySelectorAll('#bom-body .bom-row').forEach(function(row) {
      bindItemSelChange(row);
      bindTempPriceInput(row);
      bindSpAdjInput(row);
    });

    bindSourceTypeSwitches();
    bindAddRow();
    recomputeAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindAll);
  } else {
    bindAll();
  }
})();
