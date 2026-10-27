"""deploy_triage_pure.py —— 部署失败的归因与「契合度」判定（纯函数，不出网、不读文件）。

## 为什么要有这一层

部署失败后要不要「自动继续处理」，取决于一个前置判断：**这次失败跟这次改动
有关系吗**。判错的代价不对称：

- 环境问题被误判成代码问题 → 自动去改代码，把好的改动改坏，且真问题（比如
  DRY_RUN 没关）还在原地，下次部署照炸。
- 代码问题被误判成环境问题 → 多花几小时等一个不会自己好的东西。

所以判定必须**从能确定的开始**。本模块只做机械判定：锁冲突、DRY_RUN 未关、
工作树漂移这三类**确定与本 PR 代码无关**，直接判死，不给模型任何参与机会 ——
让模型从零judge，等于给它机会把环境问题说成代码问题。

判不了的（verify 红了但日志没指向改动文件）才交语义层。这是刻意的分工：
机械层便宜、可复现、无幻觉；模型只处理机械层啃不动的部分。

## 三个输出

``triage()`` —— 这次失败是什么性质（kind）+ 是否落在本 PR 影响面（scope）
``parse_requirements()`` —— 从 PR body 的「报告 1/4」表里抽出 R 条目
``assess_alignment()`` —— 综合上面两者 + changed files + 日志，给出能否继续

对齐结论四态，语义是**动作授权**而不是描述：

- ``aligned``      失败指向本 PR 改过的文件。允许自动继续，但仅限本 PR 范围。
- ``drifted``      与本 PR 无关（锁 / DRY_RUN / 漂移 / 环境）。**禁止改代码**。
- ``unverifiable`` PR 里读不到可核对的目标。**禁止自动动手**，先补 PR body。
- ``unknown``      机械层判不了，交语义层。

## 为什么还要 ``next_action()``

``verdict`` 是判定，不是指令。让每个消费端 agent 自己把 verdict + kind 翻译成
「那我现在该干什么」，等于把同一套解释逻辑复制到每个 agent 里 —— 迟早各自
解释出不同的动作，其中一个直接去改了不该改的代码。

``next_action()`` 把它压成**六个穷举值**，消费端只做查表，不做推理。这也是
``halt`` 存在的理由：自动修复本身要有熔断，否则两个 agent 会互相「修」对方的
修复，直到把 PR 改成另一个东西。


## 硬保险

``assess_alignment()`` 会先看 PR body 有没有可解析的 R 条目。没有就直接
``unverifiable`` —— 把「PR 必须写需求对齐表」从规范变成机器可执行的闸门。
理由：一个没有声明目标的 PR，它的「失败是否偏离目标」这个问题本身无意义。
"""

from __future__ import annotations

import re
from typing import Any

# ── 归因类别 ──────────────────────────────────────────────────────────────
KIND_SUCCESS = "success"
KIND_USAGE = "usage"
KIND_LOCK = "lock"
KIND_CODE = "code"
KIND_DRIFT = "drift"
KIND_ENV = "env"
KIND_UNKNOWN = "unknown"

# ── 影响面 ────────────────────────────────────────────────────────────────
SCOPE_IN = "in-scope"
SCOPE_OUT = "out-of-scope"
SCOPE_UNDETERMINED = "undetermined"

# ── 对齐结论 ──────────────────────────────────────────────────────────────
VERDICT_ALIGNED = "aligned"
VERDICT_DRIFTED = "drifted"
VERDICT_UNVERIFIABLE = "unverifiable"
VERDICT_UNKNOWN = "unknown"

# ── 下一步动作（给 agent 的授权枚举，消费端只查表不推理）──────────────────
# 人不是执行者，所以这里没有「通知人」「等人审批」这两个值 —— 需要人拍板的
# 事从来不在这条链路上，它由 PR 门禁与 merge 权限管。
NEXT_NONE = "none"        # 成功，无事可做
NEXT_AUTOFIX = "autofix"  # 失败在本 PR 影响面内，可在 PR 范围内改代码
NEXT_ENVFIX = "envfix"    # 环境/流程问题，修环境，**禁止改 PR 代码**
NEXT_DERIVE = "derive"    # PR 没声明目标，先推导并补写目标，再重判
NEXT_AUDIT = "audit"      # 调用姿势错了（rc=64），修调用方，不要碰代码
NEXT_HALT = "halt"        # 熔断：停止自动重试，回去重审目标本身

# 同一 PR 连续自动修复的上限。超过就说明「这个改动方向本身有问题」，继续
# 试只会把 PR 越改越远离原意 —— 那时候正确的动作是重审目标，不是再修一次。
MAX_AUTOFIX = 3

# 退出码 → 默认归因。stage 可覆盖（verify 有 ``verify-drift`` 子类型）。
_BY_RC: dict[int, tuple[str, str]] = {
    64: (KIND_USAGE, SCOPE_OUT),
    10: (KIND_LOCK, SCOPE_OUT),
    11: (KIND_LOCK, SCOPE_OUT),
    20: (KIND_CODE, SCOPE_IN),
    21: (KIND_CODE, SCOPE_UNDETERMINED),
    22: (KIND_ENV, SCOPE_OUT),
    23: (KIND_ENV, SCOPE_OUT),
    24: (KIND_ENV, SCOPE_UNDETERMINED),
}

# 日志证据 → 判定。只在**确定**时命中：宁可 unknown 交语义层，不要猜。
# 每条是 (子串, kind, scope, 一句人话原因)。
_LOG_EVIDENCE: tuple[tuple[str, str, str, str], ...] = (
    (
        "[dry_run] BLOCKED",
        KIND_ENV,
        SCOPE_OUT,
        "绿实例仍在 DRY_RUN 下承接流量（写被静默拦截）。"
        "这是 10-15 / 10-17 两次事故的同型故障，与本次改动的代码无关。",
    ),
    (
        "已回滚蓝",
        KIND_ENV,
        SCOPE_OUT,
        "观察窗异常已自动回滚蓝，流量已回到旧版本；需按回滚原因排查。",
    ),
)

# PR body 里的需求条目：``| R-001 | 需求描述 | 状态 |``。
# 允许 ``| **R-001** |`` 这种加粗写法 —— 那是人写表格的自然习惯，不能因为
# 多了两个星号就解析不出来（解析不出来会直接判 unverifiable 挡住自动化）。
_REQ_ROW = re.compile(r"^\|\s*\**\s*(R-\d+)\s*\**\s*\|\s*(.*?)\s*\|", re.MULTILINE)
# 需求对齐表的标题（有表头才算合格，只有 R 字样可能是别处的引用）
_REQ_SECTION = re.compile(r"^##+\s*报告\s*1\s*[/／]\s*4", re.MULTILINE)


def triage(
    rc: int,
    stage: str = "",
    log_tail: str = "",
) -> dict[str, Any]:
    """退出码 + 阶段 + 日志尾 → 归因。

    ``stage`` 优先查（``verify-drift`` 是 rc=21 的另一种成因，靠 rc 分不开），
    再看日志证据，最后落退出码默认表。
    """
    rc = int(rc)
    stage = (stage or "").strip()

    if rc == 0:
        return _result(KIND_SUCCESS, SCOPE_OUT, "部署全链完成", rc, stage)

    if rc == 25:
        return _result(
            KIND_ENV,
            SCOPE_OUT,
            "候选模式前置不满足（分支落后 main / sha 不符 / PR 非 open），"
            "未触碰蓝绿。",
            rc,
            stage,
        )
    if rc == 26:
        return _result(
            KIND_ENV,
            SCOPE_OUT,
            "验证期间 main 被改动，FF 合并失败；已切回蓝，main 未包含候选代码。",
            rc,
            stage,
        )

    if stage == "verify-drift":
        return _result(
            KIND_DRIFT,
            SCOPE_OUT,
            "verify 期间工作树被另一会话改动（HEAD 变了），verify 结果不可信。"
            "根因是部署锁护不住 git pull，与本次改动内容无关。",
            rc,
            stage,
        )

    for needle, kind, scope, reason in _LOG_EVIDENCE:
        if needle in log_tail:
            return _result(kind, scope, reason, rc, stage)

    if stage == "observe" and rc == 24:
        return _result(
            KIND_ENV,
            SCOPE_UNDETERMINED,
            "观察窗探活未返回 200/302，但日志未命中已知环境特征 —— 可能是新版本代码"
            "起得来但页面报错，也可能是探针本身。需看 green 实例日志。",
            rc,
            stage,
        )

    kind, scope = _BY_RC.get(rc, (KIND_UNKNOWN, SCOPE_UNDETERMINED))
    reason = {
        KIND_USAGE: "参数/用法错误，命令本身没跑起来。",
        KIND_LOCK: "部署锁被占用或 acquire 失败 —— 另一部署在跑，等它收尾后重跑即可。",
        KIND_CODE: "阶段执行失败，最常见成因是本次改动的代码或迁移。",
        KIND_ENV: "编排/环境阶段失败，通常与本次改动的代码无直接关系。",
    }.get(kind, f"退出码 {rc} 无预置语义，交语义层读日志判定。")
    return _result(kind, scope, reason, rc, stage)


def _result(
    kind: str, scope: str, reason: str, rc: int, stage: str
) -> dict[str, Any]:
    return {
        "kind": kind,
        "scope": scope,
        "reason": reason,
        "rc": rc,
        "stage": stage or "unknown",
        "code_actionable": kind == KIND_CODE,
    }


def parse_requirements(pr_body: Any) -> list[dict[str, str]]:
    """从 PR body 抽 R 条目。无「报告 1/4」节或无可解析行 → 空列表。

    空列表是一个**有意义的结论**：``assess_alignment()`` 靠它判 unverifiable。
    """
    body = str(pr_body or "")
    if not _REQ_SECTION.search(body):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for rid, text in _REQ_ROW.findall(body):
        if rid in seen:
            continue
        seen.add(rid)
        # 去掉状态列残留的 markdown 标记，但**保留下划线**：需求里常出现
        # ``FEEDBACK_LLM_API_KEY`` / ``DRY_RUN`` 这类标识符，把 ``_`` 当斜体
        # 标记洗掉会变成 ``FEEDBACKLLMAPIKEY`` —— 机器读起来是另一个东西了。
        clean = re.sub(r"[*~`]", "", text).strip()
        if clean:
            out.append({"id": rid, "text": clean})
    return out


def _mentioned_files(log_tail: str, changed_files: list[str]) -> list[str]:
    """日志里出现了哪些本次改动过的文件。

    路径口径不一致（日志是 ``/opt/dailycheck/x/y.py``，diff 是 ``x/y.py``），
    所以按「包含」匹配，但要求至少带一级目录 —— 裸文件名（``items.py``）
    太容易在日志里误命中，宁可漏判交 unknown。
    """
    hits: list[str] = []
    text = log_tail or ""
    for f in changed_files or []:
        f = str(f).strip()
        if not f or "/" not in f or len(f) < 8:
            continue
        if f in text and f not in hits:
            hits.append(f)
    return hits


def assess_alignment(
    triage_result: dict[str, Any],
    pr_body: Any,
    changed_files: list[str] | None = None,
    log_tail: str = "",
) -> dict[str, Any]:
    """判定「这次失败是否偏离本 PR 的原定目标」。

    ``verdict`` 是动作授权，不是描述。消费端据此决定能不能动手：
    只有 ``aligned`` 允许自动改代码，且限定在本 PR 改过的文件范围内。
    """
    reqs = parse_requirements(pr_body)
    kind = triage_result.get("kind", KIND_UNKNOWN)
    scope = triage_result.get("scope", SCOPE_UNDETERMINED)

    base = {
        "verdict": VERDICT_UNKNOWN,
        "requirements": reqs,
        "reason": "",
        "action": "",
        "hits": [],
    }

    if kind == KIND_SUCCESS:
        base.update(
            verdict=VERDICT_ALIGNED,
            reason="部署成功，无需对齐判定。",
            action="none",
        )
        return base

    if not reqs:
        base.update(
            verdict=VERDICT_UNVERIFIABLE,
            reason="PR body 里没有可解析的「报告 1/4」需求条目 —— "
            "无从判断失败是否偏离目标。",
            action="agent 先从 PR body 的 Closes/Fixes 链接与本次 diff 推导目标，"
            "以追加方式补写「报告 1/4」节（不改原文），再重跑 assess 重判；"
            "推导不出来就落 halt，不要盲改。",
        )
        return base

    hits = _mentioned_files(log_tail, list(changed_files or []))
    base["hits"] = hits

    if scope == SCOPE_OUT:
        base.update(
            verdict=VERDICT_DRIFTED,
            reason=triage_result.get("reason", "") + "（与本 PR 影响面无关）",
            action="按归因处置环境/流程问题后重跑部署；**不要改本次改动过的代码**。",
        )
        return base

    if hits:
        base.update(
            verdict=VERDICT_ALIGNED,
            reason="失败日志指向本次改动过的文件：" + "、".join(hits[:3]),
            action="可在本 PR 范围内继续修；改动不得超出原需求条目。",
        )
        return base

    base.update(
        verdict=VERDICT_UNKNOWN,
        reason=(
            "归因倾向本 PR（" + str(kind) + "），但日志未指向任何改动过的文件，"
            "机械层无法确认因果。"
        ),
        action="对照需求条目与失败日志判断失败是否由本 PR 引入；判不了就 halt，"
        "重审目标，不要试错式改动。",
    )
    return base


def next_action(
    triage_result: dict[str, Any],
    alignment: dict[str, Any],
    autofix_count: int = 0,
) -> str:
    """判定结果 → 消费端 agent 该做什么。六个穷举值，见模块 docstring。

    ``autofix_count`` 是**同一 PR 已发生的 autofix 次数**（由事件流累计）。
    到顶就 halt：不是「放弃」，是「这个方向修不动了，重审目标」。

    判定顺序有讲究：先判死最不需要推理的（成功 / 调用姿势），再判目标缺失
    （derive），最后才轮到授权改代码 —— 因为 ``autofix`` 是这里唯一的写权限，
    必须让最严格的闸门挡在它前面。
    """
    kind = str(triage_result.get("kind") or KIND_UNKNOWN)
    verdict = str(alignment.get("verdict") or VERDICT_UNKNOWN)

    if kind == KIND_SUCCESS:
        return NEXT_NONE
    if kind == KIND_USAGE:
        return NEXT_AUDIT
    if verdict == VERDICT_UNVERIFIABLE:
        return NEXT_DERIVE
    if verdict == VERDICT_ALIGNED and kind == KIND_CODE:
        return NEXT_AUTOFIX if int(autofix_count) < MAX_AUTOFIX else NEXT_HALT
    return NEXT_ENVFIX
