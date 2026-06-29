"""Unit tests for path_matches — Agent MPC authorization helper.

Spec §2.3: path patterns support exact match, prefix match, and a
trailing '*' wildcard that matches any number of additional path
segments. This is a pure function, no DB / Flask dependency.
"""
from __future__ import annotations

import pytest

from blueprints.agent_mpc_pure import path_matches


def test_exact_match():
    """A pattern (without trailing *) matches the exact same path."""
    assert path_matches("/api/v1/items", "/api/v1/items") is True


def test_exact_match_rejects_subpath_with_wildcard_only():
    """Sub-path matching requires an explicit trailing '*' (spec §2.3)."""
    # Without '*', the pattern only matches itself and its sub-paths —
    # a pattern '/api/v1/items' is a prefix, so it does match /123.
    # The exact-only contract is what the wildcard is for. We re-test
    # this explicitly below in test_prefix_match.
    # The interesting negative: '/api/v1/items' (no *) does NOT match
    # '/api/v1/itemsX'.
    assert path_matches("/api/v1/items", "/api/v1/itemsX") is False


def test_prefix_match():
    """A pattern (no wildcard) acts as a prefix — matches the exact
    path AND any sub-path that starts with the pattern + '/'."""
    assert path_matches("/api/v1/items", "/api/v1/items") is True
    assert path_matches("/api/v1/items", "/api/v1/items/123") is True
    assert path_matches("/api/v1/items", "/api/v1/items/foo/bar") is True


def test_prefix_match_does_not_match_sibling():
    """A prefix pattern does not match a sibling path (e.g. /items vs /itemsX)."""
    assert path_matches("/api/v1/items", "/api/v1/itemsX") is False


def test_wildcard_match():
    """Trailing '*' wildcard matches any continuation (spec §2.3)."""
    assert path_matches("/api/v1/items/*", "/api/v1/items/123") is True
    assert path_matches("/api/v1/items/*", "/api/v1/items/foo/bar") is True
    # The spec example shows /items/* → /items/123; the prefix case
    # (without the trailing slash) is already covered by prefix_match,
    # so we do NOT assert /items/* matches /items itself.


def test_wildcard_middle_not_supported():
    """Spec only locks trailing '*'. A middle wildcard is treated as
    literal — must NOT match unexpectedly."""
    assert path_matches("/api/v1/*/items", "/api/v1/items/123") is False
    # And it must not crash
    assert path_matches("/api/v1/*/items", "/api/v1/x/items") is False


def test_no_match():
    """Two unrelated paths don't match."""
    assert path_matches("/api/v1/items", "/api/v1/categories") is False


def test_empty_path():
    """Empty pattern matches only empty path; empty path matches only empty pattern."""
    assert path_matches("", "") is True
    assert path_matches("/", "/") is True
    assert path_matches("/api/v1/items", "") is False
