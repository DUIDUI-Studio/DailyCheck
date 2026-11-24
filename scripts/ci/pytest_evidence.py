"""Opt-in pytest completion evidence for the zero-new-failure gate.

Enable with -p scripts.ci.pytest_evidence --dc-evidence=<fresh session.json>.
This plugin observes reports only; it does not change collection or outcomes.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from os.path import relpath
from pathlib import Path

import pytest

OUTCOMES = ("passed", "failed", "skipped", "xfailed", "xpassed", "error")


def pytest_addoption(parser):
    parser.addoption("--dc-evidence", metavar="PATH", help="write DailyCheck session evidence")
    parser.addoption("--dc-progress", metavar="PATH", help="write advisory DailyCheck progress")


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    destination = getattr(early_config.known_args_namespace, "dc_progress", None)
    if destination:
        progress = _Progress(Path(destination))
        progress._save()
        early_config.pluginmanager.register(progress)


class _Progress:
    """Observe serial pytest transitions without affecting its result."""

    def __init__(self, path):
        self.path = path
        self.origin = time.monotonic_ns()
        self.current = None
        self.snapshot = {
            "version": 1, "capture": path.name, "selected": None,
            "started": 0, "completed": 0, "collection_ms": None,
            "last_started": None, "last_completed": None,
        }

    def _elapsed(self):
        return (time.monotonic_ns() - self.origin) // 1_000_000

    def _node(self, nodeid):
        name = nodeid.split("[", 1)[0]
        return {"node": name if len(name) <= 120 else None, "ms": self._elapsed()}

    def _save(self):
        # Publish whole snapshots even if the container is killed mid-write.
        # The reader accepts only the final path, never an unfinished temp file.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=self.path.parent) as handle:
                temporary = Path(handle.name)
                json.dump(self.snapshot, handle, separators=(",", ":"))
            os.replace(temporary, self.path)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session):
        self.snapshot.update(selected=len(session.items), collection_ms=self._elapsed())
        self._save()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_logstart(self, nodeid):
        self.current = nodeid
        self.snapshot.update(started=self.snapshot["started"] + 1,
                             last_started=self._node(nodeid))
        self._save()

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report):
        if report.when == "teardown" and self.current == report.nodeid:
            self.current = None
            self.snapshot.update(completed=self.snapshot["completed"] + 1,
                                 last_completed=self._node(report.nodeid))
            self._save()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    destination = session.config.getoption("--dc-evidence")
    if not destination:
        return
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if reporter is None:
        raise RuntimeError("DailyCheck test evidence requires the terminal reporter")
    reports = {name: reporter.stats.get(name, []) for name in OUTCOMES}
    completed = {report.nodeid for items in reports.values() for report in items}
    selected = {item.nodeid for item in session.items}
    def display_nodeid(report):
        path, separator, suffix = report.nodeid.partition("::")
        relative = Path(relpath(session.config.rootpath / path, session.config.invocation_params.dir)).as_posix()
        return relative + separator + suffix

    evidence = {
        "schema_version": 1,
        "pytest_exit": int(exitstatus),
        "selected": session.testscollected,
        "selected_unique": len(selected),
        "completed": len(completed),
        "uncompleted": sorted(selected - completed),
        "unexpected": sorted(completed - selected),
        "counts": {name: len(items) for name, items in reports.items()},
        "failed_nodeids": [display_nodeid(report) for report in reports["failed"]],
        "error_nodeids": [display_nodeid(report) for report in reports["error"]],
    }
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
