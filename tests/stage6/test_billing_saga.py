"""Сквозные тесты саги Assessment -> Invoice -> Account через события —
DoD фазы 6: UC-4 (Calculate -> Issue -> PostCharge) и UC-6 (Recalculate ->
IssueCorrecting -> PostCorrection) идут через ``EventDispatcher``, а не
прямыми вызовами.

Каждый шаг реально выполняется на СВОЁМ соединении (как в проде через
``EventDispatcher``), поэтому здесь используется ``new_connection`` с
настоящими коммитами, а не фикстура ``db_connection`` с откатом — иначе
следующий шаг саги не увидел бы данные предыдущего (разные транзакции).
Изоляция между тестами — через случайные account_id/tariff_id/jurisdiction
(``_unique``), а не через rollback.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from billing.application.billing_calculation import calculate_assessment, recalculate_assessment
from billing.application.billing_saga import register_billing_saga
from billing.application.dispatcher import EventDispatcher
from billing.application.tariff_validation import validate_tariff_version
from billing.domain.account import EntryDirection, EntryType
from billing.domain.consumption_stream import ExternalEventId
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


def _setup_comfort_tariff(test_database_url: str, *, jurisdiction: str, tariff_id: str) -> TariffVersion:
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
        return published


def _record_consumption(test_database_url: str, *, account_id: str, value: str, when: datetime) -> None:
    with new_connection(test_database_url) as conn:
        PostgresConsumptionStreamRepository(conn).record_usage(
            account_id,
            "electricity_kwh",
            Quantity(Decimal(value), "electricity_kwh"),
            ExternalEventId(_unique("evt")),
            now=when,
        )


def _saga_dispatcher(test_database_url: str) -> EventDispatcher:
    dispatcher = EventDispatcher(lambda: new_connection(test_database_url))
    register_billing_saga(dispatcher)
    return dispatcher


def test_uc4_flows_through_events_calculate_issue_post_charge(test_database_url: str) -> None:
    jurisdiction = _unique("RU")
    tariff_id = _unique("comfort")
    account_id = _unique("acc")
    period = BillingPeriod(2026, 6)
    tariff = _setup_comfort_tariff(test_database_url, jurisdiction=jurisdiction, tariff_id=tariff_id)
    _record_consumption(test_database_url, account_id=account_id, value="340", when=_dt(2026, 6, 15))

    with new_connection(test_database_url) as conn:
        assessment, event = calculate_assessment(
            account_id,
            period,
            tariff,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            StubFormulaEngine(PostgresTariffVersionRepository(conn)),
            PostgresBillingAssessmentRepository(conn),
            metric="electricity_kwh",
            now=_dt(2026, 7, 1),
        )

    _saga_dispatcher(test_database_url).dispatch(event)

    with new_connection(test_database_url) as conn:
        invoice = PostgresInvoiceRepository(conn).find_by_assessment_version(account_id, period, 1)
        entries = PostgresAccountRepository(conn).entries_for(account_id)

    assert invoice is not None
    assert invoice.total == assessment.total
    assert invoice.correction_link is None
    assert len(entries) == 1
    assert entries[0].entry_type is EntryType.POSTED
    assert entries[0].direction is EntryDirection.DEBIT
    assert entries[0].invoice_id == invoice.invoice_id
    assert entries[0].amount == assessment.total


def test_uc6_flows_through_events_recalculate_issue_correcting_post_correction(
    test_database_url: str,
) -> None:
    jurisdiction = _unique("RU")
    tariff_id = _unique("comfort")
    account_id = _unique("acc")
    period = BillingPeriod(2026, 6)
    tariff = _setup_comfort_tariff(test_database_url, jurisdiction=jurisdiction, tariff_id=tariff_id)
    _record_consumption(test_database_url, account_id=account_id, value="340", when=_dt(2026, 6, 15))

    dispatcher = _saga_dispatcher(test_database_url)

    with new_connection(test_database_url) as conn:
        _v1, event = calculate_assessment(
            account_id,
            period,
            tariff,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            StubFormulaEngine(PostgresTariffVersionRepository(conn)),
            PostgresBillingAssessmentRepository(conn),
            metric="electricity_kwh",
            now=_dt(2026, 7, 1),
        )
    dispatcher.dispatch(event)

    with new_connection(test_database_url) as conn:
        original_invoice = PostgresInvoiceRepository(conn).find_by_assessment_version(account_id, period, 1)

    # Авария: потребление пересмотрено задним числом (UC-6), 340 -> 310.
    _record_consumption(test_database_url, account_id=account_id, value="-30", when=_dt(2026, 6, 25))

    with new_connection(test_database_url) as conn:
        result = recalculate_assessment(
            account_id,
            period,
            tariff,
            PostgresReferenceParameterRepository(conn),
            PostgresConsumptionStreamRepository(conn),
            StubFormulaEngine(PostgresTariffVersionRepository(conn)),
            PostgresBillingAssessmentRepository(conn),
            metric="electricity_kwh",
            now=_dt(2026, 7, 10),
        )
    dispatcher.dispatch(result.event)

    with new_connection(test_database_url) as conn:
        correcting_invoice = PostgresInvoiceRepository(conn).find_by_assessment_version(account_id, period, 2)
        entries = PostgresAccountRepository(conn).entries_for(account_id)

    assert original_invoice is not None
    assert correcting_invoice is not None
    assert correcting_invoice.correction_link is not None
    assert correcting_invoice.correction_link.original_invoice_id == original_invoice.invoice_id
    # Исходная квитанция не мутировала.
    assert original_invoice.total == result.superseded.total

    assert len(entries) == 2
    charge = next(e for e in entries if e.invoice_id == original_invoice.invoice_id)
    correction = next(e for e in entries if e.invoice_id == correcting_invoice.invoice_id)
    assert charge.entry_type is EntryType.POSTED
    assert correction.entry_type is EntryType.POSTED
    assert correction.correction_link is not None
    assert correction.correction_link.original_invoice_id == original_invoice.invoice_id
    # 310 кВт·ч < 340 -> корректировка уменьшает начисление -> кредит.
    assert correction.direction is EntryDirection.CREDIT

    with new_connection(test_database_url) as conn:
        balance = PostgresAccountRepository(conn).balance(account_id)
    assert balance == correcting_invoice.total
