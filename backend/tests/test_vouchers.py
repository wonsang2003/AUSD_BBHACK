"""
Tests for app/chain/vouchers.py — daily-voucher day arithmetic, nonces and typed-data builders,
plus the full "Maximum safety" flow on a local anvil chain: the owner signs 7 vouchers, the keeper
redeems today's one split across agents, a wrong-day voucher and a cancelled voucher are refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from eth_account import Account

from app.chain import eip712
from app.chain.controller import controller_error_name
from app.chain.vouchers import (
    DAYS_PER_WEEK,
    SECONDS_PER_DAY,
    VoucherStatus,
    build_cancel,
    build_voucher,
    build_week,
    compute_voucher_nonce,
    day_bounds,
    day_index,
)
from app.chain.web3client import TxRevertedError
from app.constants import BASE_SEPOLIA_CHAIN_ID, ONE_AUSD

OWNER = "0xe05fcC23807536bEe418f142D19fa0d21BB0cfF7"
CONTROLLER = "0x000000000000000000000000000000000000c0DE"  # checksummed
AUSD = "0xa9012a055bd4e0eDfF8Ce09f960291C09D5322dC"
SALT = "0x" + "5a" * 32
NOON_OFFSET_SECONDS = 12 * 3_600

DAILY_VALUE = 8 * ONE_AUSD
DAILY_CAP = 10 * ONE_AUSD
AGENT_CAPS = [6 * ONE_AUSD, 4 * ONE_AUSD]
TODAYS_SPLIT = [5 * ONE_AUSD, 3 * ONE_AUSD]


# ─── day arithmetic (no chain) ───────────────────────────────────────────────

def test_day_index_is_floor_of_timestamp_over_one_day():
    # Act + Assert
    assert day_index(0) == 0
    assert day_index(SECONDS_PER_DAY - 1) == 0
    assert day_index(SECONDS_PER_DAY) == 1
    assert day_index(1_790_000_000) == 1_790_000_000 // 86_400


def test_day_bounds_make_the_voucher_valid_exactly_during_its_utc_day():
    # Arrange
    day = 20_372
    day_start = day * SECONDS_PER_DAY

    # Act
    valid_after, valid_before = day_bounds(day)

    # Assert: ERC-3009 is strict (validAfter < now < validBefore), so dayStart .. dayEnd − 1 qualify.
    assert valid_after == day_start - 1
    assert valid_before == day_start + SECONDS_PER_DAY
    assert valid_after < day_start and day_start + SECONDS_PER_DAY - 1 < valid_before


def test_compute_voucher_nonce_depends_on_owner_day_and_salt():
    # Act
    base = compute_voucher_nonce(OWNER, 20_372, SALT)

    # Assert
    assert base == compute_voucher_nonce(OWNER.lower(), 20_372, bytes.fromhex(SALT[2:]))
    assert base != compute_voucher_nonce(OWNER, 20_373, SALT)
    assert base != compute_voucher_nonce(OWNER, 20_372, "0x" + "00" * 32)
    assert len(bytes.fromhex(base[2:])) == 32


def test_build_voucher_is_a_receive_with_authorization_to_the_controller():
    # Arrange
    nonce = compute_voucher_nonce(OWNER, 20_372, SALT)

    # Act
    typed = build_voucher(
        chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=AUSD, controller_address=CONTROLLER,
        owner=OWNER, day_index=20_372, value=DAILY_VALUE, nonce=nonce,
    )

    # Assert
    assert typed["primaryType"] == "ReceiveWithAuthorization"
    assert typed["domain"]["name"] == "Agora Dollar"
    assert typed["message"]["to"] == CONTROLLER
    assert typed["message"]["value"] == DAILY_VALUE
    assert (typed["message"]["validAfter"], typed["message"]["validBefore"]) == day_bounds(20_372)
    assert typed["message"]["nonce"] == nonce


def test_build_cancel_targets_the_voucher_nonce():
    # Act
    typed = build_cancel(chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=AUSD, owner=OWNER, nonce=SALT)

    # Assert
    assert typed["primaryType"] == "CancelAuthorization"
    assert typed["message"] == {"authorizer": OWNER, "nonce": SALT}


def test_voucher_status_values():
    # Assert
    assert VoucherStatus.ALL == ("pending", "redeemed", "cancelled", "failed")


# ─── on-chain flow ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VoucherBudget:
    owner: str
    owner_key: str = field(repr=False)
    agents: list[str] = field(default_factory=list)
    today: int = 0


@pytest.fixture
def voucher_budget(controller, budget_signer, fund_ausd, wallets, chain_clock) -> VoucherBudget:
    """Chain moved to 12:00 UTC of the next day; the user opens a 10 AUSD/day budget in voucher mode."""
    today = day_index(chain_clock.now()) + 1
    chain_clock.warp_to(today * SECONDS_PER_DAY + NOON_OFFSET_SECONDS)
    owner = Account.from_key(wallets.user).address
    agents = [wallets.address("agent"), wallets.address("agent2")]
    fund_ausd(owner, 100 * ONE_AUSD)
    config = budget_signer.config(
        owner, daily_cap=DAILY_CAP, expiry=chain_clock.now() + 30 * SECONDS_PER_DAY,
        agents=agents, agent_caps=AGENT_CAPS, sectors=["OptionsEvent", "OnchainPerps"],
    )
    controller.open_voucher_budget(config, budget_signer.sign_config(wallets.user, config))
    return VoucherBudget(owner=owner, owner_key=wallets.user, agents=agents, today=today)


def _sign_week(controller, mock_ausd, budget: VoucherBudget) -> dict[int, tuple[eip712.Authorization, str]]:
    """The owner signs 7 vouchers from today; returns day → (authorization, signature)."""
    week = build_week(
        controller=controller, chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=mock_ausd.address,
        controller_address=controller.address, owner=budget.owner, first_day_index=budget.today,
        value=DAILY_VALUE, salt=SALT,
    )
    return {v.day_index: (v.authorization, eip712.sign_typed_data(budget.owner_key, v.typed_data).hex) for v in week}


def _redeem(controller, budget: VoucherBudget, auth: eip712.Authorization, signature: str, amounts: list[int]):
    return controller.redeem_voucher(
        budget.owner, auth.value, auth.valid_after, auth.valid_before, auth.nonce, signature, budget.agents, amounts
    )


@pytest.mark.chain
def test_voucher_nonce_matches_the_contract(controller, wallets):
    # Arrange
    owner = wallets.address("user")

    # Act
    on_chain = controller.voucher_nonce(owner, 20_372, SALT)

    # Assert
    assert on_chain == compute_voucher_nonce(owner, 20_372, SALT)


@pytest.mark.chain
def test_build_week_signs_seven_consecutive_days_with_contract_nonces(controller, mock_ausd, voucher_budget):
    # Act
    vouchers = _sign_week(controller, mock_ausd, voucher_budget)

    # Assert
    assert sorted(vouchers) == list(range(voucher_budget.today, voucher_budget.today + DAYS_PER_WEEK))
    for day, (auth, _) in vouchers.items():
        assert auth.nonce == controller.voucher_nonce(voucher_budget.owner, day, SALT)
        assert (auth.valid_after, auth.valid_before) == day_bounds(day)
    assert len({auth.nonce for auth, _ in vouchers.values()}) == DAYS_PER_WEEK


@pytest.mark.chain
def test_redeem_todays_voucher_splits_value_by_amounts(controller, mock_ausd, voucher_budget):
    # Arrange
    vouchers = _sign_week(controller, mock_ausd, voucher_budget)
    auth, signature = vouchers[voucher_budget.today]
    owner_before = mock_ausd.balance_of(voucher_budget.owner)

    # Act
    result = _redeem(controller, voucher_budget, auth, signature, TODAYS_SPLIT)

    # Assert
    assert result.ok
    assert [mock_ausd.balance_of(agent) for agent in voucher_budget.agents] == TODAYS_SPLIT
    assert mock_ausd.balance_of(voucher_budget.owner) == owner_before - DAILY_VALUE
    assert mock_ausd.balance_of(controller.address) == 0
    assert mock_ausd.authorization_state(voucher_budget.owner, auth.nonce) is True
    assert controller.remaining_today(voucher_budget.owner) == DAILY_CAP - DAILY_VALUE


@pytest.mark.chain
def test_redeem_reverts_for_a_voucher_of_another_day(controller, mock_ausd, voucher_budget):
    # Arrange: tomorrow's voucher, presented today.
    vouchers = _sign_week(controller, mock_ausd, voucher_budget)
    auth, signature = vouchers[voucher_budget.today + 1]

    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        _redeem(controller, voucher_budget, auth, signature, TODAYS_SPLIT)
    assert controller_error_name(exc_info.value.reason) == "VoucherNotForToday"


@pytest.mark.chain
def test_redeem_reverts_when_split_does_not_sum_to_value(controller, mock_ausd, voucher_budget):
    # Arrange
    vouchers = _sign_week(controller, mock_ausd, voucher_budget)
    auth, signature = vouchers[voucher_budget.today]

    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        _redeem(controller, voucher_budget, auth, signature, [5 * ONE_AUSD, 2 * ONE_AUSD])
    assert controller_error_name(exc_info.value.reason) == "VoucherSplitMismatch"


@pytest.mark.chain
def test_redeem_reverts_when_the_same_voucher_is_redeemed_twice(controller, mock_ausd, voucher_budget):
    # Arrange: a small 2 AUSD voucher, so a second redemption would still fit every cap.
    auth = eip712.Authorization(
        from_address=voucher_budget.owner, to=controller.address, value=2 * ONE_AUSD,
        valid_after=day_bounds(voucher_budget.today)[0], valid_before=day_bounds(voucher_budget.today)[1],
        nonce=controller.voucher_nonce(voucher_budget.owner, voucher_budget.today, SALT),
    )
    typed = eip712.build_receive_with_authorization(chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=mock_ausd.address, auth=auth)
    signature = eip712.sign_typed_data(voucher_budget.owner_key, typed).hex
    _redeem(controller, voucher_budget, auth, signature, [ONE_AUSD, ONE_AUSD])

    # Act + Assert: the token refuses the used nonce.
    with pytest.raises(TxRevertedError):
        _redeem(controller, voucher_budget, auth, signature, [ONE_AUSD, ONE_AUSD])
    assert mock_ausd.balance_of(controller.address) == 0


@pytest.mark.chain
def test_cancelled_future_voucher_cannot_be_redeemed_on_its_day(
    controller, mock_ausd, voucher_budget, relayer, chain_clock
):
    # Arrange: the owner cancels tomorrow's voucher today; the relayer submits the cancellation.
    vouchers = _sign_week(controller, mock_ausd, voucher_budget)
    tomorrow = voucher_budget.today + 1
    auth, signature = vouchers[tomorrow]
    cancel = build_cancel(
        chain_id=BASE_SEPOLIA_CHAIN_ID, ausd_address=mock_ausd.address, owner=voucher_budget.owner, nonce=auth.nonce
    )
    cancel_sig = eip712.sign_typed_data(voucher_budget.owner_key, cancel).hex
    relayer.send(mock_ausd.cancel_authorization_fn(voucher_budget.owner, auth.nonce, cancel_sig))
    chain_clock.warp_to(tomorrow * SECONDS_PER_DAY + NOON_OFFSET_SECONDS)

    # Act + Assert
    with pytest.raises(TxRevertedError):
        _redeem(controller, voucher_budget, auth, signature, TODAYS_SPLIT)
    assert [mock_ausd.balance_of(agent) for agent in voucher_budget.agents] == [0, 0]

    # The day after still works: only the cancelled day was killed.
    day_after_auth, day_after_sig = vouchers[tomorrow + 1]
    chain_clock.warp_to((tomorrow + 1) * SECONDS_PER_DAY + NOON_OFFSET_SECONDS)
    assert _redeem(controller, voucher_budget, day_after_auth, day_after_sig, TODAYS_SPLIT).ok


@pytest.mark.chain
def test_recharge_is_refused_for_a_voucher_mode_budget(controller, voucher_budget):
    # Act + Assert
    with pytest.raises(TxRevertedError) as exc_info:
        controller.recharge(voucher_budget.owner, voucher_budget.agents[0], ONE_AUSD)
    assert controller_error_name(exc_info.value.reason) == "WrongBudgetMode"
