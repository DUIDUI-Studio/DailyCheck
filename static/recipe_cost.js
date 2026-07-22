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

  function lineItemCost(row, useTemp) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var itemSel = row.querySelector('.bom-item-sel');
    if (!itemSel) return {purchase: 0, selling: 0, grams: 0};
    var opt = itemSel.options[itemSel.selectedIndex];
    var uc = parseFloat(opt.getAttribute('data-uc') || '0');
    var sp = parseFloat(opt.getAttribute('data-sp') || '0');
    var gpu = parseFloat(opt.getAttribute('data-gram') || '0');
    var qtyStock = gpu > 0 ? qty / gpu : qty;
    var tempSp = useTemp
      ? parseFloat(row.querySelector('.bom-temp-price').value) || 0
      : sp;
    return {
      purchase: round2(qtyStock * uc),
      selling: round2(qtyStock * tempSp),
      grams: gpu > 0 ? qty : 0,
    };
  }

  function lineIcCost(row, useTemp, icRecipes) {
    var qty = parseFloat(row.querySelector('.bom-qty').value) || 0;
    var sel = row.querySelector('.bom-ic-sel');
    if (!sel) return {purchase: 0, selling: 0, grams: 0};
    var icId = parseInt(sel.value);
    var ic = icRecipes[icId];
    if (!ic) return {purchase: 0, selling: 0, grams: 0};
    // ic_recipes measure in grams too (output_unit fixed to 'g'), so grams ==
    // qty * ic.output_qty (the IC recipe's own per-unit grams).
    var grams = qty * (parseFloat(ic.output_qty) || 0);
    return {
      purchase: round2(qty * ic.cost_purchase_per_unit),
      selling: round2(qty * ic.cost_selling_per_unit),
      grams: grams,
    };
  }

  function recomputeAll() {
    var useTemp = document.getElementById('use-temp-prices').checked;
    var rows = document.querySelectorAll('#bom-body .bom-row');
    var totalP = 0, totalS = 0, totalGrams = 0;
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
      totalGrams += cost.grams;
      row.querySelector('.bom-cost-purchase').textContent = cost.purchase.toFixed(2);
      row.querySelector('.bom-cost-selling').textContent = cost.selling.toFixed(2);

      // Toggle temp-price input: editable only when useTemp AND item row
      var tpInput = row.querySelector('.bom-temp-price');
      if (tpInput) {
        if (useTemp && st === 'item') {
          tpInput.disabled = false;
        } else {
          tpInput.disabled = true;
          if (st === 'item') {
            var itemSel = row.querySelector('.bom-item-sel');
            if (itemSel) {
              var sp = parseFloat(
                itemSel.options[itemSel.selectedIndex].getAttribute('data-sp') || '0'
              );
              tpInput.value = sp;
            }
          } else {
            tpInput.value = 0;
          }
        }
      }
    });

    var salePrice = parseFloat(
      document.getElementById('bom-sale-price').value || '0'
    );

    // Two margin flavors:
    //   margin_purchase = (sale_price - cost_purchase) / sale_price
    //   margin_selling = (cost_selling - cost_purchase) / cost_selling
    var marginPurchase = salePrice > 0 ? (salePrice - totalP) / salePrice : null;
    var marginSelling = totalS > 0 ? (totalS - totalP) / totalS : null;
    var profit = totalS - totalP;
    var profitMargin = totalS > 0 ? profit / totalS : null;

    // Auto-compute output_qty = total grams (when not in useTemp mode, and only items)
    // Skip auto-update if useTemp is on — temp prices don't affect total grams.
    var outQty = document.getElementById('output-qty');
    if (outQty && !useTemp) {
      outQty.value = round2(totalGrams).toFixed(2);
    }

    document.getElementById('total-purchase').textContent = totalP.toFixed(2);
    document.getElementById('total-selling').textContent = totalS.toFixed(2);
    var fsp = document.getElementById('footer-sale-price');
    if (fsp) fsp.textContent = salePrice.toFixed(2);
    var fmp = document.getElementById('footer-margin-purchase');
    if (fmp) fmp.textContent = fmtPct(marginPurchase);
    var fms = document.getElementById('footer-margin-selling');
    if (fms) fms.textContent = fmtPct(marginSelling);
    var fp = document.getElementById('footer-profit');
    if (fp) fp.textContent = profit.toFixed(2);
    var fpm = document.getElementById('footer-profit-margin');
    if (fpm) fpm.textContent = fmtPct(profitMargin);
  }

  function bindItemSelChange(row) {
    var sel = row.querySelector('.bom-item-sel');
    if (!sel) return;
    sel.addEventListener('change', function() {
      recomputeAll();
    });
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
        if (st === 'ic_recipe') {
          cell.innerHTML = buildIcRecipeSelect();
        } else {
          cell.innerHTML = buildItemSelect();
        }
        bindItemSelChange(row);
        bindTempPriceInput(row);
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
      tbody.appendChild(tr);
      bindSourceTypeSwitches();
      bindItemSelChange(tr);
      bindTempPriceInput(tr);
      recomputeAll();
    });
  }

  function bindAll() {
    var toggle = document.getElementById('use-temp-prices');
    if (toggle) toggle.addEventListener('change', recomputeAll);

    var salePrice = document.getElementById('bom-sale-price');
    if (salePrice) salePrice.addEventListener('input', recomputeAll);

    document.querySelectorAll('#bom-body .bom-row').forEach(function(row) {
      bindItemSelChange(row);
      bindTempPriceInput(row);
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