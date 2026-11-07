"""Issue #123 回归：verify 探针凭据可移植 + 失败明细可读。

三个缺陷面：
1. `token_urlsafe` 可能以 '-' 开头 → click 把密码当选项（create-user 失败）
2. `create-user` 未用 '--' 选项终止符 → 显式传入 '-' 开头密码同样失败
3. verify 失败只写报告文件，pipeline 日志 / deploy_report 尾部看不到失败 check
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE = REPO_ROOT / "scripts" / "blue_green" / "deploy_pipeline.sh"
VERIFY = REPO_ROOT / "scripts" / "blue_green" / "verify.sh"

_SUMMARY_START = "# <<< emit-failure-summary:start >>>"
_SUMMARY_END = "# <<< emit-failure-summary:end >>>"


def _pipeline_generator_code() -> str:
    """取出 deploy_pipeline.sh 里生成探针密码的 Python 片段。"""
    content = PIPELINE.read_text(encoding="utf-8")
    line = next(
        (
            ln for ln in content.splitlines()
            if ln.strip().startswith("SIM_PASS=") and "-c '" in ln
        ),
        None,
    )
    assert line is not None, "deploy_pipeline.sh 找不到 SIM_PASS 自动生成行"
    return line.split("-c '", 1)[1].rsplit("'", 1)[0]


def _run_python(code: str) -> str:
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_generated_probe_password_never_starts_with_dash():
    """同一条生成语句执行 300 次，首字符不得为 '-'（约 1.5%/次的原始缺陷）。"""
    code = _pipeline_generator_code()
    wrapper = f"src = {code!r}\nfor _ in range(300):\n    exec(src)\n"
    samples = _run_python(wrapper).splitlines()
    assert len(samples) == 300, f"生成器未产出 300 条样本：{len(samples)}"
    bad = [s for s in samples if s.startswith("-")]
    assert not bad, f"探针密码以 '-' 开头（click 会当选项）：{bad[:3]}"
    assert all(len(s) >= 18 for s in samples), "探针密码长度异常"


def test_create_user_uses_option_terminator():
    fn = VERIFY.read_text(encoding="utf-8")
    assert re.search(
        r'create-user\s+--admin\s+--\s+"\$\{SIM_USER_USER\}"\s+"\$\{SIM_USER_PASSWORD\}"',
        fn,
    ), "verify.sh 的 create-user 缺少 '--' 选项终止符（Issue #123）"


def _init_master(db_dir: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "init-master"],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        env={**os.environ, "DAILYCHECK_DB_DIR": str(db_dir),
             "DAILYCHECK_SECRET_KEY": "test-secret"},
    )
    assert r.returncode == 0, r.stdout + r.stderr


def _run_create_user(db_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "create-user", *args],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        env={**os.environ, "DAILYCHECK_DB_DIR": str(db_dir),
             "DAILYCHECK_SECRET_KEY": "test-secret"},
    )


def test_dash_prefixed_credentials_succeed_with_terminator(tmp_path):
    """真实 CLI：带 '--' 时 '-' 开头用户名/密码可正常创建（绿库 provision 形态）。"""
    _init_master(tmp_path)
    ok = _run_create_user(tmp_path, "--admin", "--", "-probe-user", "-probe-pass")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "Created user" in ok.stdout


def test_without_terminator_click_rejects_dash_prefixed_password(tmp_path):
    """反证：没有 '--' 时同一组凭据会被 click 当选项（原缺陷可复现）。"""
    _init_master(tmp_path)
    bad = _run_create_user(tmp_path, "-probe-user", "-probe-pass", "--admin")
    assert bad.returncode != 0
    assert "No such option" in (bad.stdout + bad.stderr)


def _extract_summary_fn() -> str:
    content = VERIFY.read_text(encoding="utf-8")
    assert _SUMMARY_START in content and _SUMMARY_END in content, (
        "verify.sh 缺 emit-failure-summary 标记（测试抽取函数体的锚点）"
    )
    body = content.split(_SUMMARY_START, 1)[1].split(_SUMMARY_END, 1)[0].strip()
    assert "_emit_failure_summary() {" in body, "标记之间不是该函数"
    return body


def _run_summary_fn(overall_pass: int, r2_state: str, n2_note: str) -> str:
    script = "\n".join([
        "set -uo pipefail",
        f"OVERALL_PASS={overall_pass}",
        'R1="✅ PASS"',
        f'R2="{r2_state}"',
        'R3="⏳"; R4="⏳"; R5="⏳"; R6="⏳"; R7="⏳"; R8="⏳"',
        'N1=""', f'N2="{n2_note}"',
        'N3=""; N4=""; N5=""; N6=""; N7=""; N8=""',
        "CHECK_NAME=()",
        'CHECK_NAME[1]="集成测试"',
        'CHECK_NAME[2]="模拟用户操作"',
        _extract_summary_fn(),
        "_emit_failure_summary",
        "",
    ])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_failure_summary_names_failed_check_and_its_log_tail():
    out = _run_summary_fn(0, "❌ FAIL", "FAIL: 绿库账号未就位")
    assert "FAILED_CHECK=2" in out
    assert "模拟用户操作" in out
    assert "FAIL: 绿库账号未就位" in out
    assert "FAILED_CHECK=1" not in out, "通过项不应出现在失败明细"


def test_failure_summary_silent_on_success():
    assert _run_summary_fn(1, "✅ PASS", "").strip() == ""
