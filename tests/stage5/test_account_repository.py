"""Account поверх реальной БД — DoD фазы 5: append-only; баланс = свёртка
проводок (не хранимое поле); two-phase без задвоения."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from billing.domain.account import EntryType
from billing.domain.shared import BillingPeriod, Money
from billing.infrastructure.db.account_repository import PostgresAccountRepository


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def test_entries_are_append_only_and_ordered(db_connection) -> None:
    repo = PostgresAccountRepository(db_connection)
    period = BillingPeriod(2026, 6)

    repo.reserve_pending("acc-4471", Money(Decimal("576.00")), period, now=_dt(2026, 6, 15))
    repo.post_charge("acc-4471", uuid.uuid4(), Money(Decimal("1107.20")), period, now=_dt(2026, 7, 1))

    entries = repo.entries_for("acc-4471")
    assert len(entries) == 2
    assert entries[0].entry_type is EntryType.PENDING
    assert entries[1].entry_type is EntryType.POSTED


def test_balance_is_derived_not_stored(db_connection) -> None:
    repo = PostgresAccountRepository(db_connection)
    period = BillingPeriod(2026, 6)
    invoice_id = uuid.uuid4()

    repo.post_charge("acc-4471", invoice_id, Money(Decimal("1107.20")), period, now=_dt(2026, 7, 1))
    repo.post_correction(
        "acc-4471", uuid.uuid4(), invoice_id, Decimal("-116.00"), period, now=_dt(2026, 7, 10)
    )

    assert repo.balance("acc-4471") == Money(Decimal("991.20"))


def test_pending_and_posted_are_not_double_counted_once_the_real_charge_lands(db_connection) -> None:
    repo = PostgresAccountRepository(db_connection)
    period = BillingPeriod(2026, 6)

    repo.reserve_pending("acc-4471", Money(Decimal("576.00")), period, now=_dt(2026, 6, 15))
    assert repo.projected_balance("acc-4471") == Money(Decimal("576.00"))
    assert repo.balance("acc-4471") == Money(Decimal("0"))

    repo.post_charge("acc-4471", uuid.uuid4(), Money(Decimal("1107.20")), period, now=_dt(2026, 7, 1))

    # Финальная posted-проводка за июнь landed — прежняя pending-прикидка за
    # июнь больше не учитывается ни в balance, ни в projected_balance.
    assert repo.balance("acc-4471") == Money(Decimal("1107.20"))
    assert repo.projected_balance("acc-4471") == Money(Decimal("1107.20"))


def test_confirm_pending_via_repository_appends_without_mutating(db_connection) -> None:
    repo = PostgresAccountRepository(db_connection)
    period = BillingPeriod(2026, 6)
    pending, _ = repo.reserve_pending("acc-4471", Money(Decimal("576.00")), period, now=_dt(2026, 6, 15))

    confirmed, _ = repo.confirm_pending(pending.entry_id, now=_dt(2026, 7, 1))

    entries = repo.entries_for("acc-4471")
    assert len(entries) == 2
    by_id = {e.entry_id: e for e in entries}
    assert by_id[pending.entry_id].entry_type is EntryType.PENDING  # не тронута
    assert by_id[confirmed.entry_id].entry_type is EntryType.POSTED
    assert by_id[confirmed.entry_id].confirms_pending_entry_id == pending.entry_id
    assert repo.balance("acc-4471") == Money(Decimal("576.00"))
