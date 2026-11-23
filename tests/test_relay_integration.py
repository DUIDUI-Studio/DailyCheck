"""Process-level synthetic exact-H relay through OpenSSH and loopback Git HTTPS."""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import http.server
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.blue_green import deploy_state as ds
from scripts.blue_green.publication_authority import (
    GITHUB_SOURCE_IDENTITY,
    SOURCE_KEYS,
    resolve_source_binding,
)
from scripts.ci.relay_exact import offline
from scripts.hosting import github_ff_payload
from scripts.prod_agent.relay_protocol import COMMAND, SIGNER, WORKER_PATH, canonical, sign
from scripts.prod_agent.relay_vps import ENTRY
from tests.test_github_transport import ROOT, evidence

REPO = Path(__file__).resolve().parents[1]
CANARY = "synthetic_gh_secret_never_crosses_relay_"
SSHD = "/usr/sbin/sshd"
REF = "refs/heads/chore/issue-160-test"
ID = "PR_synthetic_160"
SSH_LANE = "DAILYCHECK_RELAY_SSH_INTEGRATION"


def cmd(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], capture_output=True, timeout=60,
                          check=True, **kwargs).stdout.strip()


def write(path, value):
    path.write_bytes(canonical(value))
    path.chmod(0o600)


def read(path):
    return json.loads(path.read_bytes())


def executable(path, code):
    path.write_text("#!" + sys.executable + "\n" + code)
    path.chmod(0o700)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if urlsplit(self.path).path.startswith(ROOT + "/issues/"):
            return self.report_http()
        assert not self.headers.get("Authorization"), "anonymous Git fetch only"
        super().do_GET()

    def do_POST(self):
        self.report_http()

    def report_http(self):
        assert self.headers["Authorization"].startswith("Bearer " + CANARY + "report_")
        root = Path(self.directory).parent
        path = urlsplit(self.path).path
        ledger = root / "comments.json"
        rows = read(ledger) if ledger.exists() else []
        options = read(root / "control.json")
        route = ROOT + "/issues/160/comments"
        status = 200
        if self.command == "POST":
            assert path == route
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["body"]
            assert CANARY not in body and "923751" not in body and "923752" not in body
            with (root / "report-posts.log").open("a") as stream:
                stream.write("POST\n")
            value = {"id": 456, "body": body, "issue_url": "https://api.github.com" + ROOT + "/issues/160",
                     "user": {"id": 321}}
            mode = options.get("report_mode", "ack")
            if mode not in {"unknown", "bare_ack"}:
                write(ledger, [*rows, value])
            if mode in {"unknown", "lost"}:
                self.close_connection = True
                return
            status = 201
        elif path == route:
            value = [] if options.get("hide_report") else rows
        elif path == ROOT + "/issues/comments/456" and rows:
            value = rows[0]
        else:
            status, value = 404, {}
        raw = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def relay(tmp_path):
    if os.environ.get(SSH_LANE) != "1":
        pytest.skip("requires the isolated Linux/root relay SSH lane")
    assert sys.platform == "linux" and os.geteuid() == 0, "isolated Linux/root required"
    assert Path(SSHD).is_file() and Path("/usr/bin/ssh").is_file(), "OpenSSH required"
    assert offline(), "offline lane required"
    assert Path("/run/sshd").is_dir(), "OpenSSH privilege separation directory required"
    root = tmp_path.resolve()
    root.chmod(0o700)
    state, worker, source, web = [root / x for x in ("state", "worker", "source", "web")]
    for directory in (state, worker, source, web):
        directory.mkdir(mode=0o700)
    repo = root / "code"
    cmd("git", "init", "-q", "-b", "main", repo)
    for k, v in (("user.email", "relay@example.invalid"), ("user.name", "Synthetic Relay"),
                 ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
        cmd("git", "-C", repo, "config", k, v)
    (repo / "README.md").write_text("serving base\n")
    cmd("git", "-C", repo, "add", ".")
    cmd("git", "-C", repo, "commit", "-qm", "baseline")
    base = cmd("git", "-C", repo, "rev-parse", "HEAD").decode()
    seed = root / "seed.git"
    cmd("git", "clone", "-q", "--bare", "--no-local", repo, seed)
    seed.chmod(0o700)
    cmd("git", "-C", repo, "checkout", "-qb", REF.removeprefix("refs/heads/"))
    (repo / "README.md").write_text("serving candidate\n")
    cmd("git", "-C", repo, "commit", "-qam", "candidate")
    head = cmd("git", "-C", repo, "rev-parse", "HEAD").decode()
    tree = cmd("git", "-C", repo, "rev-parse", "HEAD^{tree}").decode()
    public = web / "fixture.git"
    cmd("git", "clone", "-q", "--bare", "--no-local", repo, public)
    cmd("git", "-C", public, "update-server-info")
    ca, cert_key = root / "cert.pem", root / "cert-key.pem"
    cmd("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        "-keyout", cert_key, "-out", ca)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(ca, cert_key)
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                         lambda *a: Quiet(*a, directory=web)) as server:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield from configured(root, state, worker, source, seed, ca, base, head, tree, server)
        finally:
            server.shutdown()
            thread.join()


def configured(root, state, worker, source, seed, ca, base, head, tree, server):
    source_entry = dict(zip(SOURCE_KEYS, ("github", *GITHUB_SOURCE_IDENTITY, "issue", 923751), strict=True))
    binding = {"version": 1, "bindings": [{"pr": 160, "public": ds.PUBLIC_CODE_HOSTING,
               "source": source_entry, "issue_identities": {
                   "public": {"number": 160, "id": 985}, "source": {"id": 923752}}}]}
    for directory in (source, state):
        write(directory / "bindings.json", binding)
    expected = {"head": head, "base": base, "tree": tree, "ref": REF, "attempt": "4" * 32,
                "pr": 160, "pr_id": 160, "pr_node": ID, "generation": 21,
                "source": resolve_source_binding(source, 160).binding_sha256}
    native = ds._empty_state()
    native.update(generation=20, deployed={"sha": base, "generation": 18, "pr": 157},
                  last_attempt={"pr": 43, "sha": "9" * 40, "result": "failed"})
    ds.save_state(state, native)
    write(root / "route.json", {"route": "blue", "sha": base})
    write(root / "control.json", {})
    table = json.loads(json.dumps(evidence()).replace("a" * 40, head).replace("b" * 40, base))
    pr = table[ROOT + "/pulls/160"]["data"]
    pr.update(node_id=ID, merged=False, body="Independent exact-H review")
    table[ROOT]["data"].update(node_id="R_kgDOVl3HNQ", private=False, visibility="public")
    main = ROOT + "/git/ref/heads/main"
    table[main] = {"data": {"ref": "refs/heads/main", "object": {"sha": base}}}
    table[ROOT + f"/compare/{base}...{head}?per_page=1&page=2"] = {
        "data": {"status": "ahead", "base_commit": {"sha": base},
                 "merge_base_commit": {"sha": base}}}
    table[ROOT + "/rules/branches/main?page=1"] = {"data": []}
    names = ["test", "agent-framework-reports"]
    table[ROOT + "/branches/main/protection"] = {"data": {
        "url": "https://api.github.com" + ROOT + "/branches/main/protection",
        "enforce_admins": {"enabled": True},
        "required_status_checks": {"strict": True, "contexts": names,
                                   "checks": [{"context": n, "app_id": 15368} for n in names]},
        "required_pull_request_reviews": {"required_approving_review_count": 1,
            "dismiss_stale_reviews": True, "require_code_owner_reviews": False},
    }}
    table[ROOT + "/pulls/160/reviews?page=1"] = {"data": [
        {"id": 13, "user": {"id": 14}, "state": "APPROVED", "commit_id": head}]}
    write(root / "api.json", table)
    common = f"""
import json, os, sys, time
from pathlib import Path
root = Path({str(root)!r})
control = json.loads((root / 'control.json').read_text())
def write(path, value):
    path.write_text(json.dumps(value))
"""
    stage = root / "stage"
    stage_pin = executable(stage, common + """
import hashlib
import signal
operation = sys.argv[1]
target = json.load(sys.stdin)
assert set(os.environ) <= {'PATH', 'LANG', 'LC_CTYPE', '__CF_USER_TEXT_ENCODING'}
assert all(not Path('/dev/fd', str(i)).exists() for i in range(3, 64))
assert 'source' in target and 'token' not in target and '923751' not in str(target)
write(root / 'last_stage_pid.json', {'pid': os.getpid(), 'operation': operation})
path = root / 'route.json'
current = json.loads(path.read_text())
if operation == 'standby':
    current = {'route':'blue', 'sha':target['base']}
elif operation in ('up','verify','promote','switch','rollback'):
    with (root / 'stages.log').open('a') as log:
        log.write(operation + '\\n')
    assert (Path('direct.git/refs/relay/head').exists()
            or tuple(Path('bundle.git/objects/pack').glob('*.idx')))
    if operation == control.get('fail_stage'):
        sys.exit(20)
    if operation == control.get('interrupted_stage'):
        os.kill(os.getpid(), signal.SIGKILL)
    if operation == 'up':
        while (root / 'hold').exists():
            time.sleep(.05)
        assert (Path('direct.git/refs/relay/head').exists()
                or tuple(Path('bundle.git/objects/pack').glob('*.idx')))
    if operation in ('switch','rollback'):
        color, sha = ('green', target['head']) if operation == 'switch' else ('blue', target['base'])
        write(path, {'route':color,'sha':sha})
    sys.exit(0)
print(json.dumps(dict(current, identity=hashlib.sha256(json.dumps(current,sort_keys=True).encode()).hexdigest())))
""")
    auth = root / "auth"
    auth_pin = executable(auth, common + f"""
request = json.load(sys.stdin)
phase = request['phase']
now = int(time.time())
expiry = now + (10 if phase == 'preflight' else 120)
if phase == 'finalize' and control.get('expired_finalize'):
    expiry = now - 1
token = {CANARY!r} + phase + '_' + os.urandom(12).hex()
if control.get('stateless_grants'):
    token = 'ghs_123_' + token + '.' + ('a' * 420) + '.signature'
if control.get('reuse'):
    token = {CANARY!r} + 'preflight_0123456789'
grant = dict(request, repo_id=1448986421, installation_id=123, expires=expiry, token=token)
field = control.get('bad_grant')
if field in ('head', 'base', 'source'):
    grant['target'][field] = 'a' * len(grant['target'][field])
elif field == 'missing':
    del grant['token']
elif field == 'expires':
    grant['expires'] = now - 1
elif field:
    grant[field] = 'incorrect'
write(root / (phase + '.expiry'), expiry)
print(json.dumps(grant))
""")
    curl = root / "curl"
    curl_pin = executable(curl, common + f"""
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs
import subprocess
args = sys.argv[1:]
payload = sys.stdin.read()
assert {CANARY!r} in payload and {CANARY!r} not in str(args) and {CANARY!r} not in str(os.environ)
phase = payload.split({CANARY!r},1)[1].split('_',1)[0]
expiry = json.loads((root / (phase + '.expiry')).read_text())
table_file = root / 'api.json'
table = json.loads(table_file.read_text())
url = urlsplit(args[-1])
if url.path.startswith({ROOT!r} + '/issues/'):
    args[-1] = {f"https://127.0.0.1:{server.server_port}"!r} + url.path
    if url.query:
        args[-1] += '?' + url.query
    sys.exit(subprocess.run(['/usr/bin/curl', *args[:-1], '--cacert', {str(ca)!r}, args[-1]],
                            input=payload.encode()).returncode)
query = parse_qs(url.query)
path = url.path + ('?page='+query['page'][0] if 'page' in query else '')
if '/compare/' in path:
    path = url.path + '?' + url.query
row = table.get(path, {{'status':404, 'data':{{}}}})
if time.time() >= expiry:
    row = {{'status':401, 'data':{{}}}}
elif path == '/graphql':
    line = next(line for line in payload.splitlines() if line.startswith('data = '))
    mutation = json.loads(json.loads(line[7:]))
    with (root / 'writes.log').open('a') as log:
        log.write(json.dumps(mutation) + '\\n')
    mode = control.get('merge_mode', 'ack')
    if mode not in ('unknown', 'bare_ack', 'denied'):
        pr = table[{ROOT + "/pulls/160"!r}]['data']
        pr.update(state='closed', merged=True, merge_commit_sha={head!r},
                  merged_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        table[{main!r}]['data']['object']['sha'] = {head!r} if mode != 'drift' else 'c'*40
        write(table_file, table)
    if mode in ('lost','unknown','drift'):
        sys.exit(52)
    row = ({{'status':401, 'data':{{'message':'Bad credentials'}}}} if mode == 'denied'
           else {{'data':{{'data':{{'updateRefs':{{'clientMutationId':{expected["attempt"]!r}}}}}}}}})
Path(args[args.index('--dump-header')+1]).write_bytes(b'HTTP/1.1 200 OK\\r\\nX-GitHub-Request-Id: 00AA:1234\\r\\n\\r\\n')
print(json.dumps(row['data']))
print(row.get('status',200),end='')
""")
    port = unused_port()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "host")], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "key")], check=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / "sign")], check=True)
    signers = root / "signers"
    signers.write_text(SIGNER + " " + (root / "sign.pub").read_text())
    signers.chmod(0o600)
    known = root / "known"
    known.write_text(f"[127.0.0.1]:{port} " + (root / "host.pub").read_text())
    known.chmod(0o600)
    target_url = f"https://127.0.0.1:{server.server_port}/fixture.git"
    authority = root / "authority"
    authority.mkdir()
    # Re-exec source comes from a private fixture mirror, copied before SSH startup.
    shutil.copytree(REPO / "scripts", authority / "scripts", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "tests", authority / "tests", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    vps = {"target": expected, "state": str(state), "seed": str(seed), "authority": str(authority),
           "worker_signers": str(signers), "report_actor_id": 321,
           "fixture_observation_seconds": 1, "fixture_executor": str(stage),
           "fixture_executor_sha256": stage_pin, "fixture_ca": str(ca),
           "fixture_fetch_url": f"https://127.0.0.1:{unused_port()}/fixture.git"}
    worker_cfg = {"target": expected, "worker_state": str(worker), "worker_binding": str(source),
                  "ssh_host": "127.0.0.1", "ssh_port": port, "ssh_user": getpass.getuser(),
                  "ssh_identity": str(root / "key"), "ssh_known_hosts": str(known),
                  "worker_signing_key": str(root / "sign"),
                  "fixture_git_url": target_url, "fixture_ca": str(ca),
                  "fixture_ca_sha256": hashlib.sha256(ca.read_bytes()).hexdigest(),
                  "fixture_curl": str(curl), "fixture_curl_sha256": curl_pin, "auth": str(auth),
                  "auth_sha256": auth_pin, "installation_id": 123, "report_actor_id": 321,
                  "approved_body_sha256": hashlib.sha256(pr["body"].encode()).hexdigest()}
    write(root / "vps.json", vps)
    write(root / "worker.json", worker_cfg)
    launch = root / "forced"
    executable(launch, common + f"""
import subprocess
cmd = [{sys.executable!r}, '-I', '-B', '-c', {ENTRY!r}, {str(authority)!r},
       'scripts.prod_agent.relay_vps', 'ssh', '--fixture-root', str(root),
       '--fixture-profile', str(root / 'vps.json')]
raw = sys.stdin.buffer.read()
assert {CANARY.encode()!r} not in raw and b'923751' not in raw and b'923752' not in raw
res = subprocess.run(cmd, input=raw, capture_output=True, timeout=25)
assert {CANARY.encode()!r} not in res.stdout + res.stderr
if raw.startswith(b'{{"bundle_sha256"') and control.get('drop_ssh_receipt'):
    time.sleep(5)
    sys.exit(1)
if raw and json.loads(raw.splitlines()[0])['op'] == 'arm' and control.get('drop_arm_receipt'):
    sys.exit(1)
sys.stdout.buffer.write(res.stdout)
sys.exit(res.returncode)
""")
    (root / "authorized").write_text(
        f'restrict,command="{launch}" ' + (root / "key.pub").read_text())
    cfg = root / "sshd_config"
    cfg.write_text(f"""HostKey {root / 'host'}
AuthorizedKeysFile {root / 'authorized'}
ListenAddress 127.0.0.1
Port {port}
PidFile {root / 'pid'}
UsePAM {'yes' if sys.platform == 'linux' else 'no'}
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin yes
AllowTcpForwarding no
AllowAgentForwarding no
PermitTTY no
""")
    with subprocess.Popen([SSHD, "-D", "-e", "-f", str(cfg)], stderr=subprocess.PIPE) as sshd:
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and sshd.poll() is None:
                with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), .1):
                    break
                time.sleep(.05)
            assert sshd.poll() is None, sshd.stderr.read()
            yield Fixture(root, state, worker, port, expected, vps, worker_cfg, table, root / "code")
        finally:
            (root / "hold").unlink(missing_ok=True)
            sshd.terminate()
            sshd.wait(timeout=5)


class Fixture:
    def __init__(self, root, state, worker, port, expected, vps, config, table, repo):
        self.root, self.state, self.worker, self.port = root, state, worker, port
        self.expected, self.vps, self.config, self.table, self.repo = expected, vps, config, table, repo
        self.worker_cli = [sys.executable, "-I", "-B", "-c", ENTRY, str(REPO),
            "scripts.prod_agent.relay_worker", "--fixture-root", str(root),
            "--fixture-profile", str(root / "worker.json")]
        self.ssh_cli = ["/usr/bin/ssh", "-F", "/dev/null", "-T", "-p", str(port), "-i",
                        str(root / "key"), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                        "-o", "UserKnownHostsFile=" + str(root / "known"), "127.0.0.1"]

    def options(self, **values):
        write(self.root / "control.json", values)

    def call(self, op, success=True):
        run = subprocess.run([*self.worker_cli, op], capture_output=True, timeout=50,
                             env={**os.environ, "GH_TOKEN": CANARY + "ambient"})
        assert (run.returncode == 0) == success, (op, run.stdout, run.stderr)
        assert CANARY.encode() not in run.stdout + run.stderr
        return json.loads(run.stdout)

    def request(self, op, raw=b"", target=None, signed=True, **fields):
        envelope = {"op": op, "target": target or self.expected, **fields}
        if op not in {"status", "report_status"} and signed:
            envelope["signature"] = sign(envelope, self.root / "sign")
        data = canonical(envelope) + b"\n" + raw
        return subprocess.run([*self.ssh_cli, COMMAND], input=data, capture_output=True, timeout=10)

    def wait(self, phase):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = self.call("status")
            if row["phase"] == phase:
                return row
            time.sleep(.1)
        pytest.fail(f"expected {phase}, last receipt {row}")

    def posts(self):
        path = self.root / "writes.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def journal(self):
        path = self.root / "stages.log"
        return path.read_text().splitlines() if path.exists() else []

    def legacy(self):
        return subprocess.run([sys.executable, "-B", str(REPO / "scripts/blue_green/deploy_state.py"),
                               "run", "--", "/usr/bin/true"], capture_output=True,
                              env={**os.environ, "DAILYCHECK_DEPLOY_STATE_DIR": str(self.state)}, timeout=10)

    def bundle(self):
        path = self.root / "test.bundle"
        cmd("git", "-C", self.repo, "bundle", "create", "--version=2", path,
            REF, "^" + self.expected["base"])
        return path.read_bytes()


@pytest.mark.parametrize("merge_mode", ["ack", "lost", "unknown", "bare_ack", "drift"])
def test_merge_and_uncertain_response(relay, merge_mode):
    relay.options(merge_mode=merge_mode, stateless_grants=merge_mode == "ack")
    assert relay.call("start")["phase"] == "accepted"
    observed = relay.wait("verified")
    assert observed["source_method"] == "incremental_bundle" and observed["observation_seconds"] >= 1
    assert observed["fallback_started_at"] <= observed["source_verified_at"]
    assert observed["source_verified_at"] <= observed["journal"][0]["started_at"]
    assert relay.legacy().returncode == 10
    success = merge_mode in {"ack", "lost"}
    row = relay.call("finalize", success=success)
    t = relay.expected
    assert relay.posts() == [github_ff_payload(t["base"], t["head"], t["ref"], t["attempt"])]
    if success:
        assert row["phase"] == "settled" and row["post_acknowledged"] is (merge_mode == "ack")
        assert row["remote"]["head"] == row["remote"]["main"] == t["head"]
        assert relay.legacy().returncode == 10
        assert relay.call("reconcile")["receipt"] == row["receipt"]
    else:
        assert relay.call("status")["phase"] == "armed"
        assert relay.legacy().returncode == 10
        relay.call("abort", success=False)
        relay.call("reconcile", success=False)
    relay.call("finalize", success=False)
    assert len(relay.posts()) == 1
    assert relay.journal() == ["up", "verify", "promote", "switch"]


@pytest.mark.parametrize("failure", ["pending", "failed", "head", "base", "missing", "source"])
def test_admission_denied(relay, failure):
    if failure in {"pending", "failed"}:
        state = ("in_progress", None) if failure == "pending" else ("completed", "failure")
        head = relay.expected["head"]
        for row in (relay.table[ROOT + f"/commits/{head}/check-runs?page=1"]["data"]["check_runs"][0],
                    relay.table[ROOT + "/actions/runs/40/jobs?page=1"]["data"]["jobs"][0]):
            row.update(status=state[0], conclusion=state[1])
    elif failure in {"head", "base"}:
        relay.table[ROOT + "/pulls/160"]["data"][failure]["sha"] = "e" * 40
    else:
        path = relay.root / "source/bindings.json"
        if failure == "missing":
            path.unlink()
        else:
            value = read(path)
            value["bindings"][0]["source"]["number"] += 1
            write(path, value)
    write(relay.root / "api.json", relay.table)
    relay.call("start", success=False)
    assert relay.call("status")["phase"] == "absent"
    assert not relay.journal() and not relay.posts()


def test_expiry_stale_approval_wrong_receipt_and_abort(relay):
    (relay.root / "hold").touch()
    assert relay.call("start")["phase"] == "accepted"
    while time.time() <= read(relay.root / "preflight.expiry"):
        time.sleep(.2)
    assert relay.call("status")["process_fence"] == "held"
    (relay.root / "hold").unlink()
    relay.wait("verified")
    relay.options(expired_finalize=True)
    relay.call("finalize", success=False)
    assert relay.call("status")["phase"] == "verified" and not relay.posts()
    relay.options()
    reviews = relay.table[ROOT + "/pulls/160/reviews?page=1"]["data"]
    reviews[0]["commit_id"] = relay.expected["base"]
    write(relay.root / "api.json", relay.table)
    relay.call("finalize", success=False)
    assert not relay.posts()
    assert relay.request("arm", observation="0" * 64, ci="0" * 64).returncode == 1
    assert relay.request("arm", observation="0" * 64, ci="0" * 64, signed=False).returncode == 1
    bad = {**relay.expected, "generation": 22}
    assert relay.request("status", target=bad).returncode == 1
    bad = {**relay.expected, "attempt": "6" * 32}
    assert relay.request("status", target=bad).returncode == 1
    reviews[0]["commit_id"] = relay.expected["head"]
    write(relay.root / "api.json", relay.table)
    assert relay.call("finalize")["phase"] == "settled"
    relay.call("start", success=False)
    assert relay.journal() == ["up", "verify", "promote", "switch"]


def test_ssh_loss_child_exclusion_and_explicit_stage_failure(relay):
    relay.options(drop_ssh_receipt=True, fail_stage="verify")
    (relay.root / "hold").touch()
    with subprocess.Popen([*relay.worker_cli, "start"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as worker:
        running = relay.wait("staging")
        assert running["process_fence"] == "held"
        # Parent dies while the exact SSH start remains open and the detached owner stages.
        worker.kill()
        worker.wait(timeout=5)
    assert relay.call("status")["process_fence"] == "held"
    assert relay.legacy().returncode == 10
    blob = relay.bundle()
    assert relay.request("start", blob, ci="c" * 64, bundle_size=len(blob),
                         bundle_sha256=hashlib.sha256(blob).hexdigest()).returncode == 1
    assert subprocess.run([*relay.ssh_cli, "sh"], input=b"", capture_output=True, timeout=5).returncode == 1
    (relay.root / "hold").unlink()
    report = relay.wait("rolled_back")
    assert report["failed_stage"] == "verify" and report["live"] is True
    assert relay.journal() == ["up", "verify", "rollback"] and not relay.posts()
    relay.call("start", success=False)


@pytest.mark.parametrize("mutation", ["digest", "base", "head", "tree", "extra_ref", "vps_source"])
def test_bundle_rejected_before_candidate_stages(relay, mutation):
    raw = relay.bundle()
    expected = dict(relay.expected)
    if mutation == "tree":
        expected["tree"] = "a" * 40
        write(relay.root / "vps.json", {**relay.vps, "target": expected})
    elif mutation in {"head", "base"}:
        raw = raw.replace(expected[mutation].encode(), ("a" * 40).encode(), 1)
    elif mutation == "extra_ref":
        raw = raw.replace(b"\n\nPACK", b"\n" + expected["head"].encode()
                          + b" refs/heads/extra\n\nPACK", 1)
    elif mutation == "vps_source":
        (relay.state / "bindings.json").unlink()
    checksum = hashlib.sha256(raw).hexdigest() if mutation != "digest" else "1" * 64
    result = relay.request("start", raw, target=expected, ci="c" * 64,
                           bundle_size=len(raw), bundle_sha256=checksum)
    if mutation == "tree":
        assert result.returncode == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = relay.request("status", target=expected)
            if json.loads(status.stdout)["phase"] == "rejected":
                break
            time.sleep(.1)
        else:
            pytest.fail("wrong tree did not reach rejected terminal state")
    else:
        assert result.returncode == 1
    assert not relay.journal() and not relay.posts()


def test_direct_anonymous_fetch_preferred(relay):
    write(relay.root / "vps.json", {**relay.vps, "fixture_fetch_url": relay.config["fixture_git_url"]})
    protection = relay.table[ROOT + "/branches/main/protection"]["data"]
    protection.pop("required_pull_request_reviews")
    write(relay.root / "api.json", relay.table)
    assert relay.call("start")["phase"] == "accepted"
    assert relay.wait("verified")["source_method"] == "anonymous_https"
    assert relay.call("abort")["phase"] == "rolled_back"


@pytest.mark.parametrize("drift", ["main_changed", "pr_closed", "pr_merged"])
def test_abort_requires_authoritative_unchanged_base(relay, drift):
    relay.call("start")
    relay.wait("verified")
    if drift == "main_changed":
        relay.table[ROOT + "/git/ref/heads/main"]["data"]["object"]["sha"] = "d" * 40
    else:
        relay.table[ROOT + "/pulls/160"]["data"].update(
            state="closed", merged=drift == "pr_merged")
    write(relay.root / "api.json", relay.table)
    relay.call("abort", success=False)
    relay.call("finalize", success=False)
    assert relay.call("status")["phase"] == "verified" and relay.legacy().returncode == 10
    assert not relay.posts() and "rollback" not in relay.journal()


def test_definitive_post_denial_permits_exact_blue_rollback(relay):
    relay.options(merge_mode="denied")
    relay.call("start")
    relay.wait("verified")
    row = relay.call("finalize", success=False)
    assert row["phase"] == "rolled_back" and row["rejection"]["reason"] == "http_401"
    assert relay.legacy().returncode == 10 and len(relay.posts()) == 1
    assert relay.call("reconcile", success=False)["receipt"] == row["receipt"]
    assert relay.journal().count("rollback") == 1


def test_dead_staging_owner_leaves_legacy_fence(relay):
    (relay.root / "hold").touch()
    relay.call("start")
    deadline = time.monotonic() + 7
    while not relay.journal() and time.monotonic() < deadline:
        time.sleep(.06)
    assert relay.journal() == ["up"]
    assert relay.call("status")["process_fence"] == "held"
    row = relay.call("status")
    os.kill(row["stage_pid"], signal.SIGKILL)
    (relay.root / "hold").unlink()
    assert relay.call("status")["process_fence"] == "unheld_reconcile"
    assert relay.legacy().returncode == 10
    assert read(relay.state / "state.json")["worker_relay"]["previous_last_attempt"]["pr"] == 43
    relay.call("start", success=False)


def test_profile_uses_physical_system_configuration_path():
    expected = "/private/etc" if sys.platform == "darwin" else "/etc"
    assert WORKER_PATH == Path(expected) / "dailycheck-relay/worker.json"
    assert Path(expected).resolve() == Path(expected)


def test_lost_arm_recovers_proven_prepost_failure(relay):
    relay.options(drop_arm_receipt=True)
    relay.call("start")
    relay.wait("verified")
    relay.call("finalize", success=False)
    assert relay.call("status")["phase"] == "armed" and not relay.posts()
    record = read(relay.worker / (relay.expected["attempt"] + ".json"))
    assert "pre_arm_at" in record and "post_at" not in record
    result = relay.call("reconcile", success=False)
    assert result["phase"] == "rolled_back" and result["rejection"]["reason"] == "local_no_post"
    assert relay.journal().count("rollback") == 1 and not relay.posts()


def test_observation_connection_refused_rolls_back_and_publishes_failure(relay):
    write(relay.root / "vps.json", {**relay.vps, "fixture_observation_port": unused_port()})
    relay.call("start")
    result = relay.wait("rolled_back")
    assert result["failed_stage"] == "observe" and result["target"]["attempt"] == relay.expected["attempt"]
    assert relay.journal() == ["up", "verify", "promote", "switch", "rollback"] and not relay.posts()
    assert result["publication"] == {"state": "ready", "comment_id": 0}
    assert relay.call("report")["publication"] == {"state": "linked", "comment_id": 456}
    assert "失败" in read(relay.root / "comments.json")[0]["body"]


def test_unwaitable_write_stage_cannot_authorize_rollback(relay):
    relay.options(interrupted_stage="switch")
    relay.call("start")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = relay.call("status")
        if row.get("process_fence") == "unheld_reconcile":
            break
        time.sleep(.1)
    assert row["phase"] == "staging" and row["process_fence"] == "unheld_reconcile"
    assert row["journal"][-1]["stage"] == "switch" and "completed_at" not in row["journal"][-1]
    assert relay.legacy().returncode == 10 and "rollback" not in relay.journal()


@pytest.mark.parametrize("mode", ["ack", "lost", "unknown", "bare_ack", "mismatched"])
def test_public_report_requires_verified_comment_and_never_reposts(relay, mode):
    relay.options(report_mode=mode)
    relay.call("start")
    relay.wait("verified")
    result = relay.call("finalize")
    assert result["phase"] == "settled" and result["publication"]["state"] == "ready"
    assert relay.legacy().returncode == 10
    assert read(relay.state / "state.json")["generation"] == relay.expected["generation"]
    attempt_id = relay.expected["attempt"]
    if mode == "mismatched":
        state = read(relay.state / "state.json")
        state["outcomes"][attempt_id]["public_receipt"] = "00000000-0000-0000-0000-000000000000"
        write(relay.state / "state.json", state)
        relay.call("report", success=False)
        assert not (relay.root / "report-posts.log").exists()
        return
    success = mode in {"ack", "lost"}
    for _ in range(2):
        row = relay.call("report", success=success)
        assert row["phase"] == "settled"
        assert row["publication"]["state"] == ("linked" if success else "uncertain")
    assert (relay.root / "report-posts.log").read_text().splitlines() == ["POST"]
    publication = read(relay.state / "state.json")["publications"][attempt_id]
    assert publication["comment_id"] == (456 if success else 0)
    assert relay.legacy().returncode == (0 if success else 10)


@pytest.mark.parametrize("field", ["missing", "expires", "installation_id", "repo_id",
                                  "phase", "head", "base", "source", "issuer_pin"])
def test_phase_auth_contract_denies_before_admission(relay, field):
    if field == "issuer_pin":
        with (relay.root / "auth").open("a") as stream:
            stream.write("# different issuer bytes\n")
    else:
        relay.options(bad_grant=field)
    relay.call("start", success=False)
    assert relay.call("status")["phase"] == "absent"
    state = read(relay.state / "state.json")
    assert state["generation"] == 20 and state["last_attempt"]["pr"] == 43
    assert not relay.journal() and not relay.posts()


def test_phase_grant_reuse_stops_before_merge(relay):
    relay.options(reuse=True)
    relay.call("start")
    relay.wait("verified")
    relay.call("finalize", success=False)
    assert relay.call("status")["phase"] == "verified"
    assert not relay.posts()


def test_unknown_report_ambiguity_holds_then_exact_readback_links(relay):
    relay.call("start")
    relay.wait("verified")
    relay.call("finalize")
    relay.options(report_mode="lost", hide_report=True)
    assert relay.call("report", success=False)["publication"]["state"] == "uncertain"
    first = read(relay.state / "state.json")
    comment, = read(relay.root / "comments.json")
    assert relay.legacy().returncode == 10
    relay.options()
    write(relay.root / "comments.json", [comment, {**comment, "id": 457}])
    relay.call("report", success=False)
    assert relay.legacy().returncode == 10
    assert read(relay.state / "state.json") == first
    write(relay.root / "comments.json", [comment])
    assert relay.call("report")["publication"] == {"state": "linked", "comment_id": 456}
    assert relay.legacy().returncode == 0
    assert (relay.root / "report-posts.log").read_text().splitlines() == ["POST"]
    after = read(relay.state / "state.json")
    for field in ("deployed", "outcomes", "generation", "last_attempt"):
        assert first[field] == after[field]
