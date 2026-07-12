"""application/billing_calculation.py — сквозной тест поверх реальной БД,
связывающий TariffVersion + ReferenceParameter + ConsumptionStream +
StubFormulaEngine. DoD фазы 4:

- каскад когерентен (правило от суммарного потребления видит всю сумму);
- резолвинг параметра берёт valid_on = конец периода, а не now.

Числа воспроизводят пример UC-4/UC-10 из use_case.md (340 кВт·ч, база 300 по
3.20, превышение ×1.15, НДС 20%) — совпадение с документацией проверяет
модель, а не только код.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from billing.application.billing_calculation import (
    calculate_assessment,
    recalculate_assessment,
)
from billing.application.tariff_validation import validate_tariff_version
from billing.domain.billing_assessment import AssessmentStatus, Money
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
from billing.infrastructure.db.billing_assessment_repository import (
    PostgresBillingAssessmentRepository,
)
from billing.infrastructure.db.consumption_stream_repository import (
    PostgresConsumptionStreamRepository,
)
from billing.infrastructure.db.reference_parameter_repository import (
    PostgresReferenceParameterRepository,
)
from billing.infrastructure.db.tariff_version_repository import (
    PostgresTariffVersionRepository,
)
from billing.infrastructure.formula_engine.stub_formula_engine import StubFormulaEngine


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _comfort_v1() -> FormalizationResult:
    return FormalizationResult(
        source_text=SourceText(
            text="база 300 кВт·ч по 3.20, превышение с надбавкой 15%", formalizer_model_version="mock-v1"
        ),
        scope_manifest=ScopeManifest(
            scope_name="comfort_v1",
            inputs=(
                ScopeInput(arg_name="vat_rate", arg_type="Decimal", binding=Binding.ref_param("vat_rate", "RU")),
                ScopeInput(arg_name="consumption", arg_type="Quantity", binding=Binding.metric("electricity_kwh")),
            ),
        ),
        formula_form=FormulaForm.stub({"base_kwh": "300", "base_rate": "3.20", "overage_multiplier": "1.15"}),
        coefficients=Coefficients(payload={"base_rate": "3.20", "overage_multiplier": "1.15"}),
        temporal_validity=TemporalValidity(valid_from=_dt(2026, 7, 1)),
    )


def _published_comfort_v1(db_connection) -> TariffVersion:
    tariff_versions = PostgresTariffVersionRepository(db_connection)
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    draft, _ = TariffVersion.draft_from_text("comfort", 1, _comfort_v1(), now=_dt(2026, 6, 1))
    tariff_versions.save(draft)
    validated, _ = validate_tariff_version(draft, reference_parameters, now=_dt(2026, 6, 1))
    tariff_versions.save(validated)
    published, _ = validated.publish(approved_by="qa-lead", now=_dt(2026, 6, 2))
    tariff_versions.save(published)
    return published


def _register_vat_rate(
    db_connection,
    *,
    rate: str,
    valid_from: datetime,
    valid_to: datetime | None = None,
    registered_at: datetime | None = None,
) -> None:
    """``registered_at`` (tx_from) по умолчанию — тот же момент, что и
    valid_from, но его можно сдвинуть раньше: норма, объявленная заранее (а
    не "узнанная" системой ровно в день вступления в силу), должна быть
    известна для резолвинга на более ранний ``as_of_tx`` (например, на
    момент TariffVersion.Validate)."""
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    reference_parameters.register_value(
        "vat_rate",
        "RU",
        ParameterValue.scalar(Decimal(rate)),
        TemporalValidity(valid_from=valid_from, valid_to=valid_to),
        Provenance(regulation_ref="98-FZ", document_id="doc-1", effective_date=valid_from.date()),
        now=registered_at or valid_from,
    )


def test_calculate_reproduces_uc4_numbers_with_cascade_coherent_overage(db_connection) -> None:
    """Потребление приходит ДВУМЯ фактами (200 + 140 = 340), а не одним —
    если бы порог 300 проверялся построчно, ни одна запись не превысила бы
    его и overage был бы 0. Правильный результат (overage=40) доказывает, что
    свёртка суммирует всё потребление ДО применения порога."""
    _register_vat_rate(db_connection, rate="0.20", valid_from=_dt(2024, 1, 1))
    tariff = _published_comfort_v1(db_connection)

    consumption = PostgresConsumptionStreamRepository(db_connection)
    consumption.record_usage(
        "acc-4471", "electricity_kwh", Quantity(Decimal(200), "electricity_kwh"),
        ExternalEventId("meter-1"), now=_dt(2026, 6, 10),
    )
    consumption.record_usage(
        "acc-4471", "electricity_kwh", Quantity(Decimal(140), "electricity_kwh"),
        ExternalEventId("meter-2"), now=_dt(2026, 6, 20),
    )

    assessments = PostgresBillingAssessmentRepository(db_connection)
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    formula_engine = StubFormulaEngine(PostgresTariffVersionRepository(db_connection))

    assessment, event = calculate_assessment(
        "acc-4471", BillingPeriod(2026, 6), tariff, reference_parameters, consumption,
        formula_engine, assessments, metric="electricity_kwh", now=_dt(2026, 7, 1),
    )

    by_label = {line.rule_label: line.amount for line in assessment.charge_lines}
    assert by_label["base"] == Money(Decimal("960.00"))
    assert by_label["overage"] == Money(Decimal("147.20"))
    assert by_label["vat"] == Money(Decimal("221.44"))
    assert assessment.total == Money(Decimal("1328.64"))
    assert event.version == 1

    assert assessment.calc_context.total_quantity.value == Decimal(340)
    assert assessment.calc_context.artifact_ref.tariff_id == "comfort"
    assert assessment.calc_context.artifact_ref.version == 1
    assert len(assessment.calc_context.consumption_event_ids) == 2
    assert len(assessment.calc_context.resolved_parameters) == 1
    assert assessment.calc_context.resolved_parameters[0].key == "vat_rate"


def test_calculate_resolves_vat_rate_at_end_of_period_not_now(db_connection) -> None:
    """Норма меняется 1 июля; пересчёт/расчёт ИЮНЯ 5 июля обязан взять
    июньскую ставку (0.20), а не новую июльскую (0.10) — иначе смена нормы
    "отравляет" уже прошедший период (PLAN.md, DoD фазы 4)."""
    _register_vat_rate(db_connection, rate="0.20", valid_from=_dt(2024, 1, 1), valid_to=_dt(2026, 7, 1))
    # Объявлена заранее (1 мая), вступает в силу 1 июля — известна на момент
    # Validate тарифа (now=2026-06-01 внутри _published_comfort_v1).
    _register_vat_rate(
        db_connection, rate="0.10", valid_from=_dt(2026, 7, 1), registered_at=_dt(2026, 5, 1)
    )
    tariff = _published_comfort_v1(db_connection)

    consumption = PostgresConsumptionStreamRepository(db_connection)
    consumption.record_usage(
        "acc-4471", "electricity_kwh", Quantity(Decimal(340), "electricity_kwh"),
        ExternalEventId("meter-1"), now=_dt(2026, 6, 15),
    )

    assessments = PostgresBillingAssessmentRepository(db_connection)
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    formula_engine = StubFormulaEngine(PostgresTariffVersionRepository(db_connection))

    # "Сейчас" — 5 июля, уже после смены ставки, но период расчёта — июнь.
    assessment, _event = calculate_assessment(
        "acc-4471", BillingPeriod(2026, 6), tariff, reference_parameters, consumption,
        formula_engine, assessments, metric="electricity_kwh", now=_dt(2026, 7, 5),
    )

    by_label = {line.rule_label: line.amount for line in assessment.charge_lines}
    assert by_label["vat"] == Money(Decimal("221.44"))  # 1107.20 × 0.20, не × 0.10


def test_recalculate_reflects_revised_consumption_and_produces_a_diff(db_connection) -> None:
    _register_vat_rate(db_connection, rate="0.20", valid_from=_dt(2024, 1, 1))
    tariff = _published_comfort_v1(db_connection)

    consumption = PostgresConsumptionStreamRepository(db_connection)
    consumption.record_usage(
        "acc-4471", "electricity_kwh", Quantity(Decimal(340), "electricity_kwh"),
        ExternalEventId("meter-1"), now=_dt(2026, 6, 15),
    )

    assessments = PostgresBillingAssessmentRepository(db_connection)
    reference_parameters = PostgresReferenceParameterRepository(db_connection)
    formula_engine = StubFormulaEngine(PostgresTariffVersionRepository(db_connection))

    calculate_assessment(
        "acc-4471", BillingPeriod(2026, 6), tariff, reference_parameters, consumption,
        formula_engine, assessments, metric="electricity_kwh", now=_dt(2026, 7, 1),
    )

    # Пересмотр: авария снизила фактическое потребление на 30 кВт·ч (UC-6).
    consumption.record_usage(
        "acc-4471", "electricity_kwh", Quantity(Decimal(-30), "electricity_kwh"),
        ExternalEventId("outage-adjustment"), now=_dt(2026, 6, 25),
    )

    result = recalculate_assessment(
        "acc-4471", BillingPeriod(2026, 6), tariff, reference_parameters, consumption,
        formula_engine, assessments, metric="electricity_kwh", now=_dt(2026, 7, 10),
    )

    assert result.superseded.version == 1
    assert result.new_version.version == 2
    assert result.new_version.status is AssessmentStatus.ACTIVE
    assert result.new_version.calc_context.total_quantity.value == Decimal(310)
    by_label = {line.rule_label: line.amount for line in result.new_version.charge_lines}
    assert by_label["overage"] == Money(Decimal("36.80"))  # (310-300)×3.20×1.15
    assert result.diff.total_before != result.diff.total_after
    assert any(d.rule_label == "overage" and d.changed for d in result.diff.line_diffs)

    active = assessments.get_active("acc-4471", BillingPeriod(2026, 6))
    assert active is not None and active.version == 2
