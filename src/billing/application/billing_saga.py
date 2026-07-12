"""Сага `Assessment -> Invoice -> Account` — PLAN.md, фаза 6.

"Сага" здесь — не оркестратор и не отдельный сервис, а набор обработчиков,
подписанных на ``EventDispatcher`` (application/dispatcher.py): "когда
случилось событие X — вызови команду Y" (CLAUDE.md §2). Каждый обработчик —
своя транзакция; следующий шаг цепочки диспетчер вызывает только ПОСЛЕ того,
как транзакция текущего шага закоммитилась (см. docstring
``EventDispatcher.dispatch``) — иначе следующий шаг, работая на отдельном
соединении, не увидел бы ещё не закоммиченные изменения предыдущего.

Цепочки:
    AssessmentCalculated    -> Invoice.Issue           -> InvoiceIssued
    InvoiceIssued           -> Account.PostCharge
    AssessmentRecalculated  -> Invoice.IssueCorrecting  -> CorrectingInvoiceIssued
    CorrectingInvoiceIssued -> Account.PostCorrection

**Идемпотентность повторной доставки** (DoD фазы 6) — каждый обработчик перед
командой проверяет, не выполнена ли она уже для этого же входа
(``find_by_assessment_version`` / ``find_by_invoice``), и если да — не
повторяет запись, а там, где нужно продолжить цепочку, возвращает то же
следующее событие, восстановленное из уже существующей записи. Это не
полагается на память процесса (которой при перезапуске не будет) — только на
то, что уже видно в БД.

**Не outbox.** Триггер (кто и когда вызывает ``dispatcher.dispatch(...)``)
здесь по-прежнему не переживает падение процесса между шагами — это
осознанно отложено (CLAUDE.md §6, PLAN.md «Что вне плана сейчас»). Что
реально гарантируется на этом этапе: если триггер всё-таки вызван (пусть и
повторно, пусть и после сбоя на любом шаге), результат корректен и не
задваивается.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from psycopg import Connection

from billing.application.dispatcher import EventDispatcher
from billing.domain.billing_assessment import AssessmentCalculated, AssessmentRecalculated
from billing.domain.events import DomainEvent
from billing.domain.invoice import CorrectingInvoiceIssued, Invoice, InvoiceIssued
from billing.domain.shared import BillingPeriod
from billing.infrastructure.db.account_repository import PostgresAccountRepository
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)
from billing.infrastructure.db.invoice_repository import PostgresInvoiceRepository


class SagaError(Exception):
    """Шаг саги не может продолжиться: предыдущий агрегат, на который он
    ссылается по id, не найден. По построению не должно случаться на
    happy-path (события несут ссылки только на то, что сами же создали) —
    сигнал о рассинхронизации, а не ожидаемый случай."""


def handle_assessment_calculated(
    event: AssessmentCalculated, conn: Connection
) -> Sequence[DomainEvent]:
    period = BillingPeriod.parse(event.period)
    invoices = PostgresInvoiceRepository(conn)

    existing = invoices.find_by_assessment_version(event.account_id, period, event.version)
    if existing is not None:
        return (_invoice_issued_event(existing),)

    assessments = PostgresBillingAssessmentRepository(conn)
    assessment = assessments.get_version(event.account_id, period, event.version)
    if assessment is None:
        raise SagaError(
            f"no BillingAssessment ({event.account_id!r}, {period}, v{event.version}) "
            "to issue an invoice from"
        )
    _invoice, invoice_event = invoices.issue(assessment, now=datetime.now(timezone.utc))
    return (invoice_event,)


def handle_assessment_recalculated(
    event: AssessmentRecalculated, conn: Connection
) -> Sequence[DomainEvent]:
    period = BillingPeriod.parse(event.period)
    invoices = PostgresInvoiceRepository(conn)

    existing = invoices.find_by_assessment_version(event.account_id, period, event.version)
    if existing is not None:
        return (_correcting_invoice_issued_event(existing),)

    original = invoices.find_by_assessment_version(event.account_id, period, event.version - 1)
    if original is None:
        raise SagaError(
            f"no invoice for the previous version ({event.account_id!r}, {period}, "
            f"v{event.version - 1}) to correct"
        )

    assessments = PostgresBillingAssessmentRepository(conn)
    assessment = assessments.get_version(event.account_id, period, event.version)
    if assessment is None:
        raise SagaError(
            f"no BillingAssessment ({event.account_id!r}, {period}, v{event.version}) "
            "to issue a correcting invoice from"
        )
    _invoice, invoice_event = invoices.issue_correcting(
        original.invoice_id, assessment, now=datetime.now(timezone.utc)
    )
    return (invoice_event,)


def handle_invoice_issued(event: InvoiceIssued, conn: Connection) -> Sequence[DomainEvent]:
    accounts = PostgresAccountRepository(conn)
    if accounts.find_by_invoice(event.invoice_id) is not None:
        return ()  # уже проведено — идемпотентный no-op при повторной доставке

    period = BillingPeriod.parse(event.period)
    accounts.post_charge(
        event.account_id, event.invoice_id, event.total, period, now=datetime.now(timezone.utc)
    )
    return ()


def handle_correcting_invoice_issued(
    event: CorrectingInvoiceIssued, conn: Connection
) -> Sequence[DomainEvent]:
    accounts = PostgresAccountRepository(conn)
    if accounts.find_by_invoice(event.invoice_id) is not None:
        return ()

    invoices = PostgresInvoiceRepository(conn)
    original = invoices.get(event.original_invoice_id)
    if original is None:
        raise SagaError(f"original invoice {event.original_invoice_id} not found")

    delta = event.total.amount - original.total.amount
    period = BillingPeriod.parse(event.period)
    accounts.post_correction(
        event.account_id,
        event.invoice_id,
        event.original_invoice_id,
        delta,
        period,
        now=datetime.now(timezone.utc),
    )
    return ()


def _invoice_issued_event(invoice: Invoice) -> InvoiceIssued:
    return InvoiceIssued(
        invoice_id=invoice.invoice_id,
        account_id=invoice.account_id,
        period=str(invoice.period),
        total=invoice.total,
    )


def _correcting_invoice_issued_event(invoice: Invoice) -> CorrectingInvoiceIssued:
    if invoice.correction_link is None:
        raise SagaError(
            f"invoice {invoice.invoice_id} was expected to be a correction but has no CorrectionLink"
        )
    return CorrectingInvoiceIssued(
        invoice_id=invoice.invoice_id,
        original_invoice_id=invoice.correction_link.original_invoice_id,
        account_id=invoice.account_id,
        period=str(invoice.period),
        total=invoice.total,
    )


def register_billing_saga(dispatcher: EventDispatcher) -> None:
    dispatcher.subscribe(AssessmentCalculated, handle_assessment_calculated)
    dispatcher.subscribe(AssessmentRecalculated, handle_assessment_recalculated)
    dispatcher.subscribe(InvoiceIssued, handle_invoice_issued)
    dispatcher.subscribe(CorrectingInvoiceIssued, handle_correcting_invoice_issued)
