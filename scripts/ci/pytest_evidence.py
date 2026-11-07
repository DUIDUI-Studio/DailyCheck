"""Opt-in pytest completion evidence for the zero-new-failure gate.

Enable with -p scripts.ci.pytest_evidence --dc-evidence=<fresh session.json>.
This plugin observes reports only; it does not change collection or outcomes.
"""
from __future__ import annotations

import json
from os.path import relpath
from pathlib import Path

import pytest

OUTCOMES = ("passed", "failed", "skipped", "xfailed", "xpassed", "error")


def pytest_addoption(parser):
    parser.addoption("--dc-evidence", metavar="PATH", help="write DailyCheck session evidence")


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
