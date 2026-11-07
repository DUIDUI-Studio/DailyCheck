"""Execute the portable source checkout against a local authenticated Git HTTP endpoint."""
from __future__ import annotations

import base64
import functools
import http.server
import os
import subprocess
import textwrap
import threading
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    # BaseLoader keeps the workflow key `on` a string.
    return yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)


def test_both_servers_have_explicit_checkout_and_python_paths():
    value = workflow()
    assert set(value["jobs"]) == {"test", "agent-framework-reports"}
    assert value["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert value["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.event_name" in value["concurrency"]["group"]
    assert "github.event.merge_group.head_sha" in value["concurrency"]["group"]
    assert "github.run_id" in value["concurrency"]["group"]
    assert value["on"]["merge_group"]["types"] == ["checks_requested"]
    assert value["jobs"]["agent-framework-reports"]["if"].startswith("always()")
    for job in value["jobs"].values():
        steps = job["steps"]
        by_name = {step["name"]: step for step in steps}
        gh = by_name["Checkout candidate H on GitHub"]
        source = by_name["Checkout candidate H on Gitea"]
        assert gh["uses"] == "actions/checkout@v7"
        assert gh["if"] == "github.server_url == 'https://github.com'"
        assert source["if"] == "github.server_url != 'https://github.com'"
        assert not any("://" in step.get("uses", "") for step in steps)
        assert gh["with"]["ref"] == "${{ github.event.merge_group.head_sha || github.event.pull_request.head.sha || github.sha }}"
        assert gh["with"]["persist-credentials"] == "false"
        assert by_name["Setup Python on GitHub"]["uses"] == "actions/setup-python@v7"
        assert by_name["Python on Gitea (runner image)"]["if"] == source["if"]
        assert '"PyYAML==6.0.3"' in by_name["Install dependencies"]["run"]
        prepare = by_name["Prepare isolated job paths"]["run"]
        assert "mktemp -d" in prepare and "$GITHUB_ENV" in prepare
        assert "Pytest (full suite, including MCP)" in by_name
        assert "Zero-new-failure gate" in by_name
        if job is value["jobs"]["agent-framework-reports"]:
            assert by_name["Pytest (full suite, including MCP)"]["if"] == "github.server_url == 'https://github.com'"
        assert "candidate_binding.py" in by_name["Bind tested H and event H/B to this run"]["run"]


def test_source_checkout_tests_head_with_scoped_auth_and_no_persisted_token(tmp_path):
    def git(cwd, *args):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.PIPE).strip()

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Checkout fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "sample").write_text("base")
    git(source, "add", ".")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")
    (source / "sample").write_text("candidate")
    git(source, "commit", "-qam", "candidate")
    head = git(source, "rev-parse", "HEAD")
    directory = tmp_path / "server" / "owner"
    directory.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(source), str(directory / "repo.git"))
    git(directory / "repo.git", "update-server-info")
    token = "local-checkout-fixture-token"
    expected = "basic " + base64.b64encode(("x-access-token:" + token).encode()).decode()
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.headers.get("Authorization", ""))
            if self.headers.get("Authorization", "").lower() != expected.lower():
                self.send_error(401)
                return
            return super().do_GET()

        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=str(tmp_path / "server"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    step = next(step for step in workflow()["jobs"]["test"]["steps"] if step["name"] == "Checkout candidate H on Gitea")
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    env = {**os.environ, "CHECKOUT_SERVER": f"http://127.0.0.1:{server.server_port}", "CHECKOUT_TOKEN": token,
           "CI_REPOSITORY": "owner/repo", "CI_HEAD_SHA": head, "CI_BASE_SHA": base, "GITHUB_WORKSPACE": str(workspace)}
    try:
        result = subprocess.run(["bash", "-e", "-c", textwrap.dedent(step["run"])], cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert requests and all(value.lower() == expected.lower() for value in requests)
    assert git(workspace, "rev-parse", "HEAD") == head
    assert git(workspace, "rev-parse", "origin/main") == head
    assert (workspace / "sample").read_text() == "candidate"
    assert token not in (workspace / ".git/config").read_text()
    assert token not in result.stdout + result.stderr


def test_job_temp_roots_never_reuse_each_other(tmp_path):
    prepare = workflow()["jobs"]["test"]["steps"][0]["run"]
    roots = []
    for n in (1, 2):
        output = tmp_path / f"job-{n}-env"
        env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "CI_RUN_ID": "100", "CI_RUN_ATTEMPT": "2", "GITHUB_ENV": str(output)}
        result = subprocess.run(["bash", "-e", "-c", prepare], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
        roots.append(fields["CI_ROOT"])
        assert Path(fields["TMPDIR"]).is_dir() and Path(fields["DAILYCHECK_DB_DIR"]).is_dir()
    assert roots[0] != roots[1]
