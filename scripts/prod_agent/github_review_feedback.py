"""Trusted pre-candidate BLOCKER feedback from the existing Hermes control plane.

No caller-supplied receipt, session path, approval, token grant or retry is
accepted. Root-owned session-store/tool results are the authority, just as the
existing process registry is for review custody. A compromised root/Hermes store
is outside that authority boundary. This is neither formal review nor deploy
success; only a delivered, done/awaiting review can consume this evidence.
"""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
from datetime import datetime
from pathlib import Path

from scripts.blue_green import deploy_state
from scripts.hosting import strict_json_loads
from scripts.prod_agent import github_intake

CONFIG = Path("/etc/dailycheck/github-review-feedback.json")
# Reviewed inspect/file implementations: these modes cannot call prepared(),
# refresh, formal review or candidate. Unknown engine code fails closed.
READONLY_ENGINES = frozenset(
    {
        "1422771fdf18682eae481692997933fce4573468f9253fdeeafcc6bd97a142e1",
        "234a0e44bd9a432df5c4d6c90f68fcfa01381a56dd730a51ff97adaef0191a9b",
    }
)


def need(value):
    if not value:
        raise ValueError("terminal_review_feedback_unconfirmed")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def private_path(path, *, directory=False, limit=8_388_608, mode=None):
    path = Path(path)
    for parent in path.parents:
        info = parent.lstat()
        need(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022
        )
    if directory:
        info = path.lstat()
        need(
            stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077
        )
        return path
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        need(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and before.st_size <= limit
            and (mode is None or stat.S_IMODE(before.st_mode) == mode)
        )
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        need(
            len(raw) <= limit
            and (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        )
        return raw
    finally:
        os.close(fd)


def settings():
    raw = private_path(CONFIG, limit=16384, mode=0o600)
    value = strict_json_loads(raw)
    need(
        set(value) == {"version", "state_dir", "hermes_home"}
        and type(value["version"]) is int
        and value["version"] == 1
    )
    state, home = (Path(value[k]) for k in ("state_dir", "hermes_home"))
    need(
        state.is_absolute()
        and home.is_absolute()
        and state == deploy_state.state_dir()
        and ".." not in state.parts
        and ".." not in home.parts
        and state != home
    )
    private_path(state, directory=True)
    private_path(home, directory=True)
    return state, home, digest(raw)


def instant(value):
    need(isinstance(value, str))
    return datetime.fromisoformat(value).timestamp()


def entry_contract(command, state, trigger):
    need(isinstance(command, str) and len(command) <= 2048)
    argv = shlex.split(command)
    need(
        len(argv) in {6, 7}
        and argv[1:3] == ["-I", "-B"]
        and argv[4] in {"inspect", "file"}
        and argv[5] == trigger
        and len(argv) == (7 if argv[4] == "file" else 6)
        and command == shlex.join(argv)
    )
    path = Path(argv[3])
    match = re.fullmatch(r"automatic-([0-9a-f]{40})\.py", path.name)
    need(match is not None and path.parent == state)
    raw = private_path(path, mode=0o700)
    configs = [
        node
        for node in ast.parse(raw).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in node.targets)
    ]
    need(len(configs) == 1)
    config = ast.literal_eval(configs[0].value)
    template = config["profile"]
    source_head = match[1]
    need(
        template["head"] == source_head
        and template["state"] == str(state)
        and template["uid"] == os.geteuid()
        and argv[0] == template["python"]
        and Path(config["roots"]) == state / ("automatic-work-" + source_head)
    )
    engine = Path(template["engine"])
    private_path(engine, directory=True)
    source = private_path(engine / "scripts/blue_green/release_automatic.py", mode=0o400)
    need(
        digest(source) in READONLY_ENGINES
        and digest(source) == template["pins"]["scripts/blue_green/release_automatic.py"]
        and source.count(b"\nCONFIG = None  # BUILD_CONFIG\n") == 1
        and raw
        == source.replace(
            b"CONFIG = None  # BUILD_CONFIG",
            ("CONFIG = " + repr(config) + "  # BUILD_CONFIG").encode(),
            1,
        )
    )
    if len(argv) == 7:
        name = Path(argv[6])
        need(
            not name.is_absolute()
            and ".." not in name.parts
            and str(name) == argv[6]
            and not any(ord(c) < 32 for c in argv[6])
        )
    return config, dict(
        entry_sha256=digest(raw), engine_sha256=digest(source), engine_head=source_head
    )


def observation(payload, fence):
    """Read only; called twice under the original native fence before DB commit."""
    need(github_intake.historical_review_authentic(payload))
    state, home, config_hash = settings()
    held, expected = os.fstat(fence), (state / deploy_state.ATTEMPT_LOCK_FILENAME).stat()
    need((held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino))
    fcntl.flock(fence, fcntl.LOCK_EX | fcntl.LOCK_NB)
    native = deploy_state.load_state(state, strict=True)
    need(
        native.get("lock") is None
        and not deploy_state.native_blocked(state, native)
        and (native.get("last_attempt") or {}).get("sha") != payload["pr_head_sha"]
        and not any(
            v.get("sha") == payload["pr_head_sha"] and v.get("pr") == payload["pr_number"]
            for v in native.get("outcomes", {}).values()
        )
    )
    trigger = payload["trigger_id"]
    need(re.fullmatch(r"trig_[0-9a-f]{24}", trigger) is not None)
    chat = "webhook:wdg-ci:dailycheck-" + trigger
    key = "agent:main:webhook:webhook:" + chat + ":webhook:wdg-ci"
    index = strict_json_loads(private_path(home / "sessions/sessions.json", mode=0o600))
    mapped = index[key]
    origin = mapped["origin"]
    sid = mapped["session_id"]
    need(
        mapped["session_key"] == key
        and mapped["platform"] == "webhook"
        and mapped["chat_type"] == "webhook"
        and origin["platform"] == "webhook"
        and origin["chat_type"] == "webhook"
        and origin["chat_id"] == chat
        and origin["user_id"] == "webhook:wdg-ci"
        and origin.get("thread_id") is None
        and isinstance(sid, str)
        and re.fullmatch(r"\d{8}_\d{6}_[a-zA-Z0-9]{8}", sid)
    )
    raw = private_path(home / "sessions" / ("session_" + sid + ".json"), mode=0o600)
    session = strict_json_loads(raw)
    messages = session["messages"]
    need(
        session["session_id"] == sid
        and session["platform"] == "webhook"
        and session["message_count"] == len(messages)
        and 4 <= len(messages) <= 202
        and messages[0]["role"] == "user"
        and messages[-1]["role"] == "assistant"
        and messages[-1].get("finish_reason") == "stop"
        and not messages[-1].get("tool_calls")
        and bool(messages[-1].get("content"))
        and instant(session["session_start"]) * 1000 >= payload["dispatch_claimed_at_ms"] - 1000
        and instant(session["last_updated"]) >= instant(session["session_start"])
        and instant(mapped["updated_at"]) >= instant(session["last_updated"])
    )
    calls = []
    contract = None
    for i in range(1, len(messages) - 1, 2):
        request, response = messages[i : i + 2]
        need(
            request["role"] == "assistant"
            and request.get("finish_reason") == "tool_calls"
            and len(request["tool_calls"]) == 1
            and response["role"] == "tool"
        )
        call = request["tool_calls"][0]
        need(
            call["type"] == "function"
            and call["function"]["name"] == "terminal"
            and isinstance(call["id"], str)
            and response["tool_call_id"] == call["id"]
        )
        args = strict_json_loads(call["function"]["arguments"])
        need(
            not args.get("background")
        )  # Background/timeout/unknown needs its own actual wait path.
        config, identity = entry_contract(args["command"], state, trigger)
        need(contract is None or contract == identity)
        contract = identity
        root = Path(config["roots"])
        private_path(root, directory=True)
        admission = payload["admission_id"]
        need(
            not any(
                os.path.lexists(root / name)
                for name in (admission, admission + ".intent.json", admission + ".prepared.json")
            )
        )
        result = strict_json_loads(response["content"])
        last = i == len(messages) - 3
        need(
            result.get("error") is None
            and type(result.get("exit_code")) is int
            and result["exit_code"] == (78 if last else 0)
        )
        if last:
            blocker = strict_json_loads(result["output"].strip())
            reason = blocker.get("reason")
            need(
                blocker.get("status") == "BLOCKER"
                and blocker.get("deployment_proven") is False
                and isinstance(reason, str)
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", reason)
            )
        calls.append(dict(tool_call_id=call["id"], result_sha256=digest(encoded(result))))
    need(calls and len({x["tool_call_id"] for x in calls}) == len(calls))
    processes = strict_json_loads(private_path(home / "processes.json", mode=0o600))
    need(
        isinstance(processes, list)
        and not any(row.get("task_id") == sid or row.get("session_key") == key for row in processes)
    )
    # Positive gateway turn completion, not process/session absence. New inbound
    # work after this response invalidates the observation. Unrelated chat logs
    # do not invalidate a receipt or require shared gateway changes.
    log = private_path(home / "logs/agent.log", limit=32 * 1024 * 1024).decode()
    prefixes = [
        r"gateway\.run: inbound message: platform=webhook user=wdg-ci chat="
        + re.escape(chat)
        + r" msg=",
        r"gateway\.run: response ready: platform=webhook chat=" + re.escape(chat) + r" time=",
        r"gateway\.platforms\.base: \[Webhook\] Sending response \(\d+ chars\) to "
        + re.escape(chat)
        + r"$",
    ]
    relevant = []
    for line in log.splitlines():
        for kind, pattern in enumerate(prefixes):
            if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO " + pattern, line):
                relevant.append((kind, line))
                break
    need(len(relevant) >= 3 and [kind for kind, _ in relevant[-3:]] == [0, 1, 2])
    return dict(
        version=1,
        kind="pre_candidate_BLOCKER",
        trigger_id=trigger,
        admission_id=payload["admission_id"],
        pr=payload["pr_number"],
        head=payload["pr_head_sha"],
        payload_sha256=digest(encoded(payload)),
        session_id=sid,
        session_sha256=digest(raw),
        session_index_entry_sha256=digest(encoded(mapped)),
        gateway_completion_sha256=digest(encoded(relevant)),
        config_sha256=config_hash,
        native_state_sha256=digest(private_path(state / "state.json")),
        blocker_reason=reason,
        tools=calls,
        no_candidate=True,
        formal_review=False,
        **contract,
    )
