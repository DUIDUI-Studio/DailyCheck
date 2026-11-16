"""Resolve one signed native work item into the existing exact release protocol.

The installed bootstrap selects an immutable engine, environment and interpreter.
CLI arguments select only a native trigger, never PR/head/CI/Issue permissions.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import time
import types
import urllib.parse
from pathlib import Path

CONFIG = None  # BUILD_CONFIG


class CurrentReadinessBlocked(ValueError):
    """Closed public facts, without provider bodies or private source identities."""

    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def checked_file(path, uid, *, private=False):
    path = Path(path)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == uid
                and not info.st_mode & 0o022, "automatic-path-custody")
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_nlink == 1
            and not info.st_mode & (0o077 if private else 0o022), "automatic-file-custody")
    return info


def regular(path, uid, *, digest=None, private=False, limit=1048576):
    info = checked_file(path, uid, private=private)
    require(info.st_size <= limit, "automatic-file-size")
    path = Path(path)
    raw = path.read_bytes()
    require(digest is None or hashlib.sha256(raw).hexdigest() == digest, "automatic-file-drift")
    return raw


def document(path, uid):
    return json.loads(regular(path, uid, private=True))


def create(path, value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def bootstrap(config):
    require(sys.flags.optimize == 0 and sys.flags.isolated == 1 and isinstance(config, dict)
            and set(config) == {"profile", "roots", "production_env", "production_env_sha256",
                                "curl", "curl_sha256", "hermes_processes"}, "automatic-bootstrap")
    template = config["profile"]
    require(os.getuid() == os.geteuid() == template["uid"], "automatic-uid")
    for relative, digest in template["pins"].items():
        require(not Path(relative).is_absolute() and ".." not in Path(relative).parts,
                "automatic-engine-path")
        regular(Path(template["engine"]) / relative, template["uid"], digest=digest)
    require(str(Path(sys.executable).resolve()) == template["python_target"], "automatic-python")
    regular(template["python_target"], template["uid"], digest=template["python_sha256"], limit=67108864)
    # Pin namespaces before imports: a candidate checkout cannot shadow the engine.
    for name, relative in (("scripts", "scripts"), ("scripts.prod_agent", "scripts/prod_agent"),
                           ("scripts.blue_green", "scripts/blue_green")):
        require(name not in sys.modules, "automatic-preimported-engine")
        module = types.ModuleType(name)
        module.__path__ = [str(Path(template["engine"]) / relative)]
        module.__package__ = name
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
        module.__spec__.submodule_search_locations = module.__path__
        sys.modules[name] = module
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(sys.modules[parent], attr, module)
    sys.path.insert(0, template["engine"])
    from scripts.blue_green.release_credentials import validate

    validate(template, Path(template["root"]))
    roots = Path(config["roots"])
    require(roots.is_absolute() and ".." not in roots.parts and not any(c.isspace() for c in str(roots))
            and roots != Path(template["repo"]) and Path(template["repo"]) not in roots.parents
            and roots not in Path(template["repo"]).parents,
            "automatic-roots")
    raw = regular(config["production_env"], template["uid"],
                  digest=config["production_env_sha256"], private=True)
    values = {}
    for line in raw.decode().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            require(key not in values, "automatic-environment-duplicate")
            values[key] = value
    keys = {"DAILYCHECK_HOSTING_PROVIDER", "DAILYCHECK_GITHUB_LANE", "DAILYCHECK_GITHUB_REPO",
            "DAILYCHECK_GITHUB_READ_TOKEN_FILE", "DAILYCHECK_GITHUB_REPORT_TOKEN_FILE",
            "DAILYCHECK_GITHUB_FINALIZE_TOKEN_FILE", "DAILYCHECK_PROD_REPO",
            "DAILYCHECK_DEPLOY_STATE_DIR", "DAILYCHECK_DEPLOY_EVENT_LOG", "DAILYCHECK_PROD_AGENT_INBOX"}
    require(set(values) == keys and values["DAILYCHECK_HOSTING_PROVIDER"] == "github"
            and values["DAILYCHECK_GITHUB_LANE"] == "ordinary"
            and values["DAILYCHECK_GITHUB_REPO"] == "DUIDUI-Studio/DailyCheck"
            and values["DAILYCHECK_DEPLOY_STATE_DIR"] == template["state"]
            and values["DAILYCHECK_PROD_REPO"] == template["repo"], "automatic-environment")
    for key in list(os.environ):
        if key.startswith("DAILYCHECK_GITHUB_") or key in {"GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN"}:
            os.environ.pop(key)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE"):
        os.environ.pop(key, None)
    os.environ.update(values)
    os.environ.update(DAILYCHECK_GITHUB_INTAKE_ENABLED="1",
                      DAILYCHECK_GITHUB_INTAKE_POLICY_FILE=template["intake_policy"],
                      DAILYCHECK_GITHUB_INTAKE_KEY_FILE=template["intake_key"],
                      DAILYCHECK_EVENT_REDUCER_DB=template["reducer_db"],
                      DAILYCHECK_CURL=config["curl"])
    regular(config["curl"], template["uid"], digest=config["curl_sha256"])
    raw = regular(template["shim"], template["uid"], digest=template["shim_sha"])
    shim = types.ModuleType("_automatic_approved_egress")
    shim.__file__ = template["shim"]
    exec(compile(raw, shim.__file__, "exec"), shim.__dict__)
    shim.configure_egress()  # No JWT/request/refresh.
    return template


def claim(template, trigger, *, authenticated=True):
    from scripts.prod_agent import github_intake

    require(re.fullmatch(r"trig_[0-9a-f]{24}", trigger) is not None, "automatic-trigger")
    database = Path(template["reducer_db"])
    checked_file(database, template["uid"])
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("""SELECT repo,pr_number,head_sha,event,route,state,last_error,payload_json
            FROM trigger_queue WHERE trigger_id=?""", (trigger,)).fetchall()
    require(len(rows) == 1, "automatic-native-claim-missing")
    repo, pr, head, event, route, state, error, raw = rows[0]
    payload = json.loads(raw)
    require(repo == github_intake.REPO.lower() and event == "check_suite" and route == "wdg-ci"
            and payload.get("repo_full_name") == github_intake.REPO
            and payload.get("event") == event
            and payload.get("trigger_id") == trigger and payload.get("pr_number") == pr
            and payload.get("pr_head_sha") == head and payload.get("dispatch_phase") == "prod_review"
            and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("admission_id", ""))) is not None,
            "automatic-native-claim-identity")
    if authenticated:
        rule = github_intake.policy()
        require(rule.mode == "automatic" and rule.review_actors and rule.accepts(pr, head)
                and github_intake.authentic(payload, frozenset({"ready"}))
                and (state == "claimed" and error in {"", "dispatch_sending"}
                     or state == "done" and error in {"awaiting_agent_receipt", "review_approved"}),
                "automatic-native-claim-unconfirmed")
    return payload


def pr_files(client, pr):
    """GitHub files have filename identities; keep that ABI in this release lane.

    The approved shared HostingClient and its numeric-ID pagination stay intact.
    Reuse its bounded transport/page budgets and reject partial or drifting lists.
    """
    from scripts.blue_green.release_issues import CODE
    from scripts.hosting import HostingError, Provider

    require(client.config.provider is Provider.GITHUB and client.config.repo == CODE
            and type(pr) is int and 0 < pr <= 2147483647, "automatic-files-destination")
    path = f"/repos/{CODE}/pulls/{pr}/files"
    items, seen = [], set()
    for page in range(1, client.MAX_PAGES + 1):
        data, headers = client.request("GET", path + "?" + urllib.parse.urlencode(
            {"per_page": client.PER_PAGE, "page": page}))
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise HostingError("invalid_page_items")
        for row in data:
            name = row.get("filename")
            if (not isinstance(name, str) or not 0 < len(name) <= 512
                    or Path(name).is_absolute() or str(Path(name)) != name
                    or not Path(name).parts
                    or ".." in Path(name).parts or any(ord(c) < 32 for c in name)
                    or name in seen):
                raise HostingError("invalid_or_repeated_page_item")
            seen.add(name)
        items.extend(data)
        links = re.findall(r'<([^>]+)>;\s*rel="next"', headers, flags=re.I)
        if links:
            link = urllib.parse.urlsplit(links[0])
            expected = urllib.parse.urlsplit(client.config.api_url + path)
            if (len(links) != 1
                    or (link.scheme, link.netloc, link.path)
                    != (expected.scheme, expected.netloc, expected.path)
                    or urllib.parse.parse_qs(link.query).get("page") != [str(page + 1)]
                    or not data):
                raise HostingError("invalid_pagination_link")
        if not links and len(data) < client.PER_PAGE:
            return items
    raise HostingError("pagination_exhausted")


def plan(config, trigger):
    from scripts.blue_green import deploy_state, publication_authority
    from scripts.blue_green.release_issues import CODE, OPS, issue
    from scripts.hosting import HostingClient, HostingConfig
    from scripts.prod_agent import github_intake

    template = config["profile"]
    require(deploy_state.process_lock_fd() is not None, "automatic-process-fence")
    payload = claim(template, trigger)
    directory = Path(template["state"])
    with deploy_state.state_transaction(directory):
        state = deploy_state.load_state(directory, strict=True)
        require(state.get("lock") is None and not deploy_state.native_blocked(directory, state),
                "automatic-native-unresolved")
        binding = publication_authority.resolve_source_binding(directory, payload["pr_number"])
    require(binding.issue_identities is not None, "automatic-issue-identities-missing")
    require(binding.source[:5] == ("github", "github.com", 1449125557, OPS, "issue"),
            "automatic-source-transport")
    gate = github_intake.readiness(payload)
    if gate.get("ready") is not True:
        cause = gate.get("reason")
        if not isinstance(cause, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", cause) is None:
            cause = "UNKNOWN"
        raise CurrentReadinessBlocked("automatic-current-readiness-unconfirmed",
                                      dict(ready=False, cause=cause, identity_matches=None))
    matches = dict(head=gate.get("actual_head") == payload["pr_head_sha"],
                   base=gate.get("base_sha") == payload.get("pr_base_sha"),
                   ci_run=gate.get("run_id") == payload.get("observed_ci_run_id"),
                   ci_attempt=gate.get("run_attempt") == payload.get("observed_ci_attempt"),
                   deployed_base=state.get("deployed", {}).get("sha") == gate.get("base_sha"))
    if not all(matches.values()):
        raise CurrentReadinessBlocked("automatic-current-ci-or-base-changed",
                                      dict(ready=True, cause="identity_mismatch",
                                           identity_matches=matches))
    root = Path(config["roots"]) / payload["admission_id"]
    targets = []
    for role, repository, rid, number, ident in (
        ("code", CODE, 1448986421, binding.issue_identities["public"]["number"],
         binding.issue_identities["public"]["id"]),
        ("ops", OPS, 1449125557, binding.source[-1], binding.issue_identities["source"]["id"]),
    ):
        targets.append(dict(repository=repository, repository_id=rid, number=number, issue_id=ident,
                            token_file=str(root / "credentials" / (role + ".token")),
                            grant_file=str(root / "credentials" / (role + ".grant.json"))))
    client = HostingClient(HostingConfig.from_environment())
    issue(client, targets[0])  # The private target is reread with its isolated startup grant.
    pr = client.get(f"/repos/{CODE}/pulls/{payload['pr_number']}")
    require(pr.get("number") == payload["pr_number"] and pr.get("head", {}).get("sha") == gate["actual_head"]
            and pr.get("base", {}).get("sha") == gate["base_sha"], "automatic-pr-changed")
    files = pr_files(client, payload["pr_number"])
    require(type(pr.get("changed_files")) is int and pr["changed_files"] == len(files),
            "automatic-pr-files-incomplete")
    # A stable engine can deliver future business changes. A change to its own
    # authority must match the installed exact bytes or needs a new reviewed
    # engine, never silently execute an obsolete or payload-selected release tool.
    for row in files:
        require(isinstance(row, dict) and isinstance(row.get("filename"), str),
                "automatic-pr-files")
        paths = {row["filename"], row.get("previous_filename")}
        for relative in paths.intersection(template["pins"]):
            raw = regular(Path(template["engine"]) / relative, template["uid"],
                          digest=template["pins"][relative])
            blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            require(row["filename"] == relative and row.get("status") != "removed"
                    and row.get("sha") == blob, "automatic-engine-update-required")
    final_pr = client.get(f"/repos/{CODE}/pulls/{payload['pr_number']}")
    require(final_pr.get("head", {}).get("sha") == gate["actual_head"]
            and final_pr.get("base", {}).get("sha") == gate["base_sha"]
            and final_pr.get("changed_files") == len(files), "automatic-pr-changed")
    rule = github_intake.policy()
    profile = dict(template, root=str(root), tmp=str(root / "scratch/tmp"), log=str(root / "pipeline.log"),
                   pr=payload["pr_number"], head=gate["actual_head"], base=gate["base_sha"],
                   generation=state["generation"], deployed_generation=state["deployed"]["generation"],
                   session="native-" + trigger, task="native-" + trigger,
                   dispatch="native-" + payload["admission_id"], binding_sha256=binding.binding_sha256,
                   issue_protocol={"version": 1, "targets": targets},
                   capability=dict(version=3, owner="automatic-native-policy", ci_run_id=None,
                                   ci_attempt=None, policy_sha256=rule.digest, since_ms=rule.epoch))
    from scripts.blue_green.release_credentials import validate

    validate(profile, root)
    return payload, profile, pr, files


def hermes_identity(config, profile, args):
    """Read the existing background registry, never an invented environment ID.

    Hermes records task_id=session_id and its actual local shell PID. Bind the
    caller to that live ancestor and kernel start time before any write/grant.
    The bounded wait covers spawn-before-checkpoint; no operation is replayed.
    """
    ancestors = {}
    pid = os.getpid()
    for _ in range(16):
        raw = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
        ancestors[pid] = int(raw[19])
        pid = int(raw[1])
        if pid <= 1:
            break
    boot = int(next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime ")))
    hz = os.sysconf("SC_CLK_TCK")
    expected = [profile["python"], "-I", "-B", str(Path(__file__).absolute()), *args]
    deadline = time.monotonic() + 2
    while True:
        try:
            rows = document(config["hermes_processes"], profile["uid"])
        except FileNotFoundError:
            rows = []
        require(isinstance(rows, list) and len(rows) <= 64, "automatic-hermes-registry")
        matches = [row for row in rows if isinstance(row, dict)
                   and type(row.get("pid")) is int and row["pid"] in ancestors]
        if matches or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    require(len(matches) == 1, "automatic-hermes-process-required")
    row = matches[0]
    task = row.get("task_id")
    require(row.get("pid_scope") == "host"
            and isinstance(task, str) and task != "default"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}", task) is not None
            and isinstance(row.get("session_key"), str) and 0 < len(row["session_key"]) <= 512
            and isinstance(row.get("started_at"), (int, float))
            and abs(boot + ancestors[row["pid"]] / hz - row["started_at"]) <= 2
            and isinstance(row.get("command"), str) and shlex.split(row["command"]) == expected,
            "automatic-hermes-process-mismatch")
    raw = Path("/proc", str(row["pid"]), "stat").read_text().rsplit(")", 1)[1].split()
    require(int(raw[19]) == ancestors[row["pid"]], "automatic-hermes-process-changed")
    return hashlib.sha256(json.dumps([task, row["session_key"]], separators=(",", ":")).encode()).hexdigest()


def prepared(config, payload, profile, args, *, create_new=False):
    from scripts.blue_green.release_credentials import render

    uid = profile["uid"]
    root = Path(profile["root"])
    parent = Path(config["roots"])
    # Adoption creates this private parent, outside both candidate and engine.
    metadata = parent.lstat()
    require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == uid
            and stat.S_IMODE(metadata.st_mode) == 0o700, "automatic-parent-custody")
    for ancestor in parent.parents:
        metadata = ancestor.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == uid
                and not metadata.st_mode & 0o022, "automatic-parent-custody")
    intent = parent / (payload["admission_id"] + ".intent.json")
    receipt = parent / (payload["admission_id"] + ".prepared.json")
    session = hermes_identity(config, profile, args)
    if receipt.exists():
        existing = document(receipt, uid)
        require(existing["trigger_id"] == payload["trigger_id"]
                and existing["hermes_session"] == session
                and document(root / "profile.private.json", uid) == profile,
                "automatic-prepared-work-changed")
        for name, digest in existing["members"].items():
            require(Path(name).name == name, "automatic-prepared-path")
            regular(root / name, uid, digest=digest)
            require(stat.S_IMODE((root / name).lstat().st_mode)
                    == (0o600 if name == "profile.private.json" else 0o700),
                    "automatic-prepared-mode")
        return root
    require(create_new and not intent.exists() and not root.exists(), "automatic-preparation-unconfirmed")
    create(intent, dict(trigger_id=payload["trigger_id"], head=profile["head"],
                        binding_sha256=profile["binding_sha256"], hermes_session=session))
    render(profile, root)
    create(root / "profile.private.json", profile)
    members = {name: hashlib.sha256(regular(root / name, uid)).hexdigest()
               for name in ("entry.py", "parent.py", "selector.py", "refresh.py", "review.py", "profile.private.json")}
    create(receipt, dict(trigger_id=payload["trigger_id"], hermes_session=session, members=members))
    return root


def render_bootstrap(config, destination):
    """Create the fixed operator entry; no per-work admission is installed here."""
    destination = Path(destination)
    repo = Path(config["profile"]["repo"])
    require(repo != destination and repo not in destination.parents, "automatic-bootstrap-candidate")
    for parent in destination.parents:
        metadata = parent.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid in {0, os.geteuid()}
                and not metadata.st_mode & 0o022, "automatic-bootstrap-parent")
    text = Path(__file__).read_text().replace("CONFIG = None  # BUILD_CONFIG",
                                            "CONFIG = " + repr(config) + "  # BUILD_CONFIG", 1)
    with destination.open("x") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    destination.chmod(0o700)


def main():
    require(CONFIG is not None and sys.flags.optimize == 0, "automatic-not-adopted")
    template = bootstrap(CONFIG)
    args = sys.argv[1:]
    fenced = bool(args and args[0] == "--fenced")
    if fenced:
        args = args[1:]
    require(len(args) in {2, 3} and args[0] in {"inspect", "file", "review", "execute", "status"}
            and (args[0] == "review" and len(args) == 3 and args[2] in {"APPROVED", "CHANGES_REQUESTED"}
                 or args[0] == "file" and len(args) == 3
                 or args[0] not in {"review", "file"} and len(args) == 2), "automatic-arguments")
    mode, trigger = args[:2]
    if mode == "status":
        payload = claim(template, trigger, authenticated=False)
        root = Path(CONFIG["roots"]) / payload["admission_id"]
        profile = document(root / "profile.private.json", template["uid"])
        require(profile["pr"] == payload["pr_number"] and profile["head"] == payload["pr_head_sha"],
                "automatic-status-identity")
        frozen = document(Path(CONFIG["roots"]) / (payload["admission_id"] + ".prepared.json"),
                          template["uid"])
        require(frozen["trigger_id"] == trigger, "automatic-status-identity")
        regular(root / "profile.private.json", template["uid"],
                digest=frozen["members"]["profile.private.json"], private=True)
        receipt = root / "completion-receipt.json"
        completion = document(receipt, template["uid"]) if receipt.exists() else None
        require(completion is None or (completion.get("pr") == profile["pr"]
                and completion.get("head") == profile["head"] and completion.get("version") == 1
                and completion.get("remote_orca_push") is False), "automatic-completion-identity")
        public = None if completion is None else {key: completion[key] for key in (
            "version", "pr", "head", "status", "generation", "deploy_rc", "deploy_stage",
            "public_receipt", "publication_state", "report_comment", "completion_comment",
            "report_native_rc", "report_positive_wait", "report_actual_ECHILD", "no_replay",
            "first_cause",
        ) if key in completion}
        print(json.dumps({"pr": profile["pr"], "head": profile["head"],
                          "completion": public,
                          "remote_orca_push": False}, sort_keys=True))
        return 0
    from scripts.blue_green import deploy_state

    if not fenced:
        require("DAILYCHECK_DEPLOY_ATTEMPT_FD" not in os.environ
                and "DAILYCHECK_DEPLOY_ATTEMPT" not in os.environ, "automatic-fresh-call")
        os.execv(template["python"], [template["python"], "-I", "-B", str(Path(template["engine"]) /
                 "scripts/blue_green/deploy_state.py"), "run", "--", template["python"], "-I", "-B",
                 str(Path(__file__).absolute()), "--fenced", *args])
    payload, profile, pr, files = plan(CONFIG, trigger)
    if mode in {"inspect", "file"}:
        client_module = sys.modules["scripts.hosting"]
        client = client_module.HostingClient(client_module.HostingConfig.from_environment())
        if mode == "file":
            name = args[2]
            require(len(name) <= 512 and str(Path(name)) == name and not Path(name).is_absolute()
                    and ".." not in Path(name).parts and not any(ord(c) < 32 for c in name),
                    "automatic-review-file-path")
            matches = [row for row in files if isinstance(row, dict) and row.get("filename") == name]
            require(len(matches) == 1, "automatic-review-file-not-in-pr")
            ref = profile["base"] if matches[0].get("status") == "removed" else profile["head"]
            row = client.get("/repos/DUIDUI-Studio/DailyCheck/contents/"
                             + urllib.parse.quote(name, safe="/") + "?ref=" + ref)
            require(row.get("type") == "file" and row.get("path") == name
                    and row.get("sha") == matches[0].get("sha")
                    and row.get("encoding") == "base64", "automatic-review-file-identity")
            content = base64.b64decode(row["content"].replace("\n", ""), validate=True)
            require(len(content) <= 1048576 and hashlib.sha1(
                b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest() == row["sha"],
                "automatic-review-file-digest")
            print(json.dumps(dict(pr=profile["pr"], head=profile["head"], ref=ref,
                                  path=name, blob_sha=row["sha"], text=content.decode()), sort_keys=True))
            return 0
        print(json.dumps(dict(pr=profile["pr"], head=profile["head"], base=profile["base"],
                              ci_run_id=payload["observed_ci_run_id"], ci_attempt=payload["observed_ci_attempt"],
                              engineering_issue=profile["issue_protocol"]["targets"][0]["number"],
                              title=pr.get("title"), body=pr.get("body"), files=files), sort_keys=True))
        return 0
    root = prepared(CONFIG, payload, profile, args, create_new=mode == "review")
    # The existing review/retained parent own their original fresh run fences.
    fd = deploy_state.process_lock_fd()
    require(fd is not None, "automatic-process-fence")
    os.close(fd)
    os.environ.pop("DAILYCHECK_DEPLOY_ATTEMPT_FD", None)
    command = [template["python"], "-B", str(root / ("review.py" if mode == "review" else "parent.py")),
               args[2] if mode == "review" else "--execute-admitted"]
    os.execv(template["python"], command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        reason = str(error)
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", reason) is None:
            reason = type(error).__name__
        result = {"status": "BLOCKER", "reason": reason, "deployment_proven": False}
        if isinstance(error, CurrentReadinessBlocked):
            result["readiness"] = error.evidence
        print(json.dumps(result), file=sys.stderr)
        raise SystemExit(78) from None
