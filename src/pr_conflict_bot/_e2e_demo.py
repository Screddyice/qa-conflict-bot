"""Temporary end-to-end fixture for the in-place QA fix test.

This module (and tests/test_e2e_demo.py) exist only on the
test/qa-inplace-fix-e2e branch to give the bot a clear, verify-breaking bug to
fix. Delete with the PR — do not merge to main.
"""

from __future__ import annotations


def add(a: int, b: int) -> int:
    """Return the sum of a and b."""
    return a - b
