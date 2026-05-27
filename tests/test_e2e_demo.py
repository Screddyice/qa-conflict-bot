from __future__ import annotations

from pr_conflict_bot._e2e_demo import add


def test_add_returns_sum() -> None:
    # add() should return the sum; the fixture ships it as `a - b` so this test
    # fails until the bot fixes the source in place.
    assert add(2, 3) == 5
