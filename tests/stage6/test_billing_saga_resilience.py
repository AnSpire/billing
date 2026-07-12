"""DoD фазы 6: сбой на 3-м шаге (PostCharge) не откатывает 1-2 (Calculate,
Issue) — Invoice уже выставлен и неизменяем; повторная попытка проводки —
идемпотентная операция восстановления, а не задвоение.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from billing.application.billing_calculation import calculate_assessment
from billing.application.billing_saga import handle_assessment_calculated, handle_invoice_issued
from billing.application.dispatcher import EventDispatcher
from billing.application.tariff_validation import validate_tariff_version
from billing.domain.billing_assessment import AssessmentCalculated
from billing.domain.consumption_stream import ExternalEventId
from billing.domain.invoice import InvoiceIssued
from billing.domain.reference_parameter import ParameterValue, Provenance
from billing.domain.shared import BillingPeriod, Quantity, TemporalValidity
from billing.domain.tariff_version import (
    Binding,
    Coefficients,
    FormalizationResult,
    FormulaForm,
    ScopeInput,
    ScopeManifest,
    SourceText,
    TariffVersion,
)
from billing.infrastructure.db.account_repository import PostgresAccountRepository
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)
from billing.infrastructure.db.connection import new_connection
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)
from billing.infrastructure.db.invoice_repository import PostgresInvoiceRepository
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_version_repository import (
    PostgresTariffVersionRepository,
)
from billing.infrastructure.formula_engine.stub_formula_engine import StubFormulaEngine


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _setup_and_calculate(test_database_url: str, *, dispatcher: EventDispatcher):
    """Готовит тариф+норму+потребление и прогоняет Calculate через
    ``dispatcher`` (шаг 1 + всё, на что он подписан)."""
    jurisdiction = _unique("RU")
    tariff_id = _unique("comfort")
    account_id = _unique("acc")
    period = BillingPeriod(2026, 6)

    with new_connection(test_database_url) as conn:
        PostgresReferenceParameterRepository(conn).register_value(
            "vat_rate",
            jurisdiction,
            ParameterValue.scalar(Decimal("0.20")),
            TemporalValidity(valid_from=_dt(2024, 1, 1)),
            Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=_dt(2024, 1, 1).date()),
            now=_dt(2024, 1, 1),
        )

    formalization = FormalizationResult(
        source_text=SourceText(text="comfort tariff", formalizer_model_version="mock-v1"),
        scope_manifest=ScopeManifest(
            scope_name="comfort_v1",
            inputs=(
                ScopeInput(
                    arg_name="vat_rate", arg_type="Decimal", binding=Binding.ref_param("vat_rate", jurisdiction)
                ),
                ScopeInput(
                    arg_name="consumption", arg_type="Quantity", binding=Binding.metric("electricity_kwh")
                ),
            ),
        ),
        formula_form=FormulaForm.stub({"base_kwh": "300", "base_rate": "3.20", "overage_multiplier": "1.15"}),
        coefficients=Coefficients(payload={"base_rate": "3.20", "overage_multiplier": "1.15"}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )
    with new_connection(test_database_url) as conn:
        tariff_versions = PostgresTariffVersionRepository(conn)
        reference_parameters = PostgresReferenceParameterRepository(conn)
        draft, _ = TariffVersion.draft_from_text(tariff_id, 1, formalization, now=_dt(2026, 6, 1))
        tariff_versions.save(draft)
        validated, _ = validate_tariff_version(draft, reference_parameters, now=_dt(2026, 6, 1))
        tariff_versions.save(validated)
        published, _ = validated.publish(now=_dt(2026, 6, 2))
        tariff_versions.save(published)

    with new_connection(test_database_url) as conn:
        PostgresConsumptionStreamRepository(conn).record_usage(
            account_id,
            "electricity_kwh",
            Quantity(Decimal(340), "electricity_kwh"),
            ExternalEventId(_unique("evt")),
            now=_dt(2026, 6, 15),
        )

    with new_connection(test_database_url) as conn:
        assessment, event = calculate_assessment(
            account_id,
            period,
            published,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            StubFormulaEngine(PostgresTariffVersionRepository(conn)),
            PostgresBillingAssessmentRepository(conn),
            metric="electricity_kwh",
            now=_dt(2026, 7, 1),
        )

    dispatcher.dispatch(event)
    return account_id, period, assessment


def test_failure_before_step_3_leaves_steps_1_and_2_committed(test_database_url: str) -> None:
    """Диспетчер, на котором подписан только шаг 1 (``AssessmentCalculated``
    -> Issue) — имитация краха процесса ДО того, как PostCharge вообще
    запустился. Invoice должен быть выставлен и остаться таким независимо от
    того, выполнился ли шаг 3."""
    dispatcher = EventDispatcher(lambda: new_connection(test_database_url))
    dispatcher.subscribe(AssessmentCalculated, handle_assessment_calculated)
    # handle_invoice_issued намеренно НЕ подписан.

    account_id, period, assessment = _setup_and_calculate(test_database_url, dispatcher=dispatcher)

    with new_connection(test_database_url) as conn:
        invoice = PostgresInvoiceRepository(conn).find_by_assessment_version(account_id, period, 1)
        entries = PostgresAccountRepository(conn).entries_for(account_id)

    assert invoice is not None  # шаги 1-2 закоммитились независимо от шага 3
    assert invoice.total == assessment.total
    assert entries == []  # шаг 3 не выполнялся вовсе — и это не откатило 1-2


def test_retrying_post_charge_after_recovery_is_idempotent(test_database_url: str) -> None:
    dispatcher = EventDispatcher(lambda: new_connection(test_database_url))
    dispatcher.subscribe(AssessmentCalculated, handle_assessment_calculated)

    account_id, period, assessment = _setup_and_calculate(test_database_url, dispatcher=dispatcher)

    with new_connection(test_database_url) as conn:
        invoice = PostgresInvoiceRepository(conn).find_by_assessment_version(account_id, period, 1)
    assert invoice is not None

    invoice_event = InvoiceIssued(
        invoice_id=invoice.invoice_id,
        account_id=invoice.account_id,
        period=str(invoice.period),
        total=invoice.total,
    )

    # Восстановление: обработчик шага 3 запускается впервые...
    with new_connection(test_database_url) as conn:
        handle_invoice_issued(invoice_event, conn)
    # ...и затем то же событие доставляется повторно (ретрай саги).
    with new_connection(test_database_url) as conn:
        handle_invoice_issued(invoice_event, conn)

    with new_connection(test_database_url) as conn:
        entries = PostgresAccountRepository(conn).entries_for(account_id)

    assert len(entries) == 1
    assert entries[0].invoice_id == invoice.invoice_id
    assert entries[0].amount == assessment.total


def test_redelivering_assessment_calculated_does_not_issue_a_second_invoice(test_database_url: str) -> None:
    """Идемпотентность не только у шага 3 (PostCharge) — весь дальнейший
    шаг 2 (Issue) тоже не должен задваиваться при повторной доставке того же
    ``AssessmentCalculated``, например если сага ретраит с самого начала."""
    dispatcher = EventDispatcher(lambda: new_connection(test_database_url))
    dispatcher.subscribe(AssessmentCalculated, handle_assessment_calculated)

    account_id, period, assessment = _setup_and_calculate(test_database_url, dispatcher=dispatcher)

    replay_event = AssessmentCalculated(account_id=account_id, period=str(period), version=1)
    with new_connection(test_database_url) as conn:
        handle_assessment_calculated(replay_event, conn)
    with new_connection(test_database_url) as conn:
        handle_assessment_calculated(replay_event, conn)

    with new_connection(test_database_url) as conn:
        invoice = PostgresInvoiceRepository(conn).find_by_assessment_version(account_id, period, 1)
        # Прямой SQL мимо порта — считаем реальное число строк, т.к. в
        # InvoiceRepository нет "list all" (не нужен нигде, кроме этой
        # проверки). Обработчик обязан САМ не дойти до повторной вставки —
        # UNIQUE в 0006_invoice.sql в любом случае бы её отклонил, но
        # предполагается, что до него не доходит.
        count = conn.execute(
            "SELECT count(*) FROM invoice WHERE account_id = %s AND period_year = %s AND period_month = %s",
            (account_id, period.year, period.month),
        ).fetchone()[0]

    assert invoice is not None
    assert invoice.total == assessment.total
    assert count == 1
