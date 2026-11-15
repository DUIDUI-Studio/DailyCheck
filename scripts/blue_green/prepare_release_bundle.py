"""Freeze exact Git sources and an INERT PR20 adoption preview, without network."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = (
    "release_credentials.py",
    "release_entry.py",
    "release_selector.py",
    "release_parent.py",
    "release_refresh.py",
    "release_review.py",
)


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args])


def build(head: str, base: str, output: Path):
    if any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, base)):
        raise ValueError("exact-commit-required")
    for value in (head, base):
        if git("rev-parse", value + "^{commit}").decode().strip() != value:
            raise ValueError("commit-identity")
    raw = git("show", head + ":scripts/blue_green/release_credentials.py")
    spec = importlib.util.spec_from_loader("_exact_release_renderer", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(REPO / "scripts/blue_green/release_credentials.py")
    # The renderer reads its sibling templates: require the current exact bytes.
    for name in (*TEMPLATES, "release_issues.py"):
        path = "scripts/blue_green/" + name
        if (REPO / path).read_bytes() != git("show", head + ":" + path):
            raise ValueError("renderer-checkout-drift")
    exec(compile(raw, module.__file__, "exec"), module.__dict__)
    output.mkdir(mode=0o700)

    def put(relative, raw, mode=0o600):
        target = output / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with target.open("xb") as file:
            file.write(raw)
        target.chmod(mode)

    def document(relative, value):
        put(relative, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())

    pins, serving = {}, {}
    for relative in sorted(module.PINS):
        content = git("show", head + ":" + relative)
        put("engine/" + relative, content)
        pins[relative] = hashlib.sha256(content).hexdigest()
        result = subprocess.run(
            ["git", "-C", str(REPO), "show", base + ":" + relative], capture_output=True
        )
        serving[relative] = (
            hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None
        )
    for relative in ("scripts/__init__.py", "scripts/prod_agent/__init__.py"):
        put("engine/" + relative, b"")
    for name in (*TEMPLATES, "release_issues.py", "release_config.py", "prepare_release_bundle.py"):
        relative = "scripts/blue_green/" + name
        if relative not in module.PINS:
            put("engine/" + relative, git("show", head + ":" + relative))
    remote = "/opt/dailycheck/pr20-release/" + head
    root = "/var/lib/dailycheck-deploy/pr20-" + head
    profile = dict(
        uid=0,
        python="/root/DailyCheck/.venv/bin/python",
        python_target="/usr/bin/python3.12",
        python_sha256="e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f",
        root=root,
        repo="/root/DailyCheck",
        engine=remote + "/engine",
        state="/var/lib/dailycheck-deploy",
        tmp=root + "/scratch/tmp",
        log=root + "/pipeline.log",
        shim="/root/.hermes/scripts/dailycheck_pr_poll.py",
        shim_sha="3082850da60e6b17ae954218c705b567a680e9f3ff1355aad9308caf0a94a7f5",
        pr=20,
        head=head,
        base=base,
        generation=11,
        deployed_generation=10,
        session="pr20-whole-run-" + head,
        task="task_e9db1f5316a7",
        dispatch="ctx_98c66167ff83",
        pins=pins,
        binding_sha256=None,
        capability=None,
        issue_protocol=None,
        intake_policy="/etc/dailycheck/github-ordinary-intake.policy.json",
        intake_key="/etc/dailycheck/github-ordinary-intake.key",
        reducer_db="/var/lib/dailycheck-prod-agent/event_reducer.sqlite3",
    )
    document("profile.private.json", profile)
    module.render(profile, output / "preview", preview=True)
    document(
        "live-inputs.private.json",
        dict(
            source_binding=None,
            private_source_issue=None,
            issue_protocol=None,
            whole_run_capability=None,
            ci_run_id=None,
            ci_attempt=None,
            ci_status="pending_exact_H",
            source_H=head,
            serving_B=base,
            public_issue=dict(
                repository="DUIDUI-Studio/DailyCheck",
                repository_id=1448986421,
                number=19,
                id=6391796319,
            ),
            later_owner_action="Approve one exact reviewed adoption and bounded experiment after binding, scope grants and new exact-H CI are confirmed",
        ),
    )
    policy = dict(
        version=1,
        repository_id=1448986421,
        repository="DUIDUI-Studio/DailyCheck",
        mode="quarantined",
        since_ms=0,
        review_targets=[],
        automatic_pr_range=None,
        excluded_open_prs=[15, 17, 18],
        review_actor_ids=[354261992],
    )
    document("pilot.policy.disabled.json", policy)
    for name in ("wdg-ci.prompt.md", "dailycheck-deploy.prompt.md", "ADOPTION.md"):
        text = git("show", head + ":deploy/prod-release/" + name).decode()
        for key, value in dict(
            HEAD=head, BASE=base, ROOT=root, CI_RUN="PENDING_EXACT_H_CI", CI_ATTEMPT="null"
        ).items():
            text = text.replace("{{" + key + "}}", value)
        put("config/" + name, text.encode())
    put("source.patch", git("diff", "a5f509cc48eca04869e283959689c9ce81a791b6", head, "--"))
    put("source.tar", git("archive", "--format=tar", head))
    members = {
        str(path.relative_to(output)): dict(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size=path.stat().st_size,
            mode=oct(path.stat().st_mode & 0o777),
        )
        for path in sorted(output.rglob("*"))
        if path.is_file()
    }
    document(
        "manifest.json",
        dict(
            version=1,
            status="PREPARED_INERT_NOT_ADOPTED_NOT_PRODUCTION_READY",
            source_H=head,
            serving_B=base,
            template_PR18="6351d7952b15382814e81e23a017bcedb1aa0a8c",
            source_pins=pins,
            serving_pins=serving,
            members=members,
            exact_H_CI=None,
            live_attempts_this_preparation=0,
            history="Issue19 repair3/CI4/crossCI8; config1-3/cron997050/gen11 preserved",
        ),
    )
    return output / "manifest.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.head, args.base, args.output))


if __name__ == "__main__":
    os.umask(0o077)
    main()
