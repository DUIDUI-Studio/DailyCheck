"""Two synthetic native work items through the same real automatic CLI/engine.

The external traffic stages, Git provider and fixture observation clock remain
the existing full-chain owned seams. This never represents production delivery.
"""

import os
import sys

import pytest

from tests.private_audit_testenv import private_audit_fixture  # noqa: F401
from tests.test_release_bundle import exercise_fullchain
from tests.test_release_bundle import lane as ordinary_lane

lane = ordinary_lane

A = (160, "a" * 40, 71, 71001, 7, 77001)
B = (161, "c" * 40, 72, 72001, 8, 88001)


@pytest.mark.skipif(sys.platform != "linux" or os.getuid() != 0, reason="owned root/Linux protocol")
@pytest.mark.parametrize("work,case", [
    (A, "happy"), (B, "happy"),
    (A, "up-failure"), (B, "report-failure"),
    (B, "duplicate-review"), (A, "cross-session"),
    (A, "missing-binding"), (B, "wrong-issue"),
    (A, "wrong-ops"), (A, "unknown-delivery"), (B, "forbidden-scope"),
    (A, "inspect"), (B, "inspect"), (B, "blob-drift"),
    (A, "unsafe-parent"), (B, "partial-prepare"), (A, "binding-swap"),
    (B, "native-busy"),
    (A, "unregistered"), (B, "stale-pid"),
])
def test_automatic_work_from_native_authorities(lane, tmp_path, work, case):
    exercise_fullchain(lane, tmp_path, case, work=work)
