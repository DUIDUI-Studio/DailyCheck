(() => {
  "use strict";
  const config = JSON.parse(document.getElementById("ev-data").textContent);
  const facts = config.facts,
    $ = (id) => document.getElementById("ev-" + id);
  const esc = (x) =>
    String(x ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const names = {
    unknown: "未知",
    unsplittable: "多模型 · 无法拆分",
    active: "开发中 / 未关联 PR",
    ci_unknown: "CI 未知 / 待核实",
    awaiting_prod: "awaiting_prod · 待生产审查",
    awaiting_deploy: "awaiting_deploy · 待部署证据",
    deploy_failed: "deploy_failed · 部署失败",
    delivered: "delivered · 已交付",
    closed_unmerged: "关闭未合并",
    fresh: "新鲜",
    stale: "陈旧",
    unavailable: "源不可用",
  };
  const label = (x) => names[x] || x;
  const uniq = (a) => [...new Set(a)];
  const tok = (n) =>
    n == null
      ? "—"
      : n >= 1e6
        ? (n / 1e6).toFixed(2) + "M"
        : n >= 1e3
          ? (n / 1e3).toFixed(1) + "K"
          : String(n);
  const dur = (n) =>
    n == null
      ? "—"
      : n >= 3600
        ? Math.floor(n / 3600) + "h " + Math.floor((n % 3600) / 60) + "m"
        : n >= 60
          ? Math.floor(n / 60) + "m " + Math.floor(n % 60) + "s"
          : Math.floor(n) + "s";
  const stamp = (n) =>
    n == null
      ? "—"
      : new Date(n * 1000).toLocaleString("zh-CN", {
          timeZone: "Asia/Shanghai",
          hour12: false,
        });
  const keys = ["input", "output", "cache_read", "cache_write", "reasoning"];
  function union(intervals) {
    let end = -Infinity,
      total = 0;
    for (const [a, b] of [...intervals].sort((x, y) => x[0] - y[0])) {
      total += Math.max(0, b - Math.max(a, end));
      end = Math.max(end, b);
    }
    return total;
  }
  function latestActivity(f) {
    return Math.max(
      f.at ?? -Infinity,
      ...f.events.map((e) => e.last_at ?? e.at ?? -Infinity),
      f.interval?.[1] ?? -Infinity,
    );
  }
  function select(data, opts = {}) {
    const lo = opts.start ?? -Infinity,
      hi = opts.end ?? Infinity,
      unbounded = opts.start == null && opts.end == null;
    return data.flatMap((f) => {
      if (
        (opts.role && f.role !== opts.role) ||
        (opts.model && !f.models.includes(opts.model))
      )
        return [];
      if (
        opts.status &&
        !f.prs.some((p) => p.delivery === opts.status) &&
        !(opts.status === "active" && !f.prs.length)
      )
        return [];
      if (opts.quality && f.freshness !== opts.quality) return [];
      if (
        opts.query &&
        !(JSON.stringify(f) + " #" + (f.issue ?? ""))
          .toLowerCase()
          .includes(opts.query.toLowerCase())
      )
        return [];
      const events = f.events.filter(
        (e) =>
          (!opts.model || e.model === opts.model) &&
          (e.at != null
            ? lo <= e.at && e.at < hi
            : unbounded || (f.at != null && lo <= f.at && f.at < hi)),
      );
      let interval = f.interval
        ? [Math.max(lo, f.interval[0]), Math.min(hi, f.interval[1])]
        : null;
      if (
        (interval && interval[1] <= interval[0]) ||
        (opts.model && f.models.length > 1)
      )
        interval = null;
      return unbounded ||
        events.length ||
        interval ||
        (f.at != null && lo <= f.at && f.at < hi)
        ? [{ ...f, events, interval }]
        : [];
    });
  }
  function summarize(data) {
    const workers = new Set(),
      executions = new Set(),
      prs = new Map(),
      times = new Map(),
      intervals = [],
      tokens = Object.fromEntries(keys.map((k) => [k, 0]));
    const measuredFields = new Set();
    let known = false,
      tokenCoverage = 0,
      timeCoverage = 0,
      unknownWorkers = 0,
      sessions = 0;
    for (const f of data) {
      if (f.worker) workers.add(f.worker);
      else if (f.kind === "execution") unknownWorkers++;
      if (f.kind === "execution") executions.add(f.dispatch || f.id);
      else sessions++;
      for (const p of f.prs) prs.set(p.key, p);
      if (f.events.length && f.events.every((e) => e.complete)) tokenCoverage++;
      for (const e of f.events) {
        if (e.total != null) known = true;
        for (const k of keys) {
          if (Number.isInteger(e.tokens?.[k]) && e.tokens[k] >= 0) {
            tokens[k] += e.tokens[k];
            measuredFields.add(k);
          }
        }
      }
      if (f.interval) {
        intervals.push(f.interval);
        if (f.worker) {
          if (!times.has(f.worker)) times.set(f.worker, []);
          times.get(f.worker).push(f.interval);
          timeCoverage++;
        }
      }
    }
    return {
      workers: workers.size,
      executions: executions.size,
      prs: [...prs.values()],
      unknown_workers: unknownWorkers,
      unattributed_sessions: sessions,
      retries: new Set(
        data.filter((f) => f.retry_of).map((f) => f.dispatch || f.id),
      ).size,
      tokens: Object.fromEntries(
        keys.map((k) => [k, measuredFields.has(k) ? tokens[k] : null]),
      ),
      total: known ? keys.slice(0, 4).reduce((n, k) => n + tokens[k], 0) : null,
      token_coverage: tokenCoverage,
      time_coverage: timeCoverage,
      rows: data.length,
      worker_seconds: times.size
        ? [...times.values()].reduce((n, v) => n + union(v), 0)
        : null,
      span_seconds: intervals.length
        ? Math.max(...intervals.map((i) => i[1])) -
          Math.min(...intervals.map((i) => i[0]))
        : null,
      model_coverage: data.filter(
        (f) =>
          !f.models.includes("unknown") && !f.models.includes("unsplittable"),
      ).length,
      identity_coverage: data.filter((f) => f.quality === "instance").length,
    };
  }
  // Exposed as a deterministic oracle for browser projection parity tests.
  window.__dailycheckEval = { select, summarize, union, latestActivity };
  function options(id, values) {
    for (const v of uniq(values).sort()) {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = label(v);
      $(id).append(o);
    }
  }
  options(
    "role",
    facts.map((f) => f.role),
  );
  options(
    "model",
    facts.flatMap((f) => f.models),
  );
  options("status", [
    "active",
    ...facts.flatMap((f) => f.prs.map((p) => p.delivery)),
  ]);
  let page = 0;
  const pageSize = 15,
    opened = new Set();
  function windowBounds() {
    const val = $("window").value,
      now = new Date(config.snapshot),
      today = new Date(now.getTime() + 8 * 3600e3).toISOString().slice(0, 10);
    $("custom").hidden = val !== "custom";
    if (val === "all") return {};
    if (val === "custom")
      return {
        start: $("start").value
          ? Date.parse($("start").value + "T00:00:00+08:00") / 1000
          : undefined,
        end: $("end").value
          ? Date.parse($("end").value + "T00:00:00+08:00") / 1000 + 86400
          : undefined,
      };
    const end = Date.parse(today + "T00:00:00+08:00") / 1000 + 86400;
    return { start: end - Number(val) * 86400, end };
  }
  const pills = (arr) =>
    uniq(arr)
      .map((v) => `<span class="ev-pill">${esc(label(v))}</span>`)
      .join("");
  function tokenDetail(s) {
    return `输入 ${tok(s.tokens.input)} · 输出 ${tok(s.tokens.output)} · 缓存读取 ${tok(s.tokens.cache_read)} · 缓存写入 ${tok(s.tokens.cache_write)} · 其中推理 ${tok(s.tokens.reasoning)}`;
  }
  function progress(prs) {
    const counts = {};
    for (const p of prs) counts[p.delivery] = (counts[p.delivery] || 0) + 1;
    return (
      Object.entries(counts)
        .map(
          ([k, v]) =>
            `<span class="ev-status ${k === "delivered" ? "ev-delivered" : ""}">${esc(label(k))} ${v}</span>`,
        )
        .join("<br>") || esc(label("active"))
    );
  }
  function details(group) {
    const byWorker = new Map();
    for (const f of group.facts) {
      const key = f.worker || "unknown:" + f.id;
      if (!byWorker.has(key)) byWorker.set(key, []);
      byWorker.get(key).push(f);
    }
    return [...byWorker]
      .map(([key, fs]) => {
        const s = summarize(fs);
        return `<details><summary>${esc(fs[0].worker || (fs[0].kind === "execution" ? "身份未识别执行" : "未归属会话"))} · ${esc(uniq(fs.map((f) => label(f.role))).join(" / "))} · ${s.executions} 次执行 · ${tok(s.total)} Token · ${dur(s.worker_seconds)}</summary><div class="ev-details-body">${fs
          .map((f) => {
            const ss = summarize([f]);
            return `<details><summary>${esc(f.dispatch || f.id)} · ${esc(f.agent)} / ${esc(f.effort || "effort 未知")} · ${f.retry_of ? "重试" : "执行"} · ${tok(ss.total)}</summary><dl><dt>Task / Issue</dt><dd>${esc(f.task || "—")} / ${esc(f.issue ? "#" + f.issue : "—")}</dd><dt>Provider session</dt><dd>${esc(f.session || uniq(f.events.map((e) => e.session_id)).join(" / ") || "—")}</dd><dt>请求 / 实际</dt><dd>${esc(f.requested || "—")} → ${esc(f.models.map(label).join(" / "))}${f.requested && !f.models.includes(f.requested) && !f.models.includes("unknown") ? " · 请求与实际不同" : ""}</dd><dt>开始 / 结束</dt><dd>${esc(f.started_at || "—")} / ${esc(f.ended_at || "—")}${f.running ? " · 运行已确证至采集快照" : ""}</dd><dt>已知用量</dt><dd>${esc(tokenDetail(ss))}${ss.token_coverage < 1 ? " · 分项缺失/未测量" : ""}</dd><dt>执行 / 重试链</dt><dd>${esc(f.dispatch || "未知")} / ${esc(f.retry_of || "无显式 retry_of")}</dd><dt>来源 / 新鲜度</dt><dd>${esc(f.collected_at || "采集时间未知")} · ${esc(label(f.freshness))}</dd>${f.legacy_usage_audit ? `<dt>历史待核对用量</dt><dd>${esc(tokenDetail({ tokens: f.legacy_usage_audit.tokens }))} · 可能与事件用量重叠，未加入已知合计</dd>` : ""}<dt>数据警告</dt><dd>${esc(f.warnings.join(" · ") || "—")}</dd></dl><div class="tbl"><table><thead><tr><th>模型段 / 会话事件</th><th>事件时间 CST</th><th>Token 分项</th></tr></thead><tbody>${f.events.map((e) => `<tr><td>${esc(label(e.model))}<small class="ev-muted">${esc(e.first_event_id || e.event_id)}${e.event_count ? " … " + esc(e.last_event_id) + " · " + e.event_count + " 个源事件" : ""}${e.counter_reset ? " · 计数器重置" : ""}</small></td><td>${e.at == null ? "按派单时间归档" : esc(stamp(e.at)) + " · CST 日模型桶"}</td><td>${esc(tokenDetail({ tokens: e.tokens }))}${e.complete ? "" : " · 部分分项"}</td></tr>`).join("") || '<tr><td colspan="3">缺少用量事件</td></tr>'}</tbody></table></div></details>`;
          })
          .join("")}</div></details>`;
      })
      .join("");
  }
  function render() {
    const bounds = windowBounds(),
      selected = select(facts, {
        ...bounds,
        role: $("role").value,
        model: $("model").value,
        status: $("status").value,
        quality: $("quality").value,
        query: $("search").value.trim(),
      });
    const map = new Map();
    for (const f of selected) {
      if (!map.has(f.tree)) map.set(f.tree, []);
      map.get(f.tree).push(f);
    }
    let groups = [...map].map(([key, fs]) => ({
      key,
      facts: fs,
      ...summarize(fs),
    }));
    const sorting = $("sort").value;
    const metric = (g) =>
      sorting === "tokens"
        ? (g.total ?? -1)
        : sorting === "workers"
          ? g.workers
          : sorting === "time"
            ? (g.worker_seconds ?? -1)
            : Math.max(...g.facts.map(latestActivity));
    groups.sort((a, b) =>
      sorting === "name"
        ? a.facts[0].name.localeCompare(b.facts[0].name, "zh-CN")
        : metric(b) - metric(a) || a.key.localeCompare(b.key),
    );
    const s = summarize(selected),
      treeCount = groups.filter(
        (g) => g.facts[0].quality !== "unattributed",
      ).length;
    const cards = [
      ["工作树", treeCount, `${groups.length - treeCount} 个未归属分组`],
      [
        "关联 PR",
        config.pr_data_ok ? s.prs.length : "—",
        "跨工作树再次去重 · 状态截至快照",
      ],
      [
        "已识别 Worker",
        s.workers,
        `${s.unknown_workers} 个身份未知执行 · ${s.unattributed_sessions} 个未归属会话`,
      ],
      [
        "Token 已知合计",
        tok(s.total),
        `用量分项覆盖 ${s.token_coverage}/${s.rows} 条记录`,
      ],
      [
        "Worker 累计运行",
        dur(s.worker_seconds),
        `时长覆盖 ${s.time_coverage}/${s.rows} · 同实例区间去重`,
      ],
    ];
    $("stats").innerHTML = cards
      .map(
        ([name, val, sub]) =>
          `<div class="ev-stat">${name}<b>${esc(val)}</b><small>${esc(sub)}</small></div>`,
      )
      .join("");
    $("window-note").textContent =
      ($("window").value === "all"
        ? "全生命周期"
        : `窗口内活动 · CST ${stamp(bounds.start)} — ${stamp(bounds.end)}（右边界不含）`) +
      ` · 快照 ${stamp(Date.parse(config.snapshot) / 1000)}。历史无事件时间的用量按派单时间归档；用量/时长只统计匹配执行，模型多段时长不可拆分。`;
    if (
      bounds.start != null &&
      bounds.end != null &&
      bounds.start >= bounds.end
    )
      $("window-note").textContent =
        "日期窗口无效：结束日期必须不早于开始日期。";
    const pages = Math.max(1, Math.ceil(groups.length / pageSize));
    page = Math.min(page, pages - 1);
    $("rows").innerHTML =
      groups
        .slice(page * pageSize, (page + 1) * pageSize)
        .map((g) => {
          const f = g.facts[0],
            id = "ev-tree-" + facts.findIndex((x) => x.tree === g.key),
            open = opened.has(g.key),
            partial = g.token_coverage < g.rows;
          const prs =
            g.prs
              .map(
                (p) =>
                  `#${p.number} ${esc(p.state)} · ${esc(label(p.delivery))}`,
              )
              .join("<br>") || (config.pr_data_ok ? "尚未关联" : "PR 源不可用");
          const states = Object.fromEntries(
            ["MERGED", "OPEN", "CLOSED", "UNKNOWN"].map((st) => [
              st,
              g.prs.filter((p) => p.state === st).length,
            ]),
          );
          const cell = (title, content) =>
            `<td data-label="${title}">${content}</td>`;
          return `<tr>${cell("工作树 / 分支", `<button type="button" id="${id}" data-tree="${esc(g.key)}" aria-expanded="${open}" aria-controls="${id}-detail">${open ? "▾" : "▸"} ${esc(f.name)}</button><small class="ev-muted">${esc(f.branch || "分支未知")}<br>${f.quality === "instance" ? "生命周期实例" : "历史路径归组 · 近似"}${f.deleted_at ? " · 已删除" : ""}</small>`).replace("<td ", '<td class="ev-tree" ')}${cell("PR 关联", `<span class="ev-num">${config.pr_data_ok ? g.prs.length : "—"}</span><small class="ev-muted" title="已合并 ${states.MERGED} · 开放 ${states.OPEN} · 关闭未合并 ${states.CLOSED} · 未知 ${states.UNKNOWN}">${prs}</small>`)}${cell("Worker / 执行", `<span class="ev-num">${g.workers}</span><small class="ev-muted">${g.executions} 次执行 · ${g.retries} 次显式重试<br>${g.unknown_workers} 个身份未知执行</small>`)}${cell("实际角色", pills(g.facts.map((f) => f.role)))}${cell("实际模型", pills(g.facts.flatMap((f) => f.models)))}${cell("Token 已知合计", `<span class="ev-num">${tok(g.total)}</span><small class="ev-muted">${partial ? "部分已知" : "已测分项"} ${g.token_coverage}/${g.rows}</small>`)}${cell("Worker 累计运行", `<span class="ev-num">${g.worker_seconds != null && g.time_coverage < g.rows ? "≥ " : ""}${dur(g.worker_seconds)}</span>`)}${cell("工作树历时", `<span class="ev-num">${g.span_seconds != null && g.facts.some((f) => !f.interval) ? "≥ " : ""}${dur(g.span_seconds)}</span>`)}${cell(
            "交付 / 覆盖",
            progress(g.prs) +
              `<small class="ev-muted">身份 ${g.identity_coverage}/${g.rows} · 模型 ${g.model_coverage}/${g.rows}<br>时长 ${g.time_coverage}/${g.rows}<br>${uniq(
                g.facts.map((f) => label(f.freshness)),
              )
                .map(esc)
                .join(" / ")}</small>`,
          )}</tr>${open ? `<tr class="ev-detail" id="${id}-detail"><td colspan="9"><p>${esc(f.repo)} · ${esc(f.host)} · ${esc(f.path || "路径未知")}<br>实例 ${esc(f.instance || "未知")} · ${esc(tokenDetail(g))}</p>${details(g)}<p>关联 PR（当前快照）：${prs}</p></td></tr>` : ""}`;
        })
        .join("") ||
      '<tr><td colspan="9" class="ev-empty">没有匹配的工作树，请调整筛选。</td></tr>';
    $("count").textContent =
      `${treeCount} 棵工作树 · ${s.executions} 次执行 · 汇总涵盖全部筛选结果`;
    $("page").textContent = `${page + 1} / ${pages}`;
    $("prev").disabled = page === 0;
    $("next").disabled = page === pages - 1;
    const aux = new Map();
    for (const f of selected) {
      const band =
        f.complexity == null
          ? "未知"
          : f.complexity < 4
            ? "<4.0"
            : f.complexity < 6.5
              ? "4.0–6.4"
              : "≥6.5";
      for (const model of uniq(
        f.events.map((e) => e.model).concat(f.events.length ? [] : f.models),
      )) {
        const key = JSON.stringify([f.role, model, band]);
        if (!aux.has(key))
          aux.set(key, {
            role: f.role,
            model,
            band,
            ids: new Set(),
            tokens: 0,
            known: false,
          });
        const a = aux.get(key);
        a.ids.add(f.id);
        for (const e of f.events.filter((e) => e.model === model)) {
          if (e.total != null) {
            a.tokens += e.total;
            a.known = true;
          }
        }
      }
    }
    $("aux").innerHTML = [...aux.values()]
      .map(
        (a) =>
          `<tr><td>${esc(label(a.role))}</td><td>${esc(label(a.model))}</td><td>${esc(a.band)}</td><td>${a.ids.size}</td><td>${tok(a.known ? a.tokens : null)}</td></tr>`,
      )
      .join("");
    $("rows")
      .querySelectorAll("[data-tree]")
      .forEach((button) =>
        button.addEventListener("click", () => {
          const key = button.dataset.tree,
            id = button.id;
          opened.has(key) ? opened.delete(key) : opened.add(key);
          render();
          document.getElementById(id)?.focus({ preventScroll: true });
        }),
      );
  }
  for (const id of [
    "search",
    "window",
    "role",
    "model",
    "status",
    "quality",
    "sort",
    "start",
    "end",
  ])
    $(id).addEventListener(id === "search" ? "input" : "change", () => {
      page = 0;
      render();
    });
  $("prev").onclick = () => {
    page--;
    render();
  };
  $("next").onclick = () => {
    page++;
    render();
  };
  render();
})();
