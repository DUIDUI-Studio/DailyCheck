"""Installed fixed G executor; never import application code in this process.

Only the protected controller constructs this port. Shell helpers call the
private operation entrypoint; candidate children receive neither its fence nor
its environment. Installation and independent runtime publication are separate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.ci.merge_group import commit, digest, git
from scripts.prod_agent.native_ports import InstalledPolicy, protected_path, read_protected
from scripts.prod_agent.native_queue import deploy_state

HELPERS = (
    "scripts/blue_green/up.sh", "scripts/blue_green/verify.sh",
    "scripts/blue_green/promote.sh", "scripts/blue_green/switch.sh",
    "scripts/agent_framework/prod_dryrun_evidence.sh",
    "scripts/check_known_failures.py", "scripts/ci/pytest_evidence.py",
    "tests/known_failures.txt", "scripts/playwright/sim_user.py",
)
CONTROL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
TOOL_NAMES = frozenset((
    "bash", "python3", "git", "curl", "systemctl", "journalctl", "ss", "ab",
    "awk", "sed", "grep", "cut", "tr", "stat", "cp", "mv", "rm", "mkdir",
    "chmod", "chown", "flock", "date", "sleep", "tail", "head", "sort", "wc",
    "dirname", "basename", "mktemp", "cat", "tee", "setpriv", "nginx", "env",
))
OPERATIONS = frozenset({"pytest", "sim-user", "dryrun"})
BOOTSTRAP = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
             "runpy.run_module('scripts.prod_agent.native_pipeline',run_name='__main__')")


def application_runtime(python: Path) -> dict:
    """Non-importing fingerprint of a separately provisioned application venv."""
    root = python.parent.parent
    files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(root.rglob("*")) if p.is_file()
             and p.suffix in {".py", ".so", ".pyd", ".pth"}}
    return {"python_sha256": hashlib.sha256(python.resolve().read_bytes()).hexdigest(),
            "files_sha256": digest(files)}


def database_snapshot(directory: Path) -> dict:
    """Logical content, including updated values; never emit business rows."""
    files = sorted(directory.rglob("*.db"))
    if not files or not (directory / "master.db").is_file():
        raise ValueError("verification databases absent")
    result = {}
    for path in files:
        if path.is_symlink():
            raise ValueError("verification DB symlink")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            content = hashlib.sha256()
            for line in connection.iterdump():
                content.update(line.encode() + b"\n")
            result[str(path.relative_to(directory))] = content.hexdigest()
    return result


def validate_dryrun(before, after, response, journal):
    if (not before or before != after or response != {"login": True, "reset": True}
            or not re.search(r"\[dry_run\] BLOCKED write #\d+ .*UPDATE users SET password_hash=", journal)):
        raise ValueError("green intended UPDATE not proven isolated")


# This imports only stdlib and runs under the application UID. It makes an
# intended UPDATE to the disposable green verification user's password.
DRYRUN_HTTP = r'''
import html,http.cookiejar,json,os,re,urllib.parse,urllib.request
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
base='http://127.0.0.1:8091'
def get(path):
 with opener.open(base+path,timeout=15) as r:
  if r.status!=200: raise ValueError('green page unavailable')
  return r.read(2*1024*1024).decode()
def csrf(body):
 m=re.search(r'name="csrf_token"[^>]*value="([^"]+)"',body)
 if not m: raise ValueError('green CSRF unavailable')
 return html.unescape(m[1])
def post(path,data):
 req=urllib.request.Request(base+path,data=urllib.parse.urlencode(data).encode())
 with opener.open(req,timeout=15) as r:
  if r.status!=200 or urllib.parse.urlsplit(r.url).path!= '/users':
   raise ValueError('green write route did not finish')
  return r.read(2*1024*1024).decode()
body=get('/login')
req=urllib.request.Request(base+'/login',data=urllib.parse.urlencode({
 'username':os.environ['SIM_USER_USER'],'password':os.environ['SIM_USER_PASSWORD'],
 'csrf_token':csrf(body)}).encode())
with opener.open(req,timeout=15) as r:
 if r.status!=200 or urllib.parse.urlsplit(r.url).path=='/login': raise ValueError('login failed')
body=get('/select-warehouse')
warehouse=re.search(r'/select-warehouse/(\d+)',body)
if not warehouse: raise ValueError('green warehouse prerequisite absent')
req=urllib.request.Request(base+'/select-warehouse/'+warehouse[1],data=urllib.parse.urlencode({
 'csrf_token':csrf(body)}).encode())
with opener.open(req,timeout=15) as r:
 if r.status!=200 or urllib.parse.urlsplit(r.url).path!='/land': raise ValueError('warehouse selection failed')
body=get('/users')
body=post('/users/'+os.environ['DC_PROBE_USER_ID']+'/reset-password',{
 'password':os.environ['DC_PROBE_CHALLENGE'],'csrf_token':csrf(body)})
if '已重置' not in body or '的密码' not in body: raise ValueError('intended reset absent')
print(json.dumps({'login':True,'reset':True}))
'''


class ProtectedPipeline:
    def __init__(self, policy: InstalledPolicy):
        self.policy = policy
        value = policy.value.get("pipeline")
        keys = {"version", "helpers", "tools", "app_gid", "app_python", "app_runtime",
                "source_db", "green_db", "scratch_root", "log_directory", "backup_directory",
                "unit_directory", "nginx_conf", "app_environment_file", "blue_access_log", "green_access_log"}
        if (not isinstance(value, dict) or set(value) != keys or value["version"] != 1
                or set(value["helpers"]) != set(HELPERS)
                or set(value["tools"]) != TOOL_NAMES
                or type(value["app_gid"]) is not int or value["app_gid"] <= 0):
            raise ValueError("installed protected G executor policy required")
        self.value = value
        self.python = Path(value["app_python"])
        self._validate_installation()

    def path(self, key):
        path = Path(self.value[key])
        if not path.is_absolute() or ".." in path.parts or not re.fullmatch(r"[/a-zA-Z0-9_.-]+", str(path)):
            raise ValueError("invalid fixed pipeline path")
        return path

    def _validate_installation(self):
        p = self.policy
        if not p.fixture and sys.platform != "linux":
            raise ValueError("installed Linux executor required")
        for name, pin in self.value["helpers"].items():
            raw = read_protected(p.authority / name, p.owner,
                                 anchor=p.authority if p.fixture else None, private=False)
            if hashlib.sha256(raw).hexdigest() != pin:
                raise ValueError("protected pipeline helper changed")
        for name, pin in self.value["tools"].items():
            executable = shutil.which(name, path=CONTROL_PATH)
            if executable is None:
                raise ValueError("fixed pipeline tool absent: " + name)
            executable = Path(executable).resolve()
            protected_path(executable, executable.stat().st_uid if p.fixture else 0,
                           anchor=executable if p.fixture else None)
            if hashlib.sha256(executable.read_bytes()).hexdigest() != pin:
                raise ValueError("fixed pipeline tool changed: " + name)
        protected_path(self.python.resolve(), self.python.resolve().stat().st_uid if p.fixture else 0,
                       anchor=self.python.resolve() if p.fixture else None)
        root = self.python.parent.parent
        if root.is_relative_to(p.location("repo")) or root.is_relative_to(p.location("release_root")):
            raise ValueError("application runtime must be installed outside candidate")
        if self.value["app_runtime"] != application_runtime(self.python):
            raise ValueError("application runtime changed")
        if not p.fixture:
            for path in root.rglob("*"):
                protected_path(path.resolve() if path.is_symlink() else path, 0)
        for key in ("source_db", "green_db", "scratch_root", "log_directory", "backup_directory",
                    "unit_directory", "nginx_conf", "app_environment_file", "blue_access_log", "green_access_log"):
            path = self.path(key)
            # Mutable app DBs are separately owned; their parents stay protected.
            protected_path(path.parent if key in {"green_db", "source_db", "blue_access_log", "green_access_log"}
                           else path, p.owner, anchor=p.anchor, private=key == "log_directory")
        if not p.fixture and self.path("source_db").stat().st_uid != p.value["app_uid"]:
            raise ValueError("application-owned external production DB prerequisite absent")
        if (self.path("green_db") == self.path("source_db")
                or self.path("green_db").is_relative_to(self.path("source_db"))
                or p.authority.is_relative_to(p.location("repo"))
                or p.authority.is_relative_to(p.location("release_root"))):
            raise ValueError("candidate/control/DB domains overlap")
        private = [p.authority, p.path.parent, *(p.location(k) for k in (
            "repo", "directory", "review_directory", "key_file", "read_token_file", "runtime_file", "bootstrap_file")),
            self.path("app_environment_file"), self.path("log_directory")]
        for mutable in (self.path(k) for k in ("source_db", "green_db", "scratch_root", "green_access_log")):
            if any(mutable == path or mutable.is_relative_to(path) or path.is_relative_to(mutable) for path in private):
                raise ValueError("application write path overlaps protected control inputs")
        lines = read_protected(self.path("app_environment_file"), p.owner, anchor=p.anchor).decode("ascii").splitlines()
        if len(lines) != 2 or any(not re.fullmatch(r"[A-Z_]+=[a-zA-Z0-9_-]{32,200}", line) for line in lines):
            raise ValueError("fixed two-key application EnvironmentFile required")
        app_env = dict(line.split("=", 1) for line in lines)
        if (set(app_env) != {"DAILYCHECK_SECRET_KEY", "DAILYCHECK_MCP_TOKEN"}
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{32,200}", v)
                       for v in app_env.values())
                or app_env["DAILYCHECK_MCP_TOKEN"] != p.mcp_health_token()
                or p.read_token() in app_env.values()):
            raise ValueError("separate application environment required")
        self.app_env = app_env

    def _owned(self, candidate=None):
        active = deploy_state._native_owned(deploy_state.load_state(self.policy.location("directory")))
        if active["phase"] != "deploy" or candidate is not None and active["candidate"] != candidate:
            raise ValueError("protected executor attempt mismatch")
        return active["candidate"]

    def _release(self, candidate):
        return self.policy.location("release_root") / candidate["manifest"]["group_sha"]

    def _control_env(self, candidate):
        p = self.policy
        if not hasattr(self, "probe_env"):
            self.probe_env = {"SIM_USER_USER": "dcprobe_" + os.urandom(8).hex(),
                              "SIM_USER_PASSWORD": "dc" + os.urandom(24).hex()}
        return {"PATH": CONTROL_PATH, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                **self.probe_env,
                "DAILYCHECK_DEPLOY_STATE_DIR": str(p.location("directory")),
                deploy_state.ATTEMPT_ENV: os.environ[deploy_state.ATTEMPT_ENV],
                deploy_state.ATTEMPT_FD_ENV: str(deploy_state.process_lock_fd()),
                "DC_NATIVE_POLICY": str(p.path), "DC_CONTROL_PYTHON": str(p.python),
                "DC_NATIVE_AUTHORITY": str(p.authority), "DC_NATIVE_REPO": str(p.location("repo")),
                "DAILYCHECK_APP_DIR": str(self._release(candidate)),
                "DAILYCHECK_SRC_DB_DIR": str(self.path("source_db")),
                "BLUE_MASTER": str(self.path("source_db") / "master.db"),
                "GREEN_DB": str(self.path("green_db")),
                "REPORT_DIR": str(self.path("log_directory")),
                "DC_NATIVE_LOG_DIR": str(self.path("log_directory")),
                "BLUE_ACCESS_LOG": str(self.path("blue_access_log")),
                "GREEN_ACCESS_LOG": str(self.path("green_access_log")),
                "BACKUP_DIR": str(self.path("backup_directory")),
                "EVIDENCE_BASE": str(self.path("log_directory")),
                "SYSTEMD_UNIT_DIR": str(self.path("unit_directory")),
                "NGINX_CONF": str(self.path("nginx_conf")),
                "GREEN_ENV_FILE": str(p.location("directory") / "unused-app.env"),
                "GREEN_MCP_ENV_FILE": str(p.location("directory") / "unused-mcp.env"),
                "NGINX_DAILYCHECK_CONF": str(self.path("nginx_conf"))}

    def _lower(self, args, *, db, scratch, input=None, extra=None, output=None):
        """The only candidate process boundary; no inherited descriptors/environment."""
        p = self.policy
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(scratch),
               "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1",
               "DAILYCHECK_DB_DIR": str(db), **(extra or {})}
        command = ["/usr/bin/setpriv", "--reuid=" + str(p.value["app_uid"]),
                   "--regid=" + str(self.value["app_gid"]), "--clear-groups",
                   "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                   "--no-new-privs", str(self.python), "-I", "-B", *args]
        child = subprocess.Popen(command, cwd=self._release(self._owned()), env=env,
            close_fds=True, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=output if output is not None else subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        stdout, _ = child.communicate(input)
        # Positive wait is mandatory; no timeout is relabelled as completion.
        return subprocess.CompletedProcess(command, child.returncode, stdout)

    def _scratch(self):
        scratch = Path(tempfile.mkdtemp(prefix="native-G-", dir=self.path("scratch_root")))
        os.chown(scratch, self.policy.value["app_uid"], self.value["app_gid"])
        scratch.chmod(0o700)
        return scratch

    def _export_release(self, candidate):
        """Export regular Git blobs only; no candidate hooks, symlinks or executables run."""
        p, manifest = self.policy, candidate["manifest"]
        release = self._release(candidate)
        if release.exists():
            raise ValueError("release already exists; reconcile instead of redeploy")
        tree, _ = commit(p.location("repo"), manifest["group_sha"])
        if tree != manifest["tree_sha"]:
            raise ValueError("release tree changed")
        release.mkdir(mode=0o755)
        raw = subprocess.check_output(["/usr/bin/git", "-C", str(p.location("repo")),
                                       "ls-tree", "-rz", "--full-tree", manifest["group_sha"]],
                                      env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            meta, filename = entry.split(b"\t", 1)
            mode, kind, oid = meta.decode("ascii").split()
            name = Path(filename.decode("utf-8"))
            if (name.is_absolute() or ".." in name.parts or name.parts[0] in {".git", ".venv"}
                    or kind != "blob" or mode not in {"100644", "100755"} or str(name) == "release.json"):
                raise ValueError("unsupported release tree entry")
            target = release / name
            target.parent.mkdir(parents=True, exist_ok=True)
            # Read literal blobs: archive/checkout attributes, filters and hooks
            # must never transform or execute candidate-controlled bytes as root.
            target.write_bytes(subprocess.check_output(["/usr/bin/git", "-C", str(p.location("repo")),
                "cat-file", "blob", oid], env={"PATH": "/usr/bin:/bin", "LANG": "C"}))
            target.chmod(0o555 if mode == "100755" else 0o444)
        (release / "release.json").write_text(json.dumps({"sha": manifest["group_sha"], "tree": tree}))
        (release / "release.json").chmod(0o444)
        for path in sorted(release.rglob("*"), reverse=True):
            if path.is_dir():
                path.chmod(0o555)
        release.chmod(0o555)
        # The object-store checkout is never executed. Move only its detached
        # identity cursor under the existing state/attempt fences for core seal;
        # application bytes come from the immutable literal-blob release above.
        with deploy_state.state_transaction(p.location("directory")):
            self._owned(candidate)
            subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                "-C", str(p.location("repo")), "update-ref", "--no-deref", "HEAD", manifest["group_sha"]],
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
                pass_fds=(deploy_state.process_lock_fd(),), check=True)

    def _stage(self, name, action):
        if deploy_state.cmd_native_stage(argparse.Namespace(stage=name, action=action)):
            raise ValueError("native journal rejected actual stage completion")

    def prepare_services(self):
        """Fixed startup recipes; no candidate-provided unit/command is consumed."""
        candidate = self._owned()
        p = self.policy
        release = self._release(candidate)
        if git(p.location("repo"), "rev-parse", "HEAD") != candidate["manifest"]["group_sha"]:
            raise ValueError("service release differs from fenced G")
        access = self.path("green_access_log")
        fd = os.open(access, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_nlink != 1:
                raise ValueError("green access log is not a dedicated file")
            os.fchown(fd, p.value["app_uid"], self.value["app_gid"])
        finally:
            os.close(fd)
        for unit, module in (("dailycheck-green.service", "gunicorn --workers 2 --bind 127.0.0.1:8091 --access-logfile " + str(access) + " app:create_app()"),
                             ("dailycheck-green-mcp.service", "mcp_server --transport http --host 127.0.0.1 --port 5101")):
            path = self.path("unit_directory") / unit
            previous = read_protected(path, p.owner, anchor=p.anchor, private=False)
            # Existing backup semantics remain; this extra copy preserves the
            # pre-native startup recipe before replacing candidate import paths.
            backup = self.path("backup_directory") / (unit + ".native-" + candidate["manifest"]["group_sha"])
            if backup.exists():
                raise ValueError("service preparation already attempted; reconcile required")
            backup.write_bytes(previous)
            backup.chmod(0o600)
            if any((self.path("unit_directory") / (unit + ".d")).glob("*.conf")):
                raise ValueError("unreviewed service drop-ins require installation reconciliation")
            private = " ".join(str(p.location(k)) for k in ("directory", "review_directory"))
            private += " " + " ".join(str(p.location(k)) for k in ("key_file", "read_token_file", "runtime_file", "bootstrap_file"))
            content = f"""[Unit]
Description=DailyCheck protected native candidate
After=network.target
[Service]
Type=simple
User=0
Group=0
SupplementaryGroups=
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_SETPCAP
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
InaccessiblePaths={private}
ReadWritePaths={self.path('green_db')} {self.path('source_db')} {access}
WorkingDirectory={release}
Environment=PATH=/usr/bin:/bin
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=DAILYCHECK_ENV=production
Environment=DRY_RUN=true
Environment=DAILYCHECK_DB_DIR={self.path('green_db')}
EnvironmentFile={self.path('app_environment_file')}
ExecStart=/usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 DAILYCHECK_ENV=production DRY_RUN=${{DRY_RUN}} DAILYCHECK_DB_DIR=${{DAILYCHECK_DB_DIR}} DAILYCHECK_SECRET_KEY=${{DAILYCHECK_SECRET_KEY}} DAILYCHECK_MCP_TOKEN=${{DAILYCHECK_MCP_TOKEN}} /usr/bin/setpriv --reuid={p.value['app_uid']} --regid={self.value['app_gid']} --clear-groups --bounding-set=-all --inh-caps=-all --ambient-caps=-all --no-new-privs {self.python} -B -m {module}
Restart=on-failure
[Install]
WantedBy=multi-user.target
"""
            path.write_text(content)
            path.chmod(0o644)
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"],
                       env={"PATH": "/usr/bin:/bin"}, check=True)

    def copy_db(self):
        """Dereference the existing DB tree only with application read authority."""
        self._owned()
        db = self.path("green_db")
        if db.is_symlink():
            raise ValueError("green DB root cannot be a symlink")
        if db.exists():
            shutil.rmtree(db)
        db.mkdir(mode=0o700)
        os.chown(db, self.policy.value["app_uid"], self.value["app_gid"])
        scratch = self._scratch()
        try:
            code = "import shutil,sys; shutil.copytree(sys.argv[1],sys.argv[2],dirs_exist_ok=True,symlinks=False)"
            result = self._lower(["-c", code, str(self.path("source_db")), str(db)], db=db, scratch=scratch)
            if result.returncode:
                raise ValueError("application-authority DB copy failed")
        finally:
            shutil.rmtree(scratch)

    def execute(self, candidate, *, guard_fd, attempt):
        if (guard_fd != deploy_state.process_lock_fd()
                or json.loads(os.environ[deploy_state.ATTEMPT_ENV]) != attempt):
            raise ValueError("protected pipeline missing original fence/attempt")
        self._validate_installation()
        self._owned(candidate)
        env = self._control_env(candidate)
        with tempfile.TemporaryFile(mode="w+") as log:
            for stage in deploy_state.NATIVE_STAGES:
                self._owned(candidate)
                self._stage(stage, "start")
                if stage == "candidate":
                    self._export_release(candidate)
                    scratch = self._scratch()
                    try:
                        result = self._lower(["-m", "pip", "check"], db=scratch / "db", scratch=scratch)
                        if result.returncode:
                            raise ValueError("installed application dependencies incomplete")
                    finally:
                        shutil.rmtree(scratch)
                elif stage == "observe":
                    self._observe(candidate)
                else:
                    args = {"up": [str(candidate["manifest"]["subject_pr_number"]), candidate["manifest"]["group_sha"]],
                            "verify": [str(candidate["manifest"]["subject_pr_number"])],
                            "promote": [], "switch": ["green", "100"]}[stage]
                    result = subprocess.run(["/bin/bash", str(self.policy.authority / "scripts/blue_green" / (stage + ".sh")),
                                             *args], cwd=self.policy.authority, env=env,
                                            pass_fds=(guard_fd,), stdout=log, stderr=subprocess.STDOUT)
                    if result.returncode:
                        raise subprocess.CalledProcessError(result.returncode, stage)
                self._stage(stage, "done")

    def _observe(self, candidate):
        since = str(int(time.time()))
        deadline = time.monotonic() + self.policy.producer.minimum_observation_seconds
        while True:
            time.sleep(min(30, max(0, deadline - time.monotonic())))
            self._owned(candidate)
            try:
                for url in ("http://127.0.0.1:8080/", "http://127.0.0.1:8091/health"):
                    result = subprocess.run(["/usr/bin/curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                        "--max-time", "10", url], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
                    if result.returncode or result.stdout not in {"200", "302"}:
                        raise ValueError("native observation route failed")
                journal = subprocess.check_output(["/usr/bin/journalctl", "-u", "dailycheck-green.service",
                    "--since", "@" + since, "--no-pager"], env={"PATH": "/usr/bin:/bin"}, text=True)
                if "[dry_run] BLOCKED" in journal:
                    raise ValueError("native observation found blocked live writes")
            except Exception:
                self._rollback(candidate)
                raise
            if time.monotonic() >= deadline:
                return

    def _rollback(self, candidate):
        subprocess.run(["/bin/bash", str(self.policy.authority / "scripts/blue_green/switch.sh"), "blue", "100"],
            env=self._control_env(candidate), pass_fds=(deploy_state.process_lock_fd(),), check=True)

    def operation(self, operation):
        if operation not in OPERATIONS:
            raise ValueError("fixed candidate operation required")
        self._owned()
        scratch = self._scratch()
        try:
            if operation == "pytest":
                return self._pytest(scratch)
            user, password = (os.environ[k] for k in ("SIM_USER_USER", "SIM_USER_PASSWORD"))
            if operation == "sim-user":
                return self._sim_user(scratch, user, password)
            return self._dryrun(scratch)
        finally:
            shutil.rmtree(scratch)

    def _pytest(self, scratch):
        log = self.path("log_directory") / "native-pytest.log"
        session = scratch / "session.json"
        code = ("import importlib.util,runpy,sys; "
                "s=importlib.util.spec_from_file_location('dc_approved_evidence',sys.argv.pop(1)); "
                "m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); "
                "sys.path.insert(0,'.'); runpy.run_module('pytest',run_name='__main__')")
        with log.open("w") as output:
            result = self._lower(["-c", code, str(self.policy.authority / "scripts/ci/pytest_evidence.py"),
                "tests/", "--basetemp=" + str(scratch / "pytest"),
                "-q", "--tb=line", "-rfE", "--color=no", "-p", "no:cacheprovider", "-p", "dc_approved_evidence",
                "--dc-evidence=" + str(session)], db=scratch / "db", scratch=scratch, output=output)
        # Only the trusted checker decides from native exit and captured files.
        return subprocess.run([str(self.policy.python), "-I", "-B",
            str(self.policy.authority / "scripts/check_known_failures.py"), str(log),
            "--pytest-exit", str(result.returncode), "--session-file", str(session)],
            cwd=self.policy.authority, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            pass_fds=(deploy_state.process_lock_fd(),)).returncode

    def _sim_user(self, scratch, user, password):
        env = {"SIM_USER_USER": user, "SIM_USER_PASSWORD": password}
        code = "import runpy,sys; sys.path.insert(0,'.'); runpy.run_module('flask',run_name='__main__')"
        result = self._lower(["-c", code, "--app", "app", "create-user", "--admin", "--", user, password],
                             db=self.path("green_db"), scratch=scratch)
        if result.returncode:
            return result.returncode
        result = self._lower([str(self.policy.authority / "scripts/playwright/sim_user.py"),
                              "--base-url", "http://127.0.0.1:8091"],
                             db=self.path("green_db"), scratch=scratch, extra=env)
        if result.returncode:
            return result.returncode
        return 0

    def _dryrun(self, scratch):
        db = self.path("green_db")
        if (db.resolve() == self.path("source_db").resolve()
                or os.path.samefile(db / "master.db", self.path("source_db") / "master.db")):
            raise ValueError("green probe cannot target production DB")
        env = {key: os.environ[key] for key in ("SIM_USER_USER", "SIM_USER_PASSWORD")}
        before = database_snapshot(db)
        with sqlite3.connect((db / "master.db").as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute("SELECT id FROM users WHERE username=? AND is_admin=1 AND is_active=1",
                                (env["SIM_USER_USER"],)).fetchall()
        if len(rows) != 1:
            raise ValueError("actual green verification user required")
        since = str(int(time.time()))
        result = self._lower(["-c", DRYRUN_HTTP], db=db, scratch=scratch, extra={**env,
            "DC_PROBE_USER_ID": str(rows[0][0]), "DC_PROBE_CHALLENGE": "dc" + os.urandom(24).hex()})
        if result.returncode:
            raise ValueError("actual intended green probe failed")
        journal = subprocess.check_output(["/usr/bin/journalctl", "-u", "dailycheck-green.service",
            "--since", "@" + since, "--no-pager"], env={"PATH": "/usr/bin:/bin"}, text=True)
        validate_dryrun(before, database_snapshot(db), json.loads(result.stdout), journal)
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=sorted(OPERATIONS | {"prepare-services", "copy-db"}))
    args = parser.parse_args()
    # Policy path is fixed by the installed controller. This private helper is
    # never installed setuid or exposed to candidate code as a privileged RPC.
    from scripts.prod_agent.native_controller import POLICY_PATH

    policy = InstalledPolicy(POLICY_PATH)
    pipeline = ProtectedPipeline(policy)
    pipeline._owned()
    if args.operation == "prepare-services":
        pipeline.prepare_services()
        return 0
    if args.operation == "copy-db":
        pipeline.copy_db()
        return 0
    return pipeline.operation(args.operation)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("protected candidate operation failed", file=sys.stderr)
        raise SystemExit(1) from None
