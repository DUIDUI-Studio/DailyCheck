# Deploy-first（候选部署 + FF-only 合并）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把生产发布顺序从「merge → 部署」改为「部署候选 → verify/切流/观察 → FF-only merge」，使 main 只包含已验证代码，同时保持 `deployed.sha == origin/main`。

**Architecture:** `deploy_pipeline.sh` 新增两个默认关闭的开关：`--candidate`（fetch PR head、校验包含 main、checkout 后部署）与 `--merge-pr`（观察窗通过后调用新助手 `deploy_merge.py` 做 FF-only 合并）。失败路径把流量切回蓝、工作树恢复到 `origin/main`。所有旧调用不带开关时行为不变。

**Tech Stack:** Bash 3.2（macOS 兼容）、Python 3.10+ stdlib、pytest、Gitea REST API、现有的 `deploy_report.py` Gitea 配置口径。

## Global Constraints

- 新 Python 代码只用 stdlib；不新增依赖。
- Bash 脚本保持 Bash 3.2 兼容：不用 `mapfile`/关联数组，用 `[[ ]]` 与函数。
- `deploy_merge.py` 的 Gitea 配置只复用 `deploy_report.py`（`gitea()` / token / API base），不复制一套。
- 新开关默认关闭；不带 `--candidate`/`--merge-pr` 时现有行为逐字节不变。
- 退出码：pipeline `25 candidate 前置不满足`、`26 FF 合并失败`；helper `30 非快进`、`31 PR 前置不匹配`、`32 API/token 失败`、`64 参数错`。
- 合并只允许 `{"do":"fast-forward-only"}`；不得用 `merge`/`squash`/`rebase`。
- 每个触及 `scripts/` 或 `tests/` 的 commit **必须**同 commit 更新 `docs/ARCHITECTURE.md`（pre-commit hook 强制）。
- 测试必须 hermetic：不触网、不碰 systemd/nginx/真实 `/var`；HTTP 用 fake curl，git 用 tmp 仓库。
- Ruff：line_length=100，规则 E/F/W/I/B/UP；改动文件零告警。
- 工作目录：`/tmp/dc-deploy-order`，分支 `docs/deploy-before-merge-design`（已含 spec 提交 `00a6db4`）。

---

### Task 1: `deploy_merge.py` —— FF-only 合并助手

**Files:**
- Create: `scripts/blue_green/deploy_merge.py`
- Create: `tests/test_deploy_merge.py`
- Modify: `docs/ARCHITECTURE.md`（§3.8，加一段助手说明）

**Interfaces:**
- Consumes: `deploy_report.gitea(method, path, payload) -> (status, body)`、`deploy_report._repo() -> str`
- Produces: CLI `deploy_merge.py check|merge --pr N --sha SHA [--dry-run]`；成功退出 0，失败 30/31/32/64；stdout 单行 JSON `{"pr","sha","merged","merged_sha","reason"}`

- [ ] **Step 1: 写失败测试**

Create `tests/test_deploy_merge.py`:

```python
"""deploy_merge.py（FF-only 合并助手）测试。

用 fake curl 的响应队列控制每次 HTTP 调用；不触网、不碰真实 Gitea。
覆盖：前置校验、FF 成功、非快进、API 失败、token 缺失、脱敏。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BG_DIR = Path(__file__).resolve().parent.parent / "scripts" / "blue_green"
SHA = "abcdef1234567890abcdef1234567890abcdef12"

_FAKE_CURL = '''#!{py}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
url = args[-1] if args else ""
method = "GET"
if "-X" in args:
    method = args[args.index("-X") + 1]
payload = None
if "--data-binary" in args:
    spec = args[args.index("--data-binary") + 1]
    if spec.startswith("@"):
        payload = Path(spec[1:]).read_text(encoding="utf-8")

with open(os.environ["FAKE_CURL_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"method": method, "url": url, "payload": payload}}) + "\\n")

qpath = Path(os.environ["FAKE_CURL_QUEUE"])
queue = json.loads(qpath.read_text(encoding="utf-8"))
item = queue.pop(0) if queue else {{"code": 500, "body": "{{}}"}}
qpath.write_text(json.dumps(queue), encoding="utf-8")
body = item["body"]
if not isinstance(body, str):
    body = json.dumps(body, ensure_ascii=False)
sys.stdout.write(body + "\\n" + str(item["code"]))
'''


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "fakecurl"
    curl.write_text(_FAKE_CURL.format(py=sys.executable), encoding="utf-8")
    curl.chmod(0o755)
    log = tmp_path / "curl.jsonl"
    queue = tmp_path / "queue.json"
    queue.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("DAILYCHECK_CURL", str(curl))
    monkeypatch.setenv("FAKE_CURL_LOG", str(log))
    monkeypatch.setenv("FAKE_CURL_QUEUE", str(queue))
    monkeypatch.setenv("DAILYCHECK_GITEA_TOKEN", "test-token")
    monkeypatch.setenv("FEEDBACK_GITEA_API", "http://gitea.test/api/v1")
    monkeypatch.setenv("FEEDBACK_GITEA_REPO", "eric/dailycheck")
    monkeypatch.delenv("DAILYCHECK_GITEA_TOKEN_FILE", raising=False)

    def respond(*items):
        queue.write_text(json.dumps(list(items)), encoding="utf-8")

    def calls():
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln]

    return {"respond": respond, "calls": calls}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BG_DIR / "deploy_merge.py"), *args],
        capture_output=True, text=True, timeout=30,
    )


def _pr(**overrides) -> dict:
    data = {
        "number": 42,
        "state": "open",
        "draft": False,
        "merged": False,
        "head": {"sha": SHA},
        "base": {"ref": "main"},
    }
    data.update(overrides)
    return data


def test_check_passes_for_open_pr(fake):
    fake["respond"]({"code": 200, "body": _pr()})
    proc = _run("check", "--pr", "42", "--sha", SHA)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["pr"] == 42
    assert out["merged"] is False
    assert len(fake["calls"]()) == 1


def test_check_rejects_closed_pr(fake):
    fake["respond"]({"code": 200, "body": _pr(state="closed")})
    proc = _run("check", "--pr", "42", "--sha", SHA)
    assert proc.returncode == 31
    assert "open" in json.loads(proc.stdout)["reason"]


def test_check_rejects_sha_mismatch(fake):
    fake["respond"]({"code": 200, "body": _pr(head={"sha": "0" * 40})})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 31


def test_check_rejects_draft(fake):
    fake["respond"]({"code": 200, "body": _pr(draft=True)})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 31


def test_check_rejects_non_main_base(fake):
    fake["respond"]({"code": 200, "body": _pr(base={"ref": "dev"})})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 31


def test_merge_uses_fast_forward_only(fake):
    fake["respond"](
        {"code": 200, "body": _pr()},
        {"code": 200, "body": {}},
        {"code": 200, "body": _pr(merged=True, merge_commit_sha=SHA)},
    )
    proc = _run("merge", "--pr", "42", "--sha", SHA)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["merged"] is True
    assert out["merged_sha"] == SHA
    posts = [c for c in fake["calls"]() if c["method"] == "POST"]
    assert len(posts) == 1
    assert json.loads(posts[0]["payload"]) == {"do": "fast-forward-only"}


def test_merge_non_fast_forward_returns_30(fake):
    fake["respond"]({"code": 200, "body": _pr()}, {"code": 405, "body": {}})
    assert _run("merge", "--pr", "42", "--sha", SHA).returncode == 30


def test_merge_api_error_returns_32(fake):
    fake["respond"]({"code": 200, "body": _pr()}, {"code": 500, "body": {}})
    assert _run("merge", "--pr", "42", "--sha", SHA).returncode == 32


def test_merge_not_marked_merged_returns_32(fake):
    fake["respond"](
        {"code": 200, "body": _pr()},
        {"code": 200, "body": {}},
        {"code": 200, "body": _pr(merged=False)},
    )
    assert _run("merge", "--pr", "42", "--sha", SHA).returncode == 32


def test_missing_token_returns_32(fake, monkeypatch):
    monkeypatch.delenv("DAILYCHECK_GITEA_TOKEN", raising=False)
    fake["respond"]({"code": 200, "body": _pr()})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 32


def test_get_non_json_returns_32(fake):
    fake["respond"]({"code": 200, "body": "not json"})
    assert _run("check", "--pr", "42", "--sha", SHA).returncode == 32


def test_dry_run_does_not_merge(fake):
    fake["respond"]({"code": 200, "body": _pr()})
    proc = _run("merge", "--pr", "42", "--sha", SHA, "--dry-run")
    assert proc.returncode == 0
    assert all(c["method"] == "GET" for c in fake["calls"]())


def test_token_never_appears_in_output(fake):
    fake["respond"]({"code": 200, "body": _pr()})
    proc = _run("check", "--pr", "42", "--sha", SHA)
    assert "test-token" not in proc.stdout + proc.stderr
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_deploy_merge.py -v`
Expected: FAIL（`deploy_merge.py` 不存在，`FileNotFoundError`）

- [ ] **Step 3: 写实现**

Create `scripts/blue_green/deploy_merge.py`:

```python
#!/usr/bin/env python3
"""blue_green/deploy_merge.py —— deploy-first 的 FF-only 合并助手。

用法:
  deploy_merge.py check --pr <N> --sha <sha>
  deploy_merge.py merge --pr <N> --sha <sha> [--dry-run]

退出码:
  0  成功 / check 通过
  30 非快进（验证期间 main 被改动）
  31 PR 前置不匹配（closed / draft / head sha 不符 / base 不是 main）
  32 Gitea API 或 token 失败
  64 参数错误

只做两件事：校验 PR 仍指向待合并的 sha；用 fast-forward-only 合并。
Gitea 配置复用 deploy_report.py（FEEDBACK_GITEA_API / REPO /
DAILYCHECK_GITEA_TOKEN_FILE / DAILYCHECK_CURL），不另起一套。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from deploy_report import _repo, gitea  # noqa: E402

RC_OK = 0
RC_NOT_FF = 30
RC_PRECONDITION = 31
RC_API = 32


def _get_pr(pr: int) -> tuple[dict[str, Any] | None, str]:
    status, body = gitea("GET", f"/repos/{_repo()}/pulls/{pr}")
    if status != 200:
        return None, f"GET PR HTTP {status}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None, "GET PR 返回非 JSON"
    if not isinstance(data, dict):
        return None, "GET PR 返回非对象"
    return data, ""


def _validate(pr_data: dict[str, Any], pr: int, sha: str) -> str:
    if str(pr_data.get("state") or "") != "open":
        return f"PR #{pr} 状态是 {pr_data.get('state')!r}，不是 open"
    if pr_data.get("draft"):
        return f"PR #{pr} 是 draft"
    base = (pr_data.get("base") or {}).get("ref")
    if base != "main":
        return f"PR #{pr} base 是 {base!r}，不是 main"
    head_sha = str((pr_data.get("head") or {}).get("sha") or "")
    if head_sha != sha:
        return f"PR #{pr} head={head_sha[:12]} 与待部署 sha={sha[:12]} 不一致"
    return ""


def _payload(pr: int, sha: str, merged: bool, reason: str, merged_sha: str = "") -> dict:
    return {
        "pr": pr,
        "sha": sha,
        "merged": merged,
        "merged_sha": merged_sha,
        "reason": reason,
    }


def cmd_check(pr: int, sha: str) -> tuple[int, dict]:
    data, err = _get_pr(pr)
    if data is None:
        return RC_API, _payload(pr, sha, False, err)
    err = _validate(data, pr, sha)
    if err:
        return RC_PRECONDITION, _payload(pr, sha, False, err)
    return RC_OK, _payload(pr, sha, False, "check 通过，未合并")


def cmd_merge(pr: int, sha: str, dry_run: bool) -> tuple[int, dict]:
    data, err = _get_pr(pr)
    if data is None:
        return RC_API, _payload(pr, sha, False, err)
    err = _validate(data, pr, sha)
    if err:
        return RC_PRECONDITION, _payload(pr, sha, False, err)
    if dry_run:
        return RC_OK, _payload(pr, sha, False, "dry-run：前置校验通过，未合并")

    status, _ = gitea(
        "POST", f"/repos/{_repo()}/pulls/{pr}/merge", {"do": "fast-forward-only"}
    )
    if status not in (200, 201):
        code = RC_NOT_FF if status in (405, 409) else RC_API
        return code, _payload(pr, sha, False, f"merge HTTP {status}")

    after, err = _get_pr(pr)
    if after is None:
        return RC_API, _payload(pr, sha, False, err)
    if not after.get("merged"):
        return RC_API, _payload(pr, sha, False, "merge 返回成功但 PR 未标记 merged")
    merged_sha = str(after.get("merge_commit_sha") or sha)
    return RC_OK, _payload(pr, sha, True, "", merged_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_merge.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "merge"):
        p = sub.add_parser(name)
        p.add_argument("--pr", type=int, required=True)
        p.add_argument("--sha", required=True)
        if name == "merge":
            p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        rc, out = cmd_check(args.pr, args.sha)
    else:
        rc, out = cmd_merge(args.pr, args.sha, args.dry_run)
    print(json.dumps(out, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_deploy_merge.py -v`
Expected: 13 passed

Run: `ruff check scripts/blue_green/deploy_merge.py tests/test_deploy_merge.py`
Expected: All checks passed

- [ ] **Step 5: 更新 ARCHITECTURE（同 commit 必须）**

在 `docs/ARCHITECTURE.md` §3.8「部署结果回写 PR」段之后、`**PR 兜底轮询**` 之前插入：

```markdown
**deploy-first 合并助手（2026-10-25 起）**：`scripts/blue_green/deploy_merge.py`（stdlib）负责 FF-only 合并前的 PR 校验与合并后复核；Gitea 配置复用 `deploy_report.py` 口径（`FEEDBACK_GITEA_API` / `FEEDBACK_GITEA_REPO` / `DAILYCHECK_GITEA_TOKEN_FILE` / `DAILYCHECK_CURL`）。退出码 0/30/31/32/64，stdout 单行 JSON（`pr` / `sha` / `merged` / `merged_sha` / `reason`）。30 = 非快进（main 在验证期间被改动），31 = PR 前置不匹配，32 = API/token 失败。
```

- [ ] **Step 6: Commit**

```bash
git add scripts/blue_green/deploy_merge.py tests/test_deploy_merge.py docs/ARCHITECTURE.md
git commit -m "feat(deploy): FF-only 合并助手 deploy_merge.py"
```

---

### Task 2: `deploy_pipeline.sh` —— 候选模式前置 + checkout

**Files:**
- Modify: `scripts/blue_green/deploy_pipeline.sh`（usage、参数、`_prepare_candidate`、cleanup）
- Modify: `tests/test_deploy_pipeline.py`（`candidate_sandbox` fixture + 候选用例）
- Modify: `docs/ARCHITECTURE.md`（§3.8，加候选模式说明）

**Interfaces:**
- Consumes: `deploy_merge.py check --pr N --sha SHA`（Task 1）
- Produces: `deploy_pipeline.sh` 新开关 `--candidate`；失败退出 25；cleanup 恢复 `origin/main`

- [ ] **Step 1: 写失败测试**

在 `tests/test_deploy_pipeline.py` 末尾追加：

```python
# ---------------------------------------------------------------- 候选模式

@pytest.fixture()
def candidate_sandbox(sandbox):
    """在 sandbox 的 tmp 目录里建真 git 仓库 + bare origin + 假合并助手。"""
    repo = sandbox["tmp"]

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        )

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (repo / "app.py").write_text("v1\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").stdout.strip()

    git("checkout", "-qb", "feat")
    (repo / "app.py").write_text("v2\n", encoding="utf-8")
    git("commit", "-qam", "candidate")
    sha = git("rev-parse", "HEAD").stdout.strip()

    origin = repo / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git("remote", "add", "origin", str(origin))
    git("push", "-q", "origin", "main")
    git("push", "-q", "origin", "HEAD:refs/pull/42/head")
    git("checkout", "-q", "main")

    helper = repo / "fake_merge.py"
    helper.write_text(
        f"""#!{sys.executable}
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a", encoding="utf-8") as fh:
    fh.write("merge_helper " + " ".join(args) + "\\n")
if args and args[0] == "check" and os.environ.get("FAKE_CHECK_RC"):
    sys.exit(int(os.environ["FAKE_CHECK_RC"]))
if args and args[0] == "merge":
    if os.environ.get("FAKE_MERGE_RC"):
        sys.exit(int(os.environ["FAKE_MERGE_RC"]))
    sha = args[args.index("--sha") + 1]
    subprocess.run(
        ["git", "-C", os.environ["FAKE_ORIGIN"], "update-ref", "refs/heads/main", sha],
        check=True,
    )
sys.exit(0)
""",
        encoding="utf-8",
    )
    helper.chmod(0o755)

    up = sandbox["dir"] / "up.sh"
    up.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"up.sh $(git -C '{repo}' rev-parse --short=12 HEAD)\" >> \"{sandbox['calls']}\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    up.chmod(0o755)

    return {
        **sandbox, "repo": repo, "origin": origin,
        "sha": sha, "base": base, "helper": helper,
    }


def _cand_env(sb, **extra) -> dict:
    return {
        "DAILYCHECK_DEPLOY_MERGE_BIN": str(sb["helper"]),
        "FAKE_ORIGIN": str(sb["origin"]),
        **extra,
    }


def _head(sb) -> str:
    return subprocess.run(
        ["git", "-C", str(sb["repo"]), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _branch(sb) -> str:
    return subprocess.run(
        ["git", "-C", str(sb["repo"]), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_candidate_checks_out_pr_head_before_up(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--observe-min", "0", **_cand_env(sb))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(sb)
    assert calls[0].startswith("merge_helper check")
    assert calls[1].startswith(f"up.sh {sb['sha'][:12]}")
    assert _head(sb) == sb["sha"], "候选模式成功后工作树应停在候选 sha"


def test_candidate_rejects_stale_branch_before_up(candidate_sandbox):
    sb = candidate_sandbox
    subprocess.run(["git", "-C", str(sb["repo"]), "checkout", "-q", "main"], check=True)
    (sb["repo"] / "other.py").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(sb["repo"]), "add", "other.py"], check=True)
    subprocess.run(["git", "-C", str(sb["repo"]), "commit", "-qm", "advance"], check=True)
    subprocess.run(["git", "-C", str(sb["repo"]), "push", "-q", "origin", "main"], check=True)
    sb["calls"].unlink(missing_ok=True)

    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--observe-min", "0", **_cand_env(sb))

    assert proc.returncode == 25
    assert _calls(sb) == [], "前置失败不得调 up/verify"
    assert _branch(sb) == "main"


def test_candidate_rejects_sha_mismatch(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", "0" * 40, "--candidate", "--observe-min", "0", **_cand_env(sb)
    )

    assert proc.returncode == 25
    assert _calls(sb) == []


def test_candidate_rejects_gitea_precondition(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--observe-min", "0",
        **_cand_env(sb, FAKE_CHECK_RC="31"),
    )

    assert proc.returncode == 25
    assert not any(c.startswith("up.sh") for c in _calls(sb))


def test_candidate_dry_run_does_not_checkout(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--dry-run", **_cand_env(sb))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _branch(sb) == "main"


def test_candidate_up_failure_restores_main(candidate_sandbox):
    sb = candidate_sandbox
    _write_fake(sb["dir"] / "up.sh", "up.sh", sb["calls"], 7)
    proc = _pipe(sb, "42", sb["sha"], "--candidate", "--observe-min", "0", **_cand_env(sb))

    assert proc.returncode == 20
    assert _branch(sb) == "main"
    assert _head(sb) == sb["base"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_deploy_pipeline.py -k candidate -v`
Expected: FAIL（未知选项 `--candidate` → rc 64）

- [ ] **Step 3: 改 `deploy_pipeline.sh`**

3a. 更新文件头 usage（`--candidate` / `--merge-pr`，退出码 25/26）与函数 `usage()` 的文本。

3b. 在变量区加：

```bash
CANDIDATE=0
MERGE_PR=0
TREE_MOVED=0
ORIG_REF=""
```

3c. 参数解析加：

```bash
    --candidate)          CANDIDATE=1; shift ;;
    --merge-pr)           MERGE_PR=1; shift ;;
```

3d. 参数校验加：

```bash
[[ "${MERGE_PR}" -eq 1 && "${CANDIDATE}" -eq 0 ]] && die 64 "--merge-pr 需要同时给 --candidate"
```

3e. 在 `run_stage()` 之后、观察窗之前加：

```bash
# ---------------------------------------------------------------- 候选模式
_merge_helper() {
  local merger="${DAILYCHECK_DEPLOY_MERGE_BIN:-${SCRIPT_DIR}/deploy_merge.py}"
  "${PY}" "${merger}" "$@"
}

_restore_main() {
  if git -C "${REPO_ROOT}" fetch --quiet origin main \
     && git -C "${REPO_ROOT}" checkout --quiet -B main origin/main; then
    TREE_MOVED=0
    log "✓ 工作树已恢复到 origin/main"
    return 0
  fi
  log "WARN: 恢复 origin/main 失败 —— 工作树可能不在已知好版本（${REPO_ROOT}）"
  return 1
}

_prepare_candidate() {
  log "→ candidate 前置校验 PR #${PR} sha=${SHA:0:12}"
  if ! git -C "${REPO_ROOT}" fetch --quiet origin "refs/pull/${PR}/head"; then
    log "✗ fetch refs/pull/${PR}/head 失败"
    return 1
  fi
  local fetched
  fetched="$(git -C "${REPO_ROOT}" rev-parse FETCH_HEAD 2>/dev/null || true)"
  if [[ "${fetched}" != "${SHA}" ]]; then
    log "✗ FETCH_HEAD=${fetched:0:12} 与传入 sha=${SHA:0:12} 不一致"
    return 1
  fi
  if ! git -C "${REPO_ROOT}" fetch --quiet origin main; then
    log "✗ fetch origin main 失败"
    return 1
  fi
  if ! git -C "${REPO_ROOT}" merge-base --is-ancestor origin/main "${SHA}"; then
    log "✗ 候选分支未包含 origin/main —— 先 rebase/merge main 再部署"
    return 1
  fi
  if ! _merge_helper check --pr "${PR}" --sha "${SHA}" >>"${LOG}" 2>&1; then
    log "✗ Gitea PR 前置校验失败（closed / draft / head sha 不符）"
    return 1
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "dry-run：候选前置校验通过，不 checkout"
    return 0
  fi
  ORIG_REF="$(git -C "${REPO_ROOT}" symbolic-ref --short -q HEAD \
    || git -C "${REPO_ROOT}" rev-parse HEAD)"
  if ! git -C "${REPO_ROOT}" checkout --quiet --detach "${SHA}"; then
    log "✗ checkout ${SHA:0:12} 失败"
    return 1
  fi
  TREE_MOVED=1
  log "✓ candidate checkout ${SHA:0:12}（原 ref: ${ORIG_REF}）"
  return 0
}
```

3f. 在 `cleanup()` 的心跳 kill 之后、release 之前加：

```bash
  if [[ "${TREE_MOVED}" -eq 1 && ${rc} -ne 0 ]]; then
    _restore_main || true
  fi
```

3g. 在主链 `run_stage 20 up ...` 之前加：

```bash
if [[ "${CANDIDATE}" -eq 1 ]]; then
  run_stage 25 candidate _prepare_candidate
fi
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_deploy_pipeline.py -k candidate -v`
Expected: 6 passed

Run: `pytest tests/test_deploy_pipeline.py -v`
Expected: 现有 40 用例 + 新 6 用例全绿（不带开关时行为不变）

Run: `bash -n scripts/blue_green/deploy_pipeline.sh`
Expected: 无输出

- [ ] **Step 5: 更新 ARCHITECTURE**

在 Task 1 新增段落之后追加：

```markdown
`deploy_pipeline.sh` 新增 `--candidate`：部署前 fetch `refs/pull/<pr>/head`，校验 `FETCH_HEAD == sha`、PR open/非 draft/base=main、且 sha 包含当前 `origin/main`，然后 `checkout --detach <sha>`。任一不满足 exit 25，不触碰蓝绿；失败清理把工作树恢复为 `origin/main`。
```

- [ ] **Step 6: Commit**

```bash
git add scripts/blue_green/deploy_pipeline.sh tests/test_deploy_pipeline.py docs/ARCHITECTURE.md
git commit -m "feat(deploy): pipeline 支持候选模式（fetch + 校验 + checkout）"
```

---

### Task 3: `deploy_pipeline.sh` —— FF 合并阶段 + 失败回滚

**Files:**
- Modify: `scripts/blue_green/deploy_pipeline.sh`（`_merge_candidate` + 主链插入）
- Modify: `tests/test_deploy_pipeline.py`（merge 用例）
- Modify: `docs/ARCHITECTURE.md`（§3.8，加 `--merge-pr` 说明）

**Interfaces:**
- Consumes: `deploy_merge.py merge --pr N --sha SHA`（Task 1）、`switch.sh blue 100`
- Produces: `--merge-pr` 开关；成功时工作树回到 `main`（== sha）；失败退出 26

- [ ] **Step 1: 写失败测试**

在 `tests/test_deploy_pipeline.py` 末尾追加：

```python
def test_merge_runs_after_switch_and_before_release(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--merge-pr", "--observe-min", "0",
        **_cand_env(sb),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(sb)
    names = [c.split()[0] for c in calls]
    assert names == [
        "merge_helper", "up.sh", "verify.sh", "promote.sh", "switch.sh", "merge_helper"
    ], calls
    assert calls[0].startswith("merge_helper check")
    assert calls[-1].startswith("merge_helper merge")
    assert _state(sb)["deployed"] == sb["sha"]
    assert _branch(sb) == "main"
    assert _head(sb) == sb["sha"]


def test_merge_failure_rolls_back_blue(candidate_sandbox):
    sb = candidate_sandbox
    proc = _pipe(
        sb, "42", sb["sha"], "--candidate", "--merge-pr", "--observe-min", "0",
        **_cand_env(sb, FAKE_MERGE_RC="30"),
    )

    assert proc.returncode == 26
    assert _calls(sb)[-1] == "switch.sh blue 100"
    assert _state(sb)["deployed"] is None
    assert _branch(sb) == "main"


def test_merge_pr_without_candidate_is_usage_error(sandbox):
    proc = _pipe(sandbox, "42", SHA, "--merge-pr")
    assert proc.returncode == 64
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_deploy_pipeline.py -k merge -v`
Expected: FAIL（`--merge-pr` 未实现 / 顺序不符）

- [ ] **Step 3: 改 `deploy_pipeline.sh`**

3a. 在 `_prepare_candidate()` 之后加：

```bash
_merge_candidate() {
  log "→ FF 合并 PR #${PR}"
  local out rc
  out="$(_merge_helper merge --pr "${PR}" --sha "${SHA}" 2>&1)"
  rc=$?
  printf '%s\n' "${out}" >>"${LOG}"
  if [[ ${rc} -ne 0 ]]; then
    log "✗ FF 合并失败 rc=${rc}：${out}"
    log "  回滚流量到蓝，main 不动"
    bash "${SCRIPT_DIR}/switch.sh" blue 100 >>"${LOG}" 2>&1 \
      || log "WARN: 回滚蓝失败，见 ${LOG}"
    return 1
  fi
  log "✓ FF 合并完成：${out}"
  _restore_main || true
  local head
  head="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [[ "${head}" != "${SHA}" ]]; then
    log "WARN: 合并后本地 HEAD=${head:0:12} != ${SHA:0:12}（远端 main 已正确，下次部署会重新 fetch）"
  fi
  return 0
}
```

3b. 在 `observe_window "${OBSERVE_MIN}"` 之后、`_release success` 之前加：

```bash
if [[ "${MERGE_PR}" -eq 1 && "${DRY_RUN}" -eq 0 ]]; then
  run_stage 26 merge _merge_candidate
fi
```

3c. 确认 Task 2 的 cleanup 恢复逻辑仍然只在该候选树被移动时生效；`_merge_candidate` 成功会 `_restore_main` 把 `TREE_MOVED` 置 0。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_deploy_pipeline.py -k merge -v`
Expected: 3 passed

Run: `pytest tests/test_deploy_pipeline.py -v`
Expected: 全绿

Run: `bash -n scripts/blue_green/deploy_pipeline.sh`
Expected: 无输出

- [ ] **Step 5: 更新 ARCHITECTURE**

在 Task 2 段落之后追加：

```markdown
新增 `--merge-pr`：观察窗通过后、`release success` 之前调 `deploy_merge.py merge`；FF 失败（main 在验证期间被改动）先 `switch.sh blue 100` 回滚，再 exit 26。成功路径 `fetch origin main` + `checkout -B main origin/main`，保证本地树与远端 main 一致。
```

- [ ] **Step 6: Commit**

```bash
git add scripts/blue_green/deploy_pipeline.sh tests/test_deploy_pipeline.py docs/ARCHITECTURE.md
git commit -m "feat(deploy): pipeline 观察窗后 FF-only 合并，失败回滚蓝"
```

---

### Task 4: 归因 —— rc 25/26

**Files:**
- Modify: `scripts/blue_green/deploy_triage_pure.py`
- Modify: `tests/test_deploy_report.py`
- Modify: `docs/ARCHITECTURE.md`（§3.8，归因说明）

**Interfaces:**
- Consumes: `triage(rc, stage, log_tail) -> dict`
- Produces: rc 25/26 → `KIND_ENV` / `SCOPE_OUT`；`next_action` → `NEXT_ENVFIX`

- [ ] **Step 1: 写失败测试**

在 `tests/test_deploy_report.py` 的归因段追加：

```python
@pytest.mark.parametrize(
    "rc,stage,fragment",
    [
        (25, "candidate", "候选模式前置"),
        (26, "merge", "FF 合并失败"),
    ],
)
def test_triage_deploy_first_exit_codes(rc, stage, fragment):
    r = tp.triage(rc, stage)
    assert r["kind"] == tp.KIND_ENV
    assert r["scope"] == tp.SCOPE_OUT
    assert fragment in r["reason"]
    assert tp.next_action(r, {"verdict": tp.VERDICT_UNKNOWN}) == tp.NEXT_ENVFIX
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_deploy_report.py -k deploy_first_exit_codes -v`
Expected: FAIL（kind 是 unknown）

- [ ] **Step 3: 改 `deploy_triage_pure.py`**

在 `triage()` 的 `if rc == 0:` 之后、`if stage == "verify-drift":` 之前插入：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_deploy_report.py -k deploy_first_exit_codes -v`
Expected: 2 passed

Run: `pytest tests/test_deploy_report.py -v`
Expected: 全绿

- [ ] **Step 5: 更新 ARCHITECTURE**

在 Task 3 段落之后追加：

```markdown
归因：rc 25（候选前置不满足）与 rc 26（FF 合并失败）均映射 `KIND_ENV` / `SCOPE_OUT` / `NEXT_ENVFIX`，`stage=candidate` / `merge` 写入事件流；两者都不授权改 PR 代码。
```

- [ ] **Step 6: Commit**

```bash
git add scripts/blue_green/deploy_triage_pure.py tests/test_deploy_report.py docs/ARCHITECTURE.md
git commit -m "feat(deploy): rc 25/26 归因到 envfix，不授权改代码"
```

---

### Task 5: `deploy_report.changed_files()` 候选感知

**Files:**
- Modify: `scripts/blue_green/deploy_report.py:164-180`
- Modify: `tests/test_deploy_report.py`
- Modify: `docs/ARCHITECTURE.md`（§3.8，changed_files 口径）

**Interfaces:**
- Consumes: `git -C <repo> merge-base --is-ancestor <sha> origin/main`
- Produces: `changed_files(repo, sha, base="")` 在候选 sha 上返回整个 PR 的改动

- [ ] **Step 1: 写失败测试**

在 `tests/test_deploy_report.py` 追加：

```python
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "base.py").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def test_changed_files_candidate_uses_merge_base(git_repo):
    base = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    _git(git_repo, "update-ref", "refs/remotes/origin/main", base)
    _git(git_repo, "checkout", "-qb", "feat")
    (git_repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-qm", "a")
    (git_repo / "b.py").write_text("b\n", encoding="utf-8")
    _git(git_repo, "add", "b.py")
    _git(git_repo, "commit", "-qm", "b")
    head = _git(git_repo, "rev-parse", "HEAD").stdout.strip()

    files = deploy_report.changed_files(str(git_repo), head)

    assert set(files) == {"a.py", "b.py"}, files


def test_changed_files_merged_uses_first_parent(git_repo):
    _git(git_repo, "checkout", "-qb", "feat")
    (git_repo / "a.py").write_text("a\n", encoding="utf-8")
    _git(git_repo, "add", "a.py")
    _git(git_repo, "commit", "-qm", "a")
    _git(git_repo, "checkout", "main")
    _git(git_repo, "merge", "--no-ff", "-m", "merge feat", "feat")
    merge_sha = _git(git_repo, "rev-parse", "HEAD").stdout.strip()
    _git(git_repo, "update-ref", "refs/remotes/origin/main", merge_sha)

    files = deploy_report.changed_files(str(git_repo), merge_sha)

    assert set(files) == {"a.py"}, files
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_deploy_report.py -k changed_files -v`
Expected: `test_changed_files_candidate_uses_merge_base` FAIL（只返回 `b.py`）

- [ ] **Step 3: 改 `deploy_report.py`**

把 `changed_files()` 替换为：

```python
def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True, timeout=20, check=False,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def changed_files(repo: str, sha: str, base: str = "") -> list[str]:
    """本 PR 改动过的文件。

    deploy-first 下部署目标是 PR head（尚未合入 main），``sha^1..sha`` 只能看到
    最后一个 commit —— 用 ``merge-base(origin/main, sha)..sha`` 才覆盖整个 PR。
    已合入 main 的 merge commit 仍走 ``sha^1..sha``（第一个父提交即合并前 main）。
    两个 base 都取不到时退回旧行为，保守方向是安全的：命中越少，
    ``assess_alignment`` 越倾向 unknown 而非误判 aligned。
    """
    if base:
        rng = f"{base}..{sha}"
    elif _is_ancestor(repo, sha, "origin/main"):
        rng = f"{sha}^1..{sha}"
    else:
        merge_base = _git(repo, "merge-base", "origin/main", sha).strip()
        rng = f"{merge_base}..{sha}" if merge_base else f"{sha}^1..{sha}"
    out = _git(repo, "diff", "--name-only", rng)
    files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not files and base:
        out = _git(repo, "diff", "--name-only", f"{sha}^1..{sha}")
        files = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return files
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_deploy_report.py -k changed_files -v`
Expected: 2 passed

Run: `pytest tests/test_deploy_report.py -v`
Expected: 全绿

- [ ] **Step 5: 更新 ARCHITECTURE**

在 Task 4 段落之后追加：

```markdown
`deploy_report.changed_files()` 候选感知：sha 已合入 `origin/main` 时用 `sha^1..sha`；否则用 `merge-base(origin/main, sha)..sha`，保证 deploy-first 下 autofix 能看到整个 PR 的改动。
```

- [ ] **Step 6: Commit**

```bash
git add scripts/blue_green/deploy_report.py tests/test_deploy_report.py docs/ARCHITECTURE.md
git commit -m "fix(deploy): changed_files 对未合并候选 sha 用 merge-base diff"
```

---

### Task 6: 文档同步 + 全量验证

**Files:**
- Modify: `docs/agent-framework/workflow.md`（§四/§五）
- Modify: `docs/agent-framework/session-bootstrap.md`（§二）
- Modify: `scripts/blue_green/README.md`
- Modify: `deploy/scripts/dc-notify-prod-agent.sh`（注释）
- Modify: `docs/ARCHITECTURE.md`（最终一致性检查）

- [ ] **Step 1: 改 `workflow.md` §四/§五**

§四 的「Prod Agent 在 CI 全绿后判定并执行 merge」改为：

```markdown
**Prod Agent 在 CI 全绿后判定并执行「候选部署」**：CI 两必需 check 全绿 → 独立审查 → 调 `deploy_pipeline.sh <PR> <sha> --candidate --merge-pr`。验证/切流/观察通过后由脚本执行 fast-forward-only merge；merge 前任何一步失败，PR 保持 open，不产生未验证的 main 提交。
```

§五 的时序图在 `up` 之前加 `fetch refs/pull/<N>/head → checkout --detach`，在 `观察窗` 之后加 `FF-only merge → checkout -B main origin/main`，并把 `deploy_state.release` 放到 merge 之后。

- [ ] **Step 2: 改 `session-bootstrap.md` §二**

第 2、3 步改为：

```markdown
2. CI 全绿后**不直接 merge**：读 PR 的「报告 1/4」与「生产侧事项」独立评估；
   有异议 → PR 评论打回 + 开 Issue。无异议 → 进入候选部署。
3. 调 `deploy_pipeline.sh <PR> <sha> --candidate --merge-pr`：
   verify 八项 → 绿转正 → 灰度 → 观察窗 → **FF-only merge**。
   merge 由脚本在锁内完成；失败自动切回蓝，PR 保持 open。
```

- [ ] **Step 3: 改 `scripts/blue_green/README.md`**

在「端到端编排」段加：

```markdown
deploy-first（2026-10-25 起，Prod Agent 使用）：

```bash
bash scripts/blue_green/deploy_pipeline.sh <pr-num> <pr-head-sha> --candidate --merge-pr
```

`--candidate` 校验 PR head 包含当前 main 并 checkout；`--merge-pr` 在观察窗后
FF-only 合并。失败路径：切回蓝 + 工作树恢复 `origin/main`，PR 保持 open。
退出码 25 = 候选前置不满足，26 = FF 合并失败。
```

- [ ] **Step 4: 改 `dc-notify-prod-agent.sh` 注释**

把 `check_suite → dailycheck-ci（CI 全绿即唤醒 merge 判定）` 改为：

```bash
#   check_suite   → dailycheck-ci      （CI 全绿即唤醒候选部署判定；
#                                       prompt 内置 [SILENT] 静默语义）
```

- [ ] **Step 5: 全量验证**

```bash
bash -n scripts/blue_green/deploy_pipeline.sh
ruff check scripts/blue_green/ tests/test_deploy_merge.py tests/test_deploy_pipeline.py tests/test_deploy_report.py
pytest tests/test_deploy_merge.py tests/test_deploy_pipeline.py tests/test_deploy_report.py -v
pytest -q --tb=short
```

Expected:
- `bash -n` 无输出
- ruff: All checks passed
- 三个定向文件全绿
- 全量 pytest 失败集 ⊆ `tests/known_failures.txt`（本地基线 14 条）

- [ ] **Step 6: 检查 ARCHITECTURE 一致性**

Run: `python scripts/arch_touched_sections.py`
Expected: 提示的章节都在本分支已更新（§3.8）。

- [ ] **Step 7: Commit**

```bash
git add docs/agent-framework/workflow.md docs/agent-framework/session-bootstrap.md \
  scripts/blue_green/README.md deploy/scripts/dc-notify-prod-agent.sh docs/ARCHITECTURE.md
git commit -m "docs(deploy): workflow/README 同步 deploy-first 流程"
```

> ⚠️ 实施 PR 的 body 必须带一行 `Owner-Authorization: 2026-10-25 Eric 批准 deploy-first 流程` —— 本 PR 触及 `docs/agent-framework/workflow.md`（红线 6），否则 `agent-framework-reports` 会红。

**发布顺序（Prod Agent skill 在仓库外，由 owner 执行）**：脚本 + 测试先合入（默认关闭，旧流程不受影响）→ 下一次普通 merge-first 部署把脚本带到 VPS → 再改 Prod Agent skill 为 `--candidate --merge-pr` → 最后按新文档执行一次真实 deploy-first 验收。回退只需把 skill 改回旧调用，脚本开关不用回滚。

---

## Self-Review

**Spec coverage**

| Spec 章节 | 对应任务 |
|---|---|
| §3.1 新时序（candidate → verify → switch → observe → FF merge） | Task 2 + 3 |
| §3.2 C1 包含 main | Task 2 `_prepare_candidate` |
| §3.2 C2 main 冻结 | Task 3（FF 失败回滚）+ 文档 Task 6 |
| §3.2 C3 FF-only | Task 1 `cmd_merge` |
| §3.3 不变量 `deployed.sha == main` | Task 3 `_restore_main` + 断言 |
| §4.1 `--candidate` / `--merge-pr` / cleanup | Task 2 + 3 |
| §4.2 `deploy_merge.py` | Task 1 |
| §4.3 rc 25/26 + changed_files | Task 4 + 5 |
| §4.4 文档同步 | Task 6 |
| §4.5 Prod Agent skill 发布顺序 | Task 6 文档说明（skill 在仓库外，由 owner 执行） |
| §5 测试计划 | 每个 Task 的测试步骤 |
| §6 风险（Gitea FF 支持已确认） | Task 1 固定 `fast-forward-only` |
| §7 验收标准 | Task 6 全量验证 |

**Placeholder scan:** 无 TBD/TODO；所有代码步骤均含完整代码。

**Type consistency:** `deploy_merge.py` 的 `gitea()` 复用 `deploy_report`；`_repo()` 为 `deploy_report` 现有私有函数，同目录导入一致；CLI 退出码在 Task 1/3/4 中一致（30/31/32 → pipeline 25/26）。
