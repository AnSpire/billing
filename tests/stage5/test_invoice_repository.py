"""Invoice поверх реальной БД — DoD фазы 5: корректировка порождает новую
квитанцию с CorrectionLink, исходная не мутирует и не удаляется."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from billing.domain.billing_assessment import (
    ArtifactRef,
    AssessmentStatus,
    BillingAssessment,
    CalcContext,
    ChargeLine,
    Money,
    ResolvedParameterRef,
)
from billing.domain.invoice import InvalidCorrectionError, InvoiceNotFoundError
from billing.domain.shared import BillingPeriod, Quantity
from billing.infrastructure.db.invoice_repository import PostgresInvoiceRepository


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _assessment(
    account_id: str = "acc-4471", period: BillingPeriod = BillingPeriod(2026, 6), version: int = 1, base: str = "960.00"
) -> BillingAssessment:
    calc_context = CalcContext(
        artifact_ref=ArtifactRef(
            tariff_id="comfort", version=1, artifact_hash="hash-1", toolchain_version="stub-v1"
        ),
        resolved_parameters=(
            ResolvedParameterRef(key="vat_rate", jurisdiction="RU", version_id=uuid.uuid4()),
        ),
        consumption_event_ids=(uuid.uuid4(),),
        total_quantity=Quantity(value=Decimal(340), metric="electricity_kwh"),
    )
    return BillingAssessment(
        account_id=account_id,
        period=period,
        version=version,
        status=AssessmentStatus.ACTIVE,
        charge_lines=(ChargeLine(line_id=uuid.uuid4(), rule_label="base", amount=Money(Decimal(base))),),
        calc_context=calc_context,
        created_at=_dt(2026, 7, 1),
    )


def test_issue_persists_and_round_trips(db_connection) -> None:
    repo = PostgresInvoiceRepository(db_connection)
    assessment = _assessment()

    invoice, event = repo.issue(assessment, now=_dt(2026, 7, 1))

    fetched = repo.get(invoice.invoice_id)
    assert fetched is not None
    assert fetched.total == invoice.total
    assert fetched.lines == invoice.lines
    assert fetched.correction_link is None
    assert event.invoice_id == invoice.invoice_id


def test_issue_correcting_creates_a_new_invoice_linked_to_the_original(db_connection) -> None:
    repo = PostgresInvoiceRepository(db_connection)
    original, _ = repo.issue(_assessment(base="960.00"), now=_dt(2026, 7, 1))

    corrected_assessment = _assessment(version=2, base="800.00")
    correcting, event = repo.issue_correcting(original.invoice_id, corrected_assessment, now=_dt(2026, 7, 10))

    assert correcting.invoice_id != original.invoice_id
    assert correcting.correction_link is not None
    assert correcting.correction_link.original_invoice_id == original.invoice_id
    assert event.original_invoice_id == original.invoice_id

    # Исходная квитанция физически не изменилась и не исчезла.
    original_reloaded = repo.get(original.invoice_id)
    assert original_reloaded is not None
    assert original_reloaded.correction_link is None
    assert original_reloaded.lines[0].amount == Money(Decimal("960.00"))


def test_issue_correcting_rejects_unknown_original(db_connection) -> None:
    repo = PostgresInvoiceRepository(db_connection)

    with pytest.raises(InvoiceNotFoundError):
        repo.issue_correcting(uuid.uuid4(), _assessment(), now=_dt(2026, 7, 10))


def test_issue_correcting_rejects_a_different_account_or_period(db_connection) -> None:
    repo = PostgresInvoiceRepository(db_connection)
    original, _ = repo.issue(_assessment(account_id="acc-4471", period=BillingPeriod(2026, 6)), now=_dt(2026, 7, 1))

    mismatched = _assessment(account_id="acc-9999", period=BillingPeriod(2026, 6), version=2)

    with pytest.raises(InvalidCorrectionError):
        repo.issue_correcting(original.invoice_id, mismatched, now=_dt(2026, 7, 10))
