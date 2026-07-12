"""Реализация порта ``AccountRepository`` (domain) поверх psycopg3.

Как и у ``PostgresInvoiceRepository``: порт не даёт способа обновить
``LedgerEntry`` — все команды только INSERT'ят новую строку.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from psycopg import Connection

from billing.domain.account import (
    Account,
    AccountRepository,
    CorrectionPosted,
    EntryDirection,
    EntryPosted,
    EntryType,
    InvalidLedgerEntryStateError,
    LedgerEntry,
    PendingReserved,
)
from billing.domain.shared import BillingPeriod, CorrectionLink, Money

_SELECT_COLUMNS = """
    entry_id, account_id, direction, entry_type, amount, currency,
    period_year, period_month, invoice_id, correction_of_invoice_id,
    confirms_pending_entry_id, recorded_at
"""


class PostgresAccountRepository(AccountRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def reserve_pending(
        self, account_id: str, amount: Money, period: BillingPeriod, *, now: datetime
    ) -> tuple[LedgerEntry, PendingReserved]:
        account = Account(account_id=account_id)
        entry, event = account.reserve_pending(amount, period, now=now)
        self._insert(entry)
        return entry, event

    def confirm_pending(
        self, pending_entry_id: uuid.UUID, *, now: datetime
    ) -> tuple[LedgerEntry, EntryPosted]:
        pending = self._get_entry(pending_entry_id)
        if pending is None:
            raise InvalidLedgerEntryStateError(f"no ledger entry with id {pending_entry_id}")
        account = Account(account_id=pending.account_id)
        entry, event = account.confirm_pending(pending, now=now)
        self._insert(entry)
        return entry, event

    def post_charge(
        self,
        account_id: str,
        invoice_id: uuid.UUID,
        amount: Money,
        period: BillingPeriod,
        *,
        now: datetime,
    ) -> tuple[LedgerEntry, EntryPosted]:
        account = Account(account_id=account_id)
        entry, event = account.post_charge(invoice_id, amount, period, now=now)
        self._insert(entry)
        return entry, event

    def post_correction(
        self,
        account_id: str,
        invoice_id: uuid.UUID,
        original_invoice_id: uuid.UUID,
        delta: Decimal,
        period: BillingPeriod,
        *,
        now: datetime,
    ) -> tuple[LedgerEntry, CorrectionPosted]:
        account = Account(account_id=account_id)
        entry, event = account.post_correction(
            invoice_id, original_invoice_id, delta, period, now=now
        )
        self._insert(entry)
        return entry, event

    def entries_for(self, account_id: str) -> list[LedgerEntry]:
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger_entry WHERE account_id = %s ORDER BY recorded_at",
            (account_id,),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def balance(self, account_id: str) -> Money:
        return Account.balance(self.entries_for(account_id))

    def projected_balance(self, account_id: str) -> Money:
        return Account.projected_balance(self.entries_for(account_id))

    def _get_entry(self, entry_id: uuid.UUID) -> LedgerEntry | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM ledger_entry WHERE entry_id = %s", (entry_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def _insert(self, entry: LedgerEntry) -> None:
        self._conn.execute(
            """
            INSERT INTO ledger_entry (
                entry_id, account_id, direction, entry_type, amount, currency,
                period_year, period_month, invoice_id, correction_of_invoice_id,
                confirms_pending_entry_id, recorded_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                entry.entry_id,
                entry.account_id,
                entry.direction.value,
                entry.entry_type.value,
                entry.amount.amount,
                entry.amount.currency,
                entry.period.year,
                entry.period.month,
                entry.invoice_id,
                entry.correction_link.original_invoice_id if entry.correction_link else None,
                entry.confirms_pending_entry_id,
                entry.recorded_at,
            ),
        )

    @staticmethod
    def _row_to_entry(row: tuple) -> LedgerEntry:
        (
            entry_id,
            account_id,
            direction,
            entry_type,
            amount,
            currency,
            period_year,
            period_month,
            invoice_id,
            correction_of_invoice_id,
            confirms_pending_entry_id,
            recorded_at,
        ) = row
        return LedgerEntry(
            entry_id=entry_id,
            account_id=account_id,
            direction=EntryDirection(direction),
            entry_type=EntryType(entry_type),
            amount=Money(amount=Decimal(amount), currency=currency),
            period=BillingPeriod(year=period_year, month=period_month),
            invoice_id=invoice_id,
            correction_link=(
                CorrectionLink(original_invoice_id=correction_of_invoice_id)
                if correction_of_invoice_id
                else None
            ),
            confirms_pending_entry_id=confirms_pending_entry_id,
            recorded_at=recorded_at,
        )
