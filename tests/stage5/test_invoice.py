"""Чистые тесты агрегата Invoice — без БД.

DoD фазы 5: InvoiceLine — копия ChargeLine, не ссылка; корректировка не
мутирует исходную (сама по себе Invoice иммутабельна, так что "не мутирует"
здесь проверяется через то, что IssueCorrecting не трогает объект оригинала
и создаёт независимый новый)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from billing.domain.billing_assessment import (
    ArtifactRef,
    AssessmentStatus,
    BillingAssessment,
    CalcContext,
    ChargeLine,
    Money,
    ResolvedParameterRef,
)
from billing.domain.invoice import CorrectingInvoiceIssued, Invoice, InvoiceIssued
from billing.domain.shared import BillingPeriod, Quantity


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _assessment(version: int = 1, base: str = "960.00") -> BillingAssessment:
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
        account_id="acc-4471",
        period=BillingPeriod(2026, 6),
        version=version,
        status=AssessmentStatus.ACTIVE,
        charge_lines=(
            ChargeLine(line_id=uuid.uuid4(), rule_label="base", amount=Money(Decimal(base))),
            ChargeLine(line_id=uuid.uuid4(), rule_label="vat", amount=Money(Decimal("221.44"))),
        ),
        calc_context=calc_context,
        created_at=_dt(2026, 7, 1),
    )


def test_issue_freezes_a_copy_of_charge_lines_not_a_reference() -> None:
    assessment = _assessment()

    invoice, event = Invoice.issue(assessment, now=_dt(2026, 7, 1))

    assert len(invoice.lines) == len(assessment.charge_lines)
    for invoice_line, charge_line in zip(invoice.lines, assessment.charge_lines):
        assert invoice_line.rule_label == charge_line.rule_label
        assert invoice_line.amount == charge_line.amount
        # Разные типы данных и разные id строк — не одна и та же сущность.
        assert invoice_line.line_id != charge_line.line_id
        assert not isinstance(invoice_line, ChargeLine)
    assert invoice.total == assessment.total
    assert invoice.correction_link is None
    assert isinstance(event, InvoiceIssued)


def test_recalculating_the_assessment_does_not_affect_an_already_issued_invoice() -> None:
    v1 = _assessment(version=1, base="960.00")
    invoice, _ = Invoice.issue(v1, now=_dt(2026, 7, 1))

    # BillingAssessment иммутабелен — "пересчёт" даёт НОВЫЙ объект v2, v1 не
    # трогается. Квитанция была заморожена от v1 и должна остаться такой.
    v2 = _assessment(version=2, base="500.00")

    assert invoice.lines[0].amount == Money(Decimal("960.00"))
    assert v2.charge_lines[0].amount == Money(Decimal("500.00"))
    assert invoice.assessment_version == 1


def test_issue_correcting_links_to_the_original_and_produces_a_new_invoice_id() -> None:
    v1 = _assessment(version=1, base="960.00")
    original, _ = Invoice.issue(v1, now=_dt(2026, 7, 1))

    v2 = _assessment(version=2, base="800.00")
    correcting, event = Invoice.issue_correcting(original.invoice_id, v2, now=_dt(2026, 7, 10))

    assert correcting.invoice_id != original.invoice_id
    assert correcting.correction_link is not None
    assert correcting.correction_link.original_invoice_id == original.invoice_id
    assert isinstance(event, CorrectingInvoiceIssued)
    assert event.original_invoice_id == original.invoice_id
    # Исходная квитанция не мутировала.
    assert original.correction_link is None
    assert original.lines[0].amount == Money(Decimal("960.00"))
