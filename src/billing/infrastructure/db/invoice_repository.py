"""Реализация порта ``InvoiceRepository`` (domain) поверх psycopg3.

Порт не даёт способа обновить строку — ``issue``/``issue_correcting`` всегда
INSERT, ``get`` — чтение. См. docstring ``InvoiceRepository`` в
domain/invoice.py про то, почему здесь не нужен constraint/триггер на
неизменяемость, в отличие от ``PostgresTariffVersionRepository``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from billing.domain.billing_assessment import BillingAssessment
from billing.domain.invoice import (
    CorrectingInvoiceIssued,
    InvalidCorrectionError,
    Invoice,
    InvoiceIssued,
    InvoiceLine,
    InvoiceNotFoundError,
    InvoiceRepository,
)
from billing.domain.shared import BillingPeriod, CorrectionLink, Money

_SELECT_COLUMNS = """
    invoice_id, account_id, period_year, period_month, assessment_version,
    lines, total, correction_of_invoice_id, issued_at
"""


class PostgresInvoiceRepository(InvoiceRepository):
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def issue(
        self, assessment: BillingAssessment, *, now: datetime
    ) -> tuple[Invoice, InvoiceIssued]:
        invoice, event = Invoice.issue(assessment, now=now)
        self._insert(invoice)
        return invoice, event

    def issue_correcting(
        self, original_invoice_id: uuid.UUID, assessment: BillingAssessment, *, now: datetime
    ) -> tuple[Invoice, CorrectingInvoiceIssued]:
        original = self.get(original_invoice_id)
        if original is None:
            raise InvoiceNotFoundError(f"no invoice with id {original_invoice_id}")
        if original.account_id != assessment.account_id or original.period != assessment.period:
            raise InvalidCorrectionError(
                f"invoice {original_invoice_id} is for ({original.account_id!r}, "
                f"{original.period}), not ({assessment.account_id!r}, {assessment.period})"
            )
        invoice, event = Invoice.issue_correcting(original_invoice_id, assessment, now=now)
        self._insert(invoice)
        return invoice, event

    def get(self, invoice_id: uuid.UUID) -> Invoice | None:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM invoice WHERE invoice_id = %s", (invoice_id,)
        ).fetchone()
        return self._row_to_invoice(row) if row else None

    def _insert(self, invoice: Invoice) -> None:
        self._conn.execute(
            """
            INSERT INTO invoice (
                invoice_id, account_id, period_year, period_month, assessment_version,
                lines, total, correction_of_invoice_id, issued_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                invoice.invoice_id,
                invoice.account_id,
                invoice.period.year,
                invoice.period.month,
                invoice.assessment_version,
                Jsonb([_line_to_json(line) for line in invoice.lines]),
                Jsonb(_money_to_json(invoice.total)),
                invoice.correction_link.original_invoice_id if invoice.correction_link else None,
                invoice.issued_at,
            ),
        )

    @staticmethod
    def _row_to_invoice(row: tuple) -> Invoice:
        (
            invoice_id,
            account_id,
            period_year,
            period_month,
            assessment_version,
            lines,
            total,
            correction_of_invoice_id,
            issued_at,
        ) = row
        return Invoice(
            invoice_id=invoice_id,
            account_id=account_id,
            period=BillingPeriod(year=period_year, month=period_month),
            assessment_version=assessment_version,
            lines=tuple(_line_from_json(line) for line in lines),
            total=_money_from_json(total),
            correction_link=(
                CorrectionLink(original_invoice_id=correction_of_invoice_id)
                if correction_of_invoice_id
                else None
            ),
            issued_at=issued_at,
        )


def _money_to_json(money: Money) -> dict[str, Any]:
    return {"amount": str(money.amount), "currency": money.currency}


def _money_from_json(data: dict[str, Any]) -> Money:
    return Money(amount=Decimal(data["amount"]), currency=data["currency"])


def _line_to_json(line: InvoiceLine) -> dict[str, Any]:
    return {
        "line_id": str(line.line_id),
        "rule_label": line.rule_label,
        "amount": _money_to_json(line.amount),
    }


def _line_from_json(data: dict[str, Any]) -> InvoiceLine:
    return InvoiceLine(
        line_id=uuid.UUID(data["line_id"]),
        rule_label=data["rule_label"],
        amount=_money_from_json(data["amount"]),
    )
