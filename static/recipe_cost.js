(function() {
  'use strict';

  function round2(n) { return Math.round(n * 100) / 100; }

  function fmtPct(x) {
    if (x === null || x === undefined || isNaN(x)) return '\u2014';
    return (x * 100).toFixed(1) + '%';
  }

  function lineItemCost(row, useTemp) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var itemSel = row.querySelector('.bom-item-sel');
    if (!itemSel) return {purchase: 0, selling: 0};
    var opt = itemSel.options[itemSel.selectedIndex];
    var uc = parseFloat(opt.getAttribute('data-uc') || '0');
    var sp = parseFloat(opt.getAttribute('data-sp') || '0');
    var gpu = parseFloat(opt.getAttribute('data-gram') || '0');
    var qtyStock = gpu > 0 ? qty / gpu : qty;
    var tempSp = useTemp
      ? parseFloat(row.querySelector('.bom-temp-slider').value) || 0
      : sp;
    return {
      purchase: round2(qtyStock * uc),
      selling: round2(qtyStock * tempSp),
    };
  }

  function lineIcCost(row, useTemp, icRecipes) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var sel = row.querySelector('.bom-ic-sel');
    if (!sel) return {purchase: 0, selling: 0};
    var icId = parseInt(sel.value);
    var ic = icRecipes[icId];
    if (!ic) return {purchase: 0, selling: 0};
    return {
      purchase: round2(qty * ic.cost_purchase_per_unit),
      selling: round2(qty * ic.cost_selling_per_unit),
    };
  }

  function recomputeAll() {
    var useTemp = document.getElementById('use-temp-prices').checked;
    var rows = document.querySelectorAll('#bom-body .bom-row');
    var totalP = 0, totalS = 0;
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
      row.querySelector('.bom-cost-purchase').textContent = cost.purchase.toFixed(2);
      row.querySelector('.bom-cost-selling').textContent = cost.selling.toFixed(2);

      var slider = row.querySelector('.bom-temp-slider');
      if (slider) {
        var sp = row.getAttribute('data-selling-price') || '0';
        var display = row.querySelector('.bom-temp-display');
        if (useTemp && st === 'item') {
          slider.disabled = false;
          display.textContent = parseFloat(slider.value).toFixed(2);
        } else {
          slider.disabled = true;
          if (st === 'item') {
            slider.value = sp;
            display.textContent = parseFloat(sp).toFixed(2);
          } else {
            slider.value = 0;
            display.textContent = '0.00';
          }
        }
      }
    });
    var salePrice = parseFloat(
      document.getElementById('bom-sale-price').value || '0'
    );
    var margin = salePrice > 0 ? (salePrice - totalP) / salePrice : null;
    document.getElementById('total-purchase').textContent = totalP.toFixed(2);
    document.getElementById('total-selling').textContent = totalS.toFixed(2);
    document.getElementById('footer-sale-price').textContent = salePrice.toFixed(2);
    document.getElementById('footer-margin').textContent = fmtPct(margin);
  }

  function bindSaveButtons() {
    document.querySelectorAll('.bom-save-temp').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var row = btn.closest('.bom-row');
        var itemId = btn.getAttribute('data-item-id');
        if (!itemId) return;
        var sp = row.querySelector('.bom-temp-slider').value;
        var form = document.createElement('form');
        form.method = 'POST';
        form.action = '/recipe-cost/items/' + itemId + '/update-selling-price';
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'selling_price';
        input.value = sp;
        form.appendChild(input);
        document.body.appendChild(form);
        form.submit();
      });
    });
  }

  function bindSourceTypeSwitches() {
    document.querySelectorAll('.bom-source-type').forEach(function(sel) {
      sel.addEventListener('change', function() {
        var row = sel.closest('.bom-row');
        var cell = row.querySelector('.bom-target-cell');
        var st = sel.value;
        if (st === 'ic_recipe') {
          cell.innerHTML = buildIcRecipeSelect();
        } else {
          cell.innerHTML = buildItemSelect();
        }
        recomputeAll();
      });
    });
  }

  function buildItemSelect() {
    var items = window.__items || [];
    var opts = items.map(function(it) {
      return '<option value="' + it.id + '" data-gram="' + (it.gram_per_unit || 0) +
             '" data-uc="' + (it.unit_cost || 0) + '" data-sp="' + (it.selling_price || 0) +
             '">' + it.label + '</option>';
    }).join('');
    return '<select name="bom_item_id" class="bom-item-sel">' + opts + '</select>';
  }

  function buildIcRecipeSelect() {
    var ics = window.__icRecipes || {};
    var opts = Object.keys(ics).map(function(id) {
      var ic = ics[id];
      return '<option value="' + id + '">' + ic.name + '</option>';
    }).join('');
    return '<select name="bom_ic_recipe_id" class="bom-ic-sel">' + opts + '</select>';
  }

  function bindAddRow() {
    var btn = document.getElementById('bom-add');
    if (!btn) return;
    btn.addEventListener('click', function() {
      var tbody = document.getElementById('bom-body');
      var tr = document.createElement('tr');
      tr.className = 'bom-row';
      tr.setAttribute('data-source-type', 'item');
      tr.innerHTML = [
        '<td>',
          '<input type="hidden" name="bom_row_id" value="" />',
          '<select name="bom_source_type" class="bom-source-type">',
            '<option value="item" selected>\u54c1\u9879</option>',
            '<option value="ic_recipe">\u51b0\u6fc0\u51cc\u914d\u65b9</option>',
          '</select>',
        '</td>',
        '<td class="bom-target-cell">', buildItemSelect(), '</td>',
        '<td><input type="number" step="0.01" min="0" name="bom_qty" class="bom-qty" value="0" /></td>',
        '<td><span class="bom-unit-label">\u2014</span></td>',
        '<td><span class="bom-cost-purchase">0.00</span></td>',
        '<td><span class="bom-cost-selling">0.00</span></td>',
        '<td>',
          '<input type="range" class="bom-temp-slider" min="0" step="0.01" value="0" disabled />',
          '<span class="bom-temp-display">0.00</span>',
          '<button type="button" class="btn-sm bom-save-temp" data-item-id="" disabled>\u4fdd\u5b58\u4e3a\u65b0\u4ef7</button>',
        '</td>',
        '<td><label class="del-flag"><input type="checkbox" name="bom_delete" value="1" /> \u5220\u9664</label></td>',
      ].join('');
      tbody.appendChild(tr);
      bindSourceTypeSwitches();
      bindSliderFor(tr);
      bindItemSelChange(tr);
      recomputeAll();
    });
  }

  function bindSliderFor(row) {
    var slider = row.querySelector('.bom-temp-slider');
    var display = row.querySelector('.bom-temp-display');
    if (!slider) return;
    slider.addEventListener('input', function() {
      display.textContent = parseFloat(slider.value).toFixed(2);
      recomputeAll();
    });
  }

  function bindItemSelChange(row) {
    var sel = row.querySelector('.bom-item-sel');
    if (!sel) return;
    sel.addEventListener('change', function() {
      var opt = sel.options[sel.selectedIndex];
      var sp = parseFloat(opt.getAttribute('data-sp') || '0');
      row.setAttribute('data-selling-price', sp);
      var slider = row.querySelector('.bom-temp-slider');
      if (slider) slider.value = sp;
      var saveBtn = row.querySelector('.bom-save-temp');
      if (saveBtn) saveBtn.setAttribute('data-item-id', sel.value);
      recomputeAll();
    });
  }

  function bindAll() {
    var toggle = document.getElementById('use-temp-prices');
    if (toggle) toggle.addEventListener('change', recomputeAll);

    var salePrice = document.getElementById('bom-sale-price');
    if (salePrice) salePrice.addEventListener('input', recomputeAll);

    document.querySelectorAll('#bom-body .bom-row').forEach(function(row) {
      bindSliderFor(row);
      bindItemSelChange(row);
    });

    bindSourceTypeSwitches();
    bindSaveButtons();
    bindAddRow();
    recomputeAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindAll);
  } else {
    bindAll();
  }
})();
