"""Offline admission and finite exec credential lifecycle contracts."""
import importlib

import pytest


def test_candidate_cannot_render_empty_admission(tmp_path):
    release = importlib.import_module('scripts.blue_green.release_credentials')
    with pytest.raises(ValueError):
        release.render({}, tmp_path / 'unadmitted')
    assert not (tmp_path / 'unadmitted').exists()
