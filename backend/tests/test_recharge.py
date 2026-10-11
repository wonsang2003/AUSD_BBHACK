"""Voucher split and mode helpers of the recharge executor (app/keeper/recharge.py)."""

import pytest

from app.chain.controller import BudgetMode
from app.keeper.allocator import Allocation
from app.keeper.recharge import is_voucher_mode, split_voucher

ALLOCS = [Allocation("A", 600, 0.6, ()), Allocation("B", 400, 0.4, ())]


def test_split_voucher_proportional_and_exact():
    assert split_voucher(1_000, ALLOCS) == {"A": 600, "B": 400}
    assert sum(split_voucher(999, ALLOCS).values()) == 999


def test_split_voucher_respects_headroom():
    assert split_voucher(1_000, ALLOCS, {"A": 500, "B": 600}) == {"A": 500, "B": 500}


def test_split_voucher_raises_when_headroom_too_small():
    with pytest.raises(ValueError):
        split_voucher(1_000, ALLOCS, {"A": 100, "B": 100})


def test_is_voucher_mode():
    assert is_voucher_mode(BudgetMode.VOUCHER) and not is_voucher_mode(BudgetMode.PERMIT)
