"""Чистые тесты агрегата Account — без БД.

DoD фазы 5: двойная запись (направление) соблюдается; two-phase — pending
и posted не задваиваются в балансе; PostCorrection — новая проводка, не
правка старой.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.account import (
    Account,
    CorrectionPosted,
    EntryDirection,
    EntryPosted,
    EntryType,
    InvalidLedgerEntryStateError,
    Money,
    PendingReserved,
)
from billing.domain.shared import BillingPeriod


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def test_reserve_pending_creates_a_debit_pending_entry() -> None:
    account = Account(account_id="acc-4471")

    entry, event = account.reserve_pending(Money(Decimal("576.00")), BillingPeriod(2026, 6), now=_dt(2026, 6, 15))

    assert entry.direction is EntryDirection.DEBIT
    assert entry.entry_type is EntryType.PENDING
    assert isinstance(event, PendingReserved)


def test_pending_does_not_count_towards_confirmed_balance() -> None:
    account = Account(account_id="acc-4471")
    pending, _ = account.reserve_pending(Money(Decimal("576.00")), BillingPeriod(2026, 6), now=_dt(2026, 6, 15))

    assert Account.balance([pending]) == Money(Decimal("0"))
    assert Account.projected_balance([pending]) == Money(Decimal("576.00"))


def test_post_charge_and_posted_pending_do_not_double_count_the_same_period() -> None:
    """DoD: two-phase — pending и posted не задваиваются."""
    account = Account(account_id="acc-4471")
    period = BillingPeriod(2026, 6)
    pending, _ = account.reserve_pending(Money(Decimal("576.00")), period, now=_dt(2026, 6, 15))
    posted, _ = account.post_charge(uuid.uuid4(), Money(Decimal("1107.20")), period, now=_dt(2026, 7, 1))

    # И "official" баланс, и прогноз — только posted, потому что за этот
    # период уже есть posted-проводка: старая pending-прикидка исключается.
    assert Account.balance([pending, posted]) == Money(Decimal("1107.20"))
    assert Account.projected_balance([pending, posted]) == Money(Decimal("1107.20"))


def test_pending_for_a_different_period_is_not_excluded() -> None:
    account = Account(account_id="acc-4471")
    pending_july, _ = account.reserve_pending(Money(Decimal("100.00")), BillingPeriod(2026, 7), now=_dt(2026, 7, 15))
    posted_june, _ = account.post_charge(uuid.uuid4(), Money(Decimal("1107.20")), BillingPeriod(2026, 6), now=_dt(2026, 7, 1))

    assert Account.projected_balance([pending_july, posted_june]) == Money(Decimal("1207.20"))


def test_confirm_pending_appends_a_posted_entry_without_mutating_the_pending_one() -> None:
    account = Account(account_id="acc-4471")
    pending, _ = account.reserve_pending(Money(Decimal("576.00")), BillingPeriod(2026, 6), now=_dt(2026, 6, 15))

    confirmed, event = account.confirm_pending(pending, now=_dt(2026, 7, 1))

    assert confirmed.entry_type is EntryType.POSTED
    assert confirmed.confirms_pending_entry_id == pending.entry_id
    assert confirmed.amount == pending.amount
    assert isinstance(event, EntryPosted)
    # pending остаётся pending — объект не мутировал (иммутабельность).
    assert pending.entry_type is EntryType.PENDING


def test_confirm_pending_rejects_an_already_posted_entry() -> None:
    account = Account(account_id="acc-4471")
    pending, _ = account.reserve_pending(Money(Decimal("576.00")), BillingPeriod(2026, 6), now=_dt(2026, 6, 15))
    confirmed, _ = account.confirm_pending(pending, now=_dt(2026, 7, 1))

    with pytest.raises(InvalidLedgerEntryStateError):
        account.confirm_pending(confirmed, now=_dt(2026, 7, 2))


def test_post_correction_with_negative_delta_is_a_credit() -> None:
    account = Account(account_id="acc-4471")

    entry, event = account.post_correction(
        uuid.uuid4(), uuid.uuid4(), Decimal("-116.00"), BillingPeriod(2026, 6), now=_dt(2026, 7, 10)
    )

    assert entry.direction is EntryDirection.CREDIT
    assert entry.amount == Money(Decimal("116.00"))
    assert entry.correction_link is not None
    assert isinstance(event, CorrectionPosted)


def test_post_correction_with_positive_delta_is_a_debit() -> None:
    account = Account(account_id="acc-4471")

    entry, _event = account.post_correction(
        uuid.uuid4(), uuid.uuid4(), Decimal("50.00"), BillingPeriod(2026, 6), now=_dt(2026, 7, 10)
    )

    assert entry.direction is EntryDirection.DEBIT
    assert entry.amount == Money(Decimal("50.00"))


def test_correction_reduces_balance_via_signed_direction() -> None:
    """Двойная запись: направление, а не голая сумма, определяет вклад в баланс."""
    account = Account(account_id="acc-4471")
    period = BillingPeriod(2026, 6)
    charge, _ = account.post_charge(uuid.uuid4(), Money(Decimal("1107.20")), period, now=_dt(2026, 7, 1))
    correction, _ = account.post_correction(
        uuid.uuid4(), uuid.uuid4(), Decimal("-116.00"), period, now=_dt(2026, 7, 10)
    )

    assert Account.balance([charge, correction]) == Money(Decimal("991.20"))


def test_ledger_entry_amount_must_be_non_negative() -> None:
    from billing.domain.account import LedgerEntry

    with pytest.raises(ValueError):
        LedgerEntry(
            entry_id=uuid.uuid4(),
            account_id="acc-4471",
            direction=EntryDirection.DEBIT,
            entry_type=EntryType.POSTED,
            amount=Money(Decimal("-1")),
            period=BillingPeriod(2026, 6),
            invoice_id=None,
            correction_link=None,
            confirms_pending_entry_id=None,
            recorded_at=_dt(2026, 7, 1),
        )
