"""Offline admission and finite exec credential lifecycle contracts."""

import importlib

import pytest


def test_candidate_cannot_render_empty_admission(tmp_path):
    release = importlib.import_module("scripts.blue_green.release_credentials")
    with pytest.raises(ValueError):
        release.render({}, tmp_path / "unadmitted")
    assert not (tmp_path / "unadmitted").exists()


def admission(tmp_path):
    import os
    import sys

    from scripts.blue_green.release_credentials import PINS

    root = tmp_path / "bundle"
    return dict(
        uid=os.getuid(),
        python=sys.executable,
        root=str(root),
        repo=str(tmp_path / "repo"),
        state=str(tmp_path / "state"),
        tmp=str(root / "scratch/tmp"),
        log=str(tmp_path / "log"),
        shim=str(tmp_path / "shim"),
        shim_sha="a" * 64,
        pr=160,
        head="b" * 40,
        base="c" * 40,
        generation=10,
        deployed_generation=10,
        session="session",
        task="task",
        dispatch="dispatch",
        pins={p: "d" * 64 for p in PINS},
        binding_sha256="e" * 64,
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("pr", True),
        ("pr", 160.0),
        ("pr", "160"),
        ("pr", 0),
        ("uid", False),
        ("generation", 10.0),
        ("deployed_generation", 10.0),
        ("deployed_generation", 11),
        ("head", "a" * 7),
        ("base", "z" * 40),
        ("shim_sha", "a" * 40),
        ("binding_sha256", None),
        ("session", "--force"),
        ("task", ""),
        ("dispatch", "../bad"),
        ("pins", {}),
        ("repo", "/tmp/../repo"),
        ("tmp", "/tmp/shared"),
    ],
)
def test_malformed_operator_admission_has_no_output(tmp_path, key, value):
    from scripts.blue_green.release_credentials import render

    profile = admission(tmp_path)
    profile[key] = value
    with pytest.raises(ValueError):
        render(profile, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_bundle_cannot_be_overwritten_or_placed_in_candidate(tmp_path):
    from scripts.blue_green.release_credentials import render

    profile = admission(tmp_path)
    root = tmp_path / "bundle"
    render(profile, root)
    original = (root / "selector.py").read_bytes()
    with pytest.raises(FileExistsError):
        render(profile, root)
    assert (root / "selector.py").read_bytes() == original
    profile["repo"] = str(tmp_path)
    with pytest.raises(ValueError, match="candidate-cannot-own-admission"):
        render(profile, root)
