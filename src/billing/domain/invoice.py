"""Invoice — billing_aggregates.md §4. Квитанция: зафиксированный на момент
выставления снапшот начислений. Идентичность — ``InvoiceId``.

Как и остальные агрегаты: сам ничего не знает про БД. ``account_id``,
``period``, ``assessment_version`` — не отдельный VO (в billing_aggregates.md
такого VO нет, только упоминание "заморозить версию assessment") — три
простых поля прямо на корне, заводить обёртку под них было бы лишней
косвенностью без обоснования.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from billing.domain.billing_assessment import BillingAssessment
from billing.domain.events import DomainEvent
from billing.domain.shared import BillingPeriod, CorrectionLink, Money


class InvoiceError(Exception):
    """Базовая ошибка домена Invoice."""


class InvoiceNotFoundError(InvoiceError):
    """IssueCorrecting ссылается на original_invoice_id, которого нет."""


class InvalidCorrectionError(InvoiceError):
    """Корректирующая квитанция должна относиться к тому же (account_id,
    period), что и исходная — иначе CorrectionLink врёт о том, что корректирует."""


@dataclass(frozen=True)
class InvoiceLine:
    """Замороженная КОПИЯ ChargeLine, не ссылка (billing_aggregates.md §4) —
    отдельный тип данных, а не алиас на ChargeLine: пересчёт BillingAssessment
    физически не может задеть уже созданные InvoiceLine, потому что это
    разные объекты с самого создания, не разделяющие состояние."""

    line_id: uuid.UUID
    rule_label: str
    amount: Money


@dataclass(frozen=True, kw_only=True)
class InvoiceIssued(DomainEvent):
    invoice_id: uuid.UUID
    account_id: str
    period: str
    total: Money


@dataclass(frozen=True, kw_only=True)
class CorrectingInvoiceIssued(DomainEvent):
    invoice_id: uuid.UUID
    original_invoice_id: uuid.UUID
    account_id: str
    period: str
    total: Money


@dataclass(frozen=True)
class Invoice:
    invoice_id: uuid.UUID
    account_id: str
    period: BillingPeriod
    assessment_version: int
    lines: tuple[InvoiceLine, ...]
    total: Money
    correction_link: CorrectionLink | None
    issued_at: datetime

    @staticmethod
    def issue(assessment: BillingAssessment, *, now: datetime) -> tuple[Invoice, InvoiceIssued]:
        invoice = Invoice(
            invoice_id=uuid.uuid4(),
            account_id=assessment.account_id,
            period=assessment.period,
            assessment_version=assessment.version,
            lines=_copy_lines(assessment),
            total=assessment.total,
            correction_link=None,
            issued_at=now,
        )
        event = InvoiceIssued(
            invoice_id=invoice.invoice_id,
            account_id=invoice.account_id,
            period=str(invoice.period),
            total=invoice.total,
        )
        return invoice, event

    @staticmethod
    def issue_correcting(
        original_invoice_id: uuid.UUID, assessment: BillingAssessment, *, now: datetime
    ) -> tuple[Invoice, CorrectingInvoiceIssued]:
        """``original_invoice_id`` не проверяется здесь (агрегат его даже не
        видит целиком, только id) — совпадение (account_id, period) с
        исходной квитанцией проверяет репозиторий ДО вызова этого метода
        (см. infrastructure/db/invoice_repository.py), т.к. для проверки
        нужно прочитать чужую запись — I/O, а не чистая функция."""
        invoice = Invoice(
            invoice_id=uuid.uuid4(),
            account_id=assessment.account_id,
            period=assessment.period,
            assessment_version=assessment.version,
            lines=_copy_lines(assessment),
            total=assessment.total,
            correction_link=CorrectionLink(original_invoice_id=original_invoice_id),
            issued_at=now,
        )
        event = CorrectingInvoiceIssued(
            invoice_id=invoice.invoice_id,
            original_invoice_id=original_invoice_id,
            account_id=invoice.account_id,
            period=str(invoice.period),
            total=invoice.total,
        )
        return invoice, event


def _copy_lines(assessment: BillingAssessment) -> tuple[InvoiceLine, ...]:
    return tuple(
        InvoiceLine(line_id=uuid.uuid4(), rule_label=line.rule_label, amount=line.amount)
        for line in assessment.charge_lines
    )


class InvoiceRepository(ABC):
    """Порт (см. PLAN.md, «Repository — порт в домене, реализация в
    infrastructure»). Единственная реализация — ``PostgresInvoiceRepository``.

    Неизменяемость квитанции обеспечена тем, что в порту НЕТ метода
    обновления вообще — только запись новой (``issue``/``issue_correcting``)
    и чтение (``get``). В отличие от ``TariffVersion`` (где до ``Publish``
    легитимные обновления есть, поэтому там guarded UPDATE), у ``Invoice``
    обновлять нечего ни на одной стадии жизненного цикла — отсутствие метода
    в контракте убирает саму возможность мутации на уровне типов, без
    дополнительного constraint'а или триггера в БД."""

    @abstractmethod
    def issue(
        self, assessment: BillingAssessment, *, now: datetime
    ) -> tuple[Invoice, InvoiceIssued]: ...

    @abstractmethod
    def issue_correcting(
        self, original_invoice_id: uuid.UUID, assessment: BillingAssessment, *, now: datetime
    ) -> tuple[Invoice, CorrectingInvoiceIssued]: ...

    @abstractmethod
    def get(self, invoice_id: uuid.UUID) -> Invoice | None: ...
