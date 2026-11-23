"""Anonymous Git transport and a bounded, single-ref incremental bundle."""
from __future__ import annotations

import hashlib
import os
import re
import resource
import signal
import subprocess
from urllib.parse import urlsplit

from scripts.prod_agent.relay_protocol import MAX_BUNDLE, REMOTE, SYSTEM_ENV, require

MAX_EXPANDED = 256 * 1024 * 1024
MAX_OBJECTS = 20_000
GIT = "/usr/bin/git"
GIT_ENV = {**SYSTEM_ENV, "HOME": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_PROTOCOL_FROM_USER": "0"}


def _limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_EXPANDED, MAX_EXPANDED))


def git(repo, *args, network=False, ca=None, proxy=None, input=None):
    env = {**GIT_ENV, "GIT_ALLOW_PROTOCOL": "https" if network else ""}
    command = [GIT, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "credential.helper=", "-c", "http.followRedirects=false",
               "-c", "fetch.fsckObjects=true", "-c", "transfer.unpackLimit=1"]
    if ca:
        command += ["-c", "http.sslCAInfo=" + str(ca)]
    if proxy:
        command += ["-c", "http.proxy=" + proxy]
    command += ["-C", str(repo), *args]
    stdin = subprocess.DEVNULL if input is None else subprocess.PIPE
    with subprocess.Popen(command, env=env, stdin=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True, preexec_fn=_limits) as child:
        try:
            output, error = child.communicate(input, timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
            raise
        if child.returncode:
            raise subprocess.CalledProcessError(child.returncode, args, stderr=error)
    return output


def make_store(path, seed=None):
    path.mkdir(mode=0o700)
    git(path, "init", "--bare", "--template=")
    if seed is not None:
        alternates = path / "objects/info/alternates"
        alternates.write_text(str(seed / "objects") + "\n")
    return path


def check_objects(repo, expected):
    base, head = expected["base"], expected["head"]
    require(git(repo, "cat-file", "-t", base).strip() == b"commit"
            and git(repo, "cat-file", "-t", head).strip() == b"commit"
            and git(repo, "rev-parse", head + "^{tree}").strip().decode() == expected["tree"],
            "candidate tree identity")
    git(repo, "merge-base", "--is-ancestor", base, head)


def fetch(repo, expected, *, url=REMOTE, fixture_root=None, ca=None, proxy=None):
    if fixture_root is not None:
        u = urlsplit(url)
        require(u.scheme == "https" and u.hostname == "127.0.0.1" and not u.username
                and not u.password and not u.query and not u.fragment and ca,
                "fixture Git must be loopback HTTPS")
    else:
        require(url == REMOTE and ca is None, "fixed anonymous repository")
    if proxy is not None:
        p = urlsplit(proxy)
        require(p.scheme in {"http", "https"} and p.hostname and p.port
                and not p.username and not p.password and not p.path
                and not p.query and not p.fragment, "invalid pinned anonymous proxy")
    try:
        git(repo, "fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
            "--", url, "refs/heads/main:refs/relay/base", expected["ref"] + ":refs/relay/head",
            network=True, ca=ca, proxy=proxy)
    except subprocess.TimeoutExpired:
        return False
    except subprocess.CalledProcessError as exc:
        # Ref/protocol/fsck failures do not justify substituting older objects.
        unavailable = rb"(?i)gnutls_handshake|SSL connect error|failed to connect|timed out"
        require(re.search(unavailable, exc.stderr or b""), "public Git rejected the exact request")
        return False
    for name in ("base", "head"):
        require(git(repo, "rev-parse", "refs/relay/" + name).strip().decode() == expected[name],
                "public fetch refs drifted")
    check_objects(repo, expected)
    return True


def make_bundle(repo, path, expected):
    git(repo, "update-ref", expected["ref"], expected["head"])
    git(repo, "bundle", "create", "--version=2", str(path),
        expected["ref"], "^" + expected["base"])
    raw = path.read_bytes()
    bundle_header(raw, expected)
    return raw


def bundle_header(raw, expected):
    require(0 < len(raw) <= MAX_BUNDLE, "bundle size")
    header, delimiter, pack = raw[:8192].partition(b"\n\n")
    lines = header.splitlines()
    prereq = b"-" + expected["base"].encode() + b" "
    ref = (expected["head"] + " " + expected["ref"]).encode()
    require(delimiter and pack.startswith(b"PACK") and len(lines) == 3
            and lines[0] == b"# v2 git bundle" and lines[1].startswith(prereq)
            and lines[2] == ref, "bundle must declare only exact H/ref and prerequisite B")


def import_bundle(repo, path, raw, expected, expected_digest):
    require(hashlib.sha256(raw).hexdigest() == expected_digest, "bundle digest")
    bundle_header(raw, expected)
    path.write_bytes(raw)
    try:
        git(repo, "bundle", "verify", str(path))
        require(git(repo, "bundle", "unbundle", str(path)).split() ==
                [expected["head"].encode(), expected["ref"].encode()], "bundle ref")
        check_objects(repo, expected)
        packs = list((repo / "objects/pack").glob("*.idx"))
        require(len(packs) == 1, "single bundle pack required")
        rows = git(repo, "verify-pack", "-v", str(packs[0])).splitlines()
        objects = {}
        for row in rows:
            fields = row.split()
            if len(fields) in {5, 7} and len(fields[0]) == 40:
                objects[fields[0]] = int(fields[2])
        graph = {line.split(b" ", 1)[0] for line in git(
            repo, "rev-list", "--objects", expected["head"], "^" + expected["base"]
        ).splitlines()}
        require(graph == objects.keys() and 0 < len(objects) <= MAX_OBJECTS
                and sum(objects.values()) <= MAX_EXPANDED, "bundle object graph or budget")
        git(repo, "fsck", "--strict", "--no-reflogs", expected["head"])
    finally:
        path.unlink(missing_ok=True)
