"""Fixed SSH ingress and durable stage/observation authority on the VPS."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from scripts.blue_green import deploy_state as ds
from scripts.blue_green.publication_authority import resolve_source_binding
from scripts.hosting import HostingError, strict_json_loads
from scripts.prod_agent import relay_git as objects
from scripts.prod_agent import relay_publication
from scripts.prod_agent.native_ports import protected_path
from scripts.prod_agent.relay_protocol import (
    COMMAND,
    MAX_BUNDLE,
    MAX_CONTROL,
    REPO,
    STATE_PATH,
    SYSTEM_ENV,
    StageFailure,
    canonical,
    checked_file,
    digest,
    hex_id,
    lock_file,
    positive,
    read_file,
    require,
    secure_profile,
    sync_dir,
    target,
    verify,
)

ENTRY = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module(sys.argv.pop(1),run_name='__main__')"
MARKER = ds.NATIVE_GUARD_FILENAME
STAGES = ("up", "verify", "promote", "switch")
MINIMUM = 600
TERMINAL = {"settled", "rolled_back", "rejected"}


class Server:
    def __init__(self, profile_path, fixture_root=None):
        self.profile_path = Path(profile_path)
        self.profile, self.fixture = secure_profile(self.profile_path, fixture_root)
        self.expected = target(self.profile["target"])
        self.directory = self.path("state", private=True)
        self.seed = self.path("seed", private=True)
        self.authority = self.path("authority")
        self.work = self.directory / ("relay-" + self.expected["attempt"])
        self.minimum = MINIMUM
        if self.fixture:
            self.minimum = self.profile["fixture_observation_seconds"]
            require(type(self.minimum) is int and 1 <= self.minimum <= 15, "fixture duration")
        else:
            require(sys.platform == "linux" and os.geteuid() == 0, "protected Linux VPS required")
            require(self.authority == Path(__file__).resolve().parents[2], "authority path changed")
            require(hex_id(self.profile.get("source_db_identity"), 64),
                    "protected live DB identity required")
            require(isinstance(self.profile.get("admin_audit_log"), str),
                    "protected Web admin audit path required")
            from scripts.prod_agent.relay_pipeline import verification_uid

            verification_uid(self.profile)
        os.environ.clear()
        os.environ.update(SYSTEM_ENV, DAILYCHECK_DEPLOY_STATE_DIR=str(self.directory))

    def path(self, key, private=False):
        return protected_path(Path(self.profile[key]), os.geteuid(),
                              anchor=self.fixture, private=private)

    def state(self):
        return ds.load_state(self.directory, strict=True)

    def source(self):
        checked_file(self.directory / "bindings.json", anchor=self.fixture)
        binding = resolve_source_binding(self.directory, self.expected["pr"])
        require(binding.source[0] == "github" and binding.issue_identities is not None
                and binding.binding_sha256 == self.expected["source"], "source binding changed")

    @property
    def marker(self):
        return {"version": 1, "relay": digest(self.expected)}

    def owned(self, state):
        row, lock = state.get("worker_relay"), state.get("lock")
        require(isinstance(row, dict) and row["target"] == self.expected
                and state["generation"] == self.expected["generation"]
                and state["deployed"] == row["previous_deployed"]
                and isinstance(lock, dict) and lock["attempt_id"] == self.expected["attempt"]
                and lock["generation"] == state["generation"] and lock["pr"] == self.expected["pr"]
                and lock["sha"] == self.expected["head"]
                and read_file(self.directory / MARKER) == self.marker, "relay fence mismatch")
        return row, lock

    def _bind_owner(self, fd):
        with ds.state_transaction(self.directory):
            _, lock = self.owned(self.state())
        os.environ[ds.ATTEMPT_FD_ENV] = str(fd)
        os.environ[ds.ATTEMPT_ENV] = canonical({
            key: lock[key] for key in ("token", "pr", "sha", "session")
        }).decode()

    def _set(self, **facts):
        with ds.state_transaction(self.directory):
            state = self.state()
            row, lock = self.owned(state)
            require(ds.process_lock_fd() is not None, "relay process fence lost")
            row.update(facts)
            lock["heartbeat_at"] = ds.now_epoch()
            ds.save_state(self.directory, state)

    def runtime(self, role):
        if self.fixture:
            result = self._fixture_stage(role)
            value = strict_json_loads(result, max_bytes=MAX_CONTROL)
            if role == "green" and "fixture_observation_port" in self.profile:
                from scripts.prod_agent.relay_pipeline import _http

                port = self.profile["fixture_observation_port"]
                require(type(port) is int and 1024 <= port <= 65535, "fixture probe port")
                code, _ = _http(port, "/health")
                require(code == 200, "fixture HTTP probe failed")
        else:
            from scripts.prod_agent.relay_pipeline import live_route
            value = live_route(self, role)
        require(isinstance(value, dict) and set(value) == {"sha", "route", "identity"}
                and value["sha"] == self.expected["head" if role in {"green", "candidate-standby"} else "base"]
                and value["route"] in ({"blue", "green"} if role == "previous" else
                                    {"blue"} if role == "standby" else
                                    {"green"} if role == "candidate-standby" else {role})
                and hex_id(value["identity"], 64), "runtime route/version unproven")
        return value

    def observe_green(self, role="green"):
        # This boundary runs after the positively waited switch. Read-only probe
        # failure is evidence of unhealthy observation, unlike an interrupted write.
        try:
            return self.runtime(role)
        except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as exc:
            raise StageFailure("observation read failed") from exc

    def _fixture_stage(self, operation):
        path = checked_file(self.profile["fixture_executor"], anchor=self.fixture,
                            private=False, pin=self.profile["fixture_executor_sha256"])
        child = subprocess.run([str(path), operation], env=SYSTEM_ENV,
                               input=canonical(self.expected), stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, cwd=self.work,
                               timeout=25, check=False)
        if child.returncode > 0:
            raise StageFailure(operation)
        require(child.returncode == 0, "fixture child exit uncertain")
        return child.stdout

    def stage(self, name):
        if self.fixture:
            self._fixture_stage(name)
        else:
            from scripts.prod_agent.relay_pipeline import RelayPipeline
            RelayPipeline(self).stage(name)

    def admit(self, envelope, raw):
        require(set(envelope) == {"op", "target", "ci", "bundle_size", "bundle_sha256"}
                and hex_id(envelope["ci"], 64), "admission shape")
        require(len(raw) == envelope["bundle_size"] and 0 < len(raw) <= MAX_BUNDLE
                and hex_id(envelope["bundle_sha256"], 64)
                and hashlib.sha256(raw).hexdigest() == envelope["bundle_sha256"], "bundle framing")
        objects.bundle_header(raw, self.expected)
        with lock_file(self.directory, ds.ATTEMPT_LOCK_FILENAME) as owner:
            fd = owner.fileno()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with ds.state_transaction(self.directory):
                state = self.state()
                previous = state.get("deployed") or {}
                require(not ds.native_blocked(self.directory, state) and state.get("native_queue") is None
                        and state["lock"] is None
                        and self.expected["generation"] == state["generation"] + 1
                        and previous.get("sha") == self.expected["base"]
                        and positive(previous.get("generation"))
                        and previous["generation"] <= state["generation"]
                        and (state.get("last_evicted") is None
                             or (0 < state["last_evicted"]["generation"] < previous["generation"]))
                        and self._admissible_head(state, envelope["ci"]), "baseline/fence not ready")
                self.source()
                prior = state.get("worker_relay")
                if prior is not None:
                    require(prior["phase"] in TERMINAL, "previous relay unresolved")
                    state.setdefault("worker_relay_history", {})[prior["target"]["attempt"]] = prior
                self.work.mkdir(mode=0o700)
                archive = self.work / "incoming.bundle"
                with archive.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                sync_dir(self.work)
                ds.native_guard(self.directory, self.marker)
                state["generation"] = self.expected["generation"]
                now = ds.now_epoch()
                state["lock"] = {
                    "attempt_id": self.expected["attempt"], "generation": state["generation"],
                    "token": secrets.token_hex(16), "pr": self.expected["pr"],
                    "sha": self.expected["head"], "session": "local-worker-relay",
                    "phase": "relay-stage", "acquired_at": now, "heartbeat_at": now,
                    "deployment": self.deployment(),
                }
                state["worker_relay"] = {
                    "target": self.expected, "ci": envelope["ci"],
                    "bundle": envelope["bundle_sha256"], "previous_deployed": previous,
                    "previous_last_attempt": state["last_attempt"],
                    "phase": "staging", "journal": [], "admitted_at": now,
                }
                ds.save_state(self.directory, state)
            args = [sys.executable, "-I", "-B", "-c", ENTRY, str(self.authority),
                    "scripts.prod_agent.relay_vps", "stage", "--fd", str(fd)]
            if self.fixture:
                args += ["--fixture-root", str(self.fixture), "--fixture-profile", str(self.profile_path)]
            subprocess.Popen(args, cwd="/", env=SYSTEM_ENV, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, pass_fds=(fd,))
        return {"phase": "accepted", "target": self.expected}

    def _admissible_head(self, state, ci):
        expected = self.expected
        failed = ds.failed_head_status(state, expected["pr"], expected["head"], self.deployment())
        retry = self.profile.get("verified_retry")
        if retry is None:
            return failed is None
        if self.fixture or failed != "failed-head" or self.profile.get("hosted_verify") is not True:
            return False
        prior = state.get("worker_relay") or {}
        old = prior.get("target") or {}
        number = old.get("generation")
        previous = old.get("attempt")
        publication = state["publications"].get(previous) or {}
        last = state["last_attempt"] or {}
        journal = prior.get("journal") or []
        stage = retry.get("failed_stage")
        decision = retry.get("recovery_decision")
        allowed = {"verify": ["up", "verify"], "promote": ["up", "verify", "promote"]}
        return (
            isinstance(decision, str) and decision.startswith("A-") and hex_id(decision[2:], 32)
            and stage in allowed
            and retry == {"previous_attempt": previous, "previous_receipt": prior.get("receipt"),
                          "failed_stage": stage, "ci_sha256": ci, "recovery_decision": decision}
            and type(number) is int and number == state["generation"] == expected["generation"] - 1
            and hex_id(previous, 32) and previous != expected["attempt"]
            and old == {**expected, "generation": number, "attempt": previous}
            and prior.get("ci") == ci and prior.get("phase") == "rolled_back"
            and prior.get("failed_stage") == stage and "intent" not in prior
            and [entry["stage"] for entry in journal] == allowed[stage]
            and all(type(entry.get("completed_at")) is int
                    and entry["completed_at"] >= entry["started_at"] for entry in journal[:-1])
            and "completed_at" not in journal[-1]
            and previous == last.get("outcome_id") and last.get("result") == "failed"
            and state["outcomes"][previous]["public_receipt"] == prior.get("receipt")
            and publication.get("state") == "linked" and positive(publication.get("comment_id"))
        )

    def deployment(self):
        return {"provider": "github", "host": "github.com", "repository": REPO,
                "pr": self.expected["pr"]}

    def _select_objects(self, bundle_digest):
        repo = objects.make_store(self.work / "direct.git", self.seed)
        settings = ({"fixture_root": self.fixture, "url": self.profile["fixture_fetch_url"],
                     "ca": checked_file(self.profile["fixture_ca"], anchor=self.fixture, private=False)}
                    if self.fixture else {})
        egress = self.profile.get("anonymous_proxy")
        if (self.fixture or egress is not None) and objects.fetch(
                repo, self.expected, proxy=egress, **settings):
            method = "anonymous_https"
        else:
            direct = "anonymous_https_failed" if self.fixture or egress else "bundle_only"
            self._set(source_method=direct, fallback_started_at=ds.now_epoch(), direct_result=direct)
            repo = objects.make_store(self.work / "bundle.git", self.seed)
            objects.import_bundle(repo, self.work / "verified-input.bundle",
                                  (self.work / "incoming.bundle").read_bytes(),
                                  self.expected, bundle_digest)
            method = "incremental_bundle"
        (self.work / "incoming.bundle").unlink()
        self._set(source_method=method, source_verified_at=ds.now_epoch())
        return repo

    def run_stages(self, fd):
        self._bind_owner(fd)
        row, _ = self.owned(self.state())
        require(row["phase"] == "staging" and row["journal"] == [], "stage replay")
        self._set(stage_pid=os.getpid(), stage_started_at=ds.now_epoch())
        try:
            repo = self._select_objects(row["bundle"])
            previous = self.runtime("previous")
            rollback_route = self.runtime("standby")
            self.stage_preflight(repo)
        except (ValueError, HostingError, OSError, subprocess.SubprocessError, StageFailure):
            (self.work / "incoming.bundle").unlink(missing_ok=True)
            self.finish("rejected", "candidate", fd)
            return
        self._set(previous_runtime=previous, rollback_runtime=rollback_route)
        journal = []
        stage = "up"
        try:
            for stage in STAGES:
                journal.append({"stage": stage, "started_at": ds.now_epoch()})
                self._set(journal=journal)
                if stage == "switch" and not self.fixture:
                    self.observe_green("candidate-standby")
                self.stage(stage)
                journal[-1]["completed_at"] = ds.now_epoch()
                self._set(journal=journal)
            stage = "observe"
            started = time.monotonic()
            since = ds.now_epoch()
            journal.append({"stage": stage, "started_at": since})
            self._set(journal=journal)
            route = self.observe_green()
            samples = 1
            while True:
                remaining = self.minimum - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(30 if not self.fixture else 0.2, remaining))
                if self.observe_green() != route:
                    raise StageFailure("runtime continuity changed")
                self._set()
                samples += 1
                if time.monotonic() - started >= self.minimum:
                    break
            journal[-1]["completed_at"] = ds.now_epoch()
            self._set(phase="verified", journal=journal, observed=route,
                      observation_seconds=int(time.monotonic() - started), samples=samples,
                      fixture=self.fixture is not None)
        except StageFailure:
            self.rollback(stage, fd)

    def stage_preflight(self, repo):
        if not self.fixture:
            from scripts.prod_agent.relay_pipeline import RelayPipeline
            RelayPipeline(self).prepare(repo)

    def rollback(self, stage, fd, rejection=None):
        with ds.state_transaction(self.directory):
            row, _ = self.owned(self.state())
            require(row["phase"] in {"staging", "verified"}
                    or row["phase"] == "armed" and rejection is not None,
                    "rollback after write intent denied")
        self._set(phase="rollback_started", failed_stage=stage, rejection=rejection)
        self.stage("rollback")
        require(self.runtime("blue") == row["rollback_runtime"], "blue rollback not confirmed")
        self.finish("rolled_back", stage, fd)

    def finish(self, phase, stage, fd, remote=None):
        require(ds.process_lock_fd() == fd, "settlement fence")
        with ds.state_transaction(self.directory):
            state = self.state()
            row, lock = self.owned(state)
            result = "success" if phase == "settled" else "failed"
            outcome = ds._mint_outcome(state, lock, self.expected["head"], self.expected["pr"],
                                       ds.now_epoch(), result, 0 if result == "success" else 1,
                                       stage, "relay-fixture" if self.fixture else "relay")
            require(outcome is not None, "native outcome unavailable")
            relay_publication.freeze(outcome)
            row.update(phase=phase, finished_at=ds.now_epoch(), receipt=outcome["public_receipt"],
                       remote=remote, failed_stage=None if result == "success" else stage)
            if result == "success":
                state["deployed"] = {"sha": self.expected["head"], "pr": self.expected["pr"],
                    "at": row["finished_at"], "generation": self.expected["generation"],
                    "outcome_id": self.expected["attempt"]}
                state["last_attempt"] = None
            else:
                state["last_attempt"] = {"sha": self.expected["head"], "pr": self.expected["pr"],
                    "result": "failed", "note": phase, "at": row["finished_at"],
                    "generation": self.expected["generation"], "outcome_id": self.expected["attempt"]}
            state["lock"] = None
            ds.save_state(self.directory, state)
            if phase == "rejected":
                (self.directory / MARKER).unlink()
                sync_dir(self.directory)

    def status(self):
        with ds.state_transaction(self.directory):
            state = self.state()
            row = state.get("worker_relay")
            if not row or row["target"] != self.expected:
                return {"phase": "absent", "target": self.expected}
            if row["phase"] not in TERMINAL:
                self.owned(state)
                self.source()
            else:
                saved = state["publications"].get(self.expected["attempt"])
                marker = self.directory / MARKER
                if row["phase"] == "rejected" or saved is not None and saved["state"] == "linked":
                    require(not marker.exists(), "terminal fence cleanup unconfirmed")
                else:
                    require(read_file(marker) == self.marker, "pending report fence unconfirmed")
            public = dict(row)
            public.pop("previous_deployed")
            public.pop("previous_last_attempt")
        phase = row["phase"]
        if phase in {"settled", "rolled_back"}:
            report = relay_publication.projection(state, self.expected, row)
            public["publication"] = {k: report[k] for k in ("state", "comment_id")}
        if phase in {"verified", "armed", "settlement_pending", "settled"}:
            public["live"] = self.runtime("green") == row["observed"]
            require(public["live"], "green readback could not corroborate observation")
        elif phase == "rolled_back":
            public["live"] = self.runtime("blue") == row["rollback_runtime"]
            require(public["live"], "blue route changed")
        elif phase in {"staging", "rollback_started"}:
            try:
                with lock_file(self.directory, ds.ATTEMPT_LOCK_FILENAME) as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                public["process_fence"] = "held"
            else:
                public["process_fence"] = "unheld_reconcile"
        return public

    def publication(self, envelope):
        operation = envelope["op"]
        with lock_file(self.directory, ds.ATTEMPT_LOCK_FILENAME) as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = self.status()
            require(status["phase"] in {"settled", "rolled_back"}
                    and status.get("live") is True, "terminal route evidence required")
            with ds.state_transaction(self.directory):
                state = self.state()
                self.source()
                row = state["worker_relay"]
                report = relay_publication.projection(state, self.expected, row)
                marker = self.directory / MARKER
                if operation == "report_status":
                    return {"phase": row["phase"], "target": self.expected, "publication": report}
                require(envelope["outcome"] == report["outcome"] and state["lock"] is None,
                        "stale report outcome")
                if operation == "report_intent":
                    require(report["state"] == "ready" and read_file(marker) == self.marker,
                            "report already sent or fenced")
                    state["publications"][self.expected["attempt"]] = {
                        "generation": self.expected["generation"],
                        "payload_sha256": state["outcomes"][self.expected["attempt"]]["payload_sha256"],
                        "binding_sha256": self.expected["source"],
                        "relay_outcome": report["outcome"], "relay_intent": secrets.token_hex(16),
                        "state": "uncertain", "comment_id": 0, "intent_at": ds.now_epoch(),
                    }
                else:
                    require(report["state"] == "uncertain" and read_file(marker) == self.marker
                            and envelope["intent"] == report["intent"]
                            and positive(envelope["comment_id"])
                            and positive(envelope["author"])
                            and envelope["author"] == self.profile["report_actor_id"],
                            "report readback mismatch")
                    state["publications"][self.expected["attempt"]].update(
                        state="linked", comment_id=envelope["comment_id"], linked_at=ds.now_epoch())
                ds.save_state(self.directory, state)
                if operation == "report_link":
                    marker.unlink()
                    sync_dir(self.directory)
                report = relay_publication.projection(state, self.expected, row)
                return {"phase": row["phase"], "target": self.expected, "publication": report}

    def action(self, envelope):
        op = envelope["op"]
        with lock_file(self.directory, ds.ATTEMPT_LOCK_FILENAME) as owner:
            fd = owner.fileno()
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._bind_owner(fd)
            row, _ = self.owned(self.state())
            self.source()
            require(envelope.get("observation") == observation(row) and
                    envelope.get("observation") is not None, "stale observation receipt")
            require(self.runtime("green") == row["observed"], "green readback changed")
            if op == "arm":
                require(row["phase"] == "verified" and envelope["ci"] == row["ci"], "already armed or stale CI")
                self._set(phase="armed", intent=secrets.token_hex(16), armed_at=ds.now_epoch())
            elif op == "abort":
                require(row["phase"] == "verified" and unchanged_remote(self.expected, envelope["remote"]),
                        "rollback after write intent denied or remote changed")
                self.rollback("preflight", fd)
            elif op == "reject":
                require(row["phase"] == "armed" and envelope["intent"] == row["intent"]
                        and envelope["reason"] in {"local_no_post", "http_401"}
                        and unchanged_remote(self.expected, envelope["remote"]),
                        "definitive no-write proof missing")
                self.rollback("finalize-denied", fd, rejection={
                    "reason": envelope["reason"], "remote": envelope["remote"]})
            elif op == "settle":
                facts = envelope["remote"]
                require(row["phase"] == "armed" and envelope["intent"] == row["intent"]
                        and exact_remote(self.expected, facts), "remote settlement proof mismatch")
                self._set(phase="settlement_pending", remote=facts, settlement_read_at=ds.now_epoch())
            else:
                facts = envelope["remote"]
                require(row["phase"] == "settlement_pending" and envelope["intent"] == row["intent"]
                        and row["remote"] == facts and exact_remote(self.expected, facts),
                        "settlement readback mismatch")
                self.finish("settled", "complete", fd, remote=facts)
        return self.status()

    def dispatch(self, envelope, raw):
        require(target(envelope["target"]) == self.expected, "unapproved target")
        op = envelope["op"]
        if op not in {"status", "report_status"}:
            signers = checked_file(self.profile["worker_signers"], anchor=self.fixture)
            verify(envelope, signers, self.directory)
        if op == "start":
            return self.admit(envelope, raw)
        fields = {"status": set(), "arm": {"observation", "ci"},
                  "abort": {"observation", "remote"},
                  "reject": {"observation", "intent", "reason", "remote"},
                  "settle": {"observation", "intent", "remote"},
                  "confirm": {"observation", "intent", "remote"}, "report_status": set(),
                  "report_intent": {"outcome"}, "report_link": {"outcome", "intent", "comment_id", "author"}}
        require(op in fields and not raw and set(envelope) == {"op", "target"} | fields[op],
                "unsupported relay operation")
        if op == "status":
            return self.status()
        if op.startswith("report_"):
            return self.publication(envelope)
        return self.action(envelope)


def unchanged_remote(expected, row):
    return (isinstance(row, dict) and set(row) == {"pr_id", "pr_node", "head", "main", "merged"}
            and row["pr_id"] == expected["pr_id"] and row["pr_node"] == expected["pr_node"]
            and row["head"] == expected["head"] and row["main"] == expected["base"]
            and row["merged"] is False)


def observation(row):
    if row["phase"] not in {"verified", "armed", "settlement_pending", "settled"}:
        return None
    return digest({k: row[k] for k in ("target", "ci", "journal", "observation_seconds",
                                       "samples", "observed", "source_method", "fixture")})


def exact_remote(expected, row):
    return (isinstance(row, dict) and set(row) == {"pr_id", "pr_node", "head", "main", "merged_at"}
            and row["pr_id"] == expected["pr_id"] and row["pr_node"] == expected["pr_node"]
            and row["head"] == row["main"] == expected["head"] and isinstance(row["merged_at"], str)
            and len(row["merged_at"]) == 20 and row["merged_at"].endswith("Z"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("ssh", "stage"))
    parser.add_argument("--fd", type=int)
    parser.add_argument("--fixture-root")
    parser.add_argument("--fixture-profile", type=Path)
    args = parser.parse_args()
    require(bool(args.fixture_root) == bool(args.fixture_profile), "fixture profile boundary")
    if args.operation == "ssh":
        require(os.environ.get("SSH_ORIGINAL_COMMAND") == COMMAND and args.fd is None,
                "fixed SSH command required")
        signal.alarm(65)
        line = sys.stdin.buffer.readline(MAX_CONTROL + 1)
        require(0 < len(line) <= MAX_CONTROL and line.endswith(b"\n"), "control framing")
        envelope = strict_json_loads(line, max_bytes=MAX_CONTROL)
        size = envelope.get("bundle_size", 0)
        require(type(size) is int and 0 <= size <= MAX_BUNDLE, "content size")
        raw = sys.stdin.buffer.read(size + 1)
        require(len(raw) == size, "content framing")
        signal.alarm(0)
    server = Server(args.fixture_profile or STATE_PATH, args.fixture_root)
    if args.operation == "stage":
        require(args.fd is not None and args.fd >= 3, "private staging process required")
        with os.fdopen(args.fd, "rb") as lock:
            # Never acquire an unrelated FD or run from a public SSH argument.
            expected = (server.directory / ds.ATTEMPT_LOCK_FILENAME).stat()
            held = os.fstat(lock.fileno())
            require((held.st_dev, held.st_ino) == (expected.st_dev, expected.st_ino), "attempt inode")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            server.run_stages(lock.fileno())
    else:
        sys.stdout.buffer.write(canonical(server.dispatch(envelope, raw)) + b"\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"error":"relay_denied_or_unconfirmed"}')
        raise SystemExit(1) from None
